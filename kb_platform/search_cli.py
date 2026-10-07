"""kb-search — search the knowledgebase by meaning and by words, as yourself.

    kb-search "jak funguje sdílení projektu"
    kb-search "who approves travel" --under company --k 5
    kb-search "onboarding" --json            # for scripts and agents
    kb-search --status                       # index coverage and today's spend

It runs as whoever calls it: the database connection is the caller's own
(peer authentication), so it returns only what `cat` would let them read. The
paid steps go through kb-embedd's socket, which knows the caller from the
kernel (SO_PEERCRED) and holds them to the company's caps.

By default the best hits are reranked (one rerank per search; a daily cap per
person applies — `--no-rerank` skips it). Without semantic search configured
it is plain full-text search, and says so.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import common, hybrid


def _fmt_status(st: dict) -> str:
    if st.get("starting"):
        return "kb-embedd is starting"
    lines = []
    chunks, done = st.get("chunks", 0), st.get("embedded", 0)
    cov = f"{100 * done / chunks:.1f}%" if chunks else "—"
    lines.append(f"model      {st.get('model')}  (rerank: {(st.get('rerank') or {}).get('model') or 'none'})")
    lines.append(f"state      {st.get('paused') or 'running'}" + (f" — {st['reason']}" if st.get("reason") else ""))
    lines.append(f"sections   {done} of {chunks} embedded ({cov}); pending {st.get('pending', 0)}, "
                 f"parked {st.get('parked', 0)}, kept on the box {st.get('excluded', 0)}")
    for period in ("today", "month"):
        spend = (st.get("spend") or {}).get(period) or {}
        total = sum(v.get("usd", 0) for v in spend.values())
        parts = ", ".join(f"{k} ${v.get('usd', 0):.4f} ({v.get('calls', 0)} calls)" for k, v in sorted(spend.items()))
        lines.append(f"spend {period:<5} ${total:.4f}" + (f"  [{parts}]" if parts else ""))
    b = st.get("budget") or {}
    lines.append(f"budgets    embed ${b.get('embed_day_usd')}/day, ${b.get('embed_month_usd')}/month; "
                 f"rerank ${b.get('rerank_day_usd')}/day")
    for w in st.get("warnings") or []:
        lines.append(f"warning    {w}")
    if st.get("last_error"):
        lines.append(f"last error {st['last_error']} ({st.get('last_error_at')})")
    return "\n".join(lines)


NOTE = ("Note for agents: this searches only the indexed .md files, including the hidden .md text "
        "copies of .docx/.pptx/.xlsx/.pdf files. It does not search html or any other filetype.")


async def _run(args) -> int:
    # stderr under --json so the stdout stays parseable
    print(NOTE, file=sys.stderr if args.json else sys.stdout, flush=True)
    if args.status:
        st, why = await hybrid.sock_call("GET", "/status", None, 5.0)
        if st is None:
            try:
                st = json.loads((common.RUN_DIR / "search" / "status.json").read_text())
            except (OSError, ValueError):
                print(f"semantic search status unavailable: {why}", file=sys.stderr)
                return 1
        print(json.dumps(st, indent=1) if args.json else _fmt_status(st))
        return 0
    q = " ".join(args.query).strip()
    if not q:
        print("usage: kb-search \"question\" [--k N] [--under DIR] [--json] [--no-rerank]", file=sys.stderr)
        return 2
    import psycopg
    try:
        conn = await psycopg.AsyncConnection.connect(f"dbname={common.PG_DB}")
    except psycopg.Error as e:
        print(f"kb-search: cannot reach the index as yourself: {e}".strip(), file=sys.stderr)
        return 1
    try:
        res = await hybrid.search(conn, q, final=True, limit=args.k, under=args.under,
                                  rerank=not args.no_rerank,
                                  connect=lambda: psycopg.AsyncConnection.connect(f"dbname={common.PG_DB}"))
    finally:
        await conn.close()
    if args.json:
        print(hybrid.dumps(res))
        return 0
    if not res["results"]:
        print("no matches")
    for r in res["results"]:
        mark = {"text": "words", "meaning": "meaning", "both": "both"}.get(r["why"], r["why"])
        print(f"{r['path']}:{r['line']}  [{mark}]  {r['text']}")
    how = ("semantic + full-text" + (", reranked" if res["reranked"] else "")) if res["semantic"] \
        else "full-text only"
    extra = f" ({'; '.join(res['notes'])})" if res["notes"] else ""
    print(f"-- {len(res['results'])} results, {how}{extra}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb-search", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    ap.add_argument("query", nargs="*", help="what you are looking for, in any language")
    ap.add_argument("--k", type=int, default=10, help="how many results (default 10, at most 30)")
    ap.add_argument("--under", help="only paths under this folder, e.g. company or projects/acme")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-rerank", action="store_true", help="skip the reranking step (no per-search cost)")
    ap.add_argument("--status", action="store_true", help="index coverage, state and spend")
    args = ap.parse_args(argv)
    args.k = max(1, min(args.k, 30))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
