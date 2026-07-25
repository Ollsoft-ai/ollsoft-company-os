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
import hashlib
import math
import os
import pwd
import re
import stat
import subprocess
from pathlib import Path

import psycopg
from watchfiles import awatch


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
        out = subprocess.run(["getfacl", "-cE", "--absolute-names", "--", str(p)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return mode, [], [], [], []
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
NOLOGIN = {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", ""}
PERMS_RESCAN = 1.0  # seconds; fallback so chmod propagates even if inotify misses IN_ATTRIB


def embed(text: str) -> str:
    """Deterministic 64-dim signed-hash embedding. A stand-in for a real model;
    swapping in an embedding API is a one-function change. Returns pgvector text."""
    v = [0.0] * 64
    for tok in re.findall(r"\w+", text.lower()):
        h = hashlib.md5(tok.encode()).digest()
        v[h[0] % 64] += 1.0 if (h[1] & 1) else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return "[" + ",".join(f"{x / n:.6f}" for x in v) + "]"


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
        self.conn = psycopg.connect(f"dbname={common.PG_DB}", autocommit=False)
        self.root = common.REPO_ROOT.resolve()
        self._sig: dict[str, tuple] = {}   # path -> cheap stat signature, to skip unchanged files

    def rel(self, p: Path) -> str:
        return str(p.resolve().relative_to(self.root))

    def stat_row(self, p: Path):
        st = p.lstat()
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

    def upsert_file(self, cur, rel: str, p: Path):
        owner, group, mode, is_dir, size, mtime, ru, rg, xu, xg = self.stat_row(p)
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
        if rel.startswith(".") or "/." in rel:   # skip config trees (.claude, .git)
            return
        if common.is_secret_path(rel):           # secrets never enter the index
            return
        try:
            text = p.read_text(errors="replace")
        except OSError:
            return
        with self.conn.cursor() as cur:
            self.upsert_file(cur, rel, p)
            cur.execute("DELETE FROM kb.blocks WHERE file_path=%s", (rel,))
            for b in parse_blocks(text):
                cur.execute(
                    "INSERT INTO kb.blocks(file_path,line,kind,checked,block_ref,text,"
                    "assignees,tags,tsv,embedding) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,to_tsvector('english',%s),%s::vector)",
                    (rel, b["line"], b["kind"], b["checked"], b["ref"], b["text"],
                     b["assignees"], b["tags"], b["text"], embed(b["text"])))
        self.conn.commit()

    def remove_path(self, p: Path):
        try:
            rel = self.rel(p)
        except (ValueError, OSError):
            return
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM kb.files WHERE path=%s OR path LIKE %s", (rel, rel + "/%"))
        self.conn.commit()

    def refresh_groups(self):
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE kb.user_groups")
            for u in pwd.getpwall():
                if u.pw_uid < 1000 or u.pw_shell in NOLOGIN:
                    continue
                for gid in os.getgrouplist(u.pw_name, u.pw_gid):
                    try:
                        gname = grp.getgrgid(gid).gr_name
                    except KeyError:
                        continue
                    cur.execute("INSERT INTO kb.user_groups(usr,grp) VALUES(%s,%s) "
                                "ON CONFLICT DO NOTHING", (u.pw_name, gname))
        self.conn.commit()

    def reindex_all(self):
        self.refresh_groups()
        seen = set()
        with self.conn.cursor() as cur:
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_secrets"]  # prune .claude/.git/_secrets
                dp = Path(dirpath)
                if dp != self.root:
                    self.upsert_file(cur, self.rel(dp), dp)
                    seen.add(self.rel(dp))
            self.conn.commit()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_secrets"]
            for fn in filenames:
                if fn.endswith(".md"):
                    p = Path(dirpath) / fn
                    self.reindex_file(p)
                    seen.add(self.rel(p))
        with self.conn.cursor() as cur:
            cur.execute("SELECT path FROM kb.files")
            for (path,) in cur.fetchall():
                if path not in seen:
                    cur.execute("DELETE FROM kb.files WHERE path=%s", (path,))
        self.conn.commit()

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
        service restarts — invisibly, since the file itself looks fine."""
        stale: list[Path] = []
        with self.conn.cursor() as cur:
            cur.execute("SELECT path, owner_name, group_name, mode, acl_users, acl_groups, "
                        "acl_x_users, acl_x_groups, size, mtime FROM kb.files")
            rows = cur.fetchall()
            for path, owner, group, mode, au, ag, axu, axg, size, mtime in rows:
                p = self.root / path
                try:
                    st = p.lstat()
                except OSError:
                    cur.execute("DELETE FROM kb.files WHERE path=%s", (path,))
                    self._sig.pop(path, None)
                    continue
                sig = (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), st.st_ctime_ns)
                if self._sig.get(path) == sig:
                    continue                          # unchanged -> skip getfacl
                self._sig[path] = sig
                try:
                    o, g, m, _is_dir, nsz, nmt, nru, nrg, nxu, nxg = self.stat_row(p)
                except OSError:
                    continue
                # Content changed under a missed inotify event? Re-parse it after
                # this sweep (reindex_file manages its own transaction).
                if (path.endswith(".md") and not stat.S_ISDIR(st.st_mode)
                        and (nsz != size or mtime is None or abs(nmt - mtime) > 1e-6)):
                    stale.append(p)
                if (o, g, m, nru, nrg, nxu, nxg) != (owner, group, mode, au or [], ag or [],
                                                     axu or [], axg or []):
                    cur.execute("UPDATE kb.files SET owner_name=%s, group_name=%s, mode=%s, "
                                "acl_users=%s, acl_groups=%s, acl_x_users=%s, acl_x_groups=%s, "
                                "updated_at=now() WHERE path=%s",
                                (o, g, m, nru, nrg, nxu, nxg, path))
        self.conn.commit()
        for p in stale:
            try:
                self.reindex_file(p)
            except Exception:
                self.conn.rollback()

    async def perms_loop(self):
        while True:
            await asyncio.sleep(PERMS_RESCAN)
            try:
                await asyncio.get_event_loop().run_in_executor(None, self.reconcile_perms)
            except Exception:
                pass

    async def watch_loop(self):
        async for changes in awatch(self.root, recursive=True, ignore_permission_denied=True):
            for _change, fspath in changes:
                p = Path(fspath)
                if p.name.startswith(".") or p.name.endswith(".kbtmp"):
                    continue
                try:
                    rel = self.rel(p)
                except (ValueError, OSError):
                    continue
                if rel.startswith(".") or "/." in rel:   # ignore .claude/.git subtrees
                    continue
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
                except Exception:
                    self.conn.rollback()

    async def groups_loop(self):
        # Pick up users/groups created or changed via the admin UI so RLS sees them.
        while True:
            await asyncio.sleep(5.0)
            try:
                await asyncio.get_event_loop().run_in_executor(None, self.refresh_groups)
            except Exception:
                self.conn.rollback()

    async def run(self):
        self.reindex_all()
        await asyncio.gather(self.watch_loop(), self.perms_loop(), self.groups_loop())


def main():
    idx = Indexer()
    asyncio.run(idx.run())


if __name__ == "__main__":
    main()
