"""Hybrid search: full-text + vectors, fused, optionally reranked.

One implementation behind both doors — the palette (`/api/search` in the
per-user backend) and `kb-search` (agents, scripts). Everything here runs AS
THE CALLER: the database connection is theirs (peer auth), so row-level
security decides what they may see at every step:

1. Full-text over kb.blocks, exactly as search has always worked (RLS).
2. The query's vector from kb-embedd's socket (cached there for 10 minutes;
   metered and capped there — this module never holds a key).
3. kb.search_vec(): nearest sections among files the caller may read. It is a
   SECURITY DEFINER function that filters by kb.visible_files explicitly and
   returns no text; the text is read here, as the caller, under RLS again.
4. Reciprocal-rank fusion of the two lists, at most three hits per file.
5. Only when `final` (the palette paused, or `kb-search` without
   --no-rerank): the top candidates are reranked through the socket. Never per
   keystroke — that is the one step with a per-search price.
6. Each hit points at its best line: the full-text line that matched, or the
   section line sharing most word stems with the question (kb.chunks.lines
   maps it back to the file).

Anything missing — no socket, no provider, budget spent, a timeout — degrades
to what the previous step had. Full-text alone is always an answer.
"""
from __future__ import annotations

import asyncio
import json
import re

from . import common
from .embedding import vector_literal

SOCK = common.RUN_DIR / "search" / "api.sock"
RRF_K = 60
VEC_K = 60                  # nearest sections asked for
MAX_DISTANCE = 0.62         # past this a "nearest" section is noise; kb-embedd sends the model's own
PER_FILE = 3
RERANK_TOP = 40
EMBED_TIMEOUT = 0.8         # a keystroke search does not wait longer than this for a vector
EMBED_TIMEOUT_FINAL = 3.0
RERANK_TIMEOUT = 5.0
RERANK_FLOOR = 0.05         # a meaning-only hit the reranker scores below this is dropped

# Three ways to match, ONE pass over the table. This is a SEQUENTIAL
# scan and cannot be anything else: `tsv @@` and ILIKE are not
# leakproof, so under RLS the planner may not evaluate them below the
# policy qual, and no index on kb.blocks is reachable. That is the
# security model working — a SECURITY DEFINER wrapper WOULD reach the
# index, and would hand every account a content oracle over the whole
# corpus (see the note in schema.sql). What was made fast instead is
# the two things that actually scale: the policy qual (a hashed subplan
# over kb.visible_files, replacing ~0.34 s of per-statement can_read())
# and the heap the scan reads (the dead embedding column is gone).
# Running the tiers as separate statements would only add scans.
#   1. websearch_to_tsquery — stemming, "quoted phrases", -exclusions.
#      It never raises on user input, unlike to_tsquery.
#   2. prefix tsquery — "onbo mee" finds "Onboarding meeting". Tokens are
#      stripped to [a-z0-9] runs, so nothing reaches tsquery's own
#      syntax (& | ! : * parentheses); the dictionary is schema-qualified
#      because every user owns a u_<user> schema that comes first in
#      their search_path.
#   3. ILIKE — mid-word matches, identifiers, punctuation and the
#      non-English text the 'english' stemmer mangles.
FTS_SQL = (
    # file_path/line break the remaining ties. Without them the order among
    # equal-rank, equal-length rows is whatever the (parallel) scan emitted, and
    # the same query reshuffled its results between keystrokes.
    "WITH q AS (SELECT websearch_to_tsquery('pg_catalog.english', %s) AS ws, "
    "                  CASE WHEN %s::text IS NULL THEN NULL::tsquery "
    "                       ELSE to_tsquery('pg_catalog.english', %s) END AS pq) "
    "SELECT b.file_path, b.line, b.kind, b.text, "
    "       ts_rank(b.tsv, q.ws) AS rank, "
    "       (b.tsv @@ q.pq) AS pref "
    "FROM kb.blocks b, q "
    "WHERE b.tsv @@ q.ws OR b.tsv @@ q.pq OR b.text ILIKE %s "
    "ORDER BY rank DESC, length(b.text), b.file_path, b.line LIMIT 90")


def fts_params(q: str) -> tuple:
    toks = [t for t in re.split(r"[^0-9A-Za-z]+", q.lower()) if len(t) >= 2][:6]
    pq = " & ".join(t + ":*" for t in toks) if toks else None
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return (q, pq, pq, like)


async def fts(conn, q: str) -> list[dict]:
    """Full-text hits, best first: {path, line, kind, text, rank}."""
    async with conn.cursor() as cur:
        await cur.execute(FTS_SQL, fts_params(q))
        rows = await cur.fetchall()
    out = [{"path": r[0], "line": r[1], "kind": r[2], "text": r[3],
            "rank": float(r[4]) + (0.02 if r[5] else 0.0)} for r in rows]
    out.sort(key=lambda r: -r["rank"])
    return out


# ---- the socket --------------------------------------------------------------
async def sock_call(method: str, path: str, payload: dict | None, timeout: float) -> tuple[dict | None, str | None]:
    """(answer, None) or (None, why). Never raises."""
    try:
        import aiohttp
    except ImportError:
        return None, "aiohttp missing"
    if not SOCK.exists():
        return None, "kb-embedd is not running"
    try:
        conn = aiohttp.UnixConnector(path=str(SOCK))
        async with aiohttp.ClientSession(connector=conn) as s:
            async with s.request(method, f"http://kb-embedd{path}", json=payload,
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                body = await r.json(content_type=None)
                if r.status != 200:
                    return None, (body or {}).get("why") or f"HTTP {r.status}"
                return body, None
    except (asyncio.TimeoutError, TimeoutError):
        return None, "timeout"
    except (OSError, ValueError) as e:
        return None, type(e).__name__
    except Exception as e:                     # noqa: BLE001 — aiohttp's error zoo; search must answer
        return None, type(e).__name__


async def nearest(conn, vec: list[float], k: int = VEC_K, max_distance: float = MAX_DISTANCE) -> list[dict]:
    """Nearest sections the caller may read, with their text (read under RLS)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT file_path, seq, start_line, distance FROM kb.search_vec(%s::halfvec, %s)",
                          (vector_literal(vec), k))
        hits = [r for r in await cur.fetchall() if r[3] is not None and r[3] <= max_distance]
        if not hits:
            return []
        keys = [(r[0], r[1]) for r in hits]
        await cur.execute(
            "SELECT file_path, seq, start_line, end_line, heading, text, lines FROM kb.chunks "
            "WHERE (file_path, seq) IN (SELECT * FROM unnest(%s::text[], %s::int[]))",
            ([k[0] for k in keys], [k[1] for k in keys]))
        text = {(r[0], r[1]): r for r in await cur.fetchall()}
    out = []
    for path, seq, start, dist in hits:
        row = text.get((path, seq))
        if row is None:                         # RLS said no after all, or it changed: skip
            continue
        out.append({"path": path, "seq": seq, "start": row[2], "end": row[3], "heading": row[4],
                    "text": row[5], "lines": row[6] or [], "distance": float(dist)})
    return out


_STRIP = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+|>\s*)")


def _stems(text: str) -> set[str]:
    """Word stems good enough to compare across inflection: the first five
    letters. "rozpočet"/"rozpočtu", "approve"/"approved" meet; no dictionary."""
    return {w[:5] for w in re.findall(r"[^\W\d_]{3,}", text.lower())}


def _tidy(line: str, limit: int = 200) -> str:
    s = _STRIP.sub("", line.strip()).replace("**", "")
    return s[:limit] + ("…" if len(s) > limit else "")


def snippet(chunk_text: str, q: str, limit: int = 200) -> tuple[str, int]:
    """The line of a section to show, and its offset among the section's lines:
    the one sharing most word stems with the query, else the first line that
    says something."""
    lines = chunk_text.split("\n")[1:] or chunk_text.split("\n")
    want = _stems(q)
    best, best_i, best_score = "", 0, (-1, 0)
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("|--"):
            continue
        score = (len(want & _stems(s)), min(len(s.split()), 6))
        if score > best_score:
            best, best_i, best_score = s, i, score
    return _tidy(best, limit), best_i


async def sections_for_lines(conn, text_hits: list[dict]) -> dict[tuple[str, int], dict]:
    """The section each full-text line belongs to, read as the caller (RLS)."""
    if not text_hits:
        return {}
    paths = [t["path"] for t in text_hits]
    lines = [t["line"] for t in text_hits]
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT r.p, r.l, c.seq, c.start_line, c.end_line, c.text "
            "FROM unnest(%s::text[], %s::int[]) AS r(p, l) "
            "JOIN kb.chunks c ON c.file_path = r.p AND r.l BETWEEN c.start_line AND c.end_line",
            (paths, lines))
        rows = await cur.fetchall()
    out: dict[tuple[str, int], dict] = {}
    for p, line, seq, start, end, text in rows:
        out.setdefault((p, line), {"seq": seq, "start": start, "end": end, "text": text})
    return out


def _body(text: str) -> str:
    return text.split("\n", 1)[1] if "\n" in text else text


def fuse(q: str, text_hits: list[dict], vec_hits: list[dict],
         homes: dict[tuple[str, int], dict] | None = None, vec_weight: float = 1.0) -> list[dict]:
    """Reciprocal-rank fusion at the level of a SECTION: every full-text line
    is counted for the section it sits in, so a section that matches both ways
    is one hit (and keeps the exact line that matched). Sections whose text is
    identical to one already listed — the same boilerplate pasted into ten
    project READMEs — are listed once."""
    homes = homes or {}
    units: dict[tuple, dict] = {}
    for rank, v in enumerate(vec_hits, 1):
        key = (v["path"], v["seq"])
        if key in units:
            continue
        snip, i = snippet(v["text"], q)
        lines = v.get("lines") or []
        units[key] = {"path": v["path"], "line": lines[i] if i < len(lines) else v["start"],
                      "start": v["start"], "end": v["end"],
                      "kind": "section", "text": snip, "doc": v["text"], "why": "meaning",
                      "score": vec_weight / (RRF_K + rank), "distance": round(v["distance"], 4)}
    by_file: dict[str, list[dict]] = {}
    for v in vec_hits:
        by_file.setdefault(v["path"], []).append(v)
    rank = 0
    counted: set[tuple] = set()
    for t in text_hits:
        # its section: from the database, else from the sections that matched by meaning
        home = homes.get((t["path"], t["line"])) or next(
            (v for v in by_file.get(t["path"], []) if v["start"] <= t["line"] <= v["end"]), None)
        key = (t["path"], home["seq"]) if home else (t["path"], "line", t["line"])
        if key in counted:
            continue                       # a second line of a counted section adds nothing
        counted.add(key)
        rank += 1
        u = units.get(key)
        if u is None:
            units[key] = {"path": t["path"], "line": t["line"], "kind": t["kind"], "text": _tidy(t["text"]),
                          "doc": home["text"] if home else t["text"], "why": "text",
                          "score": 1.0 / (RRF_K + rank)}
        else:
            u.update(line=t["line"], kind=t["kind"], text=_tidy(t["text"]), why="both")
            u["score"] += 1.0 / (RRF_K + rank)
    ordered = sorted(units.values(), key=lambda u: (-u["score"], u["path"], u["line"]))
    out, per_file, bodies = [], {}, set()
    for u in ordered:
        if per_file.get(u["path"], 0) >= PER_FILE:
            continue
        if u["why"] == "meaning":
            body = _body(u["doc"]).strip()
            if body in bodies:
                continue
            bodies.add(body)
        per_file[u["path"]] = per_file.get(u["path"], 0) + 1
        out.append(u)
    return out


async def _nothing():
    return None, None


async def search(conn, q: str, *, final: bool = False, limit: int = 30,
                 under: str | None = None, rerank: bool = True,
                 text: bool = True, vectors: bool = True, connect=None) -> dict:
    """{results, semantic, reranked, notes}. `conn` is the CALLER's async
    connection; a failed full-text query rolls back and yields no text hits.
    `connect` (optional) opens a second connection AS THE SAME CALLER: then
    the vector lookup runs beside the full-text scan instead of after it.
    `text`/`vectors` switch a half off (scripts/search-eval.py compares them)."""
    q = q.replace("\x00", "").strip()[:200]
    notes: list[str] = []

    async def text_side() -> list[dict]:
        if not text:
            return []
        try:
            return await fts(conn, q)
        except Exception:                      # noqa: BLE001 — never a 500 for a query
            try:
                await conn.rollback()
            except Exception:                  # noqa: BLE001
                pass
            return []

    async def vector_side() -> list[dict]:
        if not vectors:
            return []
        emb, why = await sock_call("POST", "/embed", {"text": q},
                                   EMBED_TIMEOUT_FINAL if final else EMBED_TIMEOUT)
        if not (emb and emb.get("vector")):
            if why:
                notes.append(f"semantic: {why}")
            return []
        c = None
        try:
            c = await connect() if connect else None
        except Exception:                      # noqa: BLE001 — fall back to sharing the caller's
            c = None
        use = c or conn
        try:
            return await nearest(use, emb["vector"], VEC_K * (2 if under else 1),
                                 float(emb.get("max_distance") or MAX_DISTANCE))
        except Exception as e:                 # noqa: BLE001 — e.g. schema not applied yet
            try:
                await use.rollback()
            except Exception:                  # noqa: BLE001
                pass
            notes.append(f"vectors unavailable: {type(e).__name__}")
            return []
        finally:
            if c is not None:
                await c.close()

    # Without a second connection psycopg serialises the two on `conn`, which
    # is still correct — just not concurrent.
    text_hits, vec_hits = await asyncio.gather(text_side(), vector_side())
    if under:
        pre = under.strip("/") + "/"
        text_hits = [t for t in text_hits if t["path"].startswith(pre)]
        vec_hits = [v for v in vec_hits if v["path"].startswith(pre)]
    try:
        homes = await sections_for_lines(conn, text_hits)
    except Exception:                          # noqa: BLE001 — no chunks table yet: lines stay lines
        try:
            await conn.rollback()
        except Exception:                      # noqa: BLE001
            pass
        homes = {}
    # One word — a street name, an invoice number, a class name — means those
    # letters: its nearest meanings are guesses, so they count half.
    fused = fuse(q, text_hits, vec_hits, homes, vec_weight=0.5 if len(q.split()) == 1 else 1.0)
    reranked = False
    if final and rerank and len(fused) > 1:
        top = fused[:RERANK_TOP]
        res, why = await sock_call("POST", "/rerank", {"query": q, "docs": [u["doc"] for u in top]},
                                   RERANK_TIMEOUT)
        if res and len(res.get("scores", [])) == len(top):
            for u, s in zip(top, res["scores"]):
                u["score"] = float(s)
            top = [u for u in top if u["why"] != "meaning" or u["score"] >= RERANK_FLOOR]
            top.sort(key=lambda u: (-u["score"], u["path"], u["line"]))
            fused = top + fused[RERANK_TOP:]
            reranked = True
        elif why:
            notes.append(f"rerank: {why}")
    results = []
    for u in fused[:limit]:
        r = {k: u[k] for k in ("path", "line", "kind", "text", "why")}
        r["rank"] = round(u["score"], 6)
        results.append(r)
    return {"results": results, "semantic": bool(vec_hits), "reranked": reranked, "notes": notes}


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
