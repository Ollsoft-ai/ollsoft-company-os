"""Per-user backend. Spawned by the hub via `runuser -u <user>`, so this whole
process runs AS the logged-in OS user. That is the entire security story for
this tier: every file open, every attachment write, every Postgres connection
happens with the user's kernel identity, so the kernel and RLS enforce access —
there is no application-level permission code here to get wrong.

Listens on a private unix socket (chmod 0600) reachable only by the user + root.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import grp
import pty
import pwd
import re
import shutil
import signal
import stat
import struct
import subprocess
import termios
import time
import unicodedata
from pathlib import Path

from aiohttp import WSMsgType, web

from . import common

try:
    import psycopg
except Exception:  # pragma: no cover
    psycopg = None

ME = pwd.getpwuid(os.geteuid()).pw_name
MY_SHELL = pwd.getpwuid(os.geteuid()).pw_shell
# "viewer" accounts (nologin shell) get the full webapp but no execution: the
# pty and cron endpoints are gated here, and the OS enforces the same thing
# underneath (nologin + /etc/cron.deny, managed by the hub).
CAN_SHELL = MY_SHELL not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "")


def _db():
    if psycopg is None:
        return None
    try:
        return psycopg.connect(f"dbname={common.PG_DB}", autocommit=True)
    except Exception:
        return None


async def whoami(request: web.Request) -> web.Response:
    # The definitive identity: who this process actually runs as (kernel truth),
    # not whatever header the hub claims. They must match.
    return web.json_response({"user": ME, "uid": os.geteuid(), "shell": CAN_SHELL,
                              "claimed": request.headers.get("X-KB-User")})


async def tree(request: web.Request) -> web.Response:
    root = common.REPO_ROOT

    def walk(d: Path, depth: int) -> list:
        out = []
        if depth > 12:
            return out
        try:
            entries = sorted(os.scandir(d), key=lambda e: (not e.is_dir(), e.name))
        except OSError:
            return out
        for e in entries:
            # Show everything except git internals and our temp files — including
            # dot-directories like .claude (agent config/skills).
            if e.name == ".git" or e.name.endswith(".kbtmp"):
                continue
            p = Path(e.path)
            rel = str(p.relative_to(root))
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            access = {"read": os.access(p, os.R_OK), "write": os.access(p, os.W_OK)}
            if is_dir:
                if not os.access(p, os.R_OK | os.X_OK):
                    continue
                out.append({"name": e.name, "path": rel, "dir": True, "access": access,
                            "children": walk(p, depth + 1)})
            else:
                if not os.access(p, os.R_OK):
                    continue
                if e.name.endswith(".html"):
                    kind = "artifact"
                elif e.name.endswith(".md"):
                    kind = "md"
                else:
                    kind = "file"
                out.append({"name": e.name, "path": rel, "dir": False, "kind": kind,
                            "access": access})
        return out

    return web.json_response({"root": str(root), "tree": walk(root, 0)})


async def read_file(request: web.Request) -> web.Response:
    rel = request.query.get("path", "")
    p = common.resolve_repo_path(rel)
    if p is None:
        return web.json_response({"error": "bad path"}, status=400)
    try:
        content = p.read_text(errors="replace")
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"path": rel, "content": content,
                             "access": {"read": True, "write": os.access(p, os.W_OK)}})


def _versioned_under(p: Path, rel: str, cap: int = 500) -> list[str]:
    """The versioned (.md/.html) paths at-or-under `p` — what a delete/rename of
    it touches in history. Capped; attribution is best-effort, never a blocker."""
    if not p.is_dir():
        return [rel] if common.is_versioned_path(rel) else []
    out = []
    root = common.REPO_ROOT.resolve()
    for dirpath, dirnames, filenames in os.walk(p):
        dirnames[:] = [d for d in dirnames if d not in (".git", "_secrets")]
        for fn in filenames:
            r = str(Path(dirpath, fn).relative_to(root))
            if common.is_versioned_path(r):
                out.append(r)
                if len(out) >= cap:
                    return out
    return out


async def create_file(request: web.Request) -> web.Response:
    """Create a new empty doc AS the user, so ownership/group are correct from
    birth. The sync daemon never creates files, only edits existing ones."""
    data = await request.json()
    rel = data.get("path", "")
    if not rel.endswith(".md"):
        return web.json_response({"error": "must be .md"}, status=400)
    p = common.resolve_repo_path(rel)
    if p is None:
        return web.json_response({"error": "bad path"}, status=400)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            return web.json_response({"error": "exists"}, status=409)
        p.write_text("")
        common.write_attrib_hint("create", rel)
        return web.json_response({"ok": True, "path": rel})
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)


async def fs_mkdir(request: web.Request) -> web.Response:
    """Create a folder AS the user. Kernel decides (write+x on the parent);
    setgid + default ACLs on shared trees give the right group perms, and the
    0077 umask keeps folders in private trees private."""
    data = await request.json()
    p = common.resolve_repo_path(str(data.get("path", "")))
    if p is None:
        return web.json_response({"error": "bad path"}, status=400)
    try:
        # exists()/lstat can themselves raise PermissionError when an ancestor
        # isn't traversable — that's still "you can't do this here", a 403 —
        # so guard them inside the same try, not just the mkdir.
        if os.path.lexists(p):
            return web.json_response({"error": "already exists"}, status=409)
        p.mkdir(mode=0o777)  # default ACL masks this on shared trees; umask elsewhere
    except PermissionError:
        return web.json_response({"error": "no write access to the parent folder"}, status=403)
    except FileExistsError:
        return web.json_response({"error": "already exists"}, status=409)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "path": str(p.relative_to(common.REPO_ROOT.resolve()))})


async def fs_delete(request: web.Request) -> web.Response:
    """Delete a file or folder (recursively) AS the user — exactly what `rm -r`
    in their terminal could do, no more. Top-level areas are refused outright."""
    data = await request.json()
    p = common.resolve_repo_path(str(data.get("path", "")))
    if p is None:
        return web.json_response({"error": "bad path"}, status=400)
    rel = str(p.relative_to(common.REPO_ROOT.resolve()))
    if "/" not in rel:
        return web.json_response({"error": "top-level areas cannot be deleted"}, status=400)
    try:
        st = os.lstat(p)   # can't even see it? a blocked ancestor is a 403, not a 404
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)
    except FileNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    is_dir = stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
    touched = _versioned_under(p, rel)   # collect BEFORE they're gone
    try:
        if is_dir:
            shutil.rmtree(p)
        else:
            os.unlink(p)
    except PermissionError:
        return web.json_response(
            {"error": "permission denied (you need write access to the containing folder; "
                      "a partially protected folder may have been partially deleted)"},
            status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    common.write_attrib_hint("delete", *touched)
    return web.json_response({"ok": True, "deleted": rel, "was_dir": is_dir})


def _fs_pair(data: dict) -> tuple[Path, Path, str, str] | web.Response:
    """Resolve + sanity-check a (src, dst) pair for rename/copy. Both must stay
    in the repo, src must not be a top-level area, dst must not sit at the repo
    root or inside src itself."""
    src = common.resolve_repo_path(str(data.get("src", "")))
    dst = common.resolve_repo_path(str(data.get("dst", "")))
    if src is None or dst is None:
        return web.json_response({"error": "bad path"}, status=400)
    root = common.REPO_ROOT.resolve()
    rel_src = str(src.relative_to(root))
    rel_dst = str(dst.relative_to(root))
    if "/" not in rel_src:
        return web.json_response({"error": "top-level areas cannot be moved"}, status=400)
    if "/" not in rel_dst:
        return web.json_response({"error": "the destination must be inside company/, projects/ or users/"}, status=400)
    if rel_dst == rel_src or rel_dst.startswith(rel_src + "/"):
        return web.json_response({"error": "cannot move a folder into itself"}, status=400)
    return src, dst, rel_src, rel_dst


async def fs_rename(request: web.Request) -> web.Response:
    """Move/rename AS the user — exactly what `mv` in their terminal could do
    (kernel needs write on both parent folders). The frontend retires and
    reopens any affected editor tabs itself."""
    got = _fs_pair(await request.json())
    if isinstance(got, web.Response):
        return got
    src, dst, rel_src, rel_dst = got
    try:
        os.lstat(src)
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)
    except FileNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    old_touched = _versioned_under(src, rel_src)   # collect BEFORE the move
    try:
        if os.path.lexists(dst):
            return web.json_response({"error": "something with that name already exists"}, status=409)
        if not dst.parent.is_dir():
            return web.json_response({"error": "destination folder does not exist"}, status=400)
        os.rename(src, dst)
    except PermissionError:
        return web.json_response({"error": "no write access (both the source and destination folders need it)"},
                                 status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    # history sees a rename as delete-at-old + add-at-new — attribute both sides
    new_touched = [rel_dst + old[len(rel_src):] for old in old_touched]
    common.write_attrib_hint("rename", *old_touched, *new_touched)
    return web.json_response({"ok": True, "src": rel_src, "dst": rel_dst})


async def fs_copy(request: web.Request) -> web.Response:
    """Copy AS the user (`cp -r` semantics: the copy is owned by the copier;
    mode preserved). Refuses to overwrite — the frontend picks a fresh name."""
    got = _fs_pair(await request.json())
    if isinstance(got, web.Response):
        return got
    src, dst, rel_src, rel_dst = got
    try:
        st = os.lstat(src)
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)
    except FileNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    if stat.S_ISLNK(st.st_mode):
        return web.json_response({"error": "refusing to copy a symlink"}, status=400)
    try:
        if os.path.lexists(dst):
            return web.json_response({"error": "exists"}, status=409)
        if not dst.parent.is_dir():
            return web.json_response({"error": "destination folder does not exist"}, status=400)
        # copytree/copy2 can run long for a big folder — do the I/O off the event
        # loop so this backend keeps serving the user's terminal, tree and docs.
        if stat.S_ISDIR(st.st_mode):
            await asyncio.to_thread(shutil.copytree, src, dst, symlinks=False,
                                    ignore=shutil.ignore_patterns(".git", "*.kbtmp"))
        else:
            await asyncio.to_thread(shutil.copy2, src, dst)
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)
    except shutil.Error as e:
        return web.json_response({"error": f"partial copy: {e}"}, status=400)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    common.write_attrib_hint("copy", *_versioned_under(dst, rel_dst))
    return web.json_response({"ok": True, "src": rel_src, "dst": rel_dst})


async def upload(request: web.Request) -> web.Response:
    reader = await request.multipart()
    field = await reader.next()
    dest_rel = request.query.get("dir", "company")
    dest_dir = common.resolve_repo_path(dest_rel)
    if dest_dir is None or not dest_dir.is_dir():
        return web.json_response({"error": "bad dir"}, status=400)
    files_dir = dest_dir / "_files"
    filename = os.path.basename(field.filename or "upload.bin")
    try:
        files_dir.mkdir(exist_ok=True)
        target = files_dir / filename
        size = 0
        with open(target, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                f.write(chunk)
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    link = f"_files/{filename}"
    return web.json_response({"ok": True, "link": link, "size": size,
                             "path": str(target.relative_to(common.REPO_ROOT))})


def _content_disposition(filename: str, download: bool) -> str:
    """Name the download after the file (`report.docx`, not `attachment`).
    `inline` keeps images/PDFs viewable in a tab; `attachment` forces a save
    (the explicit Download action). Control chars are stripped from the ASCII
    fallback (a raw newline would split the HTTP header), and everything is
    also carried in the RFC 5987 filename* which percent-encodes in full."""
    from urllib.parse import quote
    safe = "".join(c for c in filename if 32 <= ord(c) < 127).replace('"', "_")
    if not safe:
        safe = "download"
    disp = "attachment" if download else "inline"
    return f"{disp}; filename=\"{safe}\"; filename*=UTF-8''{quote(filename)}"


async def attachment(request: web.Request) -> web.StreamResponse:
    rel = request.query.get("path", "")
    p = common.resolve_repo_path(rel)
    if p is None:
        return web.Response(status=404)
    # Fail closed: a kernel denial (non-traversable parent) must become 403, not 500.
    try:
        if not p.is_file():
            return web.Response(status=404)
        if not os.access(p, os.R_OK):
            return web.Response(status=403)
        with open(p, "rb"):
            pass
    except PermissionError:
        return web.Response(status=403)
    except OSError:
        return web.Response(status=404)
    dl = request.query.get("dl") == "1"
    return web.FileResponse(p, headers={"Content-Disposition": _content_disposition(p.name, dl)})


async def tasks(request: web.Request) -> web.Response:
    conn = _db()
    if conn is None:
        return web.json_response({"tasks": [], "db": False})
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_path, line, checked, text FROM kb.blocks "
                "WHERE kind='task' ORDER BY file_path, line")
            rows = [{"path": r[0], "line": r[1], "checked": r[2], "text": r[3]}
                    for r in cur.fetchall()]
        return web.json_response({"tasks": rows, "db": True})
    finally:
        conn.close()


# Anchored task pattern — identical shape to the indexer's, so we only ever flip a
# real task marker, never a checkbox-looking substring embedded in prose or code.
_TASK_LINE = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\].*)$", re.DOTALL)


async def toggle_task(request: web.Request) -> web.Response:
    data = await request.json()
    rel = data.get("path", "")
    line_no = int(data.get("line", 0))
    if not rel.endswith(".md"):
        return web.json_response({"error": "not a document"}, status=400)
    p = common.resolve_repo_path(rel)
    if p is None:
        return web.json_response({"error": "bad path"}, status=400)
    rel = str(p.relative_to(common.REPO_ROOT.resolve()))
    # Scope exactly like the read side: the (path,line) must be a task the caller
    # can actually SEE in the RLS-governed index. This blocks toggling of
    # index-excluded trees (.claude/) and non-task lines even if FS-writable.
    conn = _db()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM kb.blocks WHERE file_path=%s AND line=%s AND kind='task'",
                            (rel, line_no))
                if cur.fetchone() is None:
                    return web.json_response({"error": "not an indexed task"}, status=400)
        finally:
            conn.close()
    try:
        lines = p.read_text().splitlines(keepends=True)
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=404)
    if not (1 <= line_no <= len(lines)):
        return web.json_response({"error": "bad line"}, status=400)
    idx = line_no - 1
    m = _TASK_LINE.match(lines[idx])
    if not m:
        return web.json_response({"error": "not a task line"}, status=400)
    new_char, new_state = (" ", False) if m.group(2).lower() == "x" else ("x", True)
    lines[idx] = m.group(1) + new_char + m.group(3)
    try:
        p.write_text("".join(lines))
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    common.write_attrib_hint("edit", rel)
    return web.json_response({"ok": True, "checked": new_state})


# ---- search ---------------------------------------------------------------
# Two independent halves, because they have different sources of truth:
#   * FILENAMES come from a walk of the repo run as this user, so the kernel is
#     the permission check and *every* file type is covered — including the ones
#     the Postgres index never sees (artifacts, images, PDFs, attachments).
#   * CONTENT comes from kb.blocks, where RLS re-checks the same permissions.
# Searching a filename used to return nothing at all: only block text was ever
# queried, and `plainto_tsquery` throws away punctuation, so "overview.md" and
# "pulse.html" matched literally nothing.

_WALK_LIMIT = 20000        # hard stop, so a pathological tree can't hang a search
_WALK_DEPTH = 12           # same depth cap as the tree endpoint


def _fold(s: str) -> str:
    """Casefold and strip diacritics, so `lekarska` finds `lékařská zpráva.png`."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.casefold())
                   if not unicodedata.combining(c))


def _walk_repo(limit: int = _WALK_LIMIT):
    """Yield (rel, is_dir) for everything under the repo this user can see.

    Runs as the logged-in user (like the whole backend), so `os.scandir` and
    `os.access` ARE the permission check — an unreadable subtree simply yields
    nothing. Same skip rules as the tree endpoint, so search finds exactly what
    the file tree shows and nothing more.
    """
    root = common.REPO_ROOT
    stack = [(str(root), 0)]
    seen = 0
    while stack and seen < limit:
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name == ".git" or e.name == "_secrets" or e.name.endswith(".kbtmp"):
                continue   # "never in git history, search, or the live-doc relay"
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            try:
                rel = str(Path(e.path).relative_to(root))
            except ValueError:
                continue
            seen += 1
            if seen > limit:
                break
            if is_dir:
                if not os.access(e.path, os.R_OK | os.X_OK):
                    continue
                yield rel, True
                if depth + 1 <= _WALK_DEPTH:
                    stack.append((e.path, depth + 1))
            else:
                if os.access(e.path, os.R_OK):
                    yield rel, False


def _name_score(q: str, rel: str) -> int:
    """Rank a path against a query. Higher is better, 0 means "no match".

    Tiers (best first): exact basename, basename prefix, basename substring,
    path substring, then a fuzzy subsequence over the basename and finally over
    the whole path — the same ladder an editor's "go to file" uses, so typing
    `plan`, `plan.md`, `acme/plan` or `apl` all land on projects/acme/plan.md.
    """
    name = rel.rsplit("/", 1)[-1]
    fq, fname, frel = _fold(q), _fold(name), _fold(rel)
    stem = fname.rsplit(".", 1)[0]
    if fq == fname or fq == stem:
        return 1000
    if fname.startswith(fq):
        return 900 - min(len(fname), 99)
    if fq in fname:
        return 800 - min(fname.index(fq), 99)
    if fq in frel:
        return 700 - min(frel.index(fq), 99)
    # every word somewhere in the path, in any order: "transcriptor readme"
    toks = [t for t in fq.split() if t]
    if len(toks) > 1 and all(t in frel for t in toks):
        return 600 + min(sum(max(0, 40 - frel.index(t)) for t in toks), 90)
    sub = _subseq_score(fq, fname)
    if sub:
        return 400 + sub
    sub = _subseq_score(fq, frel)
    if sub:
        return 200 + sub
    return 0


def _subseq_score(q: str, hay: str) -> int:
    """Do the query's characters appear in order in `hay`? Score by how tightly
    packed the match is (consecutive characters and matches right after a
    separator score higher), 0 if the subsequence isn't there at all."""
    if not q:
        return 0
    i = score = run = 0
    for pos, ch in enumerate(hay):
        if ch == q[i]:
            run += 1
            score += 2 + run
            if pos == 0 or hay[pos - 1] in " -_./":
                score += 4
            i += 1
            if i == len(q):
                return max(score - pos // 8, 1)
        else:
            run = 0
    return 0


async def search(request: web.Request) -> web.Response:
    q = request.query.get("q", "").strip()
    if not q:
        conn = _db()
        ok = conn is not None
        if conn:
            conn.close()
        return web.json_response({"results": [], "files": [], "db": ok})

    files = []
    try:
        scored = []
        for rel, is_dir in _walk_repo():
            s = _name_score(q, rel)
            if s:
                scored.append((s, rel, is_dir))
        scored.sort(key=lambda t: (-t[0], len(t[1]), t[1]))
        files = [{"path": rel, "dir": is_dir, "score": s} for s, rel, is_dir in scored[:40]]
    except OSError:
        files = []

    conn = _db()
    if conn is None:
        return web.json_response({"results": [], "files": files, "db": False})
    try:
        rows, seen, per_file = [], set(), {}

        def take(r, rank):
            """At most three lines from any one file, so a chatty document can't
            fill the whole result list."""
            key = (r[0], r[1])
            if key in seen or per_file.get(r[0], 0) >= 3:
                return
            seen.add(key)
            per_file[r[0]] = per_file.get(r[0], 0) + 1
            rows.append({"path": r[0], "line": r[1], "kind": r[2], "text": r[3], "rank": rank})

        # Three ways to match, ONE pass over the table. RLS makes kb.can_read()
        # a per-row plpgsql call and the planner runs it before anything else,
        # so the scan — not the matching — is the cost: running the tiers as
        # separate statements would triple the latency for nothing.
        #   1. websearch_to_tsquery — stemming, "quoted phrases", -exclusions.
        #      It never raises on user input, unlike to_tsquery.
        #   2. prefix tsquery — "onbo mee" finds "Onboarding meeting". Tokens are
        #      stripped to [a-z0-9] runs, so nothing reaches tsquery's own
        #      syntax (& | ! : * parentheses); the dictionary is schema-qualified
        #      because every user owns a u_<user> schema that comes first in
        #      their search_path.
        #   3. ILIKE — mid-word matches, identifiers, punctuation and the
        #      non-English text the 'english' stemmer mangles.
        toks = [t for t in re.split(r"[^0-9A-Za-z]+", q.lower()) if len(t) >= 2][:6]
        pq = " & ".join(t + ":*" for t in toks) if toks else None
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with conn.cursor() as cur:
            cur.execute(
                "WITH q AS (SELECT websearch_to_tsquery('pg_catalog.english', %s) AS ws, "
                "                  CASE WHEN %s::text IS NULL THEN NULL::tsquery "
                "                       ELSE to_tsquery('pg_catalog.english', %s) END AS pq) "
                "SELECT b.file_path, b.line, b.kind, b.text, "
                "       ts_rank(b.tsv, q.ws) AS rank, "
                "       (b.tsv @@ q.pq) AS pref "
                "FROM kb.blocks b, q "
                "WHERE b.tsv @@ q.ws OR b.tsv @@ q.pq OR b.text ILIKE %s "
                "ORDER BY rank DESC, length(b.text) LIMIT 90",
                (q, pq, pq, like))
            for r in cur.fetchall():
                take(r, float(r[4]) + (0.02 if r[5] else 0.0))
        rows.sort(key=lambda r: -r["rank"])
        return web.json_response({"results": rows[:30], "files": files, "db": True})
    finally:
        conn.close()


async def artifact_query(request: web.Request) -> web.Response:
    """The artifact query bridge. Runs the SQL AS THIS USER (peer auth), so
    Postgres privileges — RLS on kb, schema grants on u_* — are the entire
    security boundary. An artifact can do exactly what its viewer could type into
    psql, and nothing more. This is why sharing is a deliberate GRANT, not a hole.
    """
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "bad request"}, status=400)
    sql = data.get("sql", "")
    params = data.get("params", []) or []
    if not isinstance(sql, str) or not sql.strip():
        return web.json_response({"error": "no sql"}, status=400)
    conn = _db()
    if conn is None:
        return web.json_response({"error": "db offline"}, status=503)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                cols = [d.name for d in cur.description]
                rows = [list(r) for r in cur.fetchall()]
                out = {"cols": cols, "rows": rows}
            else:
                out = {"cols": [], "rows": [], "rowcount": cur.rowcount}
        return web.json_response(out, dumps=lambda o: json.dumps(o, default=str))
    except Exception as e:
        # Permission denials, syntax errors, etc. all surface as a clean 400.
        return web.json_response({"error": str(e).splitlines()[0]}, status=400)
    finally:
        conn.close()


# Locked-down policy for artifact documents. Delivered as an HTTP header by the
# host (not by the artifact author, who could omit it), so it cannot be bypassed:
#   default-src 'none'   -> nothing loads unless explicitly allowed
#   script/style inline  -> the artifact's own inline code runs, nothing external
#   connect-src 'none'   -> no fetch/XHR/WebSocket/beacon => no data exfiltration
#   img/font data:       -> blocks URL-based exfil (new Image().src=evil?data)
#   img/media blob:      -> lets the artifact DISPLAY binary the bridge handed it
#                           (kb-read-bytes posts a Blob in; the artifact makes its
#                           own object URL). A blob: URL is a local memory handle,
#                           not a network request, so this shows bytes without
#                           opening any way to send them anywhere.
# postMessage is not a CSP-governed channel, so the query bridge still works.
ARTIFACT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; media-src blob:; font-src data:; connect-src 'none'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)


async def artifact_raw(request: web.Request) -> web.StreamResponse:
    """Serve an artifact's HTML with a restrictive CSP header. Loaded into a
    sandboxed (opaque-origin) iframe, so: opaque origin blocks reaching the app;
    CSP blocks reaching the network. The author controls neither."""
    rel = request.query.get("path", "")
    p = common.resolve_repo_path(rel)
    if p is None or not str(p).endswith(".html"):
        return web.Response(status=404)
    try:
        if not p.is_file() or not os.access(p, os.R_OK):
            return web.Response(status=403)
        html = p.read_text(errors="replace")
    except (PermissionError, OSError):
        return web.Response(status=403)
    return web.Response(text=html, content_type="text/html",
                        headers={"Content-Security-Policy": ARTIFACT_CSP,
                                 "X-Content-Type-Options": "nosniff"})


async def artifact_read(request: web.Request) -> web.Response:
    """Read a file for an artifact, AS THIS USER — the kernel enforces access, so
    an artifact can only read what its viewer could read. Bounded by FS perms."""
    data = await request.json()
    p = common.resolve_repo_path(data.get("path", ""))
    if p is None:
        return web.json_response({"error": "bad path"}, status=400)
    try:
        content = p.read_text(errors="replace")
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"ok": True, "content": content})


async def artifact_write(request: web.Request) -> web.Response:
    """Write a file for an artifact, AS THIS USER. The write happens with the
    viewer's kernel identity, so it can only land where the viewer can write —
    same authority as the viewer typing into a terminal. Overwrites in place
    (ownership preserved); the parent directory must already exist."""
    data = await request.json()
    rel = data.get("path", "")
    content = data.get("content", "")
    if not isinstance(content, str):
        return web.json_response({"error": "content must be text"}, status=400)
    p = common.resolve_repo_path(rel)
    if p is None:
        return web.json_response({"error": "bad path"}, status=400)
    if not p.parent.is_dir():
        return web.json_response({"error": "parent folder does not exist"}, status=400)
    try:
        p.write_text(content)
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    common.write_attrib_hint("edit", str(p.relative_to(common.REPO_ROOT)))
    return web.json_response({"ok": True, "path": str(p.relative_to(common.REPO_ROOT)),
                             "bytes": len(content.encode())})


# ---- cron (the user's own crontab; runs AS them, so no privilege to get wrong) ----
# A paused job is kept in the crontab, prefixed with this marker comment.
CRON_PAUSED = "#kb:paused "
# Schedule: either an @keyword or exactly five fields of cron characters
# (digits, * / , - and month/day names). The command may be anything EXCEPT
# newlines — one UI action must map to exactly one crontab line.
_CRON_KEYWORD = re.compile(r"^@(reboot|yearly|annually|monthly|weekly|daily|midnight|hourly)$")
_CRON_FIELD = re.compile(r"^[A-Za-z0-9*/,\-]+$")
_CRON_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


def _cron_sched_ok(sched: str) -> bool:
    sched = sched.strip()
    if _CRON_KEYWORD.match(sched):
        return True
    fields = sched.split()
    return len(fields) == 5 and all(_CRON_FIELD.match(f) for f in fields)


class CronUnavailable(Exception):
    pass


def _crontab_read() -> tuple[bool, str]:
    """Return (installed, raw). `crontab -l` exits non-zero when none exists."""
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CronUnavailable(f"crontab unavailable: {e}") from e
    return (r.returncode == 0, r.stdout if r.returncode == 0 else "")


def _crontab_write(raw: str) -> str | None:
    """Install `raw` as this user's crontab. Returns an error message or None.
    crontab(1) itself re-validates the syntax — a second, authoritative gate."""
    try:
        r = subprocess.run(["crontab", "-"], input=raw, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CronUnavailable(f"crontab unavailable: {e}") from e
    if r.returncode != 0:
        return (r.stderr.strip().splitlines() or ["crontab rejected the input"])[-1]
    return None


def _split_job(text: str) -> tuple[str, str] | None:
    """Split a crontab job line into (schedule, command), or None if not a job."""
    if text.startswith("@"):
        parts = text.split(None, 1)
        if len(parts) == 2 and _CRON_KEYWORD.match(parts[0]):
            return parts[0], parts[1]
        return None
    parts = text.split(None, 5)
    if len(parts) == 6 and all(_CRON_FIELD.match(f) for f in parts[:5]):
        return " ".join(parts[:5]), parts[5]
    return None


def _cron_lines(raw: str) -> list[str]:
    """cron's own line model: split on \\n ONLY. (str.splitlines would also split
    on \\x0b/\\u2028/… and desync our line numbers from what cron sees.)"""
    return raw.split("\n")


def _cron_join(lines: list[str]) -> str:
    out = "\n".join(lines)
    if out and not out.endswith("\n"):
        out += "\n"
    return out


def _cron_listing() -> dict:
    available = CAN_SHELL and shutil.which("crontab") is not None
    try:
        installed, raw = _crontab_read() if available else (False, "")
    except CronUnavailable:
        available, installed, raw = False, False, ""
    jobs = []
    for i, line in enumerate(_cron_lines(raw), start=1):
        text = line.strip()
        paused = text.startswith(CRON_PAUSED.strip())
        if paused:
            text = text[len(CRON_PAUSED.strip()):].strip()
        if not text or (text.startswith("#") and not paused) or _CRON_ENV.match(text):
            continue  # blank / plain comment / environment line — shown via `raw` only
        job = _split_job(text)
        if job is None:
            continue
        jobs.append({"line": i, "raw": line, "schedule": job[0], "command": job[1],
                     "paused": paused})
    return {"available": available, "installed": installed, "user": ME,
            "jobs": jobs, "raw": raw, "v": 13}


# --- launcher buttons (company list is admin-written via the hub; the
# --- personal list lives in the user's own private dir, written AS them) -----
COMPANY_LAUNCHERS = common.REPO_ROOT / ".claude" / "launchers.json"
MY_LAUNCHERS = common.REPO_ROOT / "users" / ME / ".launchers.json"


def _read_launchers(path) -> list:
    try:
        buttons, err = common.validate_launchers(json.loads(path.read_text()))
        return buttons if not err else []
    except (OSError, ValueError):
        return []


async def principals(request: web.Request) -> web.Response:
    """Pickable share targets for the permissions UI: human users + shared
    groups. Account names are world-enumerable on any Unix box (getent passwd),
    so listing them here exposes nothing new."""
    # viewers (nologin shell) ARE pickable — you share files with people, and
    # the uid range already excludes every service account
    users = sorted(e.pw_name for e in pwd.getpwall()
                   if 1000 <= e.pw_uid < 65000 and e.pw_name != "nobody")
    names = {e.pw_name for e in pwd.getpwall()}
    groups = sorted(g.gr_name for g in grp.getgrall()
                    if 1000 <= g.gr_gid < 65000 and g.gr_name not in names)
    return web.json_response({"users": users, "groups": groups})


async def launchers_get(request: web.Request) -> web.Response:
    return web.json_response({"company": _read_launchers(COMPANY_LAUNCHERS),
                              "mine": _read_launchers(MY_LAUNCHERS)})


async def launchers_set(request: web.Request) -> web.Response:
    buttons, err = common.validate_launchers(await request.json())
    if err:
        return web.json_response({"error": err}, status=400)
    try:
        MY_LAUNCHERS.write_text(json.dumps({"buttons": buttons}, indent=2) + "\n")
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "mine": buttons})


def _cron_guard(handler):
    """Uniform failure mode: cron problems are clean 400s, never 500s."""
    async def wrapped(request: web.Request) -> web.Response:
        if not CAN_SHELL:
            return web.json_response({"error": "this account cannot schedule jobs"}, status=403)
        if shutil.which("crontab") is None:
            return web.json_response({"error": "cron is not available on this machine"}, status=400)
        try:
            return await handler(request)
        except CronUnavailable as e:
            return web.json_response({"error": str(e)}, status=400)
    return wrapped


async def cron_list(request: web.Request) -> web.Response:
    return web.json_response(_cron_listing())


# Anything that would break the one-UI-action==one-crontab-line invariant, or
# that cron would parse differently than the user sees. \x09 (tab) is allowed;
# other C0 controls, DEL and unicode line/paragraph separators are not.
_CRON_BADCHARS = re.compile("[\\x00-\\x08\\x0a-\\x1f\\x7f\\x85\\u2028\\u2029]")
_CRON_UNESCAPED_PCT = re.compile(r"(?<!\\)%")


async def cron_add(request: web.Request) -> web.Response:
    data = await request.json()
    sched = str(data.get("schedule", "")).strip()
    cmd = str(data.get("command", "")).strip()
    if not _cron_sched_ok(sched):
        return web.json_response(
            {"error": "bad schedule — use 5 cron fields (min hour dom mon dow) or @daily/@hourly/…"},
            status=400)
    if not cmd or _CRON_BADCHARS.search(cmd) or len(cmd) > 2000:
        return web.json_response({"error": "bad command (single line, max 2000 chars)"}, status=400)
    if _CRON_UNESCAPED_PCT.search(cmd):
        return web.json_response(
            {"error": "cron treats % specially (end-of-command + stdin) — escape it as \\%"},
            status=400)
    _, raw = _crontab_read()
    if raw and not raw.endswith("\n"):
        raw += "\n"
    err = _crontab_write(raw + f"{' '.join(sched.split())} {cmd}\n")
    if err:
        return web.json_response({"error": err}, status=400)
    return web.json_response(_cron_listing())


def _cron_take_line(data: dict) -> tuple[list[str], int] | web.Response:
    """Common guard for remove/toggle: re-read the crontab and verify the client's
    idea of the target line still matches — refuse to edit a stale line number."""
    try:
        line_no = int(data.get("line", 0))
    except (TypeError, ValueError):
        return web.json_response({"error": "bad line"}, status=400)
    _, raw = _crontab_read()
    lines = _cron_lines(raw)
    if not (1 <= line_no <= len(lines)):
        return web.json_response({"error": "no such line"}, status=400)
    if lines[line_no - 1] != data.get("raw"):
        return web.json_response({"error": "crontab changed since you loaded it — refresh"},
                                 status=409)
    return lines, line_no


async def cron_remove(request: web.Request) -> web.Response:
    got = _cron_take_line(await request.json())
    if isinstance(got, web.Response):
        return got
    lines, line_no = got
    del lines[line_no - 1]
    err = _crontab_write(_cron_join(lines))
    if err:
        return web.json_response({"error": err}, status=400)
    return web.json_response(_cron_listing())


async def cron_toggle(request: web.Request) -> web.Response:
    got = _cron_take_line(await request.json())
    if isinstance(got, web.Response):
        return got
    lines, line_no = got
    cur = lines[line_no - 1]
    stripped = cur.strip()
    if stripped.startswith(CRON_PAUSED.strip()):
        lines[line_no - 1] = stripped[len(CRON_PAUSED.strip()):].strip()
    elif _split_job(stripped):
        lines[line_no - 1] = CRON_PAUSED + stripped
    else:
        return web.json_response({"error": "not a job line"}, status=400)
    err = _crontab_write(_cron_join(lines))
    if err:
        return web.json_response({"error": err}, status=400)
    return web.json_response(_cron_listing())


# --- persistent pty sessions ------------------------------------------------
# A terminal SURVIVES its websocket: refreshing the page (or losing the
# connection) detaches the client but keeps the shell running, with recent
# output buffered; reconnecting with the same session id replays the buffer
# and reattaches. A shell ends only when its tab is explicitly killed, when
# the shell itself exits, or when the oldest detached session is evicted to
# make room. (Backend restarts still end all shells — ptys die with us.)
PTY_SESSIONS: dict = {}
PTY_BUF_MAX = 256 * 1024
PTY_QUEUE_MAX = 4 * 1024 * 1024   # unsent backlog to a slow client before resync
PTY_MAX_SESSIONS = 12
_SID_RE = re.compile(r"^[a-f0-9]{8,32}$")


class PtySession:
    def __init__(self, sid: str):
        self.sid = sid
        self.buf = bytearray()
        self.total = 0                   # bytes ever produced (replay offsets)
        self.ws = None
        self.q = None                    # attached client's queue (one at a time)
        self.qbytes = 0                  # queued-but-unsent (slow client) backpressure
        self.attach_seq = 0              # claim ticket: the newest attach wins
        self.alive = True
        self.detached_at = time.time()
        self.loop = asyncio.get_event_loop()
        pid, fd = pty.fork()
        if pid == 0:  # child -> becomes the user's shell
            # NOTHING here may raise past this block: the child is a fork of the
            # whole backend (event loop, sockets and all), so an escaping
            # exception would leave a rogue second server running as the user.
            # Every failure path ends in os._exit.
            try:
                os.environ["TERM"] = "xterm-256color"
                # Start in the shared company root (everyone can access it)
                # rather than the user's home, so the terminal lands inside the
                # knowledgebase.
                try:
                    os.chdir(common.REPO_ROOT / "company")
                except OSError:
                    try:
                        os.chdir(pwd.getpwuid(os.geteuid()).pw_dir)
                    except (OSError, KeyError):
                        os.chdir("/")
                # the account's OWN shell (defense in depth: for a viewer this
                # would be nologin, which prints its notice and exits)
                os.execvp(MY_SHELL, [MY_SHELL, "-l"])
            except BaseException:
                try:
                    os.write(2, b"kb: could not start a shell\r\n")
                except OSError:
                    pass
            os._exit(1)
        # (pty.fork's child never returns here — it exec'd or _exit'd above)
        self.pid, self.fd = pid, fd
        self.loop.add_reader(fd, self._on_readable)

    def _on_readable(self):
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        if not data:                     # EOF: the shell exited
            self._teardown()
            return
        self.total += len(data)
        self.buf.extend(data)
        if len(self.buf) > PTY_BUF_MAX:
            del self.buf[:len(self.buf) - PTY_BUF_MAX]
        if self.q is None:
            return
        # A firehose (yes | cat) plus a slow client would otherwise grow the
        # send queue without bound — the ring caps history, not the backlog.
        # Past the limit, drop the backlog and tell the client to resync from
        # the buffer: one visible catch-up beats unbounded memory.
        if self.qbytes > PTY_QUEUE_MAX:
            while not self.q.empty():
                try:
                    self.q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self.qbytes = 0
            self.q.put_nowait({"reset": True, "base": self.total - len(self.buf)})
            self.q.put_nowait(bytes(self.buf))
            self.qbytes += len(self.buf)
            return
        self.q.put_nowait(bytes(data))
        self.qbytes += len(data)

    def write(self, data: bytes) -> None:
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def kill(self) -> None:
        """Explicit kill (tab ×) — ends the shell like closing a tab always did."""
        self._teardown()

    def _teardown(self) -> None:
        if not self.alive:
            return
        self.alive = False
        try:
            self.loop.remove_reader(self.fd)
        except (OSError, ValueError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        if self.q is not None:
            self.q.put_nowait(None)      # sentinel -> the sender closes the ws
        PTY_SESSIONS.pop(self.sid, None)
        pid = self.pid

        # Reap the shell so it never lingers as a zombie in this long-lived
        # backend. Closing the pty already hands it SIGHUP/EOF; nudge it, give
        # it a grace period, then hard-kill (disowned/nohup'd grandchildren are
        # separate pids and survive).
        def _reap():
            try:
                os.kill(pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
            for _ in range(100):  # ~10s grace
                try:
                    done, _st = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    return
                if done:
                    return
                time.sleep(0.1)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

        self.loop.run_in_executor(None, _reap)


def _evict_for_new_session() -> bool:
    """Make room for a new shell. Only ever kills DETACHED sessions (nobody is
    looking at them) — an attached terminal, or a detached one when the cap is
    all attached, is never sacrificed. Returns True when there is room."""
    if len(PTY_SESSIONS) < PTY_MAX_SESSIONS:
        return True
    detached = [s for s in PTY_SESSIONS.values() if s.q is None]
    if detached:
        min(detached, key=lambda s: s.detached_at).kill()
    return len(PTY_SESSIONS) < PTY_MAX_SESSIONS


async def pty_handler(request: web.Request) -> web.StreamResponse:
    if not CAN_SHELL:
        return web.json_response({"error": "this account has no shell"}, status=403)
    sid = request.query.get("session", "")
    if not _SID_RE.match(sid):
        sid = os.urandom(8).hex()
    try:
        # bytes the client already has — a reconnect replays only what it missed
        have = max(0, int(request.query.get("have", "0")))
    except ValueError:
        have = 0
    # create=0 means "reattach only": used by a RECONNECTING tab and by the
    # kill-while-disconnected path, neither of which wants a brand-new shell
    # conjured under an id whose session is gone (that silently swaps a dead
    # shell for a fresh one — and at the cap it would evict someone else's).
    create = request.query.get("create", "1") != "0"
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    async def _bye(payload: dict) -> web.WebSocketResponse:
        try:
            await ws.send_str(json.dumps(payload))
        except (ConnectionResetError, RuntimeError):
            pass
        await ws.close()
        return ws

    sess = PTY_SESSIONS.get(sid)
    if sess is not None and sess.alive:
        # One client per session, newest wins. Claim a ticket BEFORE any await:
        # whoever claims last owns the session, so two attaches racing through
        # the awaits below can't clobber each other silently.
        sess.attach_seq += 1
        myseq = sess.attach_seq
        old = sess.ws
        if old is not None and old is not ws:
            sess.ws = None
            sess.q = None
            # tell the superseded client why, so it retires instead of fighting
            try:
                await old.send_str(json.dumps({"detached": True}))
            except Exception:
                pass
            try:
                await old.close()
            except Exception:
                pass
        # Anything can have happened while we awaited: the shell may have hit
        # EOF (its teardown had no queue to post the sentinel to), or a newer
        # attach may have claimed the session.
        if sess.attach_seq != myseq:
            return await _bye({"detached": True})
        if not sess.alive or PTY_SESSIONS.get(sid) is not sess:
            return await _bye({"exit": True})
    if sess is None or not sess.alive:
        if not create:
            return await _bye({"gone": True})
        if not _evict_for_new_session():
            return await _bye({"error": "too many terminals — close one first"})
        sess = PtySession(sid)
        PTY_SESSIONS[sid] = sess
        sess.attach_seq += 1
    # snapshot + seed + attach happen with no await in between (single-threaded
    # event loop), so no pty output can slip between replay and live stream
    queue: asyncio.Queue = asyncio.Queue()
    missed = sess.total - have
    seed: list = []
    if have <= 0 or missed < 0 or missed > len(sess.buf):
        # can't bridge the gap (fresh client, or the ring outgrew it): the
        # client starts from a clean screen and gets everything we still hold
        seed.append({"reset": True, "base": sess.total - len(sess.buf)})
        if sess.buf:
            seed.append(bytes(sess.buf))
    elif missed:
        seed.append(bytes(sess.buf[len(sess.buf) - missed:]))
    for item in seed:
        queue.put_nowait(item)
    sess.ws = ws
    sess.q = queue
    sess.qbytes = sum(len(i) for i in seed if not isinstance(i, dict))

    async def to_client():
        while True:
            item = await queue.get()
            if item is None:
                break
            try:
                if isinstance(item, dict):
                    await ws.send_str(json.dumps(item))
                else:
                    if sess.q is queue:
                        sess.qbytes = max(0, sess.qbytes - len(item))
                    await ws.send_bytes(item)
            except (ConnectionResetError, RuntimeError):
                return
        # the shell exited (or was killed) — say so explicitly, so the client
        # retires the tab; a bare close is indistinguishable from a network drop
        try:
            await ws.send_str(json.dumps({"exit": True}))
        except (ConnectionResetError, RuntimeError):
            return
        await ws.close()

    sender = asyncio.create_task(to_client())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                sess.write(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    ctl = json.loads(msg.data)
                except ValueError:
                    sess.write(msg.data.encode())
                    continue
                if not isinstance(ctl, dict) or "ping" in ctl:
                    continue             # keepalive traffic — never reaches the shell
                if "resize" in ctl:
                    sess.resize(ctl["resize"]["rows"], ctl["resize"]["cols"])
                elif ctl.get("kill"):
                    sess.kill()
                    break
                else:
                    sess.write(msg.data.encode())
    finally:
        sender.cancel()
        if sess.ws is ws:                # plain detach: the shell keeps running
            sess.ws = None
            sess.q = None
            sess.detached_at = time.time()
    return ws


def make_app() -> web.Application:
    app = web.Application(client_max_size=2 * 1024 * 1024 * 1024)
    app.router.add_get("/api/whoami", whoami)
    app.router.add_get("/api/tree", tree)
    app.router.add_get("/api/file", read_file)
    app.router.add_post("/api/file", create_file)
    app.router.add_post("/api/fs/mkdir", fs_mkdir)
    app.router.add_post("/api/fs/delete", fs_delete)
    app.router.add_post("/api/fs/rename", fs_rename)
    app.router.add_post("/api/fs/copy", fs_copy)
    app.router.add_post("/api/upload", upload)
    app.router.add_get("/api/attachment", attachment)
    app.router.add_get("/api/tasks", tasks)
    app.router.add_post("/api/tasks/toggle", toggle_task)
    app.router.add_get("/api/search", search)
    app.router.add_post("/api/artifact/query", artifact_query)
    app.router.add_get("/api/artifact/raw", artifact_raw)
    app.router.add_post("/api/artifact/read", artifact_read)
    app.router.add_post("/api/artifact/write", artifact_write)
    app.router.add_get("/api/principals", principals)
    app.router.add_get("/api/launchers", launchers_get)
    app.router.add_post("/api/launchers", launchers_set)
    app.router.add_get("/api/cron", cron_list)
    app.router.add_post("/api/cron/add", _cron_guard(cron_add))
    app.router.add_post("/api/cron/remove", _cron_guard(cron_remove))
    app.router.add_post("/api/cron/toggle", _cron_guard(cron_toggle))
    app.router.add_get("/pty", pty_handler)
    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uds", required=True)
    args = ap.parse_args()
    # Owner-only socket. Safe for created files too: shared dirs (company/acme)
    # carry default ACLs that grant group access regardless of umask; only private
    # dirs and this socket fall through to the restrictive 0077 default.
    os.umask(0o077)
    if os.path.exists(args.uds):
        os.unlink(args.uds)
    app = make_app()

    async def _fix_perms(app):
        for _ in range(50):
            if os.path.exists(args.uds):
                os.chmod(args.uds, 0o600)
                return
            await asyncio.sleep(0.02)

    app.on_startup.append(_fix_perms)
    web.run_app(app, path=args.uds, print=None)


if __name__ == "__main__":
    main()
