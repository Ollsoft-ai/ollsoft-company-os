#!/usr/bin/env python3
"""Score search against a set of questions with known answers.

    scripts/search-eval.py QUESTIONS.jsonl [--variants words,meaning,hybrid,rerank] [--show-misses]

Each line of QUESTIONS.jsonl: {"q": "...", "want": ["path", ...], "kind": "paraphrase"}
— a question and the file(s) that answer it. It runs AS YOU (your database
login, your permissions), through the same code as Ctrl+K and kb-search, and
prints hit@1, hit@5 and MRR@10 per variant and per kind, plus latency.

Keep a company's question set OUT of the repository: it names real files.
Each run with `rerank` costs one rerank per question (≈ $0.003 each).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg  # noqa: E402

from kb_platform import common, hybrid  # noqa: E402

VARIANTS = {
    "words":   dict(text=True,  vectors=False, final=False),
    "meaning": dict(text=False, vectors=True,  final=False),
    "hybrid":  dict(text=True,  vectors=True,  final=False),
    "rerank":  dict(text=True,  vectors=True,  final=True),
}


def file_rank(results: list[dict], want: set[str]) -> int | None:
    """1-based rank of the first answering FILE (duplicate files collapse)."""
    seen: list[str] = []
    for r in results:
        if r["path"] not in seen:
            seen.append(r["path"])
            if r["path"] in want:
                return len(seen)
    return None


NONSENSE = ["zebrafish", "xylophone", "banana bread recipe", "asdkjh qwpoeiu", "qwerty", "walrus",
            "hos", "hosp", "the", "Lorem ipsum dolor"]


async def calibrate(conn, qs) -> int:
    """Where do right answers sit, and where does noise start? The cutoff
    belongs between the two: every answer below it, the nonsense above."""
    right = []
    for item in qs:
        e, why = await hybrid.sock_call("POST", "/embed", {"text": item["q"]}, 10)
        if not e:
            print(f"cannot embed: {why}")
            return 1
        v = await hybrid.nearest(conn, e["vector"], 100, max_distance=2.0)
        d = min((x["distance"] for x in v if x["path"] in item["want"]), default=None)
        if d is not None:
            right.append(d)
    noise = []
    for q in NONSENSE:
        e, _ = await hybrid.sock_call("POST", "/embed", {"text": q}, 10)
        v = await hybrid.nearest(conn, e["vector"], 1, max_distance=2.0)
        if v:
            noise.append((v[0]["distance"], q))
    right.sort()
    print(f"right answers ({len(right)}/{len(qs)} found by meaning): median {statistics.median(right):.3f}, "
          f"90% {right[int(len(right) * .9) - 1]:.3f}, farthest {right[-1]:.3f}")
    print("nonsense, nearest section: " + ", ".join(f"{q!r} {d:.3f}" for d, q in sorted(noise)))
    print(f"current cutoff (from kb-embedd): {e.get('max_distance')} — set KB_EMBED_MAX_DISTANCE "
          f"between {right[-1]:.2f} and the nonsense above it")
    await conn.close()
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("questions")
    ap.add_argument("--variants", default="words,meaning,hybrid,rerank")
    ap.add_argument("--show-misses", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--calibrate", action="store_true",
                    help="print vector distances of the right answers vs nonsense, to set KB_EMBED_MAX_DISTANCE")
    args = ap.parse_args()
    qs = [json.loads(line) for line in Path(args.questions).read_text().splitlines() if line.strip()]
    conn = await psycopg.AsyncConnection.connect(f"dbname={common.PG_DB}", autocommit=True)
    if args.calibrate:
        return await calibrate(conn, qs)
    table = {}
    misses = defaultdict(list)
    try:
        for name in args.variants.split(","):
            opts = VARIANTS[name]
            ranks, times, by_kind = [], [], defaultdict(list)
            for item in qs:
                t = time.perf_counter()
                res = await hybrid.search(conn, item["q"], limit=30, connect=lambda: psycopg.AsyncConnection.connect(
                    f"dbname={common.PG_DB}", autocommit=True), **opts)
                times.append(time.perf_counter() - t)
                rank = file_rank(res["results"], set(item["want"]))
                ranks.append(rank)
                by_kind[item.get("kind", "-")].append(rank)
                if rank is None or rank > 5:
                    top = []
                    for r in res["results"]:
                        if r["path"] not in top:
                            top.append(r["path"])
                    misses[name].append((item["q"], rank, top[:3]))

            def score(rs):
                n = len(rs) or 1
                return {"hit1": sum(1 for r in rs if r == 1) / n,
                        "hit5": sum(1 for r in rs if r and r <= 5) / n,
                        "mrr": sum(1 / r for r in rs if r and r <= 10) / n, "n": len(rs)}
            table[name] = {"all": score(ranks), **{k: score(v) for k, v in by_kind.items()},
                           "p50_ms": statistics.median(times) * 1000,
                           "p95_ms": sorted(times)[max(0, int(len(times) * 0.95) - 1)] * 1000}
    finally:
        await conn.close()
    if args.json:
        print(json.dumps(table, indent=1))
        return 0
    kinds = sorted({k for v in table.values() for k in v if k not in ("all", "p50_ms", "p95_ms")})
    print(f"{len(qs)} questions\n")
    print(f"{'variant':<9} {'hit@1':>6} {'hit@5':>6} {'MRR':>6}  {'p50':>6} {'p95':>6}   "
          + "  ".join(f"{k[:12]:>12}" for k in kinds))
    for name, row in table.items():
        a = row["all"]
        per = "  ".join(f"{row[k]['hit5']:>6.0%} (n={row[k]['n']:>2})" if k in row else " " * 12 for k in kinds)
        print(f"{name:<9} {a['hit1']:>6.0%} {a['hit5']:>6.0%} {a['mrr']:>6.3f}  "
              f"{row['p50_ms']:>5.0f}ms {row['p95_ms']:>5.0f}ms   {per}")
    print("\n(per-kind columns are hit@5)")
    if args.show_misses:
        for name, ms in misses.items():
            print(f"\n-- {name}: missed top 5 --")
            for q, rank, top in ms:
                print(f"  [{rank or '-'}] {q}\n        got: {' | '.join(top)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
