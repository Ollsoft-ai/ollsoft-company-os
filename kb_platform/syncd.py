"""kb-syncd — the one privileged sync component.

Two jobs, one event loop:
  1. A y-websocket relay (via pycrdt.websocket) so browsers co-edit a document
     Google-Docs style. The browser's `y-websocket` client speaks the same
     protocol, so this interops directly.
  2. A filesystem daemon that makes the .md file on disk a first-class CRDT peer:
     outbound, doc changes are debounced and written to disk (preserving the
     file's original owner/group/mode); inbound, external edits (vim, agents,
     git, scripts) are diffed and merged into the live doc as ops.

Runs as root so it can read/write every doc and restore ownership after writes.
It is reachable ONLY by the hub over a 0600 unix socket, and every connection
carries a hub-signed identity token; syncd re-checks Unix *write* permission for
that user before admitting them to a room. Fail closed.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import pwd
import re
import socket
import stat
import struct
import subprocess
import threading
import time
from contextlib import AsyncExitStack
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="syncd %(message)s")
log = logging.getLogger("kb.syncd")

from aiohttp import WSMsgType, web
from pycrdt import Text, write_var_uint
from pycrdt.websocket import WebsocketServer
from watchfiles import awatch

from . import common

FLUSH_DEBOUNCE = 0.25   # seconds of quiet before writing a doc to disk
_ACL_ACCESS = "system.posix_acl_access"   # a file's audience, in one xattr
# Persisted Y.Doc states (one binary update per room). WHY THIS EXISTS: if the
# daemon restarts while browsers hold a doc open, a fresh empty room would be
# re-seeded from disk with NEW operation ids; the reconnecting clients then
# merge their OLD history on top and the CRDT keeps both — content doubles and
# interleaves mid-word on every restart. Resuming the saved state keeps the
# operation ids stable, so reconnects converge instead of duplicating.
DOC_STATE_DIR = Path("/var/lib/kb-syncd")
GIT_DEBOUNCE = 4.0      # seconds of quiet before an auto-commit


def fs_can(path: Path, uid: int, gids: list[int], need_write: bool) -> bool:
    """Replicate FULL Unix access for a specific user, since root (this daemon)
    bypasses os.access. One adapter over common.can.

    This file used to carry its own inode check plus the ancestor-traverse
    walk, and the hub carried the inode half WITHOUT the walk — so the two
    root daemons disagreed about who could read what. The walk now lives in
    common.can and both share it.
    """
    return common.can(path, uid, gids, 2 if need_write else 4)


def auth_denied(reason: str) -> bytes:
    """A y-protocol AUTH / permission-denied frame: message type 2, subtype 0,
    then a varstring. Revocation rides the document socket's own protocol —
    the browser's y-websocket already decodes type 2 — so no side channel is
    needed and no non-Yjs frame ever reaches a Y.Doc decoder."""
    body = reason.encode()
    return (write_var_uint(2) + write_var_uint(0)
            + write_var_uint(len(body)) + body)


class Session:
    """One open editing socket. The GROUP list is deliberately NOT kept: it is
    re-read from NSS on every access re-check, because the commonest un-share
    is "removed from the group" and the token's gids froze at connect."""
    __slots__ = ("user", "uid", "channel")

    def __init__(self, user: str, uid: int, channel: "AiohttpChannel"):
        self.user, self.uid, self.channel = user, uid, channel


class AiohttpChannel:
    """Adapts an aiohttp WebSocketResponse to pycrdt.websocket's Channel protocol.

    `on_edit` (optional) fires whenever this channel delivers a document
    MUTATION message (SYNC_STEP2/SYNC_UPDATE) — that is how the daemon knows
    which verified user actually typed, for version-history attribution.

    `can_write` and `readable` are LIVE, not connect-time facts: an un-share
    has to reach a socket that is already open. Both cuts live here rather than
    in the room loop because this is the one object every byte passes through
    in both directions, whichever of pycrdt's fan-out paths is carrying it."""

    def __init__(self, ws: web.WebSocketResponse, path: str, on_edit=None,
                 can_write: bool = True):
        self._ws = ws
        self._path = path
        self._on_edit = on_edit
        self.can_write = can_write
        self.readable = True
        self._notice = None

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        # A loop, not recursion: a writer who has just lost write can have
        # hundreds of dropped updates in flight before their tab reacts.
        while True:
            msg = await self._ws.receive()
            if msg.type == WSMsgType.BINARY:
                data = msg.data
                # YMessageType.SYNC = 0; SYNC_STEP2 = 1 / SYNC_UPDATE = 2 carry edits
                if len(data) > 1 and data[0] == 0 and data[1] in (1, 2):
                    # Re-read PER MESSAGE, never trusted from connect time:
                    # this daemon is root, so an edit it forwards is an edit it
                    # persists — under the file's owner — long after the sharer
                    # took write away.
                    if not self.can_write:
                        continue
                    if self._on_edit:
                        self._on_edit()
                return data
            if msg.type == WSMsgType.TEXT:
                return msg.data.encode()
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                raise StopAsyncIteration

    def revoke_write(self) -> None:
        """Keep the socket, drop write: the tab goes read-only with its unsaved
        buffer intact. The flag IS the enforcement; the notice only lets the UI
        say why."""
        self.can_write = False
        self._tell("kb:write-revoked", close=False)

    def revoke_read(self) -> None:
        """Stop sending, NOW. `readable` is cleared synchronously, before any
        await, so every send that has NOT already begun is refused. pycrdt fans
        out with task_group.start_soon, so a fan-out already in flight may still
        land — this cuts the stream, it does not chase a packet already gone.
        Any grace period for copying work out is the browser's business, not
        this daemon's."""
        self.can_write = False
        self.readable = False
        self._tell("kb:read-revoked", close=True)

    def _tell(self, reason: str, close: bool) -> None:
        """Fire-and-forget: this is called from the flush loop, and ws.close()
        waits up to the socket's close timeout for the peer's CLOSE frame. The
        task is parked on the channel so the loop keeps a strong reference; the
        send bypasses self.send() so the mute cannot swallow its own notice."""
        async def run() -> None:
            try:
                await self._ws.send_bytes(auth_denied(reason))
            except (ConnectionResetError, RuntimeError, OSError):
                pass
            # OUTSIDE the send's failure path. With the close inside it, a send
            # that raised — peer half-gone, transport full — skipped the close
            # and left a muted socket open until the peer's own read failed.
            # That is exactly the case revoke_read exists for.
            if close:
                try:
                    await self._ws.close()
                except (ConnectionResetError, RuntimeError, OSError):
                    pass
        self._notice = asyncio.create_task(run())

    async def recv(self) -> bytes:
        try:
            return await self.__anext__()
        except StopAsyncIteration:
            raise ConnectionResetError

    async def send(self, message: bytes) -> None:
        if not self.readable:
            return          # read revoked: the room may hold this channel for
                            # another beat — nothing more goes out of it
        try:
            await self._ws.send_bytes(message)
        except ConnectionResetError:
            # The tab closed while we were mid-broadcast. A clean close already
            # ends the room's `async for` silently; only this race escapes, and
            # pycrdt re-raises anything out of its task group -- so letting it
            # out prints a 40-line ExceptionGroup for an ordinary disconnect.
            # (aiohttp's ClientConnectionResetError subclasses this builtin.)
            pass


def _line_changes(base, other):
    out = []
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            out.append([i1, i2, other[j1:j2]])
    return out


def _apply_line_edits(txt, live: str, target: str) -> None:
    """Turn the live CRDT text into `target` using the SMALLEST spans possible.

    Never a global character diff. diff3 gives a clean `target`, but flattening
    it back to `diff_main(live, target)` re-introduces exactly the problem diff3
    was chosen to avoid: on repetitive prose (near-identical lines, runs of the
    same letter) diff-match-patch finds no good alignment and emits one huge
    delete plus one
    huge insert.

    That is data loss, not cosmetics. A concurrent keystroke from a browser that
    lands INSIDE a deleted span loses its anchor characters — the CRDT keeps the
    text (it never drops an insert) but it collapses to the START of the deleted
    region. Measured: a user typing in the third line had their text surface
    inside the document's title (`#  hhhhMeeting notes`), which is precisely the
    corruption tests/e2e/test_external_merge.py reproduces.

    So: diff by LINE, and inside each changed run trim the identical head and
    tail. The deleted span can then never exceed the lines that actually
    differ, and an unlucky concurrent keystroke can at worst move to the start
    of the one line the external writer genuinely rewrote.

    Edits are applied in DESCENDING offset order so that mutating the text
    cannot shift the offsets of edits not yet applied.

    OFFSETS ARE UTF-8 BYTES, NOT CHARACTERS. pycrdt/yrs index Text by byte, and
    they do not complain when handed a Python string index: `insert()` at an
    offset that lands mid-character silently appends to the END of the document
    instead, and `del` of a partial character discards the remainder of the
    text. On a corpus this Czech, with emoji in half the folder names, passing
    len(str) offsets is not an edge case — it is most documents. Every
    conversion below goes through .encode(); keep it that way.
    """
    a = live.splitlines(keepends=True)
    b = target.splitlines(keepends=True)
    starts, acc = [], 0
    for ln in a:                      # BYTE offset of each live line
        starts.append(acc)
        acc += len(ln.encode())
    starts.append(acc)

    edits = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = "".join(a[i1:i2]), "".join(b[j1:j2])
        base = starts[i1]
        head = 0                      # shared prefix stays untouched
        while head < len(old) and head < len(new) and old[head] == new[head]:
            head += 1
        tail = 0                      # …and so does the shared suffix
        while (tail < len(old) - head and tail < len(new) - head
               and old[-1 - tail] == new[-1 - tail]):
            tail += 1
        # head/tail are character counts over text identical on both sides, so
        # their byte widths are the same in `old` and `new`.
        head_b = len(old[:head].encode())
        tail_b = len(old[len(old) - tail:].encode()) if tail else 0
        old_b = len(old.encode())
        edits.append((base + head_b, base + old_b - tail_b,
                      new[head:len(new) - tail]))

    with txt.doc.transaction():
        for start, end, ins in reversed(edits):
            if end > start:
                del txt[start:end]
            if ins:
                txt.insert(start, ins)


def three_way_merge_lines(base_text, ours_text, theirs_text):
    base = base_text.splitlines(keepends=True)
    ours = ours_text.splitlines(keepends=True)
    theirs = theirs_text.splitlines(keepends=True)
    cs = ([c + [0] for c in _line_changes(base, ours)] +
          [c + [1] for c in _line_changes(base, theirs)])
    cs.sort(key=lambda c: (c[0], c[1]))
    groups = []
    for c in cs:
        if groups:
            g = groups[-1]
            if c[0] < g[1] or (c[0] == g[1] and c[0] == c[1] and g[0] == g[1]):
                g[1] = max(g[1], c[1])
                g[2].append(c)
                continue
        groups.append([c[0], c[1], [c]])
    out, cur, conflicts = [], 0, 0

    def replay(members, gs, ge):
        pos = gs
        for m in sorted(members, key=lambda m: m[0]):
            out.extend(base[pos:m[0]])
            out.extend(m[2])
            pos = m[1]
        out.extend(base[pos:ge])

    for gs, ge, members in groups:
        out.extend(base[cur:gs])
        cur = ge
        sides = {m[3] for m in members}
        if len(sides) == 2:
            if all(m[0] == m[1] for m in members):     # co-located pure insertions: keep both
                for side in (0, 1):
                    for m in members:
                        if m[3] == side:
                            out.extend(m[2])
            else:
                conflicts += 1                          # ours (the live editors) wins
                replay([m for m in members if m[3] == 0], gs, ge)
        else:
            replay(members, gs, ge)
    out.extend(base[cur:])
    return "".join(out), conflicts



class SyncDaemon:
    def __init__(self):
        self.key = common.load_session_key()
        self.server = WebsocketServer(auto_clean_rooms=False)
        self.text_handles: dict[str, Text] = {}
        self.meta: dict[str, tuple[int, int, int]] = {}  # room -> (uid, gid, mode)
        self.last_written: dict[str, bytes] = {}
        self.seeded: set[str] = set()
        self.epochs: dict[str, str] = {}   # room -> CRDT lineage id
        # Who has which doc open, by HUB-VERIFIED identity (the token the hub
        # signed at connect time — not the client-claimed awareness name).
        # room -> {id(channel): Session}. Holding the channel, not just the
        # name, is what lets an un-share reach a socket that is already open.
        self.presence: dict[str, dict[int, Session]] = {}
        # Version-history attribution: who actually SENT edits to each room
        # since its last flush (room -> {user: last-edit ts}), and what each
        # flush window should be committed as (path -> author).
        self.recent_editors: dict[str, dict[str, float]] = {}
        self.dirty_docs: dict[str, str] = {}
        # dirty_docs is written by flush_loop on the event loop and consumed by
        # _git_commit in an executor THREAD. The lock is what lets the commit ask
        # "is a flush's author still in flight?" and get a truthful answer.
        self._attrib_lock = threading.Lock()
        self.git_dirty = False
        self._deferred: set[str] = set()      # flushes backed off (log once, not per tick)
        self.tasks: list[asyncio.Task] = []
        self._loop_deaths: dict[str, float] = {}

    def note_edit(self, name: str, user: str) -> None:
        self.recent_editors.setdefault(name, {})[user] = time.time()

    def _presence_add(self, name: str, key: int, sess: Session) -> None:
        self.presence.setdefault(name, {})[key] = sess

    def _presence_drop(self, name: str, key: int) -> None:
        room = self.presence.get(name)
        if room is not None:
            room.pop(key, None)
            if not room:
                self.presence.pop(name, None)

    # --- live access enforcement -------------------------------------------
    ACL_RECHECK = 4.0    # seconds between access re-checks of open sockets

    def _live_gids(self, user: str, cache: dict) -> list[int] | None:
        """This user's groups AS OF NOW, from NSS — never the gids in the token
        their tab connected with. Those froze at connect, so "I took them out of
        the project group" would re-check green for as long as the tab stayed
        open. None means the account is gone; that is a revocation too."""
        if user not in cache:
            try:
                cache[user] = os.getgrouplist(user, pwd.getpwnam(user).pw_gid)
            except (KeyError, OSError):
                cache[user] = None
        return cache[user]

    def enforce_access(self, rooms=None, users=None) -> None:
        """Re-evaluate open sockets against the kernel and act on what changed.

        Losing WRITE keeps the socket — the tab keeps its unsaved buffer and
        goes read-only. Losing READ cuts the send path immediately.
        Revocation only: a re-grant needs a reopen, because a tab that was told
        it is read-only has already reconfigured itself.

        `rooms` and `users` are a UNION filter (a roster rewrite reaches
        documents nowhere near the path that triggered it); neither given means
        every session. Synchronous on purpose — it must not await inside the
        flush loop, and the per-pass caches keep it to a few dozen stats."""
        acc: dict[tuple[int, str], tuple[bool, bool]] = {}
        gids: dict[str, list[int] | None] = {}
        # `not rooms and not users`, NOT `is None`: invalidate_handler turns a
        # JSON `"users": []` into an empty set, and an emergent "no filter
        # matched anything" would silently sweep nothing at all.
        everything = not rooms and not users
        rooms, users = rooms or set(), users or set()
        for name, members in list(self.presence.items()):
            in_room = name in rooms
            if not (everything or in_room or users):
                continue
            p = common.REPO_ROOT / name
            if not p.exists():
                continue     # deleted or renamed: flush_loop retires the room,
                             # and "gone" must not be reported as "revoked"
            for sess in list(members.values()):
                if not (everything or in_room or sess.user in users):
                    continue
                key = (sess.uid, name)
                if key not in acc:
                    g = self._live_gids(sess.user, gids)
                    acc[key] = ((False, False) if g is None else
                                (fs_can(p, sess.uid, g, False),
                                 fs_can(p, sess.uid, g, True)))
                read, write = acc[key]
                ch = sess.channel
                if not read and ch.readable:
                    log.info("read revoked for %s on %s — cutting the session",
                             sess.user, name)
                    ch.revoke_read()
                elif read and ch.can_write and not write:
                    log.info("write revoked for %s on %s", sess.user, name)
                    ch.revoke_write()

    async def invalidate_handler(self, request: web.Request) -> web.Response:
        """The hub says an ACL just moved — re-check the sockets it names, now.

        A valid hub token is the whole authorization, because this endpoint
        cannot grant anything: it only re-asks the kernel and acts on a NO. It
        is registered on the root-only 0600 socket, never on the
        world-connectable vc one."""
        token = request.headers.get("X-KB-Auth")
        if not (token and common.read_token(self.key, token)):
            return web.Response(status=403, text="no identity")
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        rooms = users = None
        rel = str(body.get("path") or "").strip("/")
        if rel:
            p = common.resolve_repo_path(rel)
            if p is None:
                return web.json_response({"error": "bad path"}, status=400)
            rel = str(p.relative_to(common.REPO_ROOT.resolve()))
            # A share is a SUBTREE change — the rehome walk touches every
            # descendant — so match the folder and everything under it.
            rooms = {n for n in self.presence if n == rel or n.startswith(rel + "/")}
        who = body.get("users")
        if isinstance(who, list):
            users = {u for u in who if isinstance(u, str)}
        self.enforce_access(rooms=rooms, users=users)
        return web.json_response({"ok": True})

    # --- seeding / flushing -------------------------------------------------
    def _state_path(self, name: str) -> Path:
        import hashlib
        return DOC_STATE_DIR / (hashlib.sha256(name.encode()).hexdigest()[:24] + ".y")

    def doc_epoch(self, name: str) -> str:
        """The doc's CRDT lineage id. A client may only join the live session
        with the CURRENT epoch — a tab whose Y.Doc came from an older lineage
        (e.g. from before a state reset) would otherwise merge its entire stale
        history back in, duplicating and interleaving the text. Persisted next
        to the doc state; a fresh seed (no saved state) starts a new lineage."""
        ep = self.epochs.get(name)
        if ep:
            return ep
        epath = self._state_path(name).with_suffix(".e")
        try:
            ep = epath.read_text().strip()
        except OSError:
            ep = ""
        if not ep:
            ep = os.urandom(6).hex()
            try:
                DOC_STATE_DIR.mkdir(parents=True, exist_ok=True)
                os.chmod(DOC_STATE_DIR, 0o700)
                epath.write_text(ep)
            except OSError:
                pass
        self.epochs[name] = ep
        return ep

    async def epoch_handler(self, request: web.Request) -> web.Response:
        token = request.headers.get("X-KB-Auth")
        if not (token and common.read_token(self.key, token)):
            return web.Response(status=403, text="no identity")
        rel = request.match_info["path"]
        p = common.resolve_repo_path(rel)
        if p is None:
            return web.Response(status=404, text="bad path")
        rel = str(p.relative_to(common.REPO_ROOT.resolve()))
        return web.json_response({"epoch": self.doc_epoch(rel)})

    def _retire_room_attr(self, name: str) -> None:
        self.recent_editors.pop(name, None)
        with self._attrib_lock:            # the commit thread reads this dict
            self.dirty_docs.pop(name, None)

    async def _drop_room(self, name: str) -> None:
        """Forget the pycrdt YRoom behind `name`.

        auto_clean_rooms=False, so pycrdt never removes a room by itself: unless
        we do it, self.server.rooms keeps the room — and its Y.Doc — for the life
        of the process, and the next get_room(name) hands back the OLD document.
        For a path whose file changed underneath that IS the bug: seed_room's two
        "is the text empty?" guards both see the dead content, refuse to load the
        file now at that path, and every flush afterwards defers on the
        last_written mismatch. Delete a file and recreate it and the editor shows
        the deleted text while nothing you type ever reaches disk.

        Callers do their own bookkeeping FIRST and call this LAST: everything
        they drop is synchronous, so a half-retired room is never observable
        across the await here.
        """
        room = self.server.rooms.get(name)
        if room is None:
            return
        try:
            await self.server.delete_room(name=name)   # pops the map, then stops
            # YRoom.stop() unobserves but leaves room._subscription bound, so the
            # Rust Subscription lives until the room is garbage-collected — which
            # can happen on the executor thread running _git_commit, and pycrdt
            # then raises "Subscription is unsendable, but is being dropped on
            # another thread". Drop the last reference HERE, on the event loop
            # thread that created it. (Observed once in production after this
            # function was introduced; pinned to pycrdt-websocket 0.16.4.)
            room._subscription = None
        except KeyError:
            return                                     # already gone; nothing to do
        except RuntimeError:
            # YRoom.stop() awaits awareness.stop() BEFORE cancelling its own
            # scope, so a room caught in its startup window raises here and
            # strands a live task group, its pumps and the ydoc observer for the
            # life of the process. The map entry — the half that poisons the next
            # open — is already gone (delete_room pops before its first await),
            # so finish the teardown by hand. This reaches into pycrdt internals
            # and is pinned to pycrdt-websocket 0.16.4.
            tg = room._task_group
            if tg is not None:
                tg.cancel_scope.cancel()
                room._task_group = None
            if room._subscription is not None:
                try:
                    room.ydoc.unobserve(room._subscription)
                except Exception:
                    pass
                room._subscription = None
            log.warning("room %s was mid-startup; tore it down by hand", name)

    async def _retire_room(self, name: str) -> None:
        """The file backing an open room vanished (renamed or deleted out from
        under the editors). Drop ALL room state instead of letting flush_loop
        resurrect the file at the old path (it would recreate it as root from
        remembered metadata — split-brain). Bumping the lineage (delete .e) and
        the saved CRDT state means a stale tab still pointed here reconnects to a
        NEW epoch and gets 409'd into a fresh reopen — where it discovers the
        404 and closes. The frontend's tree-prune is what retires those tabs."""
        self.text_handles.pop(name, None)
        self.last_written.pop(name, None)
        self.meta.pop(name, None)
        self.seeded.discard(name)
        self.epochs.pop(name, None)
        self._deferred.discard(name)
        # enforce_access deliberately SKIPS a path that does not exist — an
        # external editor saving by rename makes it briefly absent, and cutting
        # everyone on that would be worse. So the un-share-by-move case is cut
        # here instead, where flush_loop has already decided the room is dead
        # and is about to destroy its epoch and state. Without this, moving a
        # document into a private folder leaves every revoked reader still
        # receiving each co-editor's keystroke.
        for sess in list(self.presence.get(name, {}).values()):
            if sess.channel.readable:
                log.info("read revoked for %s on %s — backing file gone",
                         sess.user, name)
                sess.channel.revoke_read()
        self._retire_room_attr(name)   # don't leak attribution state for a gone room
        sp = self._state_path(name)
        for f in (sp, sp.with_suffix(".e")):
            try:
                f.unlink()
            except OSError:
                pass
        await self._drop_room(name)
        log.info("retired room %s (backing file gone)", name)

    def _save_doc_state(self, name: str, ydoc) -> None:
        try:
            DOC_STATE_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(DOC_STATE_DIR, 0o700)   # states contain private doc content
            sp = self._state_path(name)
            tmp = sp.with_suffix(".tmp")
            tmp.write_bytes(ydoc.get_update())
            os.replace(tmp, sp)
        except OSError as e:
            log.warning("doc-state save failed for %s: %s", name, e)

    async def seed_room(self, room, name: str) -> None:
        if name in self.seeded:
            return
        self.seeded.add(name)
        path = common.REPO_ROOT / name
        content = ""
        try:
            st = path.lstat()
            self.meta[name] = (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))
            if stat.S_ISREG(st.st_mode):
                content = path.read_text(errors="replace")
        except OSError:
            self.meta[name] = (0, 0, 0o644)
        txt = room.ydoc.get("content", type=Text)
        resumed = False
        if str(txt) == "":
            try:
                room.ydoc.apply_update(self._state_path(name).read_bytes())
                resumed = True
            except (OSError, Exception):   # no/corrupt state -> fresh seed below
                resumed = False
        self.text_handles[name] = txt
        self.last_written[name] = str(txt).encode()
        if resumed:
            # same op ids as before the restart; reconcile anything that was
            # edited on disk while we were down through the normal merge path
            if content != str(txt):
                self.apply_external(name, content)
            log.info("resumed room %s from saved state (%d chars)", name, len(str(txt)))
        else:
            if content and str(txt) == "":
                with room.ydoc.transaction():
                    txt += content
            self.last_written[name] = str(txt).encode()
            log.info("seeded room %s (%d chars)", name, len(str(txt)))
        self._save_doc_state(name, room.ydoc)

    def _atomic_write(self, name: str, content: str) -> bool:
        """Write content to the doc's file, preserving its owner, group, mode
        AND its ACL.

        The ACL is not decoration: on this platform it IS the audience. A file
        shared with two colleagues carries `user:alice:rw`, `user:bob:rw` and
        `user:kbindexer:r`, with `group::---` and a mask; the mode bits alone
        say `0660`, which means something completely different without the
        ACL — the file's whole group, which for a shared file is `kb-users`,
        i.e. the company. Replacing the file and restoring only the mode
        therefore did two bad things at once on the FIRST keystroke after a
        share: it dropped the named people (and the indexer, so the document
        fell out of search), and it widened the file to everyone in the group.
        Found on 2026-09-22 while chasing why a public link could not save —
        the link's ACL entry was being erased the same way.

        Runs as root, so it must not be redirectable by a user-planted symlink:
        the parent dir is reached via openat/O_NOFOLLOW, the temp file has an
        UNPREDICTABLE name and is created O_EXCL|O_NOFOLLOW (so a pre-planted
        symlink is refused, not followed), and the rename is a renameat within the
        pinned dir. A doc that is itself a symlink is refused outright.

        NEVER clobbers an unseen external edit: if the bytes on disk differ from
        the last state this daemon has seen (last_written), an external writer
        got there first — we back off (return False) so the watcher can merge
        that edit into the doc; the NEXT flush then writes the merged result.
        """
        parent_rel = os.path.dirname(name)
        fname = os.path.basename(name)
        data = content.encode()
        try:
            pfd = common.opendir_beneath(parent_rel)
        except OSError:
            return False
        try:
            try:
                lst = os.stat(fname, dir_fd=pfd, follow_symlinks=False)
                if stat.S_ISLNK(lst.st_mode):
                    return False
                uid, gid, mode = lst.st_uid, lst.st_gid, stat.S_IMODE(lst.st_mode)
                known = self.last_written.get(name)
                if known is not None:
                    rfd = os.open(fname, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=pfd)
                    try:
                        on_disk = os.read(rfd, lst.st_size + 1)
                    finally:
                        os.close(rfd)
                    if on_disk != known and on_disk != data:
                        log.info("flush of %s deferred: unseen external edit on disk", name)
                        return False
            except OSError:
                uid, gid, mode = self.meta.get(name, (0, 0, 0o644))
            acl = self._acl_of(fname, pfd)
            tmpname = f".{fname}.{os.urandom(6).hex()}.kbtmp"
            fd = os.open(tmpname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=pfd)
            try:
                os.write(fd, data)
                os.fchown(fd, uid, gid)
                os.fchmod(fd, mode)          # chmod rewrites the mask, so the
                if acl is not None:          # ACL goes on AFTER it
                    try:
                        os.setxattr(fd, _ACL_ACCESS, acl)
                    except OSError as e:
                        log.warning("could not carry the ACL of %s across a flush: %s", name, e)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmpname, fname, src_dir_fd=pfd, dst_dir_fd=pfd)
        finally:
            os.close(pfd)
        self.last_written[name] = data
        return True

    @staticmethod
    def _acl_of(fname: str, pfd: int) -> bytes | None:
        """The file's ACL as the raw xattr, read through an fd (this is root
        in somebody's own directory: never by path, never following a link)."""
        try:
            fd = os.open(fname, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=pfd)
        except OSError:
            return None
        try:
            return os.getxattr(fd, _ACL_ACCESS)
        except OSError:
            return None                      # no extended ACL, or no xattr support
        finally:
            os.close(fd)

    async def flush_loop(self) -> None:
        # Poll every active room and persist any whose CRDT text has diverged from
        # disk. Polling (rather than trusting observer callbacks) is robust to
        # pycrdt observer-lifecycle quirks and is cheap at this scale.
        next_acl = 0.0
        while True:
            await asyncio.sleep(FLUSH_DEBOUNCE)
            # The access re-check rides this loop on its OWN, much slower tick:
            # it is a real kernel read (a stat + getxattr per ancestor) per open
            # socket, which at 250 ms would be the daemon's whole workload. The
            # hub's push is what makes an un-share feel instant; this is the
            # backstop for every ACL change that never tells us — setfacl in a
            # terminal, an agent, a restored backup. It must never be able to
            # stop the flushing: a doc that stops reaching disk loses work.
            now = time.monotonic()
            if now >= next_acl:
                next_acl = now + self.ACL_RECHECK
                try:
                    self.enforce_access()
                except Exception as e:
                    log.warning("access re-check failed: %s", e)
            for name, txt in list(self.text_handles.items()):
                try:
                    content = str(txt)
                except Exception:
                    continue
                # The file moved or was deleted while open: never resurrect it at
                # the old path (that recreated it as root) — retire the room.
                # Checked BEFORE the clean/dirty short-circuit below: a room whose
                # text already matches last_written is exactly the case that used
                # to `continue` past this and leak forever, keeping the deleted
                # path's stale CRDT state authoritative over anything later
                # written to disk there.
                if not (common.REPO_ROOT / name).exists():
                    await self._retire_room(name)
                    continue
                if content.encode() == self.last_written.get(name):
                    continue
                try:
                    # Bytes and author under ONE lock. _git_commit reads
                    # dirty_docs from a thread AFTER it has staged the worktree;
                    # if these two halves could interleave there, it would see a
                    # staged file with no pending author and sweep somebody's
                    # edit into the unattributed commit.
                    with self._attrib_lock:
                        written = self._atomic_write(name, content)
                        if written:
                            # Attribute this flush window to whoever actually sent
                            # edits (verified ws identity), most-recent typist wins.
                            # LIMITATION (documented, by design): a commit is a whole-
                            # file snapshot with ONE author, so when several people
                            # co-edit a shared-writable doc in the same window, the
                            # window is credited to the last typist — not per line.
                            # Attribution is authoritative for singly-writable files
                            # (a user's own/private docs); on shared docs it is the
                            # best single guess. Per-line multi-author blame would
                            # need Yjs clientID->user mapping (a separate feature).
                            eds = self.recent_editors.pop(name, None)
                            if eds:
                                self.dirty_docs[name] = max(eds, key=eds.get)
                    if written:
                        self.git_dirty = True
                        self._save_doc_state(name, txt.doc)
                        log.info("flushed %s (%d chars) to disk", name, len(content))
                except OSError as e:
                    log.warning("flush failed for %s: %s", name, e)

    # --- inbound (disk -> doc) ---------------------------------------------
    def apply_external(self, name: str, new_text: str) -> None:
        """Merge an external (disk) change into the live doc via a DETERMINISTIC
        line-level three-way merge (diff3) — never fuzzy patching.

        The external writer (vim, a script, claude code) based its write on some
        earlier file state; the live doc may hold keystrokes typed since then.
        Naive live-vs-disk diffing scattered char ops ("word salad"); the
        diff-match-patch fuzzy patch_apply this used to run was no better — it silently splits hunks into
        <=32-char chunks (its bitap Match_MaxBits) and anchors each chunk
        independently, splicing fragments into lookalike lines. diff3 instead:
        base = shadow (last agreed state); regions changed by ONE side apply
        verbatim as whole lines; regions changed by BOTH resolve to the live
        editors' version and the external writer's conflicting change is
        dropped (logged; the flush then writes the merged truth back to disk).
        Output lines are always whole lines from one side — splicing inside a
        line is structurally impossible.
        """
        txt = self.text_handles.get(name)
        if txt is None:
            return
        live = str(txt)
        if live == new_text:
            self.last_written[name] = new_text.encode()
            return
        shadow = self.last_written.get(name, b"").decode(errors="replace")
        if shadow == new_text:
            return   # disk merely caught up with our own flush
        if live == shadow:
            target = new_text            # nobody typed meanwhile: take disk verbatim
        else:
            target, conflicts = three_way_merge_lines(shadow, live, new_text)
            if conflicts:
                log.warning("external merge for %s: %d conflicting region(s) — "
                            "the live editors' lines were kept", name, conflicts)
        _apply_line_edits(txt, live, target)
        # Record what's ON DISK as the new shadow. If the merge kept concurrent
        # keystrokes (doc != disk), the flush loop sees the difference and writes
        # the merged text back out — that's the reconciliation, not a loop.
        self.last_written[name] = new_text.encode()
        self._save_doc_state(name, txt.doc)

    async def watch_loop(self) -> None:
        async for changes in awatch(common.REPO_ROOT, recursive=True,
                                    ignore_permission_denied=True, debounce=200, step=50):
            for _change, fspath in changes:
                p = Path(fspath)
                if p.name.endswith(".kbtmp") or p.name.startswith(".kb"):
                    continue
                try:
                    name = str(p.resolve().relative_to(common.REPO_ROOT.resolve()))
                except (ValueError, OSError):
                    continue
                # Any real change to the tree is worth a git snapshot, whether or
                # not the file is currently open in an editing session.
                self.git_dirty = True
                if name not in self.text_handles:
                    continue
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                if data == self.last_written.get(name):
                    continue
                self.apply_external(name, data.decode(errors="replace"))

    # --- loop supervision ---------------------------------------------------
    _RESTART_WINDOW = 60.0     # a second death inside this is persistent, not a blip

    def supervise(self, name: str, factory) -> asyncio.Task:
        """Run one of the three loops as a task that cannot die quietly.

        Each loop IS a guarantee: flush = your edits reach disk, watch =
        external edits reach the doc, git = the KB stays versioned. A bare
        create_task loses all three the same way — the task ends, asyncio
        mentions it at GC as "Task exception was never retrieved", /health keeps
        answering ok and git-state.json keeps looking green while the KB quietly
        stops being versioned.

        One retry, then take the process down. The two failures are not the same
        shape: a transient (a git index.lock, an inotify hiccup) is worth a
        restart rather than dropping every live editing session, but a
        persistent one must NOT be restarted in a tight loop — that is how one
        incident buried 61,895 identical lines in the journal. Exiting is the
        loud option and it already has a ladder: Restart=on-failure brings the
        daemon back with rooms resumed from saved state, and StartLimitBurst
        gives up into `failed`, which fires OnFailure=kb-alert@ with the journal
        tail. A dead loop has no ladder.
        """
        task = asyncio.get_running_loop().create_task(factory(), name=name)
        task.add_done_callback(lambda t: self._loop_died(name, factory, t))
        self.tasks.append(task)
        return task

    def _loop_died(self, name: str, factory, task: asyncio.Task) -> None:
        if task.cancelled():
            return                                    # shutdown, not a fault
        exc = task.exception()
        now = time.monotonic()
        again = now - self._loop_deaths.get(name, 0.0) < self._RESTART_WINDOW
        self._loop_deaths[name] = now
        log.error("LOOP %s STOPPED (%s) — %s", name,
                  type(exc).__name__ if exc else "returned unexpectedly",
                  "twice in a minute, taking the daemon down" if again else "restarting it",
                  exc_info=exc)
        if again:
            # A plain SystemExit, NOT aiohttp's GracefulExit: run_app catches
            # GracefulExit and returns normally, which is exit status 0 — and
            # Restart=on-failure would never fire. This one still runs
            # on_cleanup, then leaves the process with status 1.
            raise SystemExit(1)
        self.supervise(name, factory)

    async def git_loop(self) -> None:
        while True:
            await asyncio.sleep(GIT_DEBOUNCE)
            if not self.git_dirty:
                continue
            self.git_dirty = False
            # The authors are taken INSIDE _git_commit, next to the git calls
            # they belong to. Taking them here — before an executor hop that
            # lasts seconds — orphaned every flush that landed in between: the
            # sweep committed those bytes unattributed, and the author was then
            # dropped on the next pass as "nothing staged".
            await asyncio.get_event_loop().run_in_executor(None, self._git_commit)

    _HINTS_PER_PASS = 2000    # DoS backstop: a flood of hint FILES past this is dropped
    _CHECKS_PER_PASS = 1000   # …and a global cap on write-checks (runuser spawns) per pass,
                              # so a hint with thousands of distinct fake paths can't stall
                              # the commit loop with a million subprocesses

    def _can_read_as(self, user: str, rel: str, cache: dict) -> bool:
        """Can this person open that file, right now, as the kernel sees it?
        Asked before a notification is written: an inbox line carries a path
        and a line of text, so telling someone about a document they cannot
        read would be the leak, not the courtesy."""
        key = (user, rel)
        if key in cache:
            return cache[key]
        try:
            r = subprocess.run(["/usr/sbin/runuser", "-u", user, "--",
                                "/usr/bin/test", "-r", str(common.REPO_ROOT / rel)],
                               capture_output=True, timeout=10)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        cache[key] = ok
        return ok

    def _notify_mentions(self, rel: str, author: str, cache: dict) -> None:
        """A document was just committed: tell anyone NEWLY `@named` in it.

        Newly, because the names a document already had are not news — and the
        previous version is the commit we just built on, so `git show HEAD~1`
        answers it exactly. Stateless on purpose: an in-memory set would make
        a new document (nothing remembered) silent, and a restart would decide
        whether you hear about a mention or not.
        """
        if common.is_secret_path(rel) or common.is_trash_path(rel):
            return
        try:
            text = (common.REPO_ROOT / rel).read_text(errors="replace")[:400_000]
        except OSError:
            return
        now = {m.lower() for m in common.MENTION_RE.findall(text)}
        if not now:
            return
        prev = subprocess.run(["git", "-C", str(common.REPO_ROOT), "show", f"HEAD~1:{rel}"],
                              capture_output=True, text=True)
        was = ({m.lower() for m in common.MENTION_RE.findall(prev.stdout)}
               if prev.returncode == 0 else set())   # not in the parent commit: all of it is new
        for name in sorted(now - was):
            if name == (author or "").lower():
                continue                       # mentioning yourself is not news
            try:
                pw = pwd.getpwnam(name)
            except KeyError:
                continue                       # @something that is not a person here
            if pw.pw_uid < 1000 or pw.pw_uid >= 65000:
                continue
            if not self._can_read_as(name, rel, cache):
                continue                       # …and never name a document they cannot open
            line, quote = 0, ""
            for i, raw in enumerate(text.splitlines(), 1):
                if ("@" + name) in raw.lower():
                    line, quote = i, raw.strip()[:200]
                    break
            common.add_inbox_event(name, {"kind": "mention", "path": rel, "line": line,
                                          "text": quote, "actor": author or ""})

    def _can_write_as(self, user: str, rel: str, cache: dict) -> bool:
        # cache MUST be keyed by (user, rel): multiple users are checked against
        # the same paths in one pass, so a path-only key would let one user
        # inherit another's answer (a forged-hint bypass).
        key = (user, rel)
        if key in cache:
            return cache[key]
        try:
            r = subprocess.run(["/usr/sbin/runuser", "-u", user, "--",
                                "/usr/bin/test", "-w", str(common.REPO_ROOT / rel)],
                               capture_output=True, timeout=10)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        cache[key] = ok
        return ok

    def _consume_attrib_hints(self) -> dict[str, str]:
        """Fold in the per-user backends' "I just wrote/deleted these paths"
        hints into path -> author. Authorship = the hint FILE's st_uid (kernel-
        set when the user's own backend created it — unforgeable as another user).

        A hint is trusted for a path ONLY if its owner can WRITE that path: you
        can only change a file you can write, so this stops A from claiming (or
        stealing) an edit to a file A cannot touch. Bounded per pass so a flood
        of the world-writable drop-box can't stall the commit loop or fill /run;
        the excess is unlinked unprocessed (attribution is best-effort — the
        sweep still commits every change, so nothing is ever lost)."""
        out: dict[str, str] = {}
        wcache: dict = {}
        checks = 0
        try:
            entries = sorted(common.ATTRIB_DIR.iterdir(), key=lambda p: p.name)
        except OSError:
            return out
        for i, f in enumerate(entries):
            if i < self._HINTS_PER_PASS and checks < self._CHECKS_PER_PASS:
                try:
                    st = f.lstat()
                    if stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode) \
                            and st.st_nlink == 1 and st.st_size <= 65536 and 1000 <= st.st_uid < 65000:
                        user = pwd.getpwuid(st.st_uid).pw_name
                        data = json.loads(f.read_text())
                        for p in data.get("paths", [])[:500]:
                            if not (isinstance(p, str) and common.is_versioned_path(p)):
                                continue
                            key = (user, p)
                            if key not in wcache:      # each distinct (user,path) costs one runuser
                                if checks >= self._CHECKS_PER_PASS:
                                    break
                                checks += 1
                            if self._can_write_as(user, p, wcache):
                                out[p] = user          # later (write-verified) hints win
                except (OSError, ValueError, KeyError):
                    pass
            try:
                f.unlink()   # ALWAYS drain the drop-box, processed or not (best-effort)
            except OSError:
                pass
        return out

    def _take_authors(self) -> dict[str, str]:
        """Consume the pending live-session authors (path -> user)."""
        with self._attrib_lock:
            docs, self.dirty_docs = self.dirty_docs, {}
        return docs

    def _pending_authors(self) -> list[str]:
        """Paths whose author has NOT been consumed yet. Read AFTER staging:
        flush_loop holds the same lock across write-then-record, so any path
        `git add` could have seen is guaranteed to be in here."""
        with self._attrib_lock:
            return sorted(self.dirty_docs)

    def _git_commit(self) -> None:
        """Commit the pending changes — ATTRIBUTED, ONE COMMIT PER FILE. Each
        commit touches exactly one path, so its subject names only that file and
        `--name-status` lists only that file: a commit can never disclose the
        name of a co-changed file the reader can't see (the per-file read gate
        does the rest). Authorship = verified CRDT identity (outranks) or a
        write-verified attrib hint; anything left over is swept into one
        kb-syncd commit so history never loses a change (the DR guarantee).
        Scope: .gitignore whitelists *.md/*.html; secrets are additionally
        excluded here in root-owned code — history outlives deletions and
        permission changes."""
        root = str(common.REPO_ROOT)
        try:
            status = subprocess.run(["git", "-C", root, "status", "--porcelain",
                                     "--", ".", ":(exclude)**/_secrets/**",
                                     ":(exclude)_secrets/**"],
                                    capture_output=True, text=True)
            if not status.stdout.strip():
                self._write_git_state()
                return
            attrib = self._consume_attrib_hints()
            for path, author in self._take_authors().items():   # live-session identity outranks hints
                if author and common.is_versioned_path(path):
                    attrib[path] = author

            def commit_one(path: str, author: str, email: str) -> bool:
                if common.is_secret_path(path):
                    return False
                subprocess.run(["git", "-C", root, "reset", "-q"], capture_output=True)
                # LITERAL pathspec: `path` ultimately comes from the world-writable
                # attrib drop-box, so a name like ':(glob)**/_secret[s]/**' must be
                # a filename, not pathspec magic that could stage other people's
                # files. (The sweep below keeps magic on purpose — its pathspecs
                # are constants, not user input.)
                subprocess.run(["git", "-C", root, "add", "-A", "--", path],
                               capture_output=True,
                               env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"})
                if subprocess.run(["git", "-C", root, "diff", "--cached", "--quiet"],
                                  capture_output=True).returncode == 0:
                    return False                             # nothing actually staged for this path
                subprocess.run(["git", "-C", root, "-c", f"user.name={author}",
                                "-c", f"user.email={email}", "commit", "-q",
                                "-m", f"edit: {path}"], capture_output=True)
                return True

            rcache: dict = {}
            for path in sorted(attrib):                      # attributed, one commit each
                if not commit_one(path, attrib[path], f"{attrib[path]}@kb.local"):
                    continue
                try:
                    self._notify_mentions(path, attrib[path], rcache)
                except Exception:                            # noqa: BLE001 — never break a commit
                    log.debug("mention notify failed for %s", path, exc_info=True)
            # the unattributed remainder — never lose history over missing
            # attribution. Sweep is one commit; its subject is generic and its
            # file list is permission-filtered by the activity endpoint.
            subprocess.run(["git", "-C", root, "add", "-A", "--",
                            ".", ":(exclude)**/_secrets/**",
                            ":(exclude)_secrets/**"], check=True)
            # Hold back anything flushed while the commits above ran: its author
            # arrived after we took the list, so sweeping it now would credit a
            # person's edit to kb-syncd — and there is no second chance, the next
            # pass would find nothing staged and drop the name. Unstaged, the
            # change just waits one debounce and gets committed with its author.
            # (LITERAL pathspecs: a document name may contain glob characters.)
            late = self._pending_authors()
            if late:
                subprocess.run(["git", "-C", root, "reset", "-q", "--", *late],
                               capture_output=True,
                               env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"})
            if subprocess.run(["git", "-C", root, "diff", "--cached", "--quiet"],
                              capture_output=True).returncode != 0:
                subprocess.run(["git", "-C", root, "-c", "user.name=kb-syncd",
                                "-c", "user.email=kb-syncd@localhost",
                                "commit", "-q", "-m", "sync: auto-snapshot"], check=True)
        except (OSError, subprocess.CalledProcessError):
            pass
        self._write_git_state()

    def _write_git_state(self) -> None:
        """Publish repo stats to /run/kb/git-state.json (world-readable tmpfs).
        .git itself is root-only — its objects contain every committed version
        of every file, including private ones, so group access to .git would
        bypass file permissions and RLS wholesale. This file is how tests and
        UI read 'how many snapshots, is anything secret tracked' instead."""
        root = str(common.REPO_ROOT)
        try:
            count = subprocess.run(["git", "-C", root, "rev-list", "--count", "HEAD"],
                                   capture_output=True, text=True).stdout.strip()
            head = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            tracked_secrets = subprocess.run(
                ["git", "-C", root, "ls-files", "--", "**/_secrets/**", "_secrets/**"],
                capture_output=True, text=True).stdout.strip()
            state = {"commits": int(count or 0), "head": head,
                     "tracked_secrets": len([l for l in tracked_secrets.splitlines() if l])}
            tmp = "/run/kb/git-state.json.tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, "/run/kb/git-state.json")
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass

    # --- HTTP/WS handler ----------------------------------------------------
    async def ws_doc(self, request: web.Request) -> web.StreamResponse:
        token = request.headers.get("X-KB-Auth")
        ident = common.read_token(self.key, token) if token else None
        if not ident:
            return web.Response(status=403, text="no identity")
        rel = request.match_info["path"]
        p = common.resolve_repo_path(rel)
        if p is None:
            return web.Response(status=404, text="bad path")
        rel = str(p.relative_to(common.REPO_ROOT.resolve()))
        # Secrets have no live sessions: their contents must never transit the
        # CRDT relay or sit in Y.Docs. The dedicated viewer reads them directly.
        if common.is_secret_path(rel):
            return web.Response(status=403, text="secrets have no live sessions")
        # Lineage gate: a client must present the doc's CURRENT epoch. A tab
        # from an older lineage carries Y.Doc history that would re-merge as
        # duplicated/interleaved text — refusing it is always the safe outcome.
        if request.query.get("e") != self.doc_epoch(rel):
            return web.Response(status=409, text="stale doc lineage — reopen the document")
        uid = int(ident["uid"])
        gids = [int(g) for g in ident["gids"]]
        # Read admits you to the live session; write is what lets you change it.
        if not fs_can(p, uid, gids, need_write=False):
            return web.Response(status=403, text="forbidden")
        can_write = fs_can(p, uid, gids, need_write=True)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        room = await self.server.get_room(rel)
        await self.seed_room(room, rel)
        user = str(ident.get("user", "?"))
        # on_edit is installed unconditionally now: the channel calls it only on
        # a mutation it actually forwards, and whether it forwards one is
        # channel.can_write — which enforce_access flips mid-session.
        channel = AiohttpChannel(ws, rel, can_write=can_write,
                                 on_edit=lambda: self.note_edit(rel, user))
        self._presence_add(rel, id(channel), Session(user, uid, channel))
        try:
            if can_write:
                await self.server.serve(channel)
            else:
                await self.serve_readonly(room, channel)
        finally:
            self._presence_drop(rel, id(channel))
        return ws

    def _presence_identity(self, request: web.Request) -> tuple[int, list[int]] | None:
        """uid + gids of the caller: the hub's signed token (web UI), or
        SO_PEERCRED on the world-connectable vc socket (a user's own backend
        feeding /api/events, the CLI) — the kernel's word on who is asking."""
        token = request.headers.get("X-KB-Auth")
        if token:
            ident = common.read_token(self.key, token)
            if not ident:
                return None
            return int(ident["uid"]), [int(g) for g in ident["gids"]]
        name = self._vc_identity(request)
        if not name or name == "root":
            return None
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            return None
        return pw.pw_uid, list(os.getgrouplist(name, pw.pw_gid))

    async def presence_handler(self, request: web.Request) -> web.Response:
        """Who has which doc open — filtered to docs the REQUESTING user can
        read (same fs_can gate as joining the room), so presence never leaks
        the existence or audience of files outside their permissions."""
        ident = self._presence_identity(request)
        if not ident:
            return web.Response(status=403, text="no identity")
        uid, gids = ident
        out = {}
        for rel, members in list(self.presence.items()):
            if not members:
                continue
            if not fs_can(common.REPO_ROOT / rel, uid, gids, need_write=False):
                continue
            out[rel] = sorted({s.user for s in members.values()})
        return web.json_response({"presence": out})

    async def serve_readonly(self, room, channel: AiohttpChannel) -> None:
        """Admit a read-only viewer: they receive the current document and every
        live update, but any edit message they send is dropped, so they can never
        mutate a file they lack write permission on (which the root daemon would
        otherwise persist under the owner's identity)."""
        from pycrdt import YMessageType, YSyncMessageType, create_sync_message, handle_sync_message
        await self.server.start_room(room)
        room.clients.add(channel)
        try:
            await channel.send(create_sync_message(room.ydoc))  # SYNC_STEP1
            async for message in channel:
                if not message:
                    continue
                if message[0] == YMessageType.SYNC:
                    if len(message) > 1 and message[1] == YSyncMessageType.SYNC_STEP1:
                        # STEP1 only reads state and returns a reply — no mutation.
                        reply = handle_sync_message(message[1:], room.ydoc)
                        if reply is not None:
                            await channel.send(reply)
                    # SYNC_STEP2 / SYNC_UPDATE would mutate the doc -> DROPPED.
                elif message[0] == YMessageType.AWARENESS:
                    for client in list(room.clients):
                        if client is not channel:
                            try:
                                await client.send(message)
                            except Exception:
                                pass
        except (ConnectionResetError, StopAsyncIteration):
            pass
        finally:
            room.clients.discard(channel)

    async def health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    # --- version history (permission-gated reads over the root-only .git) ----
    # The rule is one sentence: you may see a file's history exactly when you
    # may READ that file right now — evaluated by the KERNEL as you (runuser),
    # so modes, ACLs and ancestor traversal all count. .git itself stays
    # root-only; these endpoints are the only door, and every one re-checks.
    # Identity: hub-signed token (web UI) or SO_PEERCRED on the vc socket
    # (CLI/agents — the kernel says who is calling; nothing to forge).

    _REV_RE = re.compile(r"^[0-9a-f]{4,40}$")
    _VCUSER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
    _DATE_RE = re.compile(r"^[A-Za-z0-9 :.\-]{1,40}$")
    # activity paging: commits read per git call, plus the two ceilings on how
    # deep one request walks before it stops and reports truncated=True — a
    # commit count and a wall-clock budget (well under the CLI's 30s timeout).
    _VC_BATCH = 500
    _VC_MAX_SCAN = 20000
    _VC_BUDGET_S = 20.0
    _VC_CHECK_PARALLEL = 8

    def _vc_identity(self, request: web.Request) -> str | None:
        token = request.headers.get("X-KB-Auth")
        if token:
            ident = common.read_token(self.key, token)
            return str(ident["user"]) if ident and ident.get("user") else None
        sock = request.transport.get_extra_info("socket") if request.transport else None
        if sock is None:
            return None
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
        except OSError:
            return None
        if uid == 0:
            return "root"
        if not 1000 <= uid < 65000:
            return None
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return None

    def _can_read_as(self, user: str, rel: str, cache: dict | None = None) -> bool:
        """Kernel-true readability (modes + ACLs + ancestor traverse) — the whole
        security story of the history endpoints. ALWAYS a fresh kernel check per
        request, so revoking access takes effect immediately (no TTL window where
        a just-removed reader still sees history). `cache` is an OPTIONAL
        per-request dict: within one activity query the same path recurs across
        commits, and de-duping the runuser calls inside that single request is
        safe (the answer can't change mid-request) while never leaking across
        requests."""
        if user == "root":
            return True
        if cache is not None and rel in cache:
            return cache[rel]
        try:
            r = subprocess.run(["/usr/sbin/runuser", "-u", user, "--",
                                "/usr/bin/test", "-r", str(common.REPO_ROOT / rel)],
                               capture_output=True, timeout=10)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if cache is not None:
            cache[rel] = ok
        return ok

    def _git_ro(self, args: list[str]) -> subprocess.CompletedProcess:
        # GIT_LITERAL_PATHSPECS=1: a caller-supplied path is ALWAYS a literal
        # file path, never git "pathspec magic" (:(exclude), :(glob), :/, …).
        # Without this, an attacker who creates a decoy file literally named
        # ":(exclude)x.md" (which they own, so the read gate's `test -r` passes)
        # could make `git log/show -- <that>` apply the magic to OTHER files and
        # dump their content. Belt-and-suspenders with the leading-":" reject.
        env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
        # core.quotePath=false: keep the emoji folder names this KB is full of
        # readable in diff headers instead of octal escapes. Cosmetic only —
        # the machine-parsed path list relies on -z, never on this.
        return subprocess.run(["git", "-C", str(common.REPO_ROOT),
                               "-c", "core.quotePath=false", *args],
                              capture_output=True, text=True, timeout=30, env=env)

    def _git_capped(self, args: list[str], cap: int) -> tuple[int, str, bool]:
        """Run a read-only git command but read at most cap+1 bytes of stdout,
        then stop git — so a giant blob or diff is never buffered whole into
        memory (the post-hoc size checks allocated the full output first).
        Returns (returncode, text<=cap, truncated)."""
        env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
        proc = subprocess.Popen(["git", "-C", str(common.REPO_ROOT),
                                 "-c", "core.quotePath=false", *args],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        try:
            data = proc.stdout.read(cap + 1)
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        truncated = len(data) > cap
        # if we got output, the object/rev exists (success), even when we
        # terminated git mid-stream; only "no output at all" is a real failure
        rc = 0 if data else (proc.returncode or 1)
        return rc, data[:cap].decode(errors="replace"), truncated

    def _vc_path(self, request: web.Request):
        """Validate ?path= for history queries: in-repo, versioned, no secrets,
        and not a git pathspec-magic string."""
        rel = request.query.get("path", "")
        p = common.resolve_repo_path(rel)
        if p is None:
            return None, web.json_response({"error": "bad path"}, status=400)
        rel = str(p.relative_to(common.REPO_ROOT.resolve()))
        # reject pathspec magic outright (defence in depth beside LITERAL_PATHSPECS):
        # magic is signalled by a leading ':' on any path component.
        if any(seg.startswith(":") for seg in rel.split("/")):
            return None, web.json_response({"error": "bad path"}, status=400)
        if not common.is_versioned_path(rel):
            return None, web.json_response(
                {"error": "history covers documents (.md) and artifacts (.html) only"}, status=400)
        return rel, None

    async def _vc_gate(self, request: web.Request):
        """Common identity + per-path permission gate. Returns (user, rel) or
        an error response."""
        user = self._vc_identity(request)
        if not user:
            return None, None, web.Response(status=403, text="no identity")
        rel, err = self._vc_path(request)
        if err is not None:
            return None, None, err
        loop = asyncio.get_event_loop()
        if not await loop.run_in_executor(None, self._can_read_as, user, rel):
            return None, None, web.json_response({"error": "forbidden"}, status=403)
        return user, rel, None

    async def vc_log(self, request: web.Request) -> web.Response:
        _user, rel, err = await self._vc_gate(request)
        if err is not None:
            return err
        try:
            limit = max(1, min(int(request.query.get("limit", "50")), 200))
        except ValueError:
            limit = 50
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, self._git_ro,
            ["log", "-n", str(limit), "--format=%H%x1f%an%x1f%at%x1f%s", "--", rel])
        entries = []
        for line in r.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                entries.append({"rev": parts[0][:12], "author": parts[1],
                                "ts": int(parts[2]), "subject": parts[3]})
        return web.json_response({"path": rel, "entries": entries})

    async def vc_show(self, request: web.Request) -> web.Response:
        _user, rel, err = await self._vc_gate(request)
        if err is not None:
            return err
        rev = request.query.get("rev", "")
        if not self._REV_RE.match(rev):
            return web.json_response({"error": "bad rev"}, status=400)
        loop = asyncio.get_event_loop()
        cap = 5 * 1024 * 1024
        rc, content, truncated = await loop.run_in_executor(
            None, self._git_capped, ["show", f"{rev}:{rel}"], cap)
        if rc != 0:
            return web.json_response({"error": "no such version of this file"}, status=404)
        if truncated:
            return web.json_response({"error": "version too large to display"}, status=413)
        return web.json_response({"path": rel, "rev": rev, "content": content})

    async def vc_diff(self, request: web.Request) -> web.Response:
        _user, rel, err = await self._vc_gate(request)
        if err is not None:
            return err
        rev = request.query.get("rev", "")
        if not self._REV_RE.match(rev):
            return web.json_response({"error": "bad rev"}, status=400)
        loop = asyncio.get_event_loop()
        cap = 2 * 1024 * 1024
        rc, patch, truncated = await loop.run_in_executor(
            None, self._git_capped, ["show", rev, "--format=", "--patch", "--", rel], cap)
        if rc != 0:
            return web.json_response({"error": "no such version"}, status=404)
        if truncated:
            patch += "\n… (diff truncated — too large to display in full)\n"
        return web.json_response({"path": rel, "rev": rev, "patch": patch})

    _HDR_RE = re.compile(r"^\x01[0-9a-f]{40}\x1f")

    @classmethod
    def _parse_activity(cls, stdout: str) -> list:
        """Parse `git log -z --name-status --format=%x01%H%x1f%an%x1f%at%x1f%s`.

        -z turns the stream into one flat run of NUL-separated tokens, and paths
        arrive VERBATIM — the whole point, see the -z note in vc_activity:

            \x01<sha>\x1f<author>\x1f<ts>\x1f<subject> NUL \nM NUL <path> NUL A NUL <path> NUL \x01…

        Two shapes to respect: the format's trailing newline lands at the FRONT
        of the first status token of each commit (never on the paths), and R/C
        carry a similarity score plus TWO paths (old, new) where every other
        status carries one.
        """
        commits: list = []
        toks = stdout.split("\0")
        i, n = 0, len(toks)
        cur = None
        while i < n:
            t = toks[i]
            # Match the whole header shape (\x01 + 40 hex + \x1f), not just the
            # marker byte, so a stray \x01 inside a commit subject can't fake one.
            if cls._HDR_RE.match(t):
                # maxsplit=3 keeps a subject containing \x1f from shifting fields.
                parts = t[1:].split("\x1f", 3)
                cur = {"rev": parts[0][:12], "author": parts[1], "ts": int(parts[2]),
                       "subject": parts[3] if len(parts) > 3 else "", "files": []}
                commits.append(cur)
                i += 1
                continue
            status = t.lstrip("\n")          # the format's newline, see above
            if not status or cur is None:
                i += 1
                continue
            want = 2 if status[:1] in ("R", "C") else 1
            paths = [q for q in toks[i + 1:i + 1 + want] if q]
            if paths:
                cur["files"].append({"status": status[:1], "paths": paths})
            i += 1 + want
        return commits

    async def vc_activity(self, request: web.Request) -> web.Response:
        """Who changed what, when — across every file the CALLER can read.
        Commits touching only files they can't read simply don't exist to them."""
        user = self._vc_identity(request)
        if not user:
            return web.Response(status=403, text="no identity")
        since = request.query.get("since", "1 day ago")
        until = request.query.get("until", "")
        author = request.query.get("author", "")
        if not self._DATE_RE.match(since) or (until and not self._DATE_RE.match(until)):
            return web.json_response({"error": "bad date (e.g. 'yesterday', '2 days ago', '2026-07-20')"},
                                     status=400)
        if author and not self._VCUSER_RE.match(author):
            return web.json_response({"error": "bad author"}, status=400)
        try:
            limit = max(1, min(1000, int(request.query.get("limit", 200))))
        except ValueError:
            return web.json_response({"error": "bad limit"}, status=400)
        # -z is LOAD-BEARING, not a tidy-up. Without it git renders any path
        # holding a non-ASCII byte (or a tab/quote/backslash) as a C-quoted,
        # octal-escaped string — `"projects/\360\237\246\203 Lumii dev/x.md"`,
        # quotes and all. That string is not a path any more, so the readability
        # check below `test -r`s a file that cannot exist, drops the row as
        # "you may not see this", and the feed reports a week of someone's work
        # on an emoji-named folder as "no visible changes". -z emits raw bytes
        # NUL-separated: never quoted, never escaped, and tabs in a filename
        # stop being ambiguous with the status separator.
        base = [f"--since={since}", "--format=%x01%H%x1f%an%x1f%at%x1f%s",
                "--name-status", "-z"]
        if until:
            base.append(f"--until={until}")
        if author:
            base.append(f"--author=^{author} <")
        loop = asyncio.get_event_loop()
        # PAGE through history — never one flat `-n N`. Editor autosaves commit
        # every few seconds, so the newest few hundred commits can be a single
        # afternoon: a flat cap silently shadows --since and reports a week of
        # someone's work as "nothing". Instead walk newest-first in batches
        # until `limit` VISIBLE rows exist or the window runs out, and say so
        # when we stopped early. MAX_SCAN bounds the work: each unseen path
        # costs one runuser readability check.
        rc: dict = {}
        out: list = []
        scanned = 0
        truncated = False
        # A deep window must come back PARTIAL, never hang: the CLI gives up at
        # 30s, and a timeout tells the caller nothing at all, whereas a short
        # answer flagged truncated=True is still a true answer.
        deadline = loop.time() + self._VC_BUDGET_S
        while len(out) < limit and scanned < self._VC_MAX_SCAN and loop.time() < deadline:
            args = ["log", "-n", str(self._VC_BATCH), "--skip", str(scanned), *base]
            r = await loop.run_in_executor(None, self._git_ro, args)
            commits = self._parse_activity(r.stdout)
            if not commits:
                break
            scanned += len(commits)
            # Resolve this batch's UNSEEN paths concurrently. Each check is a
            # runuser+test process (~25ms); doing them one after another is what
            # made a one-day window take 11s. Same kernel checks, same
            # per-request freshness — only the waiting overlaps. Bounded so a
            # deep scan can't fork a swarm of processes on a shared box.
            unseen = list(dict.fromkeys(
                p for c in commits for f in c["files"] for p in f["paths"] if p not in rc))
            if unseen:
                sem = asyncio.Semaphore(self._VC_CHECK_PARALLEL)

                async def _check(p: str) -> None:
                    async with sem:
                        await loop.run_in_executor(None, self._can_read_as, user, p, rc)

                await asyncio.gather(*(_check(p) for p in unseen))
            for i, c in enumerate(commits):
                # REDACT within each row: keep only the paths the caller can read,
                # so a rename row (paths=[old,new]) can never disclose the
                # never-readable side. A row with no readable path disappears.
                # The check is per unique path, fresh from the kernel, de-duped
                # within THIS request only (revocation takes effect immediately).
                files = []
                for f in c["files"]:
                    # every path in this batch is already resolved in rc
                    vis = [p for p in f["paths"] if rc.get(p)]
                    if vis:
                        files.append({**f, "paths": vis})
                if files:
                    out.append({**c, "files": files})
                    if len(out) >= limit:
                        # more only if this batch still had rows, or was full
                        truncated = i + 1 < len(commits) or len(commits) == self._VC_BATCH
                        break
            if len(commits) < self._VC_BATCH:
                break   # end of history inside the window — nothing left to page
        else:
            truncated = True   # hit MAX_SCAN or the time budget, window still open
        return web.json_response({"since": since, "until": until or None,
                                  "author": author or None, "limit": limit,
                                  "scanned": scanned, "truncated": truncated,
                                  "commits": out})


def make_app() -> web.Application:
    daemon = SyncDaemon()
    app = web.Application()
    app["daemon"] = daemon
    app.router.add_get("/health", daemon.health)
    app.router.add_get("/presence", daemon.presence_handler)
    # Hub-only control plane, main (0600 root) socket ONLY — deliberately absent
    # from vc_app below, which is world-connectable.
    app.router.add_post("/invalidate", daemon.invalidate_handler)
    app.router.add_get("/epoch/{path:.*}", daemon.epoch_handler)
    app.router.add_get("/ws/doc/{path:.*}", daemon.ws_doc)
    app.router.add_get("/vc/log", daemon.vc_log)
    app.router.add_get("/vc/show", daemon.vc_show)
    app.router.add_get("/vc/diff", daemon.vc_diff)
    app.router.add_get("/vc/activity", daemon.vc_activity)

    async def on_startup(app: web.Application):
        stack = AsyncExitStack()
        await stack.enter_async_context(daemon.server)
        app["stack"] = stack
        # Second, world-connectable socket serving ONLY the /vc routes: the CLI
        # (kb-history, agents) talks here and is identified by SO_PEERCRED — the
        # kernel's word on the caller's uid. Every query still passes the
        # can-you-read-this-file-now gate, so 0666 exposes nothing extra.
        vc_app = web.Application()
        vc_app.router.add_get("/vc/log", daemon.vc_log)
        vc_app.router.add_get("/vc/show", daemon.vc_show)
        vc_app.router.add_get("/vc/diff", daemon.vc_diff)
        vc_app.router.add_get("/vc/activity", daemon.vc_activity)
        # presence too: a user's backend pushes it to their browser over
        # /api/events, identified by peer credentials like the /vc reads
        vc_app.router.add_get("/presence", daemon.presence_handler)
        if os.path.exists(common.VC_SOCK):
            os.unlink(common.VC_SOCK)
        vc_runner = web.AppRunner(vc_app)
        await vc_runner.setup()
        await web.UnixSite(vc_runner, str(common.VC_SOCK)).start()
        os.chmod(common.VC_SOCK, 0o666)
        app["vc_runner"] = vc_runner
        for name, factory in (("flush", daemon.flush_loop),
                              ("watch", daemon.watch_loop),
                              ("git", daemon.git_loop)):
            daemon.supervise(name, factory)
        # _lock_socket is not supervised: it is a one-shot that RETURNS, and its
        # only failure mode (chmod) is already caught inside it.
        daemon.tasks.append(asyncio.create_task(_lock_socket()))
        app["tasks"] = daemon.tasks

    async def _lock_socket():
        # run_app binds the socket after startup; tighten it to root-only once it exists.
        for _ in range(50):
            if os.path.exists(common.SYNCD_SOCK):
                try:
                    os.chmod(common.SYNCD_SOCK, 0o600)
                except OSError:
                    pass
                return
            await asyncio.sleep(0.02)

    async def on_cleanup(app: web.Application):
        for t in app.get("tasks", []):
            t.cancel()
        vc_runner = app.get("vc_runner")
        if vc_runner is not None:
            await vc_runner.cleanup()
        stack = app.get("stack")
        if stack is not None:
            await stack.aclose()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    common.RUN_DIR.mkdir(parents=True, exist_ok=True)
    # attribution drop-box: sticky + world-writable, like /tmp — each hint file
    # is owned by (and only readable by) its writer; syncd consumes them
    common.ATTRIB_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(common.ATTRIB_DIR, 0o1733)
    # SECURITY: .git is root-only. Its objects hold every committed version of
    # every file — group access would bypass file permissions and RLS wholesale
    # (a 0600 private doc's history would be world-extractable). Repo stats are
    # published to /run/kb/git-state.json instead of letting users run git.
    git_dir = common.REPO_ROOT / ".git"
    if git_dir.exists():
        os.chmod(git_dir, 0o700)
    # Sticky bit on the repo root: it is group-writable (kb-users create top-
    # level areas), but +t means only a file's OWNER (or root) may delete/rename
    # it — so a kb-users member can't remove the root-owned .gitignore belt or a
    # sibling's top-level file. Keeps the setgid + rwx bits (2775 -> 3775).
    try:
        rs = os.stat(common.REPO_ROOT)
        os.chmod(common.REPO_ROOT, (stat.S_IMODE(rs.st_mode) | stat.S_ISVTX))
    except OSError:
        pass
    # belt (the syncd pathspec exclusion is the wall): keep secrets out of
    # `git status` noise for root too
    gi = common.REPO_ROOT / ".gitignore"
    try:
        cur = gi.read_text() if gi.exists() else ""
        if "**/_secrets/" not in cur:
            gi.write_text(cur.rstrip("\n") + ("\n" if cur else "") + "**/_secrets/\n")
    except OSError:
        pass
    sock = str(common.SYNCD_SOCK)
    if os.path.exists(sock):
        os.unlink(sock)
    app = make_app()
    web.run_app(app, path=sock, print=None)
    try:
        os.chmod(sock, 0o600)
    except OSError:
        pass


if __name__ == "__main__":
    main()
