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
import hashlib
import json
import logging
import os
import grp
import pty
import pwd
import re
import secrets
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import termios
import time
import unicodedata
import zipfile
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

from . import common, uploads
from . import acp as kbacp
from . import settings as kbsettings

try:
    import psycopg
except Exception:  # pragma: no cover
    psycopg = None

log = logging.getLogger("kb.user")
ME = pwd.getpwuid(os.geteuid()).pw_name
MY_SHELL = pwd.getpwuid(os.geteuid()).pw_shell
# "viewer" accounts (nologin shell) get the full webapp but no execution: the
# pty and cron endpoints are gated here, and the OS enforces the same thing
# underneath (nologin + /etc/cron.deny, managed by the hub).
CAN_SHELL = MY_SHELL not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "")


async def _adb():
    """One async connection per request, peer-authed as this user (RLS is the
    security boundary). Async, not sync-on-the-loop: this process is a single
    event loop per user, so ONE slow statement in a sync handler freezes that
    user's ENTIRE backend — tree, docs, everything. That was a live bug: a
    content search took ~15 s under the old per-block RLS policy and the app
    went dark for its duration. Queries here may be arbitrarily heavy (search
    over the whole corpus, artifact-supplied SQL), so the loop must only ever
    await them.
    """
    if psycopg is None:
        return None
    try:
        return await psycopg.AsyncConnection.connect(
            f"dbname={common.PG_DB}", autocommit=True)
    except Exception:
        return None


async def whoami(request: web.Request) -> web.Response:
    # The definitive identity: who this process actually runs as (kernel truth),
    # not whatever header the hub claims. They must match.
    # `v` rides along so the client knows the pty protocol before it restores a
    # single terminal — it used to learn it from a separate /api/cron round trip
    # that raced the restore, and a terminal that lost the race reattached the
    # old way (a hard reset instead of an offset replay).
    return web.json_response({"user": ME, "uid": os.geteuid(), "shell": CAN_SHELL,
                              "claimed": request.headers.get("X-KB-User"), "v": BACKEND_V})


_NUM_RE = re.compile(r"(\d+)")
_LEAD_ICON_RE = re.compile(r"^\W+", re.UNICODE)


# Backend build marker. scripts/bounce_backends.py imports this rather than
# restating it, so the two cannot drift the way MIN_V=20 drifted from v=23.
# Bump whenever backend behaviour changes, so a stale backend cannot report
# itself current and be silently skipped by a bounce.
BACKEND_V = 35   # /api/inbox (mentions, shares); a delete is a move into .trash/


def _name_key(name: str):
    """Explorer-style ordering: symbols, then digits, then letters, then emoji.

    Plain codepoint order puts `T-Systems` above `_files` and buries every
    emoji-prefixed folder at the bottom by accident. Bucket on the first
    character instead, then sort naturally (`v2` before `v10`) on the folded
    name so case and diacritics don't split obvious neighbours.

    Inside the emoji bucket the icon is decoration, not the name: strip it and
    sort on the words after it, so `🎨 Design` precedes `💡 Know How` instead of
    the two being ordered by whichever codepoint their icon happens to have.
    """
    ch = name[:1]
    if not ch:
        cat = 0
    elif ord(ch) >= 0x2000:  # emoji and the symbol blocks — after Z, on purpose
        cat = 3
    elif ch.isdigit():
        cat = 1
    elif ch.isalpha():
        cat = 2
    else:
        cat = 0              # _ - . ( ~ …
    text = name
    if cat == 3:
        # Drops the icon plus its trailing space, and any ZWJ/variation-selector
        # parts of a multi-codepoint emoji. An icon-only name keeps its own
        # codepoints so it still sorts somewhere stable.
        text = _LEAD_ICON_RE.sub("", name) or name
    folded = _fold(text)
    parts = tuple((int(p), "") if p.isdigit() else (-1, p)
                  for p in _NUM_RE.split(folded) if p)
    return (cat, parts, name)  # raw name last: stable, total order for `a` vs `A`


def _entry_key(e):
    """How one directory row sorts: folders first by name, files newest first.

    Folders are navigation — their place has to stay put, so they keep the
    explorer ordering above. Files are work, and the one you touched last is
    the one you want, so they sort by mtime descending with the name only
    breaking ties (two files written in the same second).
    """
    try:
        is_dir = e.is_dir()
    except OSError:
        is_dir = False
    if is_dir:
        return (0, 0.0, _name_key(e.name))
    try:
        mtime = e.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (1, -mtime, _name_key(e.name))


def _aud_key(path: Path):
    """The readership of one path, or None when it cannot be read at all."""
    try:
        return common.readership_key(*common.stat_and_acl(path))
    except OSError:
        return None


def _aud_flag(child, parent) -> str | None:
    """How a child's readership differs from its folder's — None when it simply
    follows along, which is almost everything and must stay unmarked."""
    if child is None or parent is None or child == parent:
        return None
    cgid, call, cnamed = child
    _pgid, pall, _pnamed = parent
    if cgid is None and not call and not cnamed:
        return "solo"          # nobody but the owner can read it
    if call and not pall:
        return "open"          # readable more widely than the folder it sits in
    return "custom"            # a different set of people from the folder


# ---- the file tree, and why asking for it again is cheap ---------------------
# Every open browser tab asks for the tree every 4 s. Building it is real
# security work that has to run as the user — an O_PATH open, an fstat and an
# ACL read for every entry — and the answer is ~640 KB that has not changed
# 99.9 % of the times it is asked for. So the question is split in two:
#
#   * a SIGNATURE walk (scandir + one lstat per entry, no ACL reads, no Path
#     objects) hashes everything the JSON is a function of — name, mode, owner,
#     size, mtime and ctime. chmod and setfacl bump ctime, so a permission
#     change is caught without reading a single xattr. Measured: ~37 ms on this
#     box against ~180 ms for the real walk (and ~550 ms for the real walk as it
#     was, more than half of which was pathlib building objects to compute a
#     string scandir already hands over);
#   * the real walk runs only when the signature moved, and its bytes are kept,
#     so a poll that finds nothing changed is answered from the ETag.
#
# Deterministic on purpose. An inotify watcher would make the unchanged poll
# O(1) and was tried first: a recursive watch over the repo as an unprivileged
# user stayed silent, the kernel caps inotify instances per uid (31 of 128 were
# already in use on this box), and inotify drops events under load — the
# indexer's PERMS_RESCAN exists for exactly that. The signature never lies and
# needs no fallback.
#
# It is checked at most once per TREE_SIG_TTL per backend: all of one user's
# tabs share this process, so N tabs cost one walk. A mutation THROUGH this
# backend clears the hold (the middleware below), so your own new file is in
# the very next poll; a change made elsewhere — a colleague, the hub's /fs/*,
# a terminal — is seen within TREE_SIG_TTL plus the client's poll interval.
#
# Everything blocking runs in the executor. This process is one event loop per
# user, and a walk ON the loop used to freeze that user's terminal and every
# other request for its duration — every 4 s, per open tab.
TREE_MAX_DEPTH = 12
TREE_SIG_TTL = 2.0


def _tree_skip(name: str) -> bool:
    """Git internals and our own upload spools: never in the tree, and never a
    reason to rebuild it."""
    return name == ".git" or name.endswith(".kbtmp")


def _tree_signature(root: str) -> str:
    h = hashlib.blake2b(digest_size=16)

    def walk(d: str, depth: int) -> None:
        if depth > TREE_MAX_DEPTH:
            return
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if _tree_skip(e.name):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            h.update(e.name.encode("utf-8", "surrogateescape"))
            h.update(b"%d,%d,%d,%d,%d,%d\0" % (st.st_mode, st.st_uid, st.st_gid, st.st_size,
                                                st.st_mtime_ns, st.st_ctime_ns))
            if stat.S_ISDIR(st.st_mode) and os.access(e.path, os.R_OK | os.X_OK):
                walk(e.path, depth + 1)

    try:
        st = os.stat(root)
        h.update(b"%d,%d\0" % (st.st_mode, st.st_ctime_ns))
    except OSError:
        pass
    walk(root, 0)
    return h.hexdigest()


def _build_tree(root: str) -> dict:
    """The full walk: what the sidebar shows, with the kernel's answer on every
    entry. Plain strings throughout — `e.path` is already the joined path, and
    the repo-relative form is a slice of it."""
    prefix = root.rstrip("/") + "/"

    def walk(d: str, depth: int, parent_aud=None) -> list:
        out = []
        if depth > TREE_MAX_DEPTH:
            return out
        try:
            entries = sorted(os.scandir(d), key=_entry_key)
        except OSError:
            return out
        for e in entries:
            # Show everything except git internals and our temp files — including
            # dot-directories like .claude (agent config/skills).
            if _tree_skip(e.name):
                continue
            p = e.path
            rel = p[len(prefix):] if p.startswith(prefix) else p
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            access = {"read": os.access(p, os.R_OK), "write": os.access(p, os.W_OK)}
            # depth 0/1 = the areas themselves and the project/team folders
            # directly under them, where having your own audience IS the
            # convention — flagging those would put a marker on every row and
            # teach people to ignore all of them.
            aud = _aud_key(p)
            flag = _aud_flag(aud, parent_aud) if depth >= 1 else None
            if is_dir:
                if not os.access(p, os.R_OK | os.X_OK):
                    continue
                out.append({"name": e.name, "path": rel, "dir": True, "access": access,
                            **({"aud": flag} if flag else {}),
                            "children": walk(p, depth + 1, aud)})
            else:
                if not os.access(p, os.R_OK):
                    continue
                if e.name.endswith(".html"):
                    kind = "artifact"
                elif e.name.endswith(".md"):
                    kind = "md"
                else:
                    kind = "file"
                try:
                    mtime = int(e.stat().st_mtime)
                except OSError:
                    mtime = None
                out.append({"name": e.name, "path": rel, "dir": False, "kind": kind,
                            **({"mtime": mtime} if mtime is not None else {}),
                            **({"aud": flag} if flag else {}), "access": access})
        return out

    return {"root": root, "tree": walk(root, 0, _aud_key(root))}


class _TreeCache:
    __slots__ = ("sig", "sig_at", "etag", "body", "index", "lock")

    def __init__(self) -> None:
        self.sig = None          # signature the cached body was built from
        self.sig_at = 0.0        # when the signature was last checked (monotonic)
        self.etag = None         # a hash of `body`, quoted for the header
        self.body = None         # the JSON bytes
        self.index = None        # path -> what a row shows (for the event delta)
        self.lock = asyncio.Lock()


_TREE = _TreeCache()


def _tree_dirty() -> None:
    """Something changed through this backend: re-check on the very next poll
    — and right now for anyone listening on /api/events."""
    _TREE.sig_at = 0.0
    _EVENTS.kick.set()


def _tree_index(nodes: list, out: dict) -> dict:
    """path -> the tuple of everything a tree row shows. Two indexes diff into
    the delta /api/events sends instead of the tree."""
    for n in nodes:
        acc = n.get("access") or {}
        out[n["path"]] = (bool(n.get("dir")), n.get("mtime"), bool(acc.get("read")),
                          bool(acc.get("write")), n.get("aud"))
        if n.get("dir"):
            _tree_index(n.get("children") or [], out)
    return out


async def _refresh_tree() -> None:
    """Bring _TREE up to date: a signature check at most once per TREE_SIG_TTL,
    a rebuild only when the signature moved. Single flight: N tabs (and the
    event loop below) share one walk."""
    root = os.fspath(common.REPO_ROOT)
    loop = asyncio.get_running_loop()
    async with _TREE.lock:
        if _TREE.body is None or time.monotonic() - _TREE.sig_at >= TREE_SIG_TTL:
            sig = await loop.run_in_executor(None, _tree_signature, root)
            _TREE.sig_at = time.monotonic()
            if _TREE.body is None or sig != _TREE.sig:
                data = await loop.run_in_executor(None, _build_tree, root)
                body = json.dumps(data).encode()
                _TREE.sig, _TREE.body = sig, body
                # Hash of the BYTES, not the signature: a rebuild that produced
                # the same tree (a spool that came and went) is still a 304.
                _TREE.etag = '"' + hashlib.blake2b(body, digest_size=8).hexdigest() + '"'
                _TREE.index = _tree_index(data["tree"], {})


@web.middleware
async def _tree_dirty_on_write(request: web.Request, handler):
    resp = await handler(request)
    # Any write that went through this process — a new file, a folder, a
    # delete, a rename, a copy, an upload, a toggled task, an artifact's write —
    # may have changed the tree. The way to never forget one is to not
    # enumerate them.
    if request.method != "GET" and 200 <= resp.status < 300:
        _tree_dirty()
    return resp


async def tree(request: web.Request) -> web.Response:
    if request.query.get("fresh") == "1":      # "Reload the file tree": no hold
        _tree_dirty()
    await _refresh_tree()
    etag, body = _TREE.etag, _TREE.body
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers={"ETag": etag})
    return web.Response(body=body, content_type="application/json", headers={"ETag": etag})


# --- live events: the tree, presence and config PUSHED over one SSE stream ---
# The browser used to poll the tree every 4 s per tab (641 KB on a real
# knowledgebase, parsed and rebuilt on the main thread — a stutter on a phone
# every 4 s while typing, because your own saves move the signature). Now a
# tab holds one GET /api/events open and receives:
#   hello     {etag, presence, v}        on every (re)connect — the catch-up
#   tree      {etag, full, changed:[{path, mtime}]}
#             full=false: only file mtimes moved (what typing produces); the
#             client patches those rows. full=true: anything structural —
#             refetch with the ETag, deferred until typing pauses.
#   presence  {presence}                 only when the set changed
#   config    {paths}                    launchers/settings on disk changed
#   ping      {}                         every 20 s: keeps Cloudflare (100 s
#             idle cut) and the client's dead-link watchdog fed. A real event,
#             not an SSE comment: comments never reach JavaScript.
# One walker per BACKEND, alive only while a stream is open: the signature
# check every 2 s (or at once after a write through this process), presence
# from syncd's world-connectable socket every 3 s. Zero work with no listener.
EVENTS_TREE_PERIOD = 2.0
EVENTS_PRESENCE_PERIOD = 3.0
EVENTS_HEARTBEAT = 20.0
EVENTS_MAX_CHANGED = 200        # more rows than this: send full, not a delta
EVENTS_QUEUE_LIMIT = 64         # a client that stopped reading is dropped, not buffered


class _Events:
    def __init__(self) -> None:
        self.subs: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.kick = asyncio.Event()
        self.presence = None            # last presence map, or None if unavailable
        self.presence_at = 0.0
        self.vc: aiohttp.ClientSession | None = None


_EVENTS = _Events()


def _tree_delta(old: dict | None, new: dict) -> dict:
    """What moved between two indexes. `changed` lists files whose ONLY
    difference is the mtime; anything else — added, removed, a directory,
    permissions, audience — is `full`, and `paths` names what moved."""
    if old is None:
        return {"full": True, "paths": []}
    paths, changed, structural = [], [], False
    for p in old.keys() - new.keys():
        paths.append(p); structural = True
    for p, t in new.items():
        o = old.get(p)
        if o is None:
            paths.append(p); structural = True
        elif o != t:
            paths.append(p)
            if not o[0] and not t[0] and o[2:] == t[2:]:     # a file, mtime only
                changed.append({"path": p, "mtime": t[1]})
            else:
                structural = True
    if structural or len(changed) > EVENTS_MAX_CHANGED:
        return {"full": True, "paths": paths[:200]}
    return {"full": False, "changed": changed, "paths": paths[:200]}


def _config_touched(paths: list) -> bool:
    mine = f"users/{ME}/.os/"
    return any(p.startswith(common.CONFIG_DIRNAME + "/") or p.startswith(mine) for p in paths)


def _events_broadcast(event: str, data: dict) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    for q in list(_EVENTS.subs):
        if q.qsize() < EVENTS_QUEUE_LIMIT:
            q.put_nowait(msg)


async def _fetch_presence():
    """Who has which doc open, permission-filtered for me — from syncd over
    its world-connectable socket, where the kernel says who is asking. None
    when unavailable (an older syncd): the client then keeps a slow poll."""
    try:
        if _EVENTS.vc is None or _EVENTS.vc.closed:
            _EVENTS.vc = aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=str(common.VC_SOCK)),
                timeout=aiohttp.ClientTimeout(total=3))
        async with _EVENTS.vc.get("http://kb/presence") as r:
            if r.status != 200:
                return None
            j = await r.json()
            return j.get("presence") if isinstance(j, dict) else None
    except Exception:
        return None


async def _events_loop() -> None:
    # Baseline first: the loop's "last seen" must be the tree the first hello
    # announced, or the first turn would report a change that never happened.
    await _refresh_tree()
    last_etag, last_index = _TREE.etag, _TREE.index
    while _EVENTS.subs:
        try:
            await asyncio.wait_for(_EVENTS.kick.wait(), EVENTS_TREE_PERIOD)
        except asyncio.TimeoutError:
            pass
        _EVENTS.kick.clear()
        if not _EVENTS.subs:
            break
        try:
            await _refresh_tree()
            if _TREE.etag != last_etag:
                delta = _tree_delta(last_index, _TREE.index)
                last_etag, last_index = _TREE.etag, _TREE.index
                delta["etag"] = _TREE.etag
                _events_broadcast("tree", delta)
                if _config_touched(delta.get("paths", [])):
                    _events_broadcast("config", {"paths": delta["paths"]})
            now = time.monotonic()
            if now - _EVENTS.presence_at >= EVENTS_PRESENCE_PERIOD:
                _EVENTS.presence_at = now
                p = await _fetch_presence()
                if p is not None and p != _EVENTS.presence:
                    _EVENTS.presence = p
                    _events_broadcast("presence", {"presence": p})
        except Exception as e:                       # never let one bad turn end the stream
            log.warning("events loop: %s", e)
            await asyncio.sleep(1)


async def events(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue()
    _EVENTS.subs.add(q)
    try:
        await _refresh_tree()                  # before the loop baselines itself
        if _EVENTS.task is None or _EVENTS.task.done():
            _EVENTS.task = asyncio.create_task(_events_loop())
        if _EVENTS.presence is None:
            _EVENTS.presence = await _fetch_presence()
            _EVENTS.presence_at = time.monotonic()
        hello = {"etag": _TREE.etag, "presence": _EVENTS.presence, "v": BACKEND_V}
        await resp.write(f"retry: 3000\nevent: hello\ndata: {json.dumps(hello)}\n\n".encode())
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), EVENTS_HEARTBEAT)
            except asyncio.TimeoutError:
                msg = "event: ping\ndata: {}\n\n"
            await asyncio.wait_for(resp.write(msg.encode()), 10)   # a stalled reader is dropped
    except (ConnectionResetError, asyncio.CancelledError, asyncio.TimeoutError, RuntimeError):
        pass
    finally:
        _EVENTS.subs.discard(q)
    return resp


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
        common.create_with_mode(p, exclusive=True)   # audience of its folder
        common.write_attrib_hint("create", rel)
        return web.json_response({"ok": True, "path": rel})
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)


async def fs_mkdir(request: web.Request) -> web.Response:
    """Create a folder AS the user. Kernel decides (write+x on the parent); the
    new folder inherits its parent's audience (birth_mode), so a subfolder of a
    team folder is readable by that team whether the sharing is by owning group
    or by default ACL."""
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
        common.mkdir_with_mode(p)
    except PermissionError:
        return web.json_response({"error": "no write access to the parent folder"}, status=403)
    except FileExistsError:
        return web.json_response({"error": "already exists"}, status=409)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "path": str(p.relative_to(common.REPO_ROOT.resolve()))})


# ---- the inbox: what happened while you were elsewhere ----------------------

async def inbox_get(request: web.Request) -> web.Response:
    """Your events, newest first, with how many are unread. The file is yours
    (0600 in your own `.os/`); root wrote it, you own it, and you can read it
    in a terminal exactly as this does."""
    items = common.read_inbox(ME)
    items.reverse()
    return web.json_response({"events": items,
                              "unread": sum(1 for e in items if not e.get("read"))})


async def inbox_read(request: web.Request) -> web.Response:
    """Mark events read — a list of ids, or `all`. Marking is a rewrite of
    your own file, so it happens as you and needs nobody else."""
    try:
        data = await request.json()
    except ValueError:
        data = {}
    ids = data.get("ids")
    want = set(ids) if isinstance(ids, list) else None
    items = common.read_inbox(ME)
    if data.get("clear"):
        items = [] if want is None else [e for e in items if e["id"] not in want]
    else:
        for e in items:
            if want is None or e["id"] in want:
                e["read"] = True
    p = common.inbox_path(ME)
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = p.with_suffix(".kbtmp")
        with open(tmp, "w") as f:
            for e in items:
                f.write(json.dumps(e) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True, "unread": sum(1 for e in items if not e.get("read"))})


# ---- the trash -------------------------------------------------------------
# Deleting moves the thing into a `.trash/` beside it — `company/plans/x.md`
# becomes `company/plans/.trash/x.md`. That is the whole design: no ids, no
# manifest, no metadata. Where it came from is the folder the `.trash` is in,
# when it went is the inode's ctime (a rename updates it), whose it is the
# file's own owner. Everything runs AS the user, so the kernel decides what
# may move and what may come back, and an agent with a shell sees exactly
# what the app sees.

def _trash_name(trash: Path, name: str) -> Path:
    """A free name inside that `.trash` — deleting two things called notes.md
    keeps both."""
    target = trash / name
    if not os.path.lexists(target):
        return target
    stem, dot, ext = name.partition(".")
    for n in range(2, 500):
        cand = trash / (stem + " " + str(n) + (dot + ext if dot else ""))
        if not os.path.lexists(cand):
            return cand
    return trash / (name + "." + secrets.token_hex(3))


def _free_name(target: Path) -> Path:
    """…and the same courtesy on the way back: restoring never writes over
    something that has taken the name in the meantime."""
    if not os.path.lexists(target):
        return target
    stem, dot, ext = target.name.partition(".")
    n = 0
    while True:
        n += 1
        suffix = " (restored)" if n == 1 else f" (restored {n})"
        cand = target.with_name(stem + suffix + (dot + ext if dot else ""))
        if not os.path.lexists(cand):
            return cand


def _trash_dirs() -> list[Path]:
    """Every `.trash` in the repo this account can look into. A full walk, but
    only when someone opens the trash: `_secrets` and `.git` are pruned, and a
    folder the kernel refuses is simply not there."""
    out = []
    root = common.REPO_ROOT.resolve()
    for dirpath, dirnames, _ in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "_secrets") and not d.startswith(".trash")]
        d = Path(dirpath) / common.TRASH_DIRNAME
        if d.is_dir():
            out.append(d)
        # a `.trash` is never walked into: what is in it is a flat list
        dirnames[:] = [d2 for d2 in dirnames if d2 != common.TRASH_DIRNAME]
    return out


async def fs_trash(request: web.Request) -> web.Response:
    """What is in every `.trash` you can read, newest first."""
    root = common.REPO_ROOT.resolve()
    items = []
    for d in _trash_dirs():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            p = d / name
            try:
                st = os.lstat(p)
            except OSError:
                continue
            items.append({
                "path": str(p.relative_to(root)),
                "from": str((d.parent / name).relative_to(root)),
                "folder": str(d.parent.relative_to(root)),
                "name": name,
                "dir": stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode),
                "at": int(st.st_ctime),
                "size": st.st_size,
            })
    items.sort(key=lambda x: x["at"], reverse=True)
    return web.json_response({"entries": items})


def _in_trash(raw: str) -> Path | web.Response:
    p = common.resolve_repo_path(str(raw or ""))
    if p is None:
        return web.json_response({"error": "bad path"}, status=400)
    rel = str(p.relative_to(common.REPO_ROOT.resolve()))
    parts = rel.split("/")
    if len(parts) < 2 or parts[-2] != common.TRASH_DIRNAME:
        return web.json_response({"error": "that is not something in a trash"}, status=400)
    if not os.path.lexists(p):
        return web.json_response({"error": "not found"}, status=404)
    return p


def _tidy_trash(trash: Path) -> None:
    """An empty `.trash` is noise: take it away again."""
    try:
        if not os.listdir(trash):
            os.rmdir(trash)
    except OSError:
        pass


async def fs_restore(request: web.Request) -> web.Response:
    """Up one level, which is where it came from."""
    data = await request.json()
    p = _in_trash(data.get("path"))
    if isinstance(p, web.Response):
        return p
    target = _free_name(p.parent.parent / p.name)
    try:
        os.rename(p, target)
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    _tidy_trash(p.parent)
    rel = str(target.relative_to(common.REPO_ROOT.resolve()))
    common.write_attrib_hint("restore", *_versioned_under(target, rel))
    return web.json_response({"ok": True, "path": rel, "renamed": target.name != p.name})


async def fs_trash_purge(request: web.Request) -> web.Response:
    """Out of the trash for good — one thing, or everything you can reach."""
    data = await request.json()
    if data.get("all"):
        for d in _trash_dirs():
            for name in (os.listdir(d) if d.is_dir() else []):
                p = d / name
                try:
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p)
                    else:
                        os.unlink(p)
                except OSError:
                    pass
            _tidy_trash(d)
        return web.json_response({"ok": True})
    p = _in_trash(data.get("path"))
    if isinstance(p, web.Response):
        return p
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            os.unlink(p)
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    _tidy_trash(p.parent)
    return web.json_response({"ok": True})


async def fs_delete(request: web.Request) -> web.Response:
    """Delete a file or folder AS the user — which normally means MOVING it
    into the `.trash/` of the folder that owns it, so it can come back. Deleted
    for good only when asked (`permanent`), when it is a secret (a copy of one
    lingering for a month is the opposite of what `_secrets/` is for), or when
    it is already in the trash. Top-level areas are refused outright."""
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
    if common.trashable(rel) and not data.get("permanent"):
        try:
            trash = p.parent / common.TRASH_DIRNAME
            if not trash.is_dir():
                mode = stat.S_IMODE(os.stat(p.parent).st_mode)
                os.mkdir(trash, mode)
                os.chmod(trash, mode)      # mkdir's mode is cut by the umask
            moved = _trash_name(trash, p.name)
            os.rename(p, moved)
        except PermissionError:
            return web.json_response(
                {"error": "permission denied (you need write access to the containing folder)"},
                status=403)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=400)
        common.write_attrib_hint("delete", *touched)
        return web.json_response({"ok": True, "deleted": rel, "was_dir": is_dir,
                                  "trashed": str(moved.relative_to(common.REPO_ROOT.resolve()))})
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


def _audience_of(p: Path) -> dict:
    """How wide an inode is open, in the panel's vocabulary."""
    try:
        st, entries = common.stat_and_acl(p)
    except OSError:
        return {"scope": "unknown", "group": "", "people": 0}
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    # Named ACL entries count. The share panel grants by named user and named
    # group, so reading mode bits alone called every shared document "private"
    # and put a "this changes who can open it" modal in front of drags that
    # changed nothing.
    world, team, named = common.read_audience(st, entries)
    # Service accounts do not count as an audience. Every file the share panel
    # touches carries a named grant for the indexer, private ones included,
    # because search has to read them.
    named = common.human_readers(named)
    scope = "everyone" if world else ("people" if (team or named) else "private")
    try:
        g = grp.getgrnam(group)
        people = len(set(g.gr_mem) | {e.pw_name for e in pwd.getpwall()
                                      if e.pw_gid == g.gr_gid}) - (1 if "kbindexer" in g.gr_mem else 0)
    except KeyError:
        people = 0
    return {"scope": scope, "group": group, "people": max(people, 0)}


async def fs_move_preview(request: web.Request) -> web.Response:
    """Would this move change who can open the thing? Asked BEFORE the move, so
    the app can put the question to the person dragging instead of quietly
    publishing a private note into a team folder (or quietly carrying a team's
    grant out of it)."""
    data = await request.json()
    src = common.resolve_repo_path(str(data.get("src", "")))
    dst = common.resolve_repo_path(str(data.get("dst", "")))
    if src is None or dst is None or not src.exists():
        return web.json_response({"error": "bad path"}, status=400)
    if not dst.parent.is_dir():
        return web.json_response({"error": "destination folder does not exist"}, status=400)
    cur, to = _audience_of(src), _audience_of(dst.parent)
    same_dir = os.path.dirname(str(src)) == os.path.dirname(str(dst))
    return web.json_response({
        "changes": (not same_dir) and (cur["scope"] != to["scope"] or cur["group"] != to["group"]),
        "same_dir": same_dir, "from": cur, "to": to,
        "mine": os.lstat(src).st_uid == os.geteuid(),
        "dst_folder": str(dst.parent.relative_to(common.REPO_ROOT)),
    })


async def fs_rename(request: web.Request) -> web.Response:
    """Move/rename AS the user — exactly what `mv` in their terminal could do
    (kernel needs write on both parent folders). The frontend retires and
    reopens any affected editor tabs itself.

    Unlike `mv`, the moved thing then takes the DESTINATION's audience by
    default (`audience: "keep"` opts out). A rename carries the inode over
    untouched — same owner, group, mode and ACLs — and the destination's setgid
    bit does not fire, because setgid only applies when the kernel creates a
    child. So a private note dragged into a team folder stayed unreadable by
    that team AND by the indexer (invisible in search, silently), while a file
    dragged between two projects kept the first project's group. Copy has
    re-homed for exactly this reason since it was written; move now matches."""
    data = await request.json()
    got = _fs_pair(data)
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
    rehomed = False
    if (data.get("audience") != "keep"
            and os.path.dirname(rel_src) != os.path.dirname(rel_dst)):
        # Only the owner may chmod/chgrp, so a move of someone else's file is
        # left exactly as it arrived rather than half-applied — the response
        # says so and the app surfaces it.
        try:
            if os.lstat(dst).st_uid == os.geteuid():
                await asyncio.to_thread(common.reset_audience_tree, dst)
                rehomed = True
        except OSError:
            pass
    # history sees a rename as delete-at-old + add-at-new — attribute both sides
    new_touched = [rel_dst + old[len(rel_src):] for old in old_touched]
    common.write_attrib_hint("rename", *old_touched, *new_touched)
    return web.json_response({"ok": True, "src": rel_src, "dst": rel_dst,
                              "rehomed": rehomed})


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
        # copy2/copystat would carry the SOURCE's mode and POSIX ACL to the
        # destination — a private file copied into a team folder would stay
        # unreadable to that team (and to the indexer), and a file carrying
        # `u:someone:r` would smuggle that grant into a folder with a different
        # audience. Content moves; the audience is the destination's.
        if stat.S_ISDIR(st.st_mode):
            await asyncio.to_thread(shutil.copytree, src, dst, symlinks=False,
                                    copy_function=shutil.copy,
                                    ignore=shutil.ignore_patterns(".git", "*.kbtmp"))
            await asyncio.to_thread(common.reset_audience_tree, dst)
        else:
            await asyncio.to_thread(shutil.copy, src, dst)
            await asyncio.to_thread(common.reset_audience_tree, dst)
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
        if not files_dir.exists():
            common.mkdir_with_mode(files_dir)
        target = files_dir / filename
        existed = target.exists()
        size = 0
        with open(target, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                f.write(chunk)
        # A NEW attachment must be as readable as the folder it landed in. An
        # existing one keeps its own permissions: overwriting a teammate's file
        # is allowed by the group bits, but chmod-ing it is not — doing so
        # unconditionally turned a completed upload into a 403.
        if not existed:
            try:
                os.chmod(target, common.birth_mode(files_dir, False, str(target)))
            except OSError:
                pass
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    link = f"_files/{filename}"
    return web.json_response({"ok": True, "link": link, "size": size,
                             "path": str(target.relative_to(common.REPO_ROOT))})


# ---- chunked uploads --------------------------------------------------------
# The editor's drops and pastes come through here, one chunk per request, so a
# 4 GB video is no different from a screenshot as far as any hop between the
# browser and this process is concerned. `upload` above is unchanged and still
# serves small single-shot posts (scripts, the artifact `kb-upload` bridge).
# Everything runs as the user, so the kernel is still the whole permission
# story — see uploads.py for the protocol.
_UPLOADS = uploads.Sessions()


def _upload_dest(rel_dir: str, into_files: bool) -> tuple[Path | None, web.Response | None]:
    """The folder the bytes land in: `<dir>/_files` for an attachment (created
    on demand, with the audience of the folder it sits in), `<dir>` itself when
    the caller asked for the folder directly."""
    d = common.resolve_repo_path(rel_dir)
    if d is None or not d.is_dir():
        return None, web.json_response({"error": "bad dir"}, status=400)
    if not into_files:
        return d, None
    files_dir = d / "_files"
    try:
        if not files_dir.exists():
            common.mkdir_with_mode(files_dir)
    except FileExistsError:
        pass
    except PermissionError:
        return None, web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return None, web.json_response({"error": str(e)}, status=400)
    return files_dir, None


def _upload_result(sess: uploads.Session) -> dict:
    """Same shape the single-shot `/api/upload` answers with, so the editor's
    insert path does not care which one ran. `link` is relative to the DOCUMENT
    (`_files/photo.jpg`), which is what goes into the markdown."""
    out = {"ok": True, "size": sess.size,
           "path": str(Path(sess.dir_rel) / sess.name)}
    if Path(sess.dir_rel).name == "_files":
        out["link"] = f"_files/{sess.name}"
    return out


async def upload_begin(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "bad request"}, status=400)
    into_files = data.get("files", True)
    d, err = _upload_dest(str(data.get("dir", "company")), bool(into_files))
    if err:
        return err
    name = uploads.safe_name(str(data.get("name", "")))
    if not name:
        return web.json_response({"error": "bad file name"}, status=400)
    try:
        size = int(data.get("size", -1))
    except (TypeError, ValueError):
        size = -1
    if size < 0:
        return web.json_response({"error": "bad size"}, status=400)
    msg = uploads.space_error(d, size)
    if msg:
        return web.json_response({"error": msg}, status=507)
    _UPLOADS.sweep()
    if _UPLOADS.full():
        return web.json_response({"error": "too many uploads in flight"}, status=503)
    uploads.sweep_orphans(d)
    spool = d / uploads.spool_name()
    try:
        # Born with the audience of the folder, because the last step is a
        # rename inside that same folder — the inode we open here is the file
        # the user ends up with.
        fd = common.open_with_mode(spool, exclusive=True)
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    sess = _UPLOADS.add(user=None, dir_rel=str(d.relative_to(common.REPO_ROOT.resolve())),
                        name=name, spool=spool, size=size, fd=fd)
    out = uploads.begin_payload(sess)
    out["files"] = bool(into_files)
    return web.json_response(out)


async def upload_chunk(request: web.Request) -> web.Response:
    sess = _UPLOADS.get(request.query.get("id", ""), None)
    if sess is None:
        return web.json_response({"error": "unknown upload"}, status=404)
    try:
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        return web.json_response({"error": "bad offset"}, status=400)
    return await uploads.receive(request, sess, offset)


async def upload_finish(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "bad request"}, status=400)
    sid = str(data.get("id", ""))
    sess = _UPLOADS.get(sid, None)
    if sess is None:
        return web.json_response({"error": "unknown upload"}, status=404)
    err = uploads.complete_error(sess)
    if err:
        return err
    target = common.REPO_ROOT / sess.dir_rel / sess.name
    try:
        # Rename, not copy: the bytes are already on the right filesystem, so
        # publishing a 4 GB file costs one atomic syscall and no second write.
        # Like `mv` in the user's own terminal, this replaces the destination
        # inode — so an overwritten attachment is reborn with this user's
        # ownership and the folder's audience, which is what the folder's write
        # permission already allowed them to do by hand.
        os.replace(sess.spool, target)
    except OSError as e:
        _UPLOADS.drop(sid)
        return web.json_response({"error": str(e)}, status=400)
    _UPLOADS.drop(sid, unlink=False)    # the spool IS the file now
    return web.json_response(_upload_result(sess))


async def upload_abort(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except ValueError:
        data = {}
    sid = str(data.get("id", ""))
    if _UPLOADS.get(sid, None) is None:
        return web.json_response({"error": "unknown upload"}, status=404)
    _UPLOADS.drop(sid)
    return web.json_response({"ok": True})


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


ATTACHMENT_CSP = (
    "default-src 'none'; img-src 'self' data: blob:; media-src 'self' blob:; "
    "style-src 'unsafe-inline'; object-src 'none'; script-src 'none'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)


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
    # This route serves ARBITRARY uploaded bytes inline, on the app's own
    # origin. Artifacts do not come through here — a .html is classified as an
    # artifact and goes to /api/artifact/raw, which sandboxes it. But an SVG is
    # XML that may carry <script>, uploads have no extension check, and the
    # browser will happily render one as a document: click it in the tree and
    # its script runs as you, with your session, against /fs/share and
    # /admin/*.
    #
    # The CSP costs nothing visible — images, PDFs and SVGs still preview
    # exactly as before, scripts simply do not run. nosniff stops a mislabelled
    # file being re-interpreted as HTML. frame-ancestors keeps the response out
    # of a foreign frame.
    return web.FileResponse(p, headers={
        "Content-Disposition": _content_disposition(p.name, dl),
        "Content-Security-Policy": ATTACHMENT_CSP,
        "X-Content-Type-Options": "nosniff",
    })


# A whole folder, as one file you can hand to someone. The ceiling is on the
# UNCOMPRESSED total and is deliberately conservative: the hub buffers every
# proxied /api/* response in memory, as root, on behalf of everyone — so an
# unbounded archive here is an unbounded allocation there. A folder over the
# limit is a clean 413 naming its size, not a truncated download.
FOLDER_ZIP_MAX_BYTES = 1024 * 1024 * 1024


def _zip_entries(root: Path):
    """What belongs in a folder download, in tree order: the same exclusions the
    sidebar applies (git internals, our own upload spools), no symlink ever
    followed, and anything the kernel refuses simply left out rather than
    failing the whole archive. Yields (path, arcname_suffix, is_dir)."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        keep = []
        for n in sorted(dirnames):
            sub = d / n
            if n == ".git" or n.endswith(".kbtmp") or sub.is_symlink():
                continue
            if not os.access(sub, os.R_OK | os.X_OK):
                continue
            keep.append(n)
            # Carried explicitly so an empty folder survives the round-trip.
            yield sub, str(sub.relative_to(root)), True
        dirnames[:] = keep
        for name in sorted(filenames):
            f = d / name
            if name.endswith(".kbtmp") or f.is_symlink():
                continue
            try:
                if not f.is_file() or not os.access(f, os.R_OK):
                    continue
            except OSError:
                continue
            yield f, str(f.relative_to(root)), False


def _build_folder_zip(root: Path, top: str, out) -> None:
    """Blocking: runs in an executor, never on the event loop."""
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for path, arc, is_dir in _zip_entries(root):
            name = top + "/" + arc
            try:
                if is_dir:
                    z.writestr(zipfile.ZipInfo(name + "/"), b"")
                else:
                    z.write(path, arcname=name)
            except OSError:
                continue    # vanished or locked between the walk and the read


async def folder_zip(request: web.Request) -> web.StreamResponse:
    rel = request.query.get("path", "")
    p = common.resolve_repo_path(rel)
    if p is None:
        return web.json_response({"error": "bad path"}, status=404)
    try:
        if not p.is_dir():
            return web.json_response({"error": "not a folder"}, status=404)
        if not os.access(p, os.R_OK | os.X_OK):
            return web.json_response({"error": "forbidden"}, status=403)
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError:
        return web.json_response({"error": "not a folder"}, status=404)

    loop = asyncio.get_running_loop()

    def measure() -> int:
        total = 0
        for path, _arc, is_dir in _zip_entries(p):
            if is_dir:
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            if total > FOLDER_ZIP_MAX_BYTES:
                break
        return total

    size = await loop.run_in_executor(None, measure)
    if size > FOLDER_ZIP_MAX_BYTES:
        gb = FOLDER_ZIP_MAX_BYTES / (1024 ** 3)
        return web.json_response(
            {"error": f"that folder holds more than {gb:.0f} GB — "
                      "download a subfolder, or copy it from a terminal"},
            status=413)

    # A probe lets the UI find out whether this download is even possible
    # BEFORE it navigates: a browser pointed at a failing download navigates
    # away to show the error JSON, which would throw the app away.
    if request.query.get("probe") == "1":
        return web.json_response({"ok": True, "bytes": size})

    # The name the archive unpacks INTO. The repo root has no name of its own,
    # and a folder called ".." or "." can't exist, so this is always a real
    # single path segment.
    top = p.name or "knowledgebase"
    tmp = tempfile.TemporaryFile()    # unnamed: it is gone the moment we close it
    try:
        await loop.run_in_executor(None, _build_folder_zip, p, top, tmp)
        total = tmp.tell()
        tmp.seek(0)
        resp = web.StreamResponse(headers={
            "Content-Disposition": _content_disposition(top + ".zip", True),
            "X-Content-Type-Options": "nosniff",
        })
        resp.content_type = "application/zip"
        resp.content_length = total
        await resp.prepare(request)
        while True:
            chunk = await loop.run_in_executor(None, tmp.read, 256 * 1024)
            if not chunk:
                break
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        tmp.close()


async def tasks(request: web.Request) -> web.Response:
    conn = await _adb()
    if conn is None:
        return web.json_response({"tasks": [], "db": False})
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT file_path, line, checked, text FROM kb.blocks "
                "WHERE kind='task' ORDER BY file_path, line")
            rows = [{"path": r[0], "line": r[1], "checked": r[2], "text": r[3]}
                    for r in await cur.fetchall()]
        return web.json_response({"tasks": rows, "db": True})
    finally:
        await conn.close()


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
    # index-excluded trees (.claude/, .os/) and non-task lines even if FS-writable.
    conn = await _adb()
    if conn is not None:
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1 FROM kb.blocks WHERE file_path=%s AND line=%s AND kind='task'",
                                  (rel, line_no))
                if await cur.fetchone() is None:
                    return web.json_response({"error": "not an indexed task"}, status=400)
        finally:
            await conn.close()
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
    """Do the query's characters appear in order in `hay`, each one either
    consecutive to the previous match or starting a word? Runs and word starts
    score higher; letters plucked from the middles of unrelated words ("tomas"
    out of "terraform.tfvars") are no match at all. Mirrors subseqMatch in
    frontend/src/app.js — keep the two in step."""
    if not q:
        return 0
    i = score = run = 0
    for pos, ch in enumerate(hay):
        if ch != q[i]:
            run = 0
            continue
        boundary = pos == 0 or hay[pos - 1] in " -_./"
        if run == 0 and not boundary:   # mid-word starts don't count
            continue
        run += 1
        score += 2 + run
        if boundary:
            score += 4
        i += 1
        if i == len(q):
            return max(score - pos // 8, 1)
    return 0


async def search(request: web.Request) -> web.Response:
    # NUL bytes are legal in a query string but not in a Postgres text value —
    # psycopg raises, and an unhandled raise here is a 500 for a typo.
    q = request.query.get("q", "").replace("\x00", "").strip()[:200]
    if not q:
        conn = await _adb()
        ok = conn is not None
        if conn:
            await conn.close()
        return web.json_response({"results": [], "files": [], "db": ok})

    # The name-matching walk is os.scandir over the whole repo — real disk I/O
    # with no async API, so it runs in a worker thread; the loop stays free to
    # serve the tree and open documents while a search is in flight.
    def _name_matches():
        try:
            scored = []
            for rel, is_dir in _walk_repo():
                s = _name_score(q, rel)
                if s:
                    scored.append((s, rel, is_dir))
            scored.sort(key=lambda t: (-t[0], len(t[1]), t[1]))
            return [{"path": rel, "dir": is_dir, "score": s} for s, rel, is_dir in scored[:40]]
        except OSError:
            return []

    # Start the walk NOW and let it run in its thread while the content query
    # is in flight: the two halves of a search are independent, and awaiting
    # the walk first simply added its ~120 ms to the query's ~130 ms for no
    # reason. Whoever finishes last decides the latency.
    files_task = asyncio.create_task(asyncio.to_thread(_name_matches))

    conn = None
    try:
        conn = await _adb()
        if conn is None:
            return web.json_response({"results": [], "files": await files_task, "db": False})
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
        toks = [t for t in re.split(r"[^0-9A-Za-z]+", q.lower()) if len(t) >= 2][:6]
        pq = " & ".join(t + ":*" for t in toks) if toks else None
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        try:
          async with conn.cursor() as cur:
            await cur.execute(
                "WITH q AS (SELECT websearch_to_tsquery('pg_catalog.english', %s) AS ws, "
                "                  CASE WHEN %s::text IS NULL THEN NULL::tsquery "
                "                       ELSE to_tsquery('pg_catalog.english', %s) END AS pq) "
                "SELECT b.file_path, b.line, b.kind, b.text, "
                "       ts_rank(b.tsv, q.ws) AS rank, "
                "       (b.tsv @@ q.pq) AS pref "
                "FROM kb.blocks b, q "
                "WHERE b.tsv @@ q.ws OR b.tsv @@ q.pq OR b.text ILIKE %s "
                # file_path/line break the remaining ties. Without them the
                # order among equal-rank, equal-length rows is whatever the
                # scan emitted — and now that kb.blocks is narrow enough for
                # the planner to go PARALLEL, that varies run to run: the same
                # query reshuffled its results between keystrokes.
                "ORDER BY rank DESC, length(b.text), b.file_path, b.line LIMIT 90",
                (q, pq, pq, like))
            for r in await cur.fetchall():
                take(r, float(r[4]) + (0.02 if r[5] else 0.0))
        except Exception:
            # A malformed query must never 500: the filename half already has an
            # answer, and half a result list beats an error page. The rollback
            # itself is guarded too — if what failed was the CONNECTION (DB
            # restarted mid-query), rollback() re-raises and would be the 500.
            try:
                await conn.rollback()
            except Exception:
                pass
            rows = []
        rows.sort(key=lambda r: -r["rank"])
        return web.json_response({"results": rows[:30], "files": await files_task, "db": True})
    finally:
        if conn is not None:
            await conn.close()
        # An error path that never awaited the walk would otherwise leave a
        # pending task behind ("Task was destroyed but it is pending").
        if not files_task.done():
            files_task.cancel()


_SQL_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
# Privilege/ownership/role changes, and the loopholes that reach them indirectly
# (DO blocks and EXECUTE run dynamic SQL; COPY ... TO PROGRAM/FROM touches the
# filesystem as the server). No artifact on this box uses any of them — verified
# across all 25 artifact documents before this was added.
_PRIVILEGE_SQL = re.compile(
    r"\b(?:GRANT|REVOKE"
    r"|CREATE\s+(?:ROLE|USER|GROUP)|ALTER\s+(?:ROLE|USER|GROUP)|DROP\s+(?:ROLE|USER)"
    r"|OWNER\s+TO|SECURITY\s+DEFINER"
    r"|COPY\b[^;]*\b(?:TO|FROM)\b"
    r"|\bDO\b\s*\$|EXECUTE\s+FORMAT|SET\s+ROLE|SET\s+SESSION\s+AUTHORIZATION)\b",
    re.I)


def _strip_sql_comments(sql: str) -> str:
    """Comments are the cheapest way to hide a keyword from a lexical check."""
    return _SQL_COMMENT_RE.sub(" ", sql)


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
    # An artifact is authored by one person and OPENED BY ANOTHER, and this runs
    # the author's SQL with the VIEWER's Postgres authority. RLS keeps the viewer
    # from reading what they could not read anyway — but nothing stopped the
    # author's SQL from copying the viewer's own visible rows somewhere the
    # AUTHOR can reach, e.g.
    #     CREATE TABLE u_<viewer>.x AS SELECT text FROM kb.blocks;
    #     GRANT SELECT ON u_<viewer>.x TO <author>;
    # which turns "carol opened bob's dashboard" into bob reading carol's
    # private indexed documents.
    #
    # The handing-over step is a privilege change, so that is what is refused.
    # Plain INSERT/UPDATE/DELETE/CREATE TABLE stay allowed on purpose: six live
    # artifacts here (obed, pixel, piskvorky, wordle, planning-poker,
    # i-love-claude) write on every use, and a blanket read-only would break them.
    #
    # HONEST LIMIT: this is a lexical check on the statement text. It stops the
    # exfil chain above and every accidental variant, but it is not a parser —
    # sufficiently creative SQL could evade it. The structural fix (a consent gate
    # before opening someone else's artifact) is a UX change, not a patch here.
    if _PRIVILEGE_SQL.search(_strip_sql_comments(sql)):
        return web.json_response(
            {"error": "an artifact may not change privileges, roles or ownership"},
            status=403)
    conn = await _adb()
    if conn is None:
        return web.json_response({"error": "db offline"}, status=503)
    try:
        async with conn.cursor() as cur:
            # A runaway artifact query used to be able to sit on this user's only
            # connection indefinitely.
            await cur.execute("SET statement_timeout = '30s'")
            await cur.execute(sql, params)
            if cur.description:
                cols = [d.name for d in cur.description]
                rows = [list(r) for r in await cur.fetchall()]
                out = {"cols": cols, "rows": rows}
            else:
                out = {"cols": [], "rows": [], "rowcount": cur.rowcount}
        return web.json_response(out, dumps=lambda o: json.dumps(o, default=str))
    except Exception as e:
        # Permission denials, syntax errors, etc. all surface as a clean 400.
        return web.json_response({"error": str(e).splitlines()[0]}, status=400)
    finally:
        await conn.close()


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
        existed = p.exists()
        if existed:
            p.write_text(content)           # overwrite in place: keep its perms
        else:
            common.create_with_mode(p, content.encode())
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    common.write_attrib_hint("edit", str(p.relative_to(common.REPO_ROOT)))
    return web.json_response({"ok": True, "path": str(p.relative_to(common.REPO_ROOT)),
                             "bytes": len(content.encode())})


# ---- artifact folder tools (list / mkdir / delete), all AS the viewer -------
# An artifact can already read and write files in its own folder; these three
# give it the rest of a file manager — see what is there, make a subfolder,
# remove something — with the same authority and the same containment.
#
# One entry cap and one depth cap for kb-list: enough for a real folder tree,
# small enough that a runaway recursion can't build a multi-megabyte response.
ARTIFACT_LIST_MAX = 2000
ARTIFACT_LIST_MAX_DEPTH = 10


def _artifact_scope(data: dict):
    """Resolve (target, artifact folder, artifact file) for a scoped artifact FS
    action, refusing any target outside the artifact's own directory tree.

    The host page checks this too, before it ever calls us. It is repeated here
    because these verbs CREATE and DELETE: the containment that matters most is
    the one destructive actions sit behind, so it must not depend on a single
    caller remembering to check. Returns a web.Response on refusal."""
    art = common.resolve_repo_path(str(data.get("artifact", "")))
    if art is None or not str(art).endswith(".html"):
        return web.json_response({"error": "bad artifact path"}, status=400)
    root = common.REPO_ROOT.resolve()
    base = art.parent
    if base == root:
        return web.json_response(
            {"error": "an artifact at the repo root has no folder to work in"}, status=403)
    p = common.resolve_repo_path(str(data.get("path", "")))
    if p is None:
        return web.json_response({"error": "bad path"}, status=400)
    if p != base and base not in p.parents:
        return web.json_response({"error": "path outside this artifact's folder"}, status=403)
    return p, base, art


async def artifact_list(request: web.Request) -> web.Response:
    """List a folder at or under the artifact's own, AS THIS USER. Directories
    the viewer cannot open are skipped exactly as they are in the tree — an
    artifact sees the folder the way its viewer's terminal would.

    Dot-files are included: an artifact's own state (`.pipeline-data.json`) is
    dot-prefixed by convention, so hiding them would hide the very files an
    artifact keeps."""
    data = await request.json()
    scoped = _artifact_scope(data)
    if isinstance(scoped, web.Response):
        return scoped
    p, _base, _art = scoped
    try:
        depth = int(data.get("depth", 1))
    except (TypeError, ValueError):
        return web.json_response({"error": "depth must be a number"}, status=400)
    depth = max(1, min(depth, ARTIFACT_LIST_MAX_DEPTH))
    root = common.REPO_ROOT.resolve()
    if common.is_secret_path(str(p.relative_to(root))):
        return web.json_response({"error": "secrets are not listable"}, status=403)
    entries: list[dict] = []
    truncated = False

    def walk(d: Path, level: int) -> None:
        nonlocal truncated
        try:
            rows = sorted(os.scandir(d), key=_entry_key)
        except OSError:
            return
        for e in rows:
            # `_secrets/` is skipped whole: the platform keeps secrets out of
            # everywhere content can escape a permission check, and an artifact
            # never needs to enumerate them — kb-fetch injects `secret:` refs
            # server-side precisely so the artifact never handles a credential.
            if e.name in (".git", "_secrets") or e.name.endswith(".kbtmp"):
                continue
            if len(entries) >= ARTIFACT_LIST_MAX:
                truncated = True
                return
            q = Path(e.path)
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            row = {"name": e.name, "path": str(q.relative_to(root)), "dir": is_dir,
                   "mtime": int(st.st_mtime),
                   "read": os.access(q, os.R_OK), "write": os.access(q, os.W_OK)}
            if not is_dir:
                row["size"] = st.st_size
            entries.append(row)
            if is_dir and level < depth and os.access(q, os.R_OK | os.X_OK):
                walk(q, level + 1)

    try:
        if not p.is_dir():
            return web.json_response({"error": "not a folder"}, status=400)
        os.scandir(p).close()          # surface "you can't open this" as a 403
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=404)
    walk(p, 1)
    return web.json_response({"ok": True, "path": str(p.relative_to(root)),
                              "entries": entries, "truncated": truncated})


async def artifact_mkdir(request: web.Request) -> web.Response:
    """Create a subfolder under the artifact's own folder, AS THIS USER. Missing
    intermediate folders are created too — one level at a time, so each inherits
    the audience of the folder it lands in (a single os.makedirs would give the
    intermediate ones the process umask instead)."""
    data = await request.json()
    scoped = _artifact_scope(data)
    if isinstance(scoped, web.Response):
        return scoped
    p, base, _art = scoped
    root = common.REPO_ROOT.resolve()
    try:
        if os.path.lexists(p):
            return web.json_response({"error": "already exists"}, status=409)
        missing = []
        q = p
        while q != base and not os.path.lexists(q):
            missing.append(q)
            q = q.parent
        for d in reversed(missing):
            common.mkdir_with_mode(d)
    except PermissionError:
        return web.json_response({"error": "no write access to the parent folder"}, status=403)
    except FileExistsError:
        return web.json_response({"error": "already exists"}, status=409)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "path": str(p.relative_to(root))})


async def artifact_delete(request: web.Request) -> web.Response:
    """Delete a file or folder under the artifact's own folder, AS THIS USER —
    exactly what `rm` in the viewer's terminal could do, no more.

    Two things are refused whatever the permissions say: the artifact's own
    folder (its whole world, and deleting it would take neighbouring artifacts
    with it) and the artifact's own file, so an open page cannot delete itself
    out from under the person reading it. A non-empty folder needs an explicit
    `recursive`, so one wrong path can't quietly take a subtree with it."""
    data = await request.json()
    scoped = _artifact_scope(data)
    if isinstance(scoped, web.Response):
        return scoped
    p, base, art = scoped
    root = common.REPO_ROOT.resolve()
    if p == base:
        return web.json_response({"error": "an artifact cannot delete its own folder"}, status=400)
    if p == art:
        return web.json_response({"error": "an artifact cannot delete itself"}, status=400)
    rel = str(p.relative_to(root))
    try:
        st = os.lstat(p)   # can't even see it? a blocked ancestor is a 403, not a 404
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)
    except FileNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    is_dir = stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode)
    if is_dir and not data.get("recursive"):
        try:
            empty = not any(os.scandir(p))
        except OSError:
            empty = False
        if not empty:
            return web.json_response(
                {"error": "folder is not empty — pass recursive: true to delete it"}, status=400)
    touched = _versioned_under(p, rel)   # collect BEFORE they're gone
    try:
        if is_dir:
            shutil.rmtree(p)
        else:
            os.unlink(p)
    except PermissionError:
        return web.json_response({"error": "permission denied"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    common.write_attrib_hint("delete", *touched)
    return web.json_response({"ok": True, "deleted": rel, "was_dir": is_dir})


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
            "jobs": jobs, "raw": raw, "v": BACKEND_V}


# --- launcher buttons (company list is admin-written via the hub; the
# --- personal list lives in the user's own .os/, written AS them) -----------
COMPANY_LAUNCHERS = common.company_config("launchers.json")
MY_LAUNCHERS = common.user_config(ME, "launchers.json")


def _my_launchers() -> Path:
    """Lazy, as me: the first read or write after the upgrade moves an older
    install's users/<me>/.launchers.json into users/<me>/.os/."""
    try:
        common.migrate_user_config(ME, "launchers.json", ".launchers.json")
    except OSError:
        pass            # the read side degrades to []; the write side reports
    return MY_LAUNCHERS


def _read_launchers(path) -> list:
    raw = common.load_config_json(path)
    if raw is None:
        return []
    buttons, err = common.validate_launchers(raw)
    return buttons if not err else []


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
                              "mine": _read_launchers(_my_launchers())})


async def launchers_set(request: web.Request) -> web.Response:
    buttons, err = common.validate_launchers(await request.json())
    if err:
        return web.json_response({"error": err}, status=400)
    try:
        _my_launchers()      # never leave a stale legacy copy behind a new write
        common.write_user_config(ME, "launchers.json",
                                 (json.dumps({"buttons": buttons}, indent=2) + "\n").encode())
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "mine": buttons})


# --- settings: shipped default -> company (.os/settings.json, read) -> mine
# --- (users/<me>/.os/settings.json, written AS me). See kb_platform/settings.py.
async def settings_get(request: web.Request) -> web.Response:
    return web.json_response(kbsettings.snapshot(ME))


async def settings_set(request: web.Request) -> web.Response:
    """{"set": {key: value}, "unset": [key]} against MY layer. Validated
    against the registry; the whole file is rewritten atomically, which also
    repairs a hand-edit that went wrong (the loader keeps the valid keys)."""
    try:
        data = await request.json()
    except ValueError:
        return web.json_response({"error": "expected JSON"}, status=400)
    change, err = kbsettings.validate_change(data, "user")
    if err:
        return web.json_response({"error": err}, status=400)
    current = kbsettings.load_layer(kbsettings.user_file(ME), "user")["values"]
    try:
        common.write_user_config(ME, kbsettings.FILE_NAME,
                                 kbsettings.dumps(kbsettings.apply_change(current, change)))
    except PermissionError:
        return web.json_response({"error": "forbidden"}, status=403)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response(kbsettings.snapshot(ME))


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
                # Start at the knowledgebase root (everyone can access it)
                # rather than the user's home, so the terminal lands with
                # company/, projects/ and users/ all one step away.
                try:
                    os.chdir(common.REPO_ROOT)
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
    app = web.Application(client_max_size=2 * 1024 * 1024 * 1024,
                          middlewares=[_tree_dirty_on_write])
    app.router.add_get("/api/whoami", whoami)
    app.router.add_get("/api/tree", tree)
    app.router.add_get("/api/file", read_file)
    app.router.add_post("/api/file", create_file)
    app.router.add_post("/api/fs/mkdir", fs_mkdir)
    app.router.add_post("/api/fs/delete", fs_delete)
    app.router.add_get("/api/inbox", inbox_get)
    app.router.add_post("/api/inbox/read", inbox_read)
    app.router.add_get("/api/fs/trash", fs_trash)
    app.router.add_post("/api/fs/restore", fs_restore)
    app.router.add_post("/api/fs/trash-purge", fs_trash_purge)
    app.router.add_post("/api/fs/rename", fs_rename)
    app.router.add_post("/api/fs/copy", fs_copy)
    app.router.add_post("/api/upload", upload)
    app.router.add_post("/api/upload/begin", upload_begin)
    app.router.add_post("/api/upload/chunk", upload_chunk)
    app.router.add_post("/api/upload/finish", upload_finish)
    app.router.add_post("/api/upload/abort", upload_abort)
    app.router.add_get("/api/attachment", attachment)
    app.router.add_get("/api/folder-zip", folder_zip)
    app.router.add_get("/api/tasks", tasks)
    app.router.add_post("/api/tasks/toggle", toggle_task)
    app.router.add_get("/api/search", search)
    app.router.add_post("/api/artifact/query", artifact_query)
    app.router.add_get("/api/artifact/raw", artifact_raw)
    app.router.add_post("/api/artifact/read", artifact_read)
    app.router.add_post("/api/artifact/write", artifact_write)
    app.router.add_post("/api/artifact/list", artifact_list)
    app.router.add_post("/api/artifact/mkdir", artifact_mkdir)
    app.router.add_post("/api/artifact/delete", artifact_delete)
    app.router.add_post("/api/fs/move-preview", fs_move_preview)
    app.router.add_get("/api/principals", principals)
    app.router.add_get("/api/launchers", launchers_get)
    app.router.add_post("/api/launchers", launchers_set)
    app.router.add_get("/api/settings", settings_get)
    app.router.add_get("/api/events", events)
    app.router.add_post("/api/settings", settings_set)
    app.router.add_get("/api/cron", cron_list)
    app.router.add_post("/api/cron/add", _cron_guard(cron_add))
    app.router.add_post("/api/cron/remove", _cron_guard(cron_remove))
    app.router.add_post("/api/cron/toggle", _cron_guard(cron_toggle))
    app.router.add_get("/pty", pty_handler)
    kbacp.add_routes(app)   # /acp and /api/acp/* — agent chat
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
