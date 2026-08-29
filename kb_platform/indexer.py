"""kb-indexer — builds the disposable Postgres index from the markdown tree.

Runs as the `kbindexer` service user (member of every content group, so it can
read all files) and owns the `kb` schema (the only writer). Everything it writes
is derived data: drop the schema and rerun and you get an identical index, which
is what keeps "markdown is truth, the DB is a cache" honest.

It denormalises each file's Unix owner/group/mode into kb.files so RLS can
re-evaluate the read check in SQL, and refreshes kb.user_groups from getent so
group membership is known to the policy.
"""
from __future__ import annotations

import asyncio
import grp
import logging
import os
import pwd
import re
import stat
import subprocess
import time
from pathlib import Path

import psycopg
from watchfiles import awatch

log = logging.getLogger("kb-indexer")
RETRY_BACKOFF = 60.0  # seconds between retries of a path whose indexing failed
MAX_TRACKED = 10_000  # cap on remembered failing paths, so a mass failure can't grow unbounded
MAX_PER_SWEEP = 200   # documents re-parsed per 1s sweep; the rest wait for the next one


def acl_info(p: Path) -> tuple[int, list[str], list[str], list[str], list[str]]:
    """Return (effective_mode, read_users, read_groups, x_users, x_groups) from POSIX ACLs.

    st_mode's group bits reflect the ACL *mask*, not the real group:: entry, so we
    recompute the true effective group perm. We also surface named entries that
    grant READ (for file-read RLS) and EXECUTE (for ancestor-directory traversal),
    so RLS mirrors the kernel AND the platform's ACL-based file sharing — including
    the traverse-only grants that make a shared file actually reachable.
    """
    st = p.lstat()
    mode = stat.S_IMODE(st.st_mode)
    try:
        r = subprocess.run(["getfacl", "-cE", "--absolute-names", "--", str(p)],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        # Fail CLOSED. On an ACL-bearing file st_mode's group bits are the MASK,
        # so returning `mode` unchanged would publish mask-as-group-permission
        # and widen the audience in RLS. Raising keeps the previous row and lets
        # the caller retry.
        raise OSError(f"getfacl failed for {p}: {e}") from e
    if r.returncode != 0:
        raise OSError(f"getfacl exited {r.returncode} for {p}: {r.stderr.strip()}")
    out = r.stdout
    group_perm = mask = None
    named_u, named_g = {}, {}
    for raw in out.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != 3:
            continue
        typ, name, perms = parts
        if typ == "group" and name == "":
            group_perm = perms
        elif typ == "mask":
            mask = perms
        elif typ == "user" and name:
            named_u[name] = perms
        elif typ == "group" and name:
            named_g[name] = perms
    if mask is None:
        return mode, [], [], [], []   # no extended ACL: st_mode is accurate
    mr, mw, mx = "r" in mask, "w" in mask, "x" in mask
    mode &= ~0o070
    if group_perm:
        gp = 0
        if "r" in group_perm and mr:
            gp |= 4
        if "w" in group_perm and mw:
            gp |= 2
        if "x" in group_perm and mx:
            gp |= 1
        mode |= gp << 3
    ru = sorted(n for n, pm in named_u.items() if "r" in pm and mr)
    rg = sorted(n for n, pm in named_g.items() if "r" in pm and mr)
    xu = sorted(n for n, pm in named_u.items() if "x" in pm and mx)
    xg = sorted(n for n, pm in named_g.items() if "x" in pm and mx)
    return mode, ru, rg, xu, xg

from . import common

TASK_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s*(.*?)\s*$")
REF_RE = re.compile(r"\s\^([A-Za-z0-9_-]+)\s*$")
ASSIGNEE_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_][A-Za-z0-9_-]*)")
TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_][A-Za-z0-9_-]*)")
PERMS_RESCAN = 1.0  # seconds; fallback so chmod propagates even if inotify misses IN_ATTRIB
# Advisory-lock key serialising kb.visible_files writers (this daemon and
# scripts/refresh_visibility.py). Arbitrary but must match on both sides.
VIS_LOCK = 0x6B62_5649  # "kbVI"


def compute_visibility(files_rows, group_rows, users) -> set[tuple[str, str]]:
    """The Python mirror of kb.can_read(): every (usr, path) pair the Unix read
    check grants, computed from the SAME state the SQL oracle reads (kb.files
    rows + kb.user_groups rows — NOT a fresh stat, so index and visibility can
    never disagree). kb.visible_files is what the RLS policies gate on; this
    function is why one hashed subplan replaces ~0.3 s of per-statement plpgsql.
    Any semantic change here must land in kb.can_read too — schema.sql says the
    same in the other direction, and the parity check is
    `SELECT count(*) FROM kb.visible_files v WHERE NOT kb.can_read(v.path)`.

    files_rows: (path, owner_name, group_name, mode, acl_users, acl_groups,
    acl_x_users, acl_x_groups) — the read check uses mode/owner/group plus the
    READ ACL columns; ancestor traversal uses mode x plus the TRAVERSE columns.
    Like the kernel (and kb._has), exactly one mode class applies — owner, else
    group, else other — and that class's denial is final."""
    files = {r[0]: r for r in files_rows}
    member: dict[str, set[str]] = {}
    for u, g in group_rows:
        member.setdefault(u, set()).add(g)

    def has(row, u, groups, nbit: int) -> bool:
        owner, group, mode = row[1], row[2], row[3]
        if owner == u:
            return bool(mode & (nbit << 6))
        if group in groups:
            return bool(mode & (nbit << 3))
        return bool(mode & nbit)

    out: set[tuple[str, str]] = set()
    for u in users:
        groups = member.get(u, set())
        reach: dict[str, bool] = {}   # dir -> it AND all its ancestors traverse

        def traversable(d: str) -> bool:
            """Iterative on purpose: kb.can_read walks ancestors in a plpgsql
            FOR loop with no depth limit, so recursing one Python frame per
            path component would diverge from the oracle by raising
            RecursionError on a deep tree — and that exception propagates out
            of the sweep, freezing visibility sync for EVERY user."""
            chain = []
            d0 = d
            while True:
                got = reach.get(d0)
                if got is not None:
                    break
                row = files.get(d0)
                # a missing ancestor row denies, exactly like can_read's NOT FOUND
                got = row is not None and (has(row, u, groups, 1)
                                           or u in (row[6] or [])
                                           or bool(groups & set(row[7] or [])))
                if not got or "/" not in d0:
                    break               # denied, or reached a top-level dir
                chain.append(d0)        # depends on its parent: resolve upward
                d0 = d0.rsplit("/", 1)[0]
            for anc in reversed(chain):
                reach[anc] = got
            reach[d0] = got
            return reach[d]

        for path, row in files.items():
            if not (has(row, u, groups, 4) or u in (row[4] or [])
                    or groups & set(row[5] or [])):
                continue
            if "/" in path and not traversable(path.rsplit("/", 1)[0]):
                continue
            out.add((u, path))
    return out


def parse_blocks(text: str) -> list[dict]:
    out = []
    for i, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not s:
            continue
        ref = None
        mref = REF_RE.search(s)
        if mref:
            ref = mref.group(1)
        mt = TASK_RE.match(raw)
        if mt:
            body = REF_RE.sub("", mt.group(2)).strip()
            assignees = sorted(set(ASSIGNEE_RE.findall(body)))
            tags = sorted(set(TAG_RE.findall(body)))
            out.append({"line": i, "kind": "task", "checked": mt.group(1).lower() == "x",
                        "ref": ref, "text": body, "assignees": assignees, "tags": tags})
        elif s.startswith("#"):
            out.append({"line": i, "kind": "heading", "checked": None, "ref": ref,
                        "text": s.lstrip("#").strip(), "assignees": [], "tags": []})
        else:
            out.append({"line": i, "kind": "text", "checked": None, "ref": ref, "text": s,
                        "assignees": [], "tags": []})
    return out


class Indexer:
    def __init__(self):
        self._conn = psycopg.connect(f"dbname={common.PG_DB}", autocommit=False)
        self.root = common.REPO_ROOT.resolve()
        self._sig: dict[str, tuple] = {}   # path -> cheap stat signature, to skip unchanged files
        self._warned: set[str] = set()          # paths already logged as failing (log once per streak)
        self._retry_after: dict[str, float] = {}  # transient failure -> next attempt (monotonic)
        # Path -> the stat signature it last FAILED at. While a path's signature
        # is unchanged there is nothing new to try, so it is skipped outright:
        # without this a file the indexer may not read (someone's private note)
        # costs a getfacl fork and an open() every second, forever.
        self._failed_at: dict[str, tuple] = {}
        # One writer at a time on self.conn: watch_loop runs in the event loop
        # while the sweeps run in an executor thread, and interleaved
        # commit/rollback on a shared transaction loses or half-applies writes.
        self._db = asyncio.Lock()
        self._groups_cache = None   # last (usr, grp) set refresh_groups synced
        # Last (usr, path) set refresh_visibility synced. MUST be dropped
        # whenever a kb.files row is deleted: kb.visible_files has an
        # ON DELETE CASCADE FK to it, so the delete is a SECOND writer that
        # empties visibility rows behind this cache's back. Without the reset,
        # a delete+recreate inside one sweep window (mv out and back, a
        # revert, an rsync restore) recomputes the SAME set, hits the
        # equality early-return, and leaves the file with NO visibility rows —
        # invisible to every user, in search and in the To-dos view, until an
        # unrelated permission change happens to force a full diff.
        self._vis_cache = None

    @property
    def conn(self) -> psycopg.Connection:
        """The one long-lived connection, re-opened if Postgres went away.

        This daemon holds a single connection from __init__ to shutdown and
        every write path reuses it, so a Postgres restart used to wedge the
        process for good: psycopg raises OperationalError("the connection is
        closed") on every later use, each `except` here only rolls back, and
        nothing reconnected. On 2026-08-29 a security upgrade restarted
        Postgres at 06:16 and this service logged 5134 failed writes over the
        next hour and three quarters — still `active (running)`, NRestarts
        unchanged, silently indexing nothing. A restart by hand was the only
        cure, and only until the next time Postgres bounces.

        Reconnecting behind the attribute covers every call site at once,
        including the ones that only open a cursor. Whatever was uncommitted
        in the dead transaction is lost either way — the point is that the
        NEXT write succeeds instead of failing forever. The sweep re-derives
        anything missed; this index is disposable by design.
        """
        if self._conn.closed:
            log.warning("postgres connection closed; reconnecting")
            self._conn = psycopg.connect(f"dbname={common.PG_DB}", autocommit=False)
        return self._conn

    def _note_failure(self, rel: str, err: Exception):
        """A path exists but could not be indexed. Log ONCE per failure streak —
        the index quietly missing content is exactly the failure mode that
        historically went unnoticed for days.

        "Permission denied" is NOT such a failure: users' private files are
        meant to be closed to this service account. Those are noted once at
        debug level and simply left out of the index, the same answer the
        kernel gives everyone else."""
        if rel in self._warned:
            return
        self._warned.add(rel)
        if isinstance(err, PermissionError):
            log.debug("not indexable (permission denied, by design): %s", rel)
        else:
            log.warning("cannot index %s (%s: %s) — it stays missing/stale in the "
                        "search index until this is fixed; will keep retrying",
                        rel, type(err).__name__, err)

    def _sig_of(self, p: Path) -> tuple | None:
        try:
            st = p.lstat()
        except OSError:
            return None
        return (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), st.st_ctime_ns)

    def _defer(self, rel: str, err: Exception, sig: tuple | None = None):
        """Record that `rel` could not be indexed. It is retried when its stat
        signature changes (a permission change bumps ctime) and, for transient
        faults only, on a timer as well."""
        self._note_failure(rel, err)
        if len(self._failed_at) < MAX_TRACKED or rel in self._failed_at:
            self._failed_at[rel] = sig if sig is not None else self._sig_of(self.root / rel)
        if isinstance(err, PermissionError):
            # Not a transient fault — a boundary. Nothing to retry on a timer.
            self._retry_after.pop(rel, None)
            return
        if len(self._retry_after) >= MAX_TRACKED and rel not in self._retry_after:
            return
        self._retry_after[rel] = time.monotonic() + RETRY_BACKOFF

    def _skip(self, rel: str, sig: tuple | None, now: float) -> bool:
        """True if this path failed before at exactly this signature and its
        retry timer (if any) has not expired — nothing has changed, so trying
        again would only burn syscalls."""
        if rel not in self._failed_at:
            return False
        if self._failed_at[rel] != sig:
            return False                       # something changed: try again
        if rel in self._retry_after:
            return now < self._retry_after[rel]    # transient: wait out the timer
        return True                            # a boundary: nothing to wait for

    def _note_success(self, rel: str):
        self._retry_after.pop(rel, None)
        self._failed_at.pop(rel, None)
        if rel in self._warned:
            self._warned.discard(rel)
            log.info("recovered: %s indexed", rel)

    def _forget(self, rel: str):
        """Path is gone — drop every trace so these maps track live files only."""
        self._sig.pop(rel, None)
        self._retry_after.pop(rel, None)
        self._failed_at.pop(rel, None)
        self._warned.discard(rel)

    def rel(self, p: Path) -> str:
        return str(p.resolve().relative_to(self.root))

    def stat_row(self, p: Path, st: os.stat_result | None = None):
        st = st if st is not None else p.lstat()
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            owner = str(st.st_uid)
        try:
            group = grp.getgrgid(st.st_gid).gr_name
        except KeyError:
            group = str(st.st_gid)
        eff_mode, ru, rg, xu, xg = acl_info(p)
        return (owner, group, eff_mode, stat.S_ISDIR(st.st_mode),
                st.st_size, st.st_mtime, ru, rg, xu, xg)

    def upsert_file(self, cur, rel: str, p: Path, st: os.stat_result | None = None):
        owner, group, mode, is_dir, size, mtime, ru, rg, xu, xg = self.stat_row(p, st)
        cur.execute(
            "INSERT INTO kb.files(path,owner_name,group_name,mode,is_dir,size,mtime,"
            "acl_users,acl_groups,acl_x_users,acl_x_groups,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) "
            "ON CONFLICT(path) DO UPDATE SET owner_name=EXCLUDED.owner_name, "
            "group_name=EXCLUDED.group_name, mode=EXCLUDED.mode, is_dir=EXCLUDED.is_dir, "
            "size=EXCLUDED.size, mtime=EXCLUDED.mtime, acl_users=EXCLUDED.acl_users, "
            "acl_groups=EXCLUDED.acl_groups, acl_x_users=EXCLUDED.acl_x_users, "
            "acl_x_groups=EXCLUDED.acl_x_groups, updated_at=now()",
            (rel, owner, group, mode, is_dir, size, mtime, ru, rg, xu, xg))

    def reindex_file(self, p: Path):
        # Never index symlinks: their lstat metadata (owner=creator, mode 0777)
        # would clobber the real target's indexed permissions, and a link out of
        # the repo would crash rel(). The real target is indexed on its own.
        if p.is_symlink() or p.suffix != ".md" or not p.is_file():
            return
        try:
            rel = self.rel(p)
        except (ValueError, OSError):
            return
        if common.is_hidden_rel(rel):            # skip dot machinery (.claude, .git) — but
            return                               # NOT derived sidecars (.x.docx.md), which
        if common.is_secret_path(rel):           # are the one indexable dot-file
            return
        # A read failure (e.g. an ACL mask silently zeroing kbindexer's grant)
        # must surface AND be retried — swallowing it here is how a file once
        # stayed out of the index for weeks with the service looking healthy.
        # Leave nothing stale behind: blocks parsed when the file WAS readable
        # would otherwise keep answering searches with content nobody can
        # re-verify (and that may since have changed completely).
        # Stat BEFORE reading: recording size/mtime from after the read would
        # stamp the row with a newer version than the text it stores, and the
        # sweep's staleness test (size/mtime differ) would then never fire —
        # a write landing mid-read would be invisible until the next restart.
        try:
            pre = p.lstat()
            text = p.read_text(errors="replace")
        except OSError:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM kb.blocks WHERE file_path=%s", (rel,))
            self.conn.commit()
            # Publish the PERMISSIONS too, right now. Losing read access is the
            # usual reason we are here (someone made the file private), and RLS
            # decides from kb.files' columns — but the sweep will not refresh
            # them for us: this very failure records the path in _failed_at at
            # its CURRENT signature, and _skip() then returns before reaching
            # the permission-refresh branch. Measured: kb.files kept serving the
            # old 0644 indefinitely after a chmod 600 (the row, i.e. the file's
            # existence/size/mtime, stayed visible to users who had just lost
            # access; content was already gone with the blocks above).
            # Separate transaction: a getfacl failure here must not roll back
            # the block deletion, which is the part that matters most.
            try:
                with self.conn.cursor() as cur:
                    self.upsert_file(cur, rel, p)
                self.conn.commit()
            except Exception:
                self.conn.rollback()   # sweep's seal path is the backstop
            raise
        with self.conn.cursor() as cur:
            self.upsert_file(cur, rel, p, pre)
            cur.execute("DELETE FROM kb.blocks WHERE file_path=%s", (rel,))
            for b in parse_blocks(text):
                cur.execute(
                    "INSERT INTO kb.blocks(file_path,line,kind,checked,block_ref,text,"
                    "assignees,tags,tsv) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,to_tsvector('english',%s))",
                    (rel, b["line"], b["kind"], b["checked"], b["ref"], b["text"],
                     b["assignees"], b["tags"], b["text"]))
        self.conn.commit()
        self._note_success(rel)

    def remove_path(self, p: Path):
        try:
            rel = self.rel(p)
        except (ValueError, OSError):
            return
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM kb.files WHERE path=%s OR path LIKE %s", (rel, rel + "/%"))
        self.conn.commit()
        self._vis_cache = None   # the FK cascade just deleted visibility rows
        for tracked in [rel, *[k for k in self._sig if k.startswith(rel + "/")]]:
            self._forget(tracked)

    def refresh_groups(self):
        desired = set()
        for u in pwd.getpwall():
            # NB: no nologin filter. "Viewer" accounts are given a nologin
            # shell on purpose (web access, no terminal) — skipping them
            # left them with zero group rows, so RLS denied them every
            # group-shared row while the kernel happily served the files.
            if u.pw_uid < 1000 or u.pw_uid >= 65000:
                continue
            for gid in os.getgrouplist(u.pw_name, u.pw_gid):
                try:
                    desired.add((u.pw_name, grp.getgrgid(gid).gr_name))
                except KeyError:
                    continue
        # Touch the table only when membership actually CHANGED. The old shape
        # — TRUNCATE + full reinsert on every 5 s tick — held ACCESS EXCLUSIVE
        # on a table kb.can_read() reads for every RLS row check, so every
        # user's queries stalled on every tick (3.8M lifetime inserts for 69
        # live rows on one box). Row-level DELETE/INSERT of the diff blocks
        # nobody, and the steady state is a pure in-memory comparison.
        if desired == self._groups_cache:
            return
        with self.conn.cursor() as cur:
            cur.execute("SELECT usr, grp FROM kb.user_groups")
            current = {(r[0], r[1]) for r in cur.fetchall()}
            for usr, g in current - desired:
                cur.execute("DELETE FROM kb.user_groups WHERE usr=%s AND grp=%s", (usr, g))
            for usr, g in desired - current:
                cur.execute("INSERT INTO kb.user_groups(usr,grp) VALUES(%s,%s) "
                            "ON CONFLICT DO NOTHING", (usr, g))
        self.conn.commit()
        self._groups_cache = desired

    def refresh_visibility(self):
        """Diff-sync kb.visible_files against compute_visibility(). Same shape
        as refresh_groups: steady state is a pure in-memory comparison, the
        table is touched only when the computed set actually changed — so the
        1 s sweep costs a couple ms of Python, not writes. Runs AFTER
        reconcile_perms in the sweep, so it reads the permission columns that
        sweep just refreshed: revocation reaches the policies in the same tick
        it reaches kb.files.

        The advisory lock serialises this against scripts/refresh_visibility.py
        (the manual/migration resync). Both do read-modify-write on the same
        table under READ COMMITTED, so without it a concurrent pair can compute
        their diffs from the same snapshot and re-INSERT pairs the other just
        revoked."""
        users = [u.pw_name for u in pwd.getpwall() if 1000 <= u.pw_uid < 65000]
        with self.conn.cursor() as cur:
            # The lock is transaction-scoped, so EVERY exit from here on must
            # end the transaction — an early `return` that just falls out would
            # hold it (and an idle-in-transaction snapshot) until some later
            # method happens to commit.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (VIS_LOCK,))
            cur.execute("SELECT path, owner_name, group_name, mode, acl_users, "
                        "acl_groups, acl_x_users, acl_x_groups FROM kb.files")
            files_rows = cur.fetchall()
            cur.execute("SELECT usr, grp FROM kb.user_groups")
            group_rows = cur.fetchall()
            desired = compute_visibility(files_rows, group_rows, users)
            if desired == self._vis_cache:
                self.conn.rollback()      # nothing to write; release the lock
                return
            cur.execute("SELECT usr, path FROM kb.visible_files")
            current = set(cur.fetchall())
            gone, new = current - desired, desired - current
            if gone:
                cur.executemany(
                    "DELETE FROM kb.visible_files WHERE usr=%s AND path=%s", sorted(gone))
            if new:
                # the file row is guaranteed to exist: `desired` was computed
                # from kb.files in this same transaction, and this connection
                # is the only writer of that table
                cur.executemany(
                    "INSERT INTO kb.visible_files(usr,path) VALUES(%s,%s) "
                    "ON CONFLICT DO NOTHING", sorted(new))
        self.conn.commit()
        self._vis_cache = desired
        if gone or new:
            log.info("visibility synced: +%d -%d pairs", len(new), len(gone))

    def _vacuum_blocks(self):
        """The startup walk rewrote every block (reindex_file is DELETE+INSERT),
        leaving ~a full table of dead tuples. Every content search is a
        sequential scan (see schema.sql on why no index is reachable under
        RLS), so that bloat is paid by every user on every search until vacuum
        reclaims it. Do it now, deterministically, instead of waiting for
        autovacuum's threshold. VACUUM can't run inside a transaction, so this
        uses its own autocommit connection; best-effort by design."""
        try:
            with psycopg.connect(f"dbname={common.PG_DB}", autocommit=True) as c:
                c.execute("VACUUM (ANALYZE) kb.blocks")
            log.info("post-walk vacuum of kb.blocks done")
        except Exception as e:
            log.warning("post-walk vacuum failed (autovacuum will catch up): %s", e)

    def reindex_all(self):
        self.refresh_groups()
        seen = set()
        errors = 0

        # Subtrees this walk could not enter. Their existing rows must survive:
        # from here an unreadable directory is indistinguishable from a deleted
        # one, and users' private folders are unreadable ON PURPOSE — treating
        # that as "gone" would wipe their rows on every restart.
        blind: list[str] = []

        def walk_err(e: OSError):
            nonlocal errors
            fn = getattr(e, "filename", None)
            try:
                rel = self.rel(Path(fn)) if fn else None
            except (ValueError, OSError):
                rel = None
            if rel:
                blind.append(rel)
            if isinstance(e, PermissionError):
                log.debug("walk: %s not readable (by design for private areas)", fn)
                return
            errors += 1
            log.warning("startup walk error at %s: %s", fn, e)

        dirs = []
        for dirpath, dirnames, filenames in os.walk(self.root, onerror=walk_err):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_secrets"]  # prune .claude/.git/_secrets
            dp = Path(dirpath)
            if dp != self.root:
                dirs.append(dp)
        for dp in dirs:
            # One directory per transaction. Catching only OSError/ValueError
            # let a psycopg error (a NOT NULL violation on an odd row, a lost
            # connection) escape and kill the daemon on startup — with
            # Restart=on-failure that is a crash-loop, i.e. no index at all.
            try:
                rel = self.rel(dp)
                with self.conn.cursor() as cur:
                    self.upsert_file(cur, rel, dp)
                self.conn.commit()
                seen.add(rel)
            except Exception as e:
                self.conn.rollback()
                errors += 1
                log.warning("cannot index dir %s: %s", dp, e)
        for dirpath, dirnames, filenames in os.walk(self.root, onerror=walk_err):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_secrets"]
            for fn in filenames:
                if fn.endswith(".md"):
                    p = Path(dirpath) / fn
                    try:
                        rel = self.rel(p)
                    except (ValueError, OSError):
                        continue
                    seen.add(rel)   # keep any existing row even if indexing fails
                    try:
                        self.reindex_file(p)
                    except Exception as e:
                        # One poison file must not take down the whole index
                        # (Restart=on-failure would crash-loop us into an empty DB).
                        self.conn.rollback()
                        if not isinstance(e, PermissionError):
                            errors += 1
                        self._defer(rel, e)
                        blind.append(rel)   # keep whatever row it already had
        self.conn.rollback()   # start the delete pass on a clean transaction
        with self.conn.cursor() as cur:
            cur.execute("SELECT path FROM kb.files")
            rows = [path for (path,) in cur.fetchall()]
            kept = 0
            for path in rows:
                if path in seen:
                    continue
                # Delete only what we could actually look at. A row under a
                # subtree the walk could not enter is presumed alive — that
                # blindness is normal (private folders) and deleting on it once
                # wiped live content when kbindexer briefly lost a group.
                if any(path == b or path.startswith(b + "/") for b in blind):
                    kept += 1
                    continue
                cur.execute("DELETE FROM kb.files WHERE path=%s", (path,))
                self._vis_cache = None   # FK cascade removed visibility rows
        self.conn.commit()
        log.info("startup reindex done: %d paths, %d errors, %d rows kept behind "
                 "unreadable paths", len(seen), errors, kept)

    def reconcile_perms(self):
        """Fallback sweep so filesystem changes propagate even if inotify missed
        them. Uses a cheap stat signature (incl. ctime, which every permission
        change AND every write bumps) to skip unchanged files, so the expensive
        work — getfacl, and re-parsing a document — runs only for files that
        actually changed.

        Covering CONTENT here, not just permissions, is load-bearing: watch_loop
        is the only other thing that re-parses a file, and inotify silently drops
        events when the kernel watch limit is hit or the queue overflows. Without
        this, one dropped event means a document's blocks stay stale until the
        service restarts — invisibly, since the file itself looks fine.

        A path's signature is recorded only AFTER it was processed successfully:
        recording it up front turned any single failure into a permanently stale
        file (never retried until its ctime happened to change again). A path
        that FAILED is remembered with the signature it failed at, so it is
        retried when something about it actually changes (a permission change
        bumps ctime) rather than once per second forever.

        Work per sweep is capped: one sweep must not hold the connection long
        enough to stall the watcher — whatever is left is simply picked up by
        the next one, a second later."""
        stale: list[tuple[Path, str, tuple]] = []
        new_files: list[str] = []
        now = time.monotonic()
        with self.conn.cursor() as cur:
            cur.execute("SELECT path, owner_name, group_name, mode, acl_users, acl_groups, "
                        "acl_x_users, acl_x_groups, size, mtime FROM kb.files")
            rows = cur.fetchall()
            rowpaths = {r[0] for r in rows}

            def discover(dirpath: Path, rel_dir: str):
                """A directory changed (or is the root): pick up children the
                watcher never delivered — inotify registration is best-effort,
                so this sweep is the guarantee that new files eventually index."""
                try:
                    entries = list(os.scandir(dirpath))
                except OSError:
                    return
                for ent in entries:
                    rel_child = f"{rel_dir}/{ent.name}" if rel_dir else ent.name
                    if rel_child in rowpaths \
                            or common.is_hidden_rel(rel_child) \
                            or common.is_secret_path(rel_child):
                        continue
                    try:
                        is_dir = ent.is_dir(follow_symlinks=False)
                        if self._skip(rel_child, self._sig_of(Path(ent.path)), now):
                            continue
                        if is_dir:
                            self.upsert_file(cur, rel_child, Path(ent.path))
                            rowpaths.add(rel_child)   # its own sweep row cascades deeper
                            self._note_success(rel_child)
                        elif ent.name.endswith(".md") and len(new_files) < MAX_PER_SWEEP:
                            new_files.append(rel_child)
                    except Exception as e:
                        # NOT just OSError: a psycopg error here used to escape
                        # the whole sweep, and since perms_loop only logs it,
                        # every later sweep died at the same entry.
                        self.conn.rollback()
                        self._defer(rel_child, e)

            discover(self.root, "")
            for path, owner, group, mode, au, ag, axu, axg, size, mtime in rows:
                p = self.root / path
                try:
                    st = p.lstat()
                except FileNotFoundError:
                    cur.execute("DELETE FROM kb.files WHERE path=%s", (path,))
                    self._vis_cache = None   # FK cascade removed visibility rows
                    self._forget(path)
                    continue
                except OSError as e:
                    # EACCES and friends: the file may well still exist, so
                    # deleting the row would drop real content (and cascade its
                    # blocks) over what might be a momentary loss of access.
                    # But its permission columns are now UNVERIFIABLE, and RLS
                    # trusts them as current — leaving them would keep serving
                    # a file in search under the access rules it had BEFORE it
                    # was locked down. So keep the row and seal it: mode 0 with
                    # no ACL grants denies everyone, including the owner, until
                    # a later sweep can stat it again and publish real values.
                    self._note_failure(path, e)
                    if mode != 0 or au or ag or axu or axg:
                        cur.execute(
                            "UPDATE kb.files SET mode=0, acl_users='{}', acl_groups='{}', "
                            "acl_x_users='{}', acl_x_groups='{}', updated_at=now() "
                            "WHERE path=%s", (path,))
                        log.warning("sealed %s in the index (cannot verify its "
                                    "permissions); it is hidden from search until "
                                    "it is readable again", path)
                        self._sig.pop(path, None)
                    continue
                sig = (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), st.st_ctime_ns)
                if self._sig.get(path) == sig and path not in self._failed_at:
                    continue                          # unchanged -> skip getfacl
                if self._skip(path, sig, now):
                    continue                          # failed at this exact state
                try:
                    o, g, m, _is_dir, nsz, nmt, nru, nrg, nxu, nxg = self.stat_row(p)
                except OSError as e:
                    self._defer(path, e, sig)
                    continue
                # Permissions are refreshed even when the CONTENT cannot be
                # indexed: RLS decides from these columns, so a revocation must
                # land regardless of whether the document itself is readable.
                if (o, g, m, nru, nrg, nxu, nxg) != (owner, group, mode, au or [], ag or [],
                                                     axu or [], axg or []):
                    cur.execute("UPDATE kb.files SET owner_name=%s, group_name=%s, mode=%s, "
                                "acl_users=%s, acl_groups=%s, acl_x_users=%s, acl_x_groups=%s, "
                                "updated_at=now() WHERE path=%s",
                                (o, g, m, nru, nrg, nxu, nxg, path))
                # Content changed under a missed inotify event (or a previous
                # index attempt failed)? Re-parse it after this sweep; the
                # signature is recorded only once that re-parse succeeds.
                is_doc = path.endswith(".md") and not stat.S_ISDIR(st.st_mode)
                if is_doc and (path in self._failed_at
                               or nsz != size or mtime is None or abs(nmt - mtime) > 1e-6):
                    if len(stale) < MAX_PER_SWEEP:
                        stale.append((p, path, sig))
                    continue                          # do NOT mark it healthy yet
                if _is_dir:
                    discover(p, path)   # entry count changed? find new children
                self._sig[path] = sig
                self._note_success(path)
        self.conn.commit()
        for rel_child in new_files:
            self._index_one(self.root / rel_child, rel_child, None)
        for p, path, sig in stale:
            self._index_one(p, path, sig)
        # Deferred paths with NO row yet (a new file whose very first index
        # attempt failed) are invisible to the row-driven loop above — retry
        # them here once something about them changes, or forget them if gone.
        for path in [k for k in list(self._failed_at) if k not in rowpaths]:
            p = self.root / path
            cur_sig = self._sig_of(p)
            if cur_sig is None and not p.exists():
                self._forget(path)
                continue
            if self._skip(path, cur_sig, now):
                continue
            self._index_one(p, path, cur_sig)

    def _index_one(self, p: Path, rel: str, sig: tuple | None):
        """(Re)index one path, recording success or scheduling a retry. A
        directory has no content to parse — it needs its ROW, and returning
        early from reindex_file used to count as success, dropping the row and
        with it every descendant's visibility."""
        try:
            if p.is_dir():
                with self.conn.cursor() as cur:
                    self.upsert_file(cur, rel, p)
                self.conn.commit()
            else:
                self.reindex_file(p)
            if sig is not None:
                self._sig[rel] = sig
            self._note_success(rel)
        except Exception as e:
            self.conn.rollback()
            self._defer(rel, e, sig)

    async def perms_loop(self):
        while True:
            await asyncio.sleep(PERMS_RESCAN)
            async with self._db:      # exclusive use of self.conn
                try:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self.reconcile_perms)
                    # visibility reads what the sweep just wrote — same tick,
                    # so a chmod/ACL change reaches the policies within ~1 s
                    await loop.run_in_executor(None, self.refresh_visibility)
                except Exception:
                    log.exception("reconcile sweep failed")
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass

    async def watch_loop(self):
        # Watch the three content roots, NOT the repo root: the recursive
        # registration aborts silently at the first unreadable directory
        # (ignore_permission_denied hides the error without resuming the walk),
        # and /srv/kb/.git is deliberately unreadable to kbindexer — watching
        # from the repo root left projects/ and users/ with NO watches at all.
        # reconcile_perms independently discovers anything a lost watch misses.
        roots = [p for n in ("company", "projects", "users")
                 if (p := self.root / n).is_dir()] or [self.root]
        async for changes in awatch(*roots, recursive=True, ignore_permission_denied=True):
            async with self._db:          # never write while a sweep is running
                self._handle_changes(changes)

    def _handle_changes(self, changes):
        for _change, fspath in changes:
            p = Path(fspath)
            if p.name.endswith(".kbtmp"):
                continue
            try:
                rel = self.rel(p)
            except (ValueError, OSError):
                continue
            if common.is_hidden_rel(rel):   # .claude/.git subtrees and dot-files —
                continue                    # except derived sidecars, which index
            if common.is_secret_path(rel):           # secrets stay out entirely
                continue
            try:
                if p.exists():
                    if p.suffix == ".md":
                        self.reindex_file(p)
                    elif p.is_dir():
                        with self.conn.cursor() as cur:
                            self.upsert_file(cur, self.rel(p), p)
                        self.conn.commit()
                else:
                    self.remove_path(p)
            except Exception as e:
                self.conn.rollback()
                self._defer(rel, e)   # reconcile_perms retries it on backoff

    async def groups_loop(self):
        # Pick up users/groups created or changed via the admin UI so RLS sees them.
        while True:
            await asyncio.sleep(5.0)
            async with self._db:
                try:
                    await asyncio.get_event_loop().run_in_executor(None, self.refresh_groups)
                except Exception:
                    log.exception("group refresh failed")
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass

    async def run(self):
        # The startup walk runs to completion BEFORE anything else touches the
        # connection: watch_loop (event loop) and the executor-run sweeps would
        # otherwise interleave commits and rollbacks on one transaction and
        # corrupt each other's writes. Nothing is missed by waiting — a file
        # created during the walk is picked up by reconcile_perms' discover().
        #
        # Visibility first, from the PRE-walk index state: kb.visible_files
        # survives restarts, but permissions may have drifted while we were
        # down, and the walk takes minutes — search must not serve a revoked
        # audience (or a blank one) for that long. During the walk itself
        # visibility updates only at its end, the same window in which inotify
        # watches aren't armed yet.
        try:
            self.refresh_groups()
            self.refresh_visibility()
        except Exception:
            log.exception("pre-walk visibility refresh failed")
            self.conn.rollback()
        self.reindex_all()
        try:
            self.refresh_visibility()
        except Exception:
            log.exception("post-walk visibility refresh failed")
            self.conn.rollback()
        self._vacuum_blocks()
        await asyncio.gather(self.watch_loop(), self.perms_loop(), self.groups_loop())


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    # watchfiles logs a line per debounced batch at INFO; under a bulk write or
    # a syncd auto-commit storm that alone floods the journal.
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
    idx = Indexer()
    asyncio.run(idx.run())


if __name__ == "__main__":
    main()
