"""Resumable chunked uploads — one protocol, both tiers.

A single POST cannot carry a large file across a real network. The edge sees to
that long before the platform does: the Cloudflare tunnel in front of this box
refuses a request body over 100 MB, which is precisely how a 300 MB video used
to come back as "larger than the server's upload limit" — a limit nothing in
this repo had set and nothing in this repo could raise. aiohttp's own
`client_max_size` is the second ceiling, and the hub buffering a proxied body in
RAM is the third.

So the browser slices the file and sends it a chunk at a time. Every request is
small enough to clear every hop, each chunk is streamed to a spool file next to
its destination and never held whole in memory, and the last call renames the
spool into place. The only ceiling left is free disk — which is the one the user
can actually see and act on, so `begin` checks it up front and says so.

Chunks carry an explicit byte offset and are written with `pwrite`, so a re-sent
chunk is idempotent: a dropped connection costs one chunk, not the upload.

Both tiers use this module. They differ only in how the spool file is born and
how it is renamed into place — the hub (`/fs/upload/*`) creates it as root with
the destination folder's owner/mode/ACL, the per-user backend (`/api/upload/*`)
creates it as the user and lets the kernel decide. Session bookkeeping, the
streaming append, and the expiry sweep are here.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

# What we ask the browser to send per request. Small enough to clear every hop's
# body limit with room to spare (Cloudflare: 100 MB), big enough that a 2 GB
# file is 256 round trips rather than thousands.
CHUNK_SIZE = 8 * 1024 * 1024
# A body larger than this is refused outright — a chunk is the only thing that
# could still pin a meaningful amount of memory in a proxy hop.
MAX_CHUNK = 32 * 1024 * 1024
READ_SIZE = 256 * 1024
# An abandoned session (closed laptop, killed tab) holds a spool file and an
# open fd; both go at the sweep.
SESSION_TTL = 6 * 3600
MAX_SESSIONS = 256
# Spool files are born `.kbtmp`: the tree, the indexer, kb-convert and syncd all
# already skip that suffix, so a half-arrived file is invisible everywhere
# instead of being listed, indexed, converted and synced mid-flight.
SPOOL_PREFIX = ".kbup-"
SPOOL_SUFFIX = ".kbtmp"
# Spools from a session that died with its process (restart, OOM) are cleaned up
# lazily, by the next upload into the same folder.
ORPHAN_AGE = 24 * 3600
# An upload must never be the thing that fills the disk out from under Postgres.
DISK_HEADROOM = 512 * 1024 * 1024


@dataclass
class Session:
    """One upload in flight. `fd` stays open for the session's whole life: that
    is what makes the appends immune to anything happening to the spool's *name*
    in a folder the uploader can also write to."""
    id: str
    user: str | None
    dir_rel: str
    name: str
    spool: Path
    size: int
    fd: int
    received: int = 0
    touched: float = field(default_factory=time.time)

    @property
    def spool_name(self) -> str:
        return self.spool.name


class Sessions:
    """In-process registry. Both tiers are single processes for their scope (one
    hub; one backend per user), so there is nothing to share and nothing to
    persist — a session that does not survive a restart is one the client
    restarts, which the chunk protocol already handles."""

    def __init__(self, limit: int = MAX_SESSIONS, ttl: int = SESSION_TTL):
        self.limit = limit
        self.ttl = ttl
        self._m: dict[str, Session] = {}

    def __len__(self) -> int:
        return len(self._m)

    def add(self, *, user: str | None, dir_rel: str, name: str,
            spool: Path, size: int, fd: int) -> Session:
        sid = secrets.token_urlsafe(18)
        s = Session(id=sid, user=user, dir_rel=dir_rel, name=name,
                    spool=spool, size=size, fd=fd)
        self._m[sid] = s
        return s

    def get(self, sid: str, user: str | None) -> Session | None:
        s = self._m.get(sid or "")
        # The id is unguessable, but ownership is still checked: on the hub one
        # dict serves the whole company, and appending to someone else's session
        # would write your bytes into their file.
        if s is None or s.user != user:
            return None
        return s

    def drop(self, sid: str, *, unlink: bool = True) -> None:
        s = self._m.pop(sid, None)
        if s is None:
            return
        _close(s, unlink=unlink)

    def sweep(self) -> None:
        cutoff = time.time() - self.ttl
        for sid, s in [(k, v) for k, v in self._m.items() if v.touched < cutoff]:
            self._m.pop(sid, None)
            _close(s, unlink=True)

    def full(self) -> bool:
        return len(self._m) >= self.limit


def _close(s: Session, *, unlink: bool) -> None:
    try:
        os.close(s.fd)
    except OSError:
        pass
    if unlink:
        try:
            os.unlink(s.spool)      # unlink never follows a symlink
        except OSError:
            pass


def spool_name() -> str:
    return f"{SPOOL_PREFIX}{secrets.token_hex(8)}{SPOOL_SUFFIX}"


def safe_name(raw: str) -> str | None:
    """The filename, which must already BE a single path component.

    The single-shot uploads take a basename of whatever arrives; here the name
    is refused outright instead. A browser never sends a separator in
    `File.name` (a folder upload puts the subfolder in `dir=`), so anything
    with one is a client that is confused or lying — and "refused" is a better
    answer to either than "silently written somewhere else"."""
    name = (raw or "").strip()
    if not name or name in (".", "..") or "\0" in name:
        return None
    if "/" in name or "\\" in name:
        return None
    return name


def space_error(dest: Path, size: int) -> str | None:
    """Refuse up front rather than 2 GB in — the one upload failure the user can
    do something about deserves to be said before the wait, not after it."""
    try:
        free = os.statvfs(dest)
        avail = free.f_bavail * free.f_frsize
    except OSError:
        return None
    if size + DISK_HEADROOM > avail:
        return (f"not enough free space on the server: {_gb(size)} needed, "
                f"{_gb(max(0, avail - DISK_HEADROOM))} free")
    return None


def _gb(n: int) -> str:
    for unit, step in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{n} bytes"


def sweep_orphans(d: Path) -> None:
    """Drop spool files whose session died with its process. Cheap enough to run
    on every `begin` (one scandir of one folder) and it keeps the cleanup where
    the evidence is, with no extra timer to own."""
    cutoff = time.time() - ORPHAN_AGE
    try:
        with os.scandir(d) as it:
            for e in it:
                if not (e.name.startswith(SPOOL_PREFIX) and e.name.endswith(SPOOL_SUFFIX)):
                    continue
                try:
                    if e.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(e.path)
                except OSError:
                    pass
    except OSError:
        pass


def begin_payload(sess: Session) -> dict:
    return {"ok": True, "id": sess.id, "offset": sess.received,
            "chunk_size": CHUNK_SIZE, "max_chunk": MAX_CHUNK}


async def receive(request: web.Request, sess: Session, offset: int) -> web.Response:
    """Stream one chunk into the spool at `offset`.

    Read off the wire in 256 KB blocks straight into `pwrite` — nothing here
    ever holds the chunk, let alone the file. Writing at an explicit offset is
    what makes a retry safe: the client may re-send any chunk it is unsure
    about, and the bytes land exactly where they landed the first time.
    """
    if offset < 0 or offset > sess.received:
        # A gap would leave a hole of zeros in the middle of the file. Tell the
        # client where we actually are and let it resume from there.
        return web.json_response({"error": "out of order", "offset": sess.received},
                                 status=409)
    pos = offset
    got = 0
    try:
        async for block in request.content.iter_chunked(READ_SIZE):
            got += len(block)
            if got > MAX_CHUNK:
                return web.json_response(
                    {"error": "chunk too large", "chunk_size": CHUNK_SIZE},
                    status=413)
            if pos + len(block) > sess.size:
                return web.json_response({"error": "more data than declared",
                                          "offset": sess.received}, status=400)
            n = 0
            while n < len(block):
                n += os.pwrite(sess.fd, block[n:], pos + n)
            pos += len(block)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    sess.received = max(sess.received, pos)
    sess.touched = time.time()
    return web.json_response({"ok": True, "offset": sess.received})


def complete_error(sess: Session) -> web.Response | None:
    """Guard the rename: a file that is short is not the file the user picked."""
    if sess.received != sess.size:
        return web.json_response(
            {"error": "upload incomplete", "offset": sess.received}, status=409)
    try:
        os.fsync(sess.fd)           # the rename must not outrun the bytes
    except OSError:
        pass
    return None


def same_inode(fd: int, name: str, dir_fd: int) -> bool:
    """Is the spool still the file we have been writing to? The uploader can
    write in this folder, so the *name* is theirs to play with; the fd is not.
    Checked once, immediately before the rename that publishes it."""
    try:
        a = os.fstat(fd)
        b = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)
