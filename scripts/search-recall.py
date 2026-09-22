#!/usr/bin/env python3
"""How much of the TRUE nearest-neighbour list does the vector index return?

    sudo -u kbindexer /opt/kb-venv/bin/python scripts/search-recall.py QUESTIONS.jsonl [--ef 100,200,400,1000]

An approximate index (HNSW) trades recall for speed, and how much it loses
depends on the corpus — a cluster of near-identical sections can hide the right
answer behind it. This compares the index with an exact scan for each question
in QUESTIONS.jsonl (the same file as scripts/search-eval.py) and prints
recall@10, the worst question and the time per query at several ef_search
values. kb.search_vec uses 400; if recall@10 drops below ~0.97 as the corpus
grows, rebuild the index denser or raise it.

Runs as kbindexer (the only account that may read kb.embeddings directly);
query vectors come from kb-embedd's socket, cached there for 10 minutes.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg  # noqa: E402

from kb_platform import common, hybrid  # noqa: E402

SQL = "SELECT hash FROM kb.embeddings ORDER BY embedding <=> %s::halfvec LIMIT 40"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("questions")
    ap.add_argument("--ef", default="100,200,400,1000")
    args = ap.parse_args()
    qs = [json.loads(x)["q"] for x in Path(args.questions).read_text().splitlines() if x.strip()]
    vecs = []
    for q in qs:
        e, why = await hybrid.sock_call("POST", "/embed", {"text": q}, 10)
        if not e:
            print(f"cannot embed {q!r}: {why}", file=sys.stderr)
            return 1
        vecs.append(hybrid.vector_literal(e["vector"]))
    exact_c = await psycopg.AsyncConnection.connect(f"dbname={common.PG_DB}", autocommit=True)
    index_c = await psycopg.AsyncConnection.connect(f"dbname={common.PG_DB}", autocommit=True)
    await exact_c.execute("SET enable_indexscan = off")          # separate sessions: no leakage
    exact = [[bytes(r[0]) for r in await (await exact_c.execute(SQL, (v,))).fetchall()] for v in vecs]
    print(f"{len(qs)} questions, {await (await index_c.execute('SELECT count(*) FROM kb.embeddings')).fetchone()} vectors")
    for ef in [int(x) for x in args.ef.split(",")]:
        await index_c.execute(f"SET hnsw.ef_search = {ef}")
        tot, worst, t = 0.0, 1.0, time.perf_counter()
        for v, ex in zip(vecs, exact):
            got = [bytes(r[0]) for r in await (await index_c.execute(SQL, (v,))).fetchall()]
            r = len(set(got[:10]) & set(ex[:10])) / 10
            tot, worst = tot + r, min(worst, r)
        print(f"ef_search={ef:<5} recall@10 {tot / len(vecs):.3f}  worst {worst:.1f}  "
              f"{1000 * (time.perf_counter() - t) / len(vecs):.1f} ms/query")
    await exact_c.close()
    await index_c.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
