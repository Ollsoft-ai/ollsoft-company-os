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
import time
from contextlib import AsyncExitStack
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="syncd %(message)s")
log = logging.getLogger("kb.syncd")

from aiohttp import WSMsgType, web
from diff_match_patch import diff_match_patch
from pycrdt import Text
from pycrdt.websocket import WebsocketServer
from watchfiles import awatch

from . import common

FLUSH_DEBOUNCE = 0.25   # seconds of quiet before writing a doc to disk
# Persisted Y.Doc states (one binary update per room). WHY THIS EXISTS: if the
# daemon restarts while browsers hold a doc open, a fresh empty room would be
# re-seeded from disk with NEW operation ids; the reconnecting clients then
# merge their OLD history on top and the CRDT keeps both — content doubles and
# interleaves mid-word on every restart. Resuming the saved state keeps the
# operation ids stable, so reconnects converge instead of duplicating.
DOC_STATE_DIR = Path("/var/lib/kb-syncd")
GIT_DEBOUNCE = 4.0      # seconds of quiet before an auto-commit


def _perm(path: Path, uid: int, gids: list[int], want: int) -> bool:
    """Kernel-exact access check on one object, symlinks refused."""
    try:
        st, entries = common.stat_and_acl(path)
    except OSError:
        return False                      # missing, or a symlink (ELOOP)
    return common.unix_access(st, entries, uid, gids, want)


def fs_can(path: Path, uid: int, gids: list[int], need_write: bool) -> bool:
    """Replicate FULL Unix access for a specific user, since root (this daemon)
    bypasses os.access. Kernel-exact including POSIX ACLs — the platform's own
    share feature grants by ACL, so bare mode bits both under-grant (named
    entries) and over-grant (the mask shows in st_mode's group bits). The
    caller must be able to read/write the file AND traverse (x) every ancestor
    directory up to REPO_ROOT — otherwise a world-readable file inside a 0700
    private dir would leak. Symlinks refused.
    """
    if not _perm(path, uid, gids, 2 if need_write else 4):
        return False
    # every ancestor directory, up to (but not including) the repo root, needs x
    root = common.REPO_ROOT.resolve()
    parent = path.resolve().parent
    while parent != root:
        if root not in parent.parents:
            return False  # escaped the repo tree
        if not _perm(parent, uid, gids, 1):
            return False
        parent = parent.parent
    return True


class AiohttpChannel:
    """Adapts an aiohttp WebSocketResponse to pycrdt.websocket's Channel protocol.

    `on_edit` (optional) fires whenever this channel delivers a document
    MUTATION message (SYNC_STEP2/SYNC_UPDATE) — that is how the daemon knows
    which verified user actually typed, for version-history attribution."""

    def __init__(self, ws: web.WebSocketResponse, path: str, on_edit=None):
        self._ws = ws
        self._path = path
        self._on_edit = on_edit

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        msg = await self._ws.receive()
        if msg.type == WSMsgType.BINARY:
            data = msg.data
            # YMessageType.SYNC = 0; SYNC_STEP2 = 1 / SYNC_UPDATE = 2 carry edits
            if self._on_edit and len(data) > 1 and data[0] == 0 and data[1] in (1, 2):
                self._on_edit()
            return data
        if msg.type == WSMsgType.TEXT:
            return msg.data.encode()
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            raise StopAsyncIteration
        return await self.__anext__()

    async def recv(self) -> bytes:
        try:
            return await self.__anext__()
        except StopAsyncIteration:
            raise ConnectionResetError

    async def send(self, message: bytes) -> None:
        await self._ws.send_bytes(message)


def _line_changes(base, other):
    out = []
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            out.append([i1, i2, other[j1:j2]])
    return out


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
        self.dmp = diff_match_patch()
        # Fuzzy patching must be CONSERVATIVE: prose is full of near-identical
        # tokens, and a hunk that anchors on the wrong lookalike injects
        # characters mid-word. Prefer dropping a hunk (the flush then reverts
        # that one external change) over guessing where it goes.
        self.dmp.Match_Threshold = 0.25
        self.dmp.Patch_DeleteThreshold = 0.25
        self.text_handles: dict[str, Text] = {}
        self.meta: dict[str, tuple[int, int, int]] = {}  # room -> (uid, gid, mode)
        self.last_written: dict[str, bytes] = {}
        self.seeded: set[str] = set()
        self.epochs: dict[str, str] = {}   # room -> CRDT lineage id
        # Who has which doc open, by HUB-VERIFIED identity (the token the hub
        # signed at connect time — not the client-claimed awareness name).
        # room -> {id(channel): username}
        self.presence: dict[str, dict[int, str]] = {}
        # Version-history attribution: who actually SENT edits to each room
        # since its last flush (room -> {user: last-edit ts}), and what each
        # flush window should be committed as (path -> author).
        self.recent_editors: dict[str, dict[str, float]] = {}
        self.dirty_docs: dict[str, str] = {}
        self.git_dirty = False

    def note_edit(self, name: str, user: str) -> None:
        self.recent_editors.setdefault(name, {})[user] = time.time()

    def _presence_add(self, name: str, key: int, user: str) -> None:
        self.presence.setdefault(name, {})[key] = user

    def _presence_drop(self, name: str, key: int) -> None:
        room = self.presence.get(name)
        if room is not None:
            room.pop(key, None)
            if not room:
                self.presence.pop(name, None)

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
        self.dirty_docs.pop(name, None)

    def _retire_room(self, name: str) -> None:
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
        self._retire_room_attr(name)   # don't leak attribution state for a gone room
        sp = self._state_path(name)
        for f in (sp, sp.with_suffix(".e")):
            try:
                f.unlink()
            except OSError:
                pass
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
        """Write content to the doc's file, preserving its current owner/group/mode.

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
            tmpname = f".{fname}.{os.urandom(6).hex()}.kbtmp"
            fd = os.open(tmpname, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=pfd)
            try:
                os.write(fd, data)
                os.fchown(fd, uid, gid)
                os.fchmod(fd, mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmpname, fname, src_dir_fd=pfd, dst_dir_fd=pfd)
        finally:
            os.close(pfd)
        self.last_written[name] = data
        return True

    async def flush_loop(self) -> None:
        # Poll every active room and persist any whose CRDT text has diverged from
        # disk. Polling (rather than trusting observer callbacks) is robust to
        # pycrdt observer-lifecycle quirks and is cheap at this scale.
        while True:
            await asyncio.sleep(FLUSH_DEBOUNCE)
            for name, txt in list(self.text_handles.items()):
                try:
                    content = str(txt)
                except Exception:
                    continue
                if content.encode() == self.last_written.get(name):
                    continue
                # The file moved or was deleted while open: never resurrect it at
                # the old path (that recreated it as root) — retire the room.
                if not (common.REPO_ROOT / name).exists():
                    self._retire_room(name)
                    continue
                try:
                    if self._atomic_write(name, content):
                        self.git_dirty = True
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
        Naive live-vs-disk diffing scattered char ops ("word salad"); dmp's
        fuzzy patch_apply was no better — it silently splits hunks into
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
        ops = self.dmp.diff_main(live, target)
        self.dmp.diff_cleanupSemantic(ops)
        with txt.doc.transaction():
            idx = 0
            for op, data in ops:
                if op == 0:
                    idx += len(data)
                elif op == -1:
                    del txt[idx:idx + len(data)]
                elif op == 1:
                    txt.insert(idx, data)
                    idx += len(data)
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

    async def git_loop(self) -> None:
        while True:
            await asyncio.sleep(GIT_DEBOUNCE)
            if not self.git_dirty:
                continue
            self.git_dirty = False
            docs = self.dirty_docs
            self.dirty_docs = {}
            await asyncio.get_event_loop().run_in_executor(None, self._git_commit, docs)

    _HINTS_PER_PASS = 2000    # DoS backstop: a flood of hint FILES past this is dropped
    _CHECKS_PER_PASS = 1000   # …and a global cap on write-checks (runuser spawns) per pass,
                              # so a hint with thousands of distinct fake paths can't stall
                              # the commit loop with a million subprocesses

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

    def _git_commit(self, docs: dict[str, str] | None = None) -> None:
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
            for path, author in (docs or {}).items():       # live-session identity outranks hints
                if author and common.is_versioned_path(path):
                    attrib[path] = author

            def commit_one(path: str, author: str, email: str) -> None:
                if common.is_secret_path(path):
                    return
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
                    return                                   # nothing actually staged for this path
                subprocess.run(["git", "-C", root, "-c", f"user.name={author}",
                                "-c", f"user.email={email}", "commit", "-q",
                                "-m", f"edit: {path}"], capture_output=True)

            for path in sorted(attrib):                      # attributed, one commit each
                commit_one(path, attrib[path], f"{attrib[path]}@kb.local")
            # the unattributed remainder — never lose history over missing
            # attribution. Sweep is one commit; its subject is generic and its
            # file list is permission-filtered by the activity endpoint.
            subprocess.run(["git", "-C", root, "add", "-A", "--",
                            ".", ":(exclude)**/_secrets/**",
                            ":(exclude)_secrets/**"], check=True)
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
        channel = AiohttpChannel(
            ws, rel,
            on_edit=(lambda: self.note_edit(rel, user)) if can_write else None)
        self._presence_add(rel, id(channel), user)
        try:
            if can_write:
                await self.server.serve(channel)
            else:
                await self.serve_readonly(room, channel)
        finally:
            self._presence_drop(rel, id(channel))
        return ws

    async def presence_handler(self, request: web.Request) -> web.Response:
        """Who has which doc open — filtered to docs the REQUESTING user can
        read (same fs_can gate as joining the room), so presence never leaks
        the existence or audience of files outside their permissions."""
        token = request.headers.get("X-KB-Auth")
        ident = common.read_token(self.key, token) if token else None
        if not ident:
            return web.Response(status=403, text="no identity")
        uid = int(ident["uid"])
        gids = [int(g) for g in ident["gids"]]
        out = {}
        for rel, members in list(self.presence.items()):
            if not members:
                continue
            if not fs_can(common.REPO_ROOT / rel, uid, gids, need_write=False):
                continue
            out[rel] = sorted(set(members.values()))
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
        return subprocess.run(["git", "-C", str(common.REPO_ROOT), *args],
                              capture_output=True, text=True, timeout=30, env=env)

    def _git_capped(self, args: list[str], cap: int) -> tuple[int, str, bool]:
        """Run a read-only git command but read at most cap+1 bytes of stdout,
        then stop git — so a giant blob or diff is never buffered whole into
        memory (the post-hoc size checks allocated the full output first).
        Returns (returncode, text<=cap, truncated)."""
        env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
        proc = subprocess.Popen(["git", "-C", str(common.REPO_ROOT), *args],
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
        args = ["log", "-n", "300", f"--since={since}",
                "--format=%x01%H%x1f%an%x1f%at%x1f%s", "--name-status"]
        if until:
            args.append(f"--until={until}")
        if author:
            args.append(f"--author=^{author} <")
        loop = asyncio.get_event_loop()
        r = await loop.run_in_executor(None, self._git_ro, args)
        commits, cur = [], None
        for line in r.stdout.splitlines():
            if line.startswith("\x01"):
                parts = line[1:].split("\x1f")
                cur = {"rev": parts[0][:12], "author": parts[1], "ts": int(parts[2]),
                       "subject": parts[3], "files": []}
                commits.append(cur)
            elif line.strip() and cur is not None:
                bits = line.split("\t")
                status_c = bits[0][:1]
                paths = [b for b in bits[1:] if b]
                cur["files"].append({"status": status_c, "paths": paths})
        # permission-filter: a file appears only if the caller can read it NOW.
        # One fresh check per unique path, de-duped within THIS request only.
        rc: dict = {}
        uniq = {p for c in commits for f in c["files"] for p in f["paths"]}
        readable = {}
        for p in uniq:
            readable[p] = await loop.run_in_executor(None, self._can_read_as, user, p, rc)
        out = []
        for c in commits:
            # REDACT within each row: keep only the paths the caller can read, so
            # a rename row (paths=[old,new]) can never disclose the never-readable
            # side. A row with no readable path disappears entirely.
            files = []
            for f in c["files"]:
                vis = [p for p in f["paths"] if readable.get(p)]
                if vis:
                    files.append({**f, "paths": vis})
            if files:
                out.append({**c, "files": files})
            if len(out) >= 200:
                break
        return web.json_response({"since": since, "until": until or None,
                                  "author": author or None, "commits": out})


def make_app() -> web.Application:
    daemon = SyncDaemon()
    app = web.Application()
    app["daemon"] = daemon
    app.router.add_get("/health", daemon.health)
    app.router.add_get("/presence", daemon.presence_handler)
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
        if os.path.exists(common.VC_SOCK):
            os.unlink(common.VC_SOCK)
        vc_runner = web.AppRunner(vc_app)
        await vc_runner.setup()
        await web.UnixSite(vc_runner, str(common.VC_SOCK)).start()
        os.chmod(common.VC_SOCK, 0o666)
        app["vc_runner"] = vc_runner
        app["tasks"] = [
            asyncio.create_task(daemon.flush_loop()),
            asyncio.create_task(daemon.watch_loop()),
            asyncio.create_task(daemon.git_loop()),
            asyncio.create_task(_lock_socket()),
        ]

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
