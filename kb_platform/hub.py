"""kb-hub — the front door. Runs as root, listens on 127.0.0.1:8300.

Responsibilities (auth + proxy ONLY — no business logic):
  * Serve the login page and static assets.
  * Authenticate POST /login against PAM, set a signed session cookie.
  * On demand, spawn a per-user backend process running AS that OS user
    (`runuser -u <user>`), listening on a private unix socket.
  * Reverse-proxy authenticated HTTP + /pty WS to that user backend.
  * Reverse-proxy /ws/doc/* to kb-syncd, injecting a hub-signed identity token
    (user, uid, supplementary gids) so syncd can enforce Unix permissions.

The hub is the only component that ever handles a password.
"""
from __future__ import annotations

import asyncio
import base64
import grp
import json
import logging
import os
import pwd
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

from . import common, pam_auth, uploads
from . import settings as kbsettings

PLATFORM_ROOT = Path(os.environ.get("KB_PLATFORM_ROOT", "/opt/kb-platform"))
STATIC_DIR = PLATFORM_ROOT / "frontend" / "static"

_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer",
        "upgrade", "proxy-authorization", "proxy-authenticate", "content-length",
        "content-encoding"}

# Headers the hub ASSERTS to a backend. They must never survive from the client:
# proxy_http copies request.headers into a plain dict and only then sets
# X-KB-User, so a client sending the name in different case ("x-kb-user: bob")
# left BOTH entries in the outgoing request — the backend's case-insensitive
# lookup can then read the attacker's value. Harmless today (user_server trusts
# only the uid it actually runs as and treats the header as "claimed"), but it is
# a trust header on a proxy boundary; strip it on the way in.
_ASSERTED = {"x-kb-user", "x-kb-auth"}

# Members of this group may edit permissions of ANY file (platform admins);
# everyone else may only edit files they own. Configurable via KB_ADMIN_GROUP
# (see common.py) because the admin group is `sudo` on Debian/Ubuntu but
# `wheel` elsewhere.
ADMIN_GROUP = common.ADMIN_GROUP
_PERM_RE = re.compile(r"^[rwx]{1,3}$")
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")
_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")   # stricter for new PG-role users
_GROUP_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NOLOGIN_SHELLS = {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", ""}
# "viewer" accounts: full webapp, no execution. The login shell IS the switch
# (kernel-native, tracked in /etc/passwd); cron.deny closes the cron door,
# which ignores login shells (jobs run via /bin/sh).
VIEWER_SHELL = "/usr/sbin/nologin"
FULL_SHELL = "/bin/bash"
CRON_DENY = Path("/etc/cron.deny")


async def _read_capped(stream, cap: int) -> bytes | None:
    """Read a body to the end, or None if it is larger than `cap`.

    `StreamReader.read(n)` is NOT "read n bytes": it waits for the first chunk
    to land and returns whatever is buffered by then, up to n. Called once with
    a big cap it therefore truncates any body that arrives in more than one
    chunk — silently, because a short read is indistinguishable from a short
    body. That is exactly how a long dictation 502'd: the transcript spanned two
    chunks, the hub kept the first, and json.loads raised on the fragment. The
    bug scales with the payload, so it hides in testing and shows up on real
    input. Loop, always.
    """
    buf = bytearray()
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > cap:
            return None


# --- audit -------------------------------------------------------------------
# Every privileged mutation this daemon performs, on one greppable line.
#
# The hub is where the security boundary lives — it creates and deletes OS
# accounts, rewrites group rosters, and changes who can open a document — and it
# recorded none of it. `kb-history` answers "what did this document say", never
# "who changed who can read it", so questions like "who un-shared HR last
# Tuesday" had no answer anywhere on the box.
#
# journald rather than a file: it is already rotated, already survives restarts,
# already the place people look, and it needs no new moving part. Read it with
#
#     journalctl -u kb-hub -g AUDIT --since yesterday
#
# Failures are logged too — a denied attempt is the more interesting half when
# you are looking for someone probing.
# Without a handler, log.info() goes nowhere: the root logger defaults to
# WARNING and web.run_app(print=None) silences aiohttp's own output. The audit
# trail would have been written and then dropped on the floor. `hub` prefix so
# it is greppable next to syncd's lines in the same journal.
logging.basicConfig(level=logging.INFO, format="hub %(message)s")
log = logging.getLogger("kb.hub")


def _audit(event: str, actor: str | None, ok: bool = True, **fields) -> None:
    """One line per privileged mutation. Never raises: an audit failure must
    not be able to fail the operation it is describing.

    The first parameter is `event`, not `action`, and that is not cosmetic: with
    **fields, ANY name used here is stolen from callers. Naming it `action`
    made `_audit("group.member", admin, action=action)` raise
    "got multiple values for argument 'action'" — a TypeError inside the audit
    call, which turned an ordinary group change into a 500. The audit trail must
    never be able to break the thing it is recording.
    """
    try:
        detail = " ".join(f"{k}={v!r}" for k, v in fields.items() if v is not None)
        log.info("AUDIT %s actor=%s result=%s %s",
                 event, actor or "-", "ok" if ok else "DENIED", detail)
    except Exception:
        pass


# --- access audit (reads, not mutations) ------------------------------------
# The trail above answers "who changed who can see what". It could not answer
# "who opened it": a read left no trace anywhere on the box, so exfiltration by
# someone who legitimately HAD access was invisible — the likeliest shape of an
# incident here, since everyone is already authenticated.
#
# What is deliberately NOT recorded: every /api/* request. Tree listings,
# search, presence polling, autosaves and CRDT frames are the app breathing,
# not a person reaching for a document. Logging them would bury the signal and
# turn an audit trail into a surveillance stream, so only three acts count — a
# collaborative document being joined, an attachment's bytes being served, and
# a whole folder leaving as one archive.
#
#     journalctl -u kb-hub -g 'AUDIT (document.open|file.preview|file.(down|zip)load)'
#
# Read the events for exactly what they say. `document.open` means a session
# was joined; `file.preview`, `file.download` and `folder.download` mean the
# server sent the bytes. None proves anyone read, understood or saved anything.
ACCESS_EVENTS = ("document.open", "file.preview", "file.download", "folder.download")


def _audit_path(rel: str) -> str | None:
    """A raw request path -> the normalized repo-relative path, or None when it
    is not a path inside the repo.

    The same resolution the attachment handler and syncd use, so the line
    records the path that was actually served rather than the caller's spelling
    of it — `../`, a doubled slash or an absolute path all normalize here, and
    anything escaping REPO_ROOT audits as nothing at all. The root itself is
    not a document, so it is None too.
    """
    p = common.resolve_repo_path(rel)
    if p is None:
        return None
    try:
        out = str(p.relative_to(common.REPO_ROOT.resolve()))
    except ValueError:                      # resolve_repo_path already refuses
        return None                         # this; belt and braces
    return out if out not in ("", ".") else None


def _range_from_zero(value: str) -> bool:
    """True for `Range: bytes=0-…`, the request that OPENS a media file.

    A browser plays a video or pages a PDF with a byte range per seek. Counting
    each of those as an access would write a dozen `file.preview` lines for one
    play; counting none of them would miss videos entirely. The range that
    starts at byte 0 is the open, so it is the one that is recorded.
    """
    v = value.replace(" ", "")
    if v[:6].lower() != "bytes=":
        return False
    return v[6:].split(",")[0].startswith("0-")


def _access_event(request: web.Request, status: int) -> tuple[str, str] | None:
    """Classify a FINISHED /api/* round-trip as an attachment access, or None.

    Only `GET /api/attachment` and `GET /api/folder-zip` count, and only once
    the backend has actually served the bytes. That ordering is the whole
    point: the backend runs as the user, so the kernel has already ruled by the
    time a status exists, and a 401/403/404 can never be mistaken for a read.
    Every other route under /api — tree, search, artifacts, upload, vc,
    presence — returns None, as do a HEAD, a conditional 304 and a path that
    does not resolve into the repo.
    """
    if request.method != "GET":
        return None
    if request.path == "/api/folder-zip":
        # A folder leaving as one archive is the largest single read this box
        # can serve, so it is the one that most needs a line. `probe=1` builds
        # nothing and serves no bytes — it is the UI asking whether the
        # download is even possible, and recording it would log an intention
        # rather than an act.
        if status != 200 or request.query.get("probe") == "1":
            return None
        rel = _audit_path(request.query.get("path", ""))
        return ("folder.download", rel) if rel is not None else None
    if request.path != "/api/attachment":
        return None
    # 200 is a whole file served; 206 is a byte range, and only the opening one
    # counts (see _range_from_zero).
    if not (status == 200
            or (status == 206 and _range_from_zero(request.headers.get("Range", "")))):
        return None
    rel = _audit_path(request.query.get("path", ""))
    if rel is None:
        return None
    # Exactly the flag the attachment handler reads to decide
    # Content-Disposition, so the event cannot disagree with what was served.
    return ("file.download" if request.query.get("dl") == "1" else "file.preview"), rel


def _audit_access(event: str, actor: str, rel: str, source: str | None = None,
                  **safe) -> None:
    """One access line. Takes ALREADY-VALIDATED data only.

    By the time anything reaches here the hub has authenticated `actor`, the
    downstream service has authorized and served `rel`, and `rel` has been
    through _audit_path. This helper never sees the request, which is what
    keeps a query string, a cookie, an Authorization header or a user agent
    from leaking into the trail by accident: `safe` is for small numeric facts
    (bytes served), nothing else.

    `source` is the same trusted-proxy-derived value the `login` event already
    records — Cloudflare's, or nothing at all for a local caller.
    """
    _audit(event, actor, path=rel, **safe, source=source or None)


def _cron_deny_set(user: str, denied: bool) -> None:
    lines = [l for l in CRON_DENY.read_text().splitlines() if l.strip()] if CRON_DENY.exists() else []
    if denied and user not in lines:
        lines.append(user)
    if not denied:
        lines = [l for l in lines if l != user]
    CRON_DENY.write_text("\n".join(lines) + ("\n" if lines else ""))
    os.chmod(CRON_DENY, 0o644)
PROTECTED_USERS = common.PROTECTED_USERS
PROFILES_FILE = common.ETC_DIR / "profiles.json"


def _run(cmd: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, text=True, capture_output=True, timeout=25)


def _psql_su(sql: str) -> subprocess.CompletedProcess:
    """Run SQL as the postgres superuser (root -> runuser -u postgres)."""
    return _run(["runuser", "-u", "postgres", "--", "psql", "-d", common.PG_DB,
                 "-v", "ON_ERROR_STOP=1", "-c", sql])


def _profiles_load() -> dict:
    try:
        return json.loads(PROFILES_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _profiles_save(d: dict) -> None:
    PROFILES_FILE.write_text(json.dumps(d, indent=2))
    try:
        os.chmod(PROFILES_FILE, 0o600)
    except OSError:
        pass


def _groups_of(user: str) -> list[str]:
    e = pwd.getpwnam(user)
    names = {g.gr_name for g in grp.getgrall() if user in g.gr_mem}
    try:
        names.add(grp.getgrgid(e.pw_gid).gr_name)
    except KeyError:
        pass
    return sorted(names)


def _human_users() -> list[dict]:
    profs = _profiles_load()
    out = []
    for e in pwd.getpwall():
        # human accounts = uid range; a nologin shell no longer disqualifies —
        # that is exactly what a "viewer" account looks like
        if not 1000 <= e.pw_uid < 65000 or e.pw_name == "nobody":
            continue
        p = profs.get(e.pw_name, {})
        out.append({"username": e.pw_name, "uid": e.pw_uid,
                    "first": p.get("first", ""), "last": p.get("last", ""),
                    "email": p.get("email", ""), "groups": _groups_of(e.pw_name),
                    "admin": _is_admin(e.pw_name),
                    "shell": e.pw_shell not in NOLOGIN_SHELLS})
    return sorted(out, key=lambda x: x["username"])


def _shared_groups() -> list[dict]:
    """Platform/shared groups (gid>=1000), excluding per-user primary groups."""
    usernames = {e.pw_name for e in pwd.getpwall()}
    out = []
    for g in grp.getgrall():
        if not (1000 <= g.gr_gid < 65000) or g.gr_name in usernames:
            continue  # skip system groups (incl. nogroup) and per-user primary groups
        out.append({"name": g.gr_name, "gid": g.gr_gid, "members": sorted(g.gr_mem)})
    return sorted(out, key=lambda x: x["name"])


def _uid_gids(user: str) -> tuple[int, list[int]]:
    e = pwd.getpwnam(user)
    return e.pw_uid, os.getgrouplist(user, e.pw_gid)


def _fs_can(path: Path, uid: int, gids: list[int], need_write: bool) -> bool:
    """Evaluate a user's Unix read/write access to path (root can't rely on
    os.access). One adapter over common.can — which also walks the ancestors.
    This used to check the inode alone and so authorised reads the kernel
    refuses: world-readable files inside a project folder the caller cannot
    traverse."""
    return common.can(path, uid, gids, 2 if need_write else 4)


def _is_admin(user: str) -> bool:
    try:
        g = grp.getgrnam(ADMIN_GROUP)
    except KeyError:
        return False
    return user in g.gr_mem or pwd.getpwnam(user).pw_gid == g.gr_gid


def _owner_name(path: Path) -> str | None:
    try:
        return pwd.getpwuid(os.lstat(path).st_uid).pw_name
    except (OSError, KeyError):
        return None


def _owns_or_admin(path: Path, user: str) -> bool:
    return _owner_name(path) == user or _is_admin(user)


def _parse_acl(path: Path) -> list[dict]:
    """Return named user/group ACL entries (the shares) for display/editing."""
    try:
        out = subprocess.run(["getfacl", "-pE", "--", str(path)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    entries = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("#") or not line or line.startswith("default:"):
            continue
        parts = line.split(":")
        if len(parts) == 3 and parts[0] in ("user", "group") and parts[1]:
            entries.append({"type": parts[0], "name": parts[1], "perms": parts[2]})
    return entries


def _dir_traversable(dfd: int, st: os.stat_result, flag: str, name: str) -> bool:
    """Does user/group `name` already have execute (traverse) on the dir open
    at `dfd`? ACL-aware: named entries, the mask, and the real group:: entry all
    change the answer vs. bare mode bits. (Used to avoid a downgrading ACL.)"""
    entries = common.acl_entries(dfd)
    if flag == "u":
        e = pwd.getpwnam(name)
        return common.unix_access(st, entries, e.pw_uid,
                                  os.getgrouplist(name, e.pw_gid), 1)
    gid = grp.getgrnam(name).gr_gid
    # A hypothetical user whose only relevant group is `name`: uid -1 can never
    # match an owner, so evaluation falls to the group class, where every
    # matching entry is unioned and the mask applied. This was 35 lines of
    # hand-rolled kernel emulation that duplicated unix_access exactly.
    return common.unix_access(st, entries, -1, [gid], 1)


def _grp_ok(gid: int) -> bool:
    try:
        grp.getgrgid(gid)
        return True
    except KeyError:
        return False


_ACL_ACCESS = "system.posix_acl_access"
_ACL_DEFAULT = "system.posix_acl_default"


# ---- backend-wrapper reaping ------------------------------------------------
# One dedicated thread reaps EVERY runuser wrapper. The previous shape —
# run_in_executor(None, proc.wait) per spawn — parked one default-executor
# thread for each backend's ENTIRE lifetime. That pool is min(32, cores+4)
# (8 on a 4-core box) and is the same pool the hub's other blocking work uses,
# so the ~9th live backend starved the hub. One polling thread costs the same
# no matter how many backends exist; a 2 s poll is prompt enough for zombies.
_reap_lock = threading.Lock()
_reap_procs: list = []
_reap_thread = None


def _reap_later(proc) -> None:
    global _reap_thread
    with _reap_lock:
        _reap_procs.append(proc)
        if _reap_thread is None or not _reap_thread.is_alive():
            _reap_thread = threading.Thread(target=_reap_forever,
                                            daemon=True, name="backend-reaper")
            _reap_thread.start()


def _reap_forever() -> None:
    while True:
        time.sleep(2.0)
        with _reap_lock:
            # poll() reaps a finished child; the survivors stay on the list
            _reap_procs[:] = [p for p in _reap_procs if p.poll() is None]


def _acl_apply_fd(tfd: int, is_dir: bool, args: list[str]) -> None:
    """Apply an ACL change (`args` e.g. ['-m','u:bob:r'] or ['-x','u:bob']) to
    the file behind the pinned fd, race-free: run setfacl on a private root-only
    temp (reusing its mask/ordering logic), then copy the resulting ACL onto the
    fd via setxattr. Never operates on a re-traversable path, so a swapped symlink
    cannot redirect it to /etc/*."""
    import tempfile
    tmpfd, tmpf = tempfile.mkstemp(dir=str(common.RUN_DIR))
    os.close(tmpfd)
    try:
        try:
            os.setxattr(tmpf, _ACL_ACCESS, os.getxattr(tfd, _ACL_ACCESS))
        except OSError:
            os.chmod(tmpf, stat.S_IMODE(os.fstat(tfd).st_mode))
        subprocess.run(["setfacl", *args, "--", tmpf], check=True, capture_output=True, timeout=5)
        try:
            acl = os.getxattr(tmpf, _ACL_ACCESS)
        except OSError:
            # No extended ACL left — the args only touched base entries (u::/g::/o::)
            # or stripped the last named one. That is a real result, not a failure:
            # take the plain mode setfacl computed and drop any ACL still on the
            # target. Raising here 500'd every "share this folder with people who
            # can all edit", the most ordinary case there is.
            try:
                os.removexattr(tfd, _ACL_ACCESS)
            except OSError:
                pass
            os.fchmod(tfd, stat.S_IMODE(os.stat(tmpf).st_mode))
        else:
            os.setxattr(tfd, _ACL_ACCESS, acl)
    finally:
        os.unlink(tmpf)
    if is_dir:
        tmpd = tempfile.mkdtemp(dir=str(common.RUN_DIR))
        try:
            try:
                os.setxattr(tmpd, _ACL_DEFAULT, os.getxattr(tfd, _ACL_DEFAULT))
            except OSError:
                pass
            subprocess.run(["setfacl", "-d", *args, "--", tmpd], check=True, capture_output=True, timeout=5)
            try:
                os.setxattr(tfd, _ACL_DEFAULT, os.getxattr(tmpd, _ACL_DEFAULT))
            except OSError:
                try:
                    os.removexattr(tfd, _ACL_DEFAULT)
                except OSError:
                    pass
        finally:
            os.rmdir(tmpd)


# ---- people-centric sharing -------------------------------------------------
# The share panel speaks PEOPLE ("who can open this, view or edit"); the
# filesystem speaks owning groups, mode bits and POSIX ACLs. This block is the
# only translation between the two, and it picks the mechanism per object so
# that the everyday action — adding or removing one person — stays O(1):
#
#   folder + "can edit"  -> the folder's OWNING GROUP        (gpasswd, no walk)
#   folder + "can view"  -> a companion "-v" group bound by a named-group ACL
#                           (one recursive apply; membership is gpasswd after)
#   file   + anyone      -> a named user ACL on that one inode
#
# Groups the platform created carry MANAGED_PREFIX, which is what licenses us to
# rewrite their membership. A hand-made group (proj-*, team-*) is rewritten only
# from the folder that OWNS it (_audience_root); editing a subfolder that merely
# inherits it forks a fresh managed group instead, so "restrict this one
# subfolder" can never silently rewrite the whole project's access list.
MANAGED_PREFIX = "kbs-"
EVERYONE_GROUP = common.EVERYONE_GROUP
INDEXER_USER = "kbindexer"
_ROLES = ("view", "edit")


def _is_user_group(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _is_shared_group(name: str) -> bool:
    """A group that can legitimately carry a folder's audience: platform gid
    range, and not somebody's per-user primary group."""
    try:
        g = grp.getgrnam(name)
    except KeyError:
        return False
    return 1000 <= g.gr_gid < 65000 and not _is_user_group(name)


def _caller_may_rewrite(group: str, caller: str) -> bool:
    """May `caller` rewrite `group`'s membership? Only if they are already in it
    (or are a platform admin). Read live from NSS, not from the caller's process
    credentials: a backend keeps the group list runuser gave it at spawn, so a
    member added moments ago would otherwise fail this check until it restarts."""
    if not caller:
        return False
    if _is_admin(caller):
        return True
    try:
        return grp.getgrnam(group).gr_gid in _uid_gids(caller)[1]
    except KeyError:
        return False


def _group_members(name: str) -> set[str]:
    """Everyone the kernel counts as a member — secondary members AND anyone
    whose PRIMARY group this is (they never appear in gr_mem)."""
    try:
        g = grp.getgrnam(name)
    except KeyError:
        return set()
    return set(g.gr_mem) | {e.pw_name for e in pwd.getpwall() if e.pw_gid == g.gr_gid}


def _primary_members(name: str) -> set[str]:
    try:
        gid = grp.getgrnam(name).gr_gid
    except KeyError:
        return set()
    return {e.pw_name for e in pwd.getpwall() if e.pw_gid == gid}


def _slug(text: str) -> str:
    """A groupadd-safe stem from a folder name. Emoji and diacritics are the
    norm here ("⚕️ olingo-medical") and groupadd accepts neither."""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:20] or "share"


def _new_managed_group(hint: str) -> str:
    base = MANAGED_PREFIX + _slug(hint)
    name = base
    for n in range(2, 200):
        try:
            grp.getgrnam(name)
        except KeyError:
            break
        name = f"{base}-{n}"
    else:
        raise OSError("no free managed group name")
    if not _GROUP_RE.match(name):
        raise OSError(f"bad managed group name {name}")
    if _run(["groupadd", name]).returncode != 0:
        raise OSError(f"groupadd {name} failed")
    return name


def _set_group_members(group: str, want: set[str]) -> set[str]:
    """Make `group`'s membership exactly `want`. Returns the users whose
    membership actually changed: their running backend was started by runuser
    with the OLD supplementary gids, so until it is restarted the grant (or,
    worse, its removal) does not apply to them."""
    have = _group_members(group)
    # A primary-group membership lives in /etc/passwd, not /etc/group — gpasswd
    # -d cannot remove it and would just fail noisily.
    fixed = _primary_members(group)
    changed = set()
    for u in sorted(want - have):
        if _run(["gpasswd", "-a", u, group]).returncode == 0:
            changed.add(u)
    for u in sorted(have - want - fixed):
        if _run(["gpasswd", "-d", u, group]).returncode == 0:
            changed.add(u)
    return changed


def _restart_backends(users) -> None:
    """Drop those users' per-user backends. A backend's credentials are fixed by
    runuser at spawn, so a group change never reaches a running one; the hub
    respawns it on the next request with the new group list. Needed on REMOVAL
    too — a stale backend keeps serving what was just revoked."""
    for u in sorted(set(users)):
        if u == INDEXER_USER:
            continue
        _run(["pkill", "-KILL", "-u", u, "-f", "kb_platform.user_server"])


def _used_gids() -> set[int]:
    """Every group id anything in the repo is currently owned by. Walks
    EVERYTHING, `_secrets/` included — _walk_repo's exclusions exist to stop us
    granting access there, not to let us forget a folder that still depends on a
    group we are about to delete."""
    used = set()
    for dirpath, dirnames, filenames in os.walk(common.REPO_ROOT, followlinks=False):
        for n in [""] + dirnames + filenames:
            path = os.path.join(dirpath, n)
            try:
                used.add(os.lstat(path).st_gid)
            except OSError:
                continue
            # Named ACL groups count as "in use" too. A viewer group is NEVER an
            # owning group — it exists only as a named entry — so counting owners
            # alone reaped it the moment it was created and took every "can view"
            # grant down with it.
            for xattr in (_ACL_ACCESS, _ACL_DEFAULT):
                try:
                    raw = os.getxattr(path, xattr, follow_symlinks=False)
                except OSError:
                    continue
                for tag, _perm, qual in common.parse_acl_bytes(raw) or ():
                    if tag == common._ACL_GROUP:
                        used.add(qual)
    return used


def _reap_orphan_groups() -> list[str]:
    """Delete the groups the platform invented, once nothing carries them.

    Sharing materialises a group per folder, and the folder can then be deleted,
    re-shared or handed to a different audience — none of which the group hears
    about. Without this they accumulate in /etc/group and in the admin panel
    forever. One walk answers the question for every managed group at once, so
    this is cheap enough to run on a timer as well as after a change."""
    managed = [g for g in grp.getgrall() if g.gr_name.startswith(MANAGED_PREFIX)]
    if not managed:
        return []
    used = _used_gids() | {e.pw_gid for e in pwd.getpwall()}
    gone = []
    for g in managed:
        if g.gr_gid not in used and _run(["groupdel", g.gr_name]).returncode == 0:
            gone.append(g.gr_name)
    return gone


def _audience_root(rel: str, group: str) -> bool:
    """Is `rel` the topmost path carrying `group`? If an ancestor carries it too
    then `rel` merely INHERITS that audience and must not rewrite it."""
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return False
    parent = os.path.dirname(rel)
    while parent:
        try:
            if os.stat(common.REPO_ROOT / parent).st_gid == gid:
                return False
        except OSError:
            pass
        parent = os.path.dirname(parent)
    return True


def _named_effective(entries) -> tuple[dict, dict]:
    """Named ACL grants with the MASK already applied — what the kernel really
    gives, not what each entry claims on its own."""
    if not entries:
        return {}, {}
    mask = 7
    for tag, perm, _q in entries:
        if tag == common._ACL_MASK:
            mask = perm
    users, groups = {}, {}
    for tag, perm, qual in entries:
        if tag == common._ACL_USER:
            try:
                users[pwd.getpwuid(qual).pw_name] = perm & mask
            except KeyError:
                pass
        elif tag == common._ACL_GROUP:
            try:
                groups[grp.getgrgid(qual).gr_name] = perm & mask
            except KeyError:
                pass
    return users, groups


def _viewer_group(edit_group: str) -> str:
    """Companion read-only group for a folder. Two groups, because an inode has
    exactly one owning group but a folder routinely has both editors and
    viewers; binding the second by named-group ACL keeps BOTH roles O(1) to
    change afterwards."""
    return (edit_group[:29] + "-v") if not edit_group.endswith("-v") else edit_group


def _walk_repo(root: Path, secrets: bool = False):
    """Every non-symlink path under `root`, secrets excluded by default.

    Sharing a folder must never reach *sideways* into a `_secrets/` folder
    inside it: the folder is listable by the team on purpose, and a recursive
    grant would publish the very thing that must not be published. Sharing the
    `_secrets` FOLDER ITSELF is the one case where its contents are exactly
    what the owner means (`secrets=True`) — a share that stopped at the folder
    would grant a team the right to list credentials they still cannot read."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if (secrets or not common.is_secret_path(str(Path(dirpath) / d)))
                       and not os.path.islink(os.path.join(dirpath, d))]
        for n in dirnames:
            yield Path(dirpath) / n, True
        for n in filenames:
            f = Path(dirpath) / n
            if (not secrets and common.is_secret_path(str(f))) or f.is_symlink():
                continue
            yield f, False


def _share_state(p: Path, rel: str, user: str) -> dict:
    """Who can open this, as PEOPLE — the same question the panel asks, answered
    from the inode itself (owning group, mode, ACLs) rather than from any
    bookkeeping the platform would have to keep in sync."""
    st, entries = common.stat_and_acl(p)
    is_dir = stat.S_ISDIR(st.st_mode)
    mode = common.effective_mode(st, entries)
    owner = _owner_name(p) or str(st.st_uid)
    group = grp.getgrgid(st.st_gid).gr_name if _grp_ok(st.st_gid) else str(st.st_gid)
    named_u, named_g = _named_effective(entries)
    profs = _profiles_load()

    def label(u: str) -> str:
        pr = profs.get(u, {})
        return " ".join(x for x in (pr.get("first"), pr.get("last")) if x) or u

    named_people = {u for u, perm in named_u.items() if u != INDEXER_USER and perm & 4}
    named_groups = {g for g, perm in named_g.items() if perm & 4 and _is_shared_group(g)}
    if (mode & 0o004) or ((mode & 0o040) and group == EVERYONE_GROUP):
        scope = "everyone"
    elif (mode & 0o040) or named_people or named_groups:
        scope = "people"
    else:
        scope = "private"

    people = [{"user": owner, "name": label(owner), "role": "owner",
               "via": "owner", "fixed": True}]
    seen = {owner}

    def add(u: str, perm: int, via: str, src: str = ""):
        if u in seen or u == INDEXER_USER or not perm & 4:
            return
        seen.add(u)
        people.append({"user": u, "name": label(u),
                       "role": "edit" if perm & 2 else "view", "via": via, "source": src})

    if scope != "everyone" and (mode & 0o040) and group != EVERYONE_GROUP \
            and _is_shared_group(group):
        for m in sorted(_group_members(group)):
            add(m, (mode >> 3) & 7, "group", group)
    for g, perm in sorted(named_g.items()):
        if g == EVERYONE_GROUP or not _is_shared_group(g):
            continue
        for m in sorted(_group_members(g)):
            add(m, perm, "group", g)
    for u, perm in sorted(named_u.items()):
        add(u, perm, "person")

    # Is this thing just following the folder it sits in? If so the panel says
    # so and a Save that changes nothing changes nothing — without this, opening
    # a file that merely inherits its team and pressing Save would silently
    # convert it into a per-file grant list that stops tracking the folder.
    inherited = False
    if "/" in rel:
        try:
            inherited = (common.readership_key(st, entries)
                         == common.readership_key(*common.stat_and_acl(p.parent)))
        except OSError:
            pass

    vg = _viewer_group(group)
    return {
        "path": rel, "is_dir": is_dir, "scope": scope, "owner": owner,
        "inherited": inherited, "parent": os.path.dirname(rel),
        "owner_name": label(owner), "group": group,
        "managed": group.startswith(MANAGED_PREFIX),
        "audience_root": _audience_root(rel, group) if _is_shared_group(group) else True,
        "viewer_group": vg if vg in {g for g in named_g} else None,
        "secret": common.is_secret_path(rel),
        "people": people,
        # A secret is its owner's alone to widen — an admin is not the owner
        # here (see fs_share_set), so the panel must not offer them a Save the
        # hub will refuse.
        "can_edit": _owns_or_admin(p, user) and (owner == user
                                                 or not common.is_secret_path(rel)),
        "advanced": {"mode": oct(stat.S_IMODE(st.st_mode))[2:], "owner": owner,
                     "group": group, "acls": _parse_acl(p)},
    }


def _pin(rel: str):
    """(parent_fd, name, lstat) for `rel`, resolved without ever following a
    symlink — every mutation below acts on a pinned fd, so a path swapped mid
    call cannot redirect a root chown/chmod/setfacl somewhere else."""
    pfd = common.opendir_beneath(os.path.dirname(rel))
    name = os.path.basename(rel)
    lst = os.stat(name, dir_fd=pfd, follow_symlinks=False)
    return pfd, name, lst


def _grant_indexer(tfd: int, is_dir: bool) -> None:
    """Keep the search indexer able to READ a restricted file. Without this,
    "only me" silently means "not searchable, even by me": kb-indexer runs as
    kbindexer, and a file it cannot read has no rows in kb.blocks at all. RLS
    still decides who may SEE those rows, so this widens nothing."""
    _acl_apply_fd(tfd, is_dir, ["-m", f"u:{INDEXER_USER}:{'rx' if is_dir else 'r'}"])


def _rehome_tree(root: Path, old_gid: int, new_gid: int, dir_bits: int,
                 file_bits: int, indexer: bool = False, every: bool = False) -> int:
    """Bring the descendants that were FOLLOWING this folder along to its new
    audience.

    Only children whose group still matches the old one are touched — anything
    carrying a different group is a deliberate override (a private note, a
    sub-team folder) and is left exactly as it is.

    `every` drops that rule (and lets the walk enter the `_secrets` folder at
    all), and only a `_secrets` folder uses it. Inside one,
    a mismatched group is not somebody's decision: every key used to be born
    owner-and-primary-group owned no matter where it sat, so honouring that
    would mean sharing a secrets folder left the keys already in it invisible —
    which is the whole complaint. Sharing a folder of credentials shares the
    credentials in it.

    Both halves matter, and the second one is easy to miss: without the chgrp
    the new people can enter the folder and read nothing in it, and without
    rewriting the OTHER bits as well as the group bits a file that was
    company-readable stays company-readable inside a folder its owner has just
    restricted to three people.
    """
    n = 0
    for path, is_dir in _walk_repo(root, every):
        try:
            st = os.lstat(path)
            if st.st_gid != old_gid and not every:
                continue
            if new_gid != st.st_gid:
                os.chown(path, -1, new_gid, follow_symlinks=False)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW
                         | (os.O_DIRECTORY if is_dir else 0))
        except OSError:
            continue
        try:
            target = (stat.S_IMODE(st.st_mode) & 0o700) | (dir_bits if is_dir else file_bits)
            os.fchmod(fd, target)
            # A chmod through an ACL writes the MASK, not group::, so anything
            # carrying a named entry needs its base entries stated outright or
            # the old group permission quietly survives the restriction. Stating
            # them also re-raises the mask over the indexer grant, so the grant
            # goes on in the SAME call and nothing chmods after it.
            if indexer or common.acl_entries(fd) is not None:
                args = _base_args(target)
                if indexer:
                    args += ["-m", f"u:{INDEXER_USER}:{'rx' if is_dir else 'r'}"]
                _acl_apply_fd(fd, is_dir, args)
            n += 1
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            os.close(fd)
    return n



def _perm_str(bits: int) -> str:
    return ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")


def _base_args(mode: int) -> list[str]:
    """The three base ACL entries spelled out from a mode. Every setfacl below
    states them explicitly, because setfacl SYNTHESISES a base from whatever it
    finds when an entry is missing — and a default ACL invented that way carries
    `group::---`, which silently cuts the folder's own team out of every file
    created in it afterwards."""
    return ["-m", f"u::{_perm_str((mode >> 6) & 7)}",
            "-m", f"g::{_perm_str((mode >> 3) & 7)}",
            "-m", f"o::{_perm_str(mode & 7)}"]


def _acl_walk(root: Path, extra_dir: list[str], extra_file: list[str],
              secrets: bool = False) -> int:
    """Apply an ACL change across a subtree, each item keeping its OWN base
    entries (so a private note inside a shared folder stays private), secrets
    excluded, every inode pinned.

    This is the one O(files) step in the whole model. It runs when a viewer
    group is first bound to a folder; adding or removing a viewer afterwards is
    a gpasswd on that group and touches no files at all."""
    n = 0
    for path, is_dir in _walk_repo(root, secrets):
        try:
            st, entries = common.stat_and_acl(path)
            args = _base_args(common.effective_mode(st, entries)) + (extra_dir if is_dir else extra_file)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW
                         | (os.O_DIRECTORY if is_dir else 0))
        except OSError:
            continue
        try:
            _acl_apply_fd(fd, is_dir, args)
            n += 1
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            os.close(fd)
    return n


def _share_apply(p: Path, rel: str, data: dict, caller: str = "") -> dict:
    """Move an inode to the requested audience. Returns what actually happened —
    the panel reports it, and the caller uses `restart` to drop the backends
    whose group list just went stale."""
    scope = data.get("scope")
    if scope not in ("everyone", "people", "private", "inherit"):
        raise ValueError("scope must be everyone, people, private or inherit")
    # `_secrets/` used to refuse every scope but "private", which read as a
    # guarantee and was not one: the same grant could always be made through
    # /fs/props, and a team with a shared project had no way to hand each other
    # a credential at all. What actually contains a secret is elsewhere and is
    # untouched by this — never in git, never in the index (the indexer prunes
    # `_secrets` before it walks in), never through the CRDT relay. So the panel
    # shares them like anything else, with two differences kept below: the
    # indexer is never made a member of a secret's audience, and a `_secrets`
    # folder brings ALL of its contents along (see _rehome_tree).
    secret = common.is_secret_path(rel)
    # A `_secrets` FOLDER is where the decision lives: while it is unshared,
    # every key born inside it is owner-only no matter how open the folder
    # around it happens to be (see common.born_closed). Sharing it here is what
    # says otherwise — so the mark goes on with the share and comes off with it.
    secret_root = common.is_secret_root(rel)

    st0, entries0 = common.stat_and_acl(p)
    before_u, before_g = _named_effective(entries0)
    old_gid = st0.st_gid
    old_group = grp.getgrgid(old_gid).gr_name if _grp_ok(old_gid) else ""
    owner = _owner_name(p) or ""

    roles: dict[str, str] = {}
    for row in data.get("people") or []:
        u, role = str(row.get("user", "")), row.get("role")
        if not _NAME_RE.match(u) or role not in _ROLES:
            raise ValueError("bad person entry")
        pwd.getpwnam(u)
        if u not in (owner, INDEXER_USER):
            roles[u] = role
    editors = {u for u, r in roles.items() if r == "edit"}
    viewers = {u for u, r in roles.items() if r == "view"}

    pfd, name, lst = _pin(rel)
    if stat.S_ISLNK(lst.st_mode):
        os.close(pfd)
        raise ValueError("refusing to change a symlink")
    is_dir = stat.S_ISDIR(lst.st_mode)
    tfd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW
                  | (os.O_DIRECTORY if is_dir else 0), dir_fd=pfd)
    out = {"scope": scope, "changed": set(), "regrouped": 0, "acl_walked": 0,
           "group": old_group, "share_with": []}
    try:
        if scope == "inherit":
            if secret_root:
                common.set_secrets_shared(tfd, True)
            os.close(tfd)
            tfd = None
            # "Same as the folder it's in", asked for deliberately — the one
            # thing that may open a `_secrets` folder up to its project, which
            # is exactly what a team that shares credentials wants and could
            # not have before.
            common.reset_audience_tree(p, secret_root_follows=common.is_secret_root(rel))
            out["group"] = (grp.getgrgid(os.lstat(p).st_gid).gr_name
                            if _grp_ok(os.lstat(p).st_gid) else "")
            return out

        if scope == "private":
            if secret_root:
                common.set_secrets_shared(tfd, False)
            for attr in ((_ACL_ACCESS, _ACL_DEFAULT) if is_dir else (_ACL_ACCESS,)):
                try:
                    os.removexattr(tfd, attr)
                except OSError:
                    pass
            os.fchmod(tfd, 0o700 if is_dir else 0o600)
            if not secret:
                _grant_indexer(tfd, is_dir)
            if is_dir:
                out["regrouped"] = _rehome_tree(p, old_gid, old_gid, 0o2000, 0o000,
                                                indexer=not secret, every=secret)
            return out

        if scope == "everyone":
            if secret_root:
                common.set_secrets_shared(tfd, True)
            gid = grp.getgrnam(EVERYONE_GROUP).gr_gid
            mode = 0o2775 if is_dir else 0o664
            os.fchown(tfd, -1, gid)
            os.fchmod(tfd, mode)
            args = _base_args(mode)
            for u in sorted(editors):
                args += ["-m", f"u:{u}:{'rwx' if is_dir else 'rw'}"]
            for u in sorted(set(before_u) - editors - {INDEXER_USER}):
                args += ["-x", f"u:{u}"]
            _acl_apply_fd(tfd, is_dir, args)
            out["group"] = EVERYONE_GROUP
            if is_dir and gid != old_gid:
                out["regrouped"] = _rehome_tree(p, old_gid, gid, 0o2075, 0o064,
                                                every=secret)
                out["reaped"] = _reap_orphan_groups()
            return out

        # --- scope "people" --------------------------------------------------
        if not is_dir:
            # One inode, so no group is needed: named user ACLs are already O(1)
            # and they say exactly what the panel says.
            os.fchmod(tfd, 0o600)
            args = _base_args(0o600)
            for u in sorted(editors):
                args += ["-m", f"u:{u}:rw"]
            for u in sorted(viewers):
                args += ["-m", f"u:{u}:r"]
            if not secret:
                args += ["-m", f"u:{INDEXER_USER}:r"]
            for u in sorted(set(before_u) - editors - viewers - {INDEXER_USER}):
                args += ["-x", f"u:{u}"]
            for g in sorted(before_g):
                args += ["-x", f"g:{g}"]
            _acl_apply_fd(tfd, is_dir, args)
            out["share_with"] = [("u", u) for u in sorted(editors | viewers)]
            return out

        if secret_root:
            common.set_secrets_shared(tfd, bool(editors or viewers))
        # A folder: the owning group carries "can edit" so that adding a person
        # later is one gpasswd and no filesystem walk at all.
        # Mutate a group ONLY from the folder that owns it. Being a group we
        # created is not enough: a subfolder that merely inherited it would
        # rewrite the whole subtree's access list while the person thinks they
        # are restricting one folder.
        # SECURITY: reusing a group means REWRITING ITS ROSTER (_set_group_members
        # below makes membership exactly `members`). Authorization for that is
        # membership of the group, not ownership of this inode — otherwise owning
        # any throwaway folder, chgrp'ing it onto a hand-made team-*/proj-* group
        # and calling /fs/share rewrote that team's roster: attacker in, everyone
        # else out. (fs_props_set now refuses the chgrp too; this is the second
        # lock, and it also covers a group arriving by setgid inheritance, a move
        # or a restored backup rather than by an explicit chgrp.)
        # NOT gated on MANAGED_PREFIX on purpose: on a real deployment many
        # folders are their own audience root under a hand-made proj-*/team-*
        # group created before the sharing panel existed, and requiring
        # "kbs-" would fork them onto a new group and silently evict every member
        # the panel did not list. Membership is the check that closes the hole
        # without breaking that.
        reuse = (_is_shared_group(old_group) and old_group != EVERYONE_GROUP
                 and _audience_root(rel, old_group)
                 and _caller_may_rewrite(old_group, caller))
        g = old_group if reuse else _new_managed_group(os.path.basename(rel))
        out["group"] = g
        out["forked"] = not reuse and bool(old_group)
        # The owner belongs in their own folder's group. Without it the kernel
        # blocks them from chgrp'ing anything INTO the folder (you may only give
        # a file away to a group you belong to), so moving a note in left it on
        # its old group; and a file a teammate creates there is reachable to the
        # owner only through the group they were not in. Skip system owners —
        # files made with "New document" take the folder's owner, which under
        # company/ is root.
        members = editors | (set() if secret else {INDEXER_USER})
        if owner and lst.st_uid >= 1000:
            members.add(owner)
        out["changed"] |= _set_group_members(g, members)
        gid = grp.getgrnam(g).gr_gid
        if gid != old_gid:
            os.fchown(tfd, -1, gid)
        os.fchmod(tfd, 0o2770)

        vg = _viewer_group(g)
        bind_viewers = bool(viewers)
        if bind_viewers:
            try:
                grp.getgrnam(vg)
            except KeyError:
                if not _GROUP_RE.match(vg) or _run(["groupadd", vg]).returncode != 0:
                    raise OSError(f"groupadd {vg} failed")
            out["changed"] |= _set_group_members(
                vg, viewers | (set() if secret else {INDEXER_USER}))

        args = _base_args(0o2770)
        args += ["-m", f"g:{vg}:rx"] if bind_viewers else (
            ["-x", f"g:{vg}"] if vg in before_g else [])
        for u in sorted(set(before_u) - {INDEXER_USER}):
            args += ["-x", f"u:{u}"]
        for gg in sorted(set(before_g) - ({vg} if bind_viewers else set())):
            args += ["-x", f"g:{gg}"]
        _acl_apply_fd(tfd, is_dir, args)
        os.fchmod(tfd, 0o2770)      # setfacl rewrote the mode's group bits (mask)

        if gid != old_gid:
            out["regrouped"] = _rehome_tree(p, old_gid, gid, 0o2070, 0o060,
                                            every=secret)
            out["reaped"] = _reap_orphan_groups()
        if bind_viewers and vg not in before_g:
            out["acl_walked"] = _acl_walk(p, ["-m", f"g:{vg}:rx"], ["-m", f"g:{vg}:r"],
                                          secrets=secret_root)
        elif not bind_viewers and vg in before_g:
            out["acl_walked"] = _acl_walk(p, ["-x", f"g:{vg}"], ["-x", f"g:{vg}"],
                                          secrets=secret_root)
            _set_group_members(vg, set())
        out["share_with"] = [("g", g)] + ([("g", vg)] if bind_viewers else [])
        return out
    finally:
        if tfd is not None:
            os.close(tfd)
        os.close(pfd)


# --- login throttle ---------------------------------------------------------
# Nothing else rate-limits POST /login: PAM here is bare pam_unix with no
# faillock, so without this the endpoint answers guesses as fast as it can hash
# them. Cloudflare Access covers the open internet; this covers everyone already
# past it — a colleague, a borrowed laptop, anything reaching the tunnel.
#
# Two independent counters, because the two attacks look different:
#   per ACCOUNT  one victim, any number of sources (spraying a known username)
#   per SOURCE   one attacker, any number of usernames (enumerating the company)
# Whichever trips first locks that key ALONE: locking the account `krystof` must
# never lock out the people sharing his office IP, and vice versa.
#
# In-memory on purpose. A hub restart clears the counters, which is fine — a
# restart is not attacker-reachable — and it keeps the login path free of I/O.
LOGIN_MAX_FAILS = 8        # failures inside the window before that key locks
LOGIN_WINDOW = 300.0       # seconds a failure keeps counting
LOGIN_LOCK = 900.0         # seconds a tripped key stays locked
LOGIN_FAIL_DELAY = 0.5     # seconds every wrong answer costs, lock or not
LOGIN_MAX_KEYS = 4096      # hard cap so a forged source header cannot grow this


class LoginThrottle:
    """Failure counting for POST /login, keyed independently by account and by
    source. Clocked with time.monotonic(), so a clock step cannot unlock a key
    early."""

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}
        self._until: dict[str, float] = {}

    def retry_after(self, keys: list[str], now: float) -> int:
        """Seconds the caller must wait; 0 means the attempt may proceed."""
        return max((int(self._until[k] - now) + 1 for k in keys
                    if self._until.get(k, 0.0) > now), default=0)

    def record_failure(self, keys: list[str], now: float) -> list[str]:
        """Count one wrong password against every key. Returns those that just
        locked, so the caller can say so out loud exactly once."""
        self._prune(now)
        locked = []
        for k in keys:
            hits = [t for t in self._fails.get(k, ()) if now - t < LOGIN_WINDOW]
            hits.append(now)
            self._fails[k] = hits
            if len(hits) >= LOGIN_MAX_FAILS:
                self._until[k] = now + LOGIN_LOCK
                del self._fails[k]          # the lock replaces the history
                locked.append(k)
        return locked

    def clear(self, keys: list[str]) -> None:
        """A correct password forgives that account's and that source's history —
        otherwise a day of typos eventually locks someone who knows their own
        password."""
        for k in keys:
            self._fails.pop(k, None)
            self._until.pop(k, None)

    def _prune(self, now: float) -> None:
        for k, t in list(self._until.items()):
            if t <= now:
                del self._until[k]
        for k, hits in list(self._fails.items()):
            if not hits or now - hits[-1] >= LOGIN_WINDOW:
                del self._fails[k]
        if len(self._fails) > LOGIN_MAX_KEYS:    # only reachable by forging the
            for k in sorted(self._fails,         # source header from ON the box
                            key=lambda k: self._fails[k][-1])[:LOGIN_MAX_KEYS // 2]:
                del self._fails[k]


def _login_source(request: web.Request) -> str:
    """Where an attempt came from, or "" when there is no meaningful answer.

    cloudflared and caddy are local processes, so request.remote is always
    127.0.0.1 and worthless on its own. CF-Connecting-IP is stamped by Cloudflare
    and cannot be forged from outside the tunnel. A process already ON this box
    could forge it — but it is then past every boundary this throttle defends,
    and the per-account counter still catches it.

    A bare loopback caller (no proxy header at all) gets "" rather than
    "127.0.0.1": every local process would otherwise share one counter, so a
    test run or a stray script could lock out the whole box's own traffic.
    """
    for h in ("CF-Connecting-IP", "X-Forwarded-For"):
        v = request.headers.get(h, "")
        if v:
            return v.split(",")[0].strip()[:64]
    remote = request.remote or ""
    return "" if remote in ("127.0.0.1", "::1", "") else remote[:64]


def _login_keys(request: web.Request, user: str) -> list[str]:
    """The throttle keys one attempt counts against. The account key is always
    present; the source key only when the source is real (see _login_source)."""
    keys = [f"user:{user.lower()}"]
    src = _login_source(request)
    if src:
        keys.append(f"from:{src}")
    return keys


class Hub:
    def __init__(self):
        self.key = common.load_session_key()
        self._user_sessions: dict[str, aiohttp.ClientSession] = {}
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        self._syncd_session: aiohttp.ClientSession | None = None
        self._stt_key = self._load_stt_key()
        self._stt_used: dict[str, tuple[str, int]] = {}   # user -> (utc day, bytes)
        self._throttle = LoginThrottle()
        self._uploads = uploads.Sessions()               # chunked uploads in flight

    # --- identity -----------------------------------------------------------
    def current_user(self, request: web.Request) -> str | None:
        tok = request.cookies.get(common.COOKIE_NAME)
        if not tok:
            return None
        data = common.read_token(self.key, tok)
        return data.get("user") if data else None

    def gids_for(self, user: str) -> list[int]:
        e = pwd.getpwnam(user)
        return os.getgrouplist(user, e.pw_gid)

    # --- per-user backend spawning -----------------------------------------
    def _user_sock(self, user: str) -> Path:
        # Each user's socket lives in a private 0700 dir owned by that user, so
        # no other user can create/squat a socket at this path.
        return common.USER_SOCK_DIR / user / "backend.sock"

    async def ensure_backend(self, user: str) -> aiohttp.ClientSession:
        sock = self._user_sock(user)
        uid = pwd.getpwnam(user).pw_uid
        sess = self._user_sessions.get(user)
        if sess is not None and not sess.closed and self._backend_alive(sock, uid):
            return sess
        lock = self._spawn_locks.setdefault(user, asyncio.Lock())
        async with lock:
            sess = self._user_sessions.get(user)
            if sess is not None and not sess.closed and self._backend_alive(sock, uid):
                return sess
            if sess is not None:
                await sess.close()
                self._user_sessions.pop(user, None)
            if not self._backend_alive(sock, uid):
                await self._spawn_backend(user, sock)
            connector = aiohttp.UnixConnector(path=str(sock))
            sess = aiohttp.ClientSession(connector=connector)
            self._user_sessions[user] = sess
            return sess

    def _backend_alive(self, sock: Path, uid: int) -> bool:
        """Probe the socket AND verify it (and its dir) are owned by the target
        user — refuse to ever connect to a socket another user could have created."""
        try:
            st = os.lstat(sock)
            if st.st_uid != uid or stat.S_ISLNK(st.st_mode):
                return False
            if os.stat(sock.parent).st_uid != uid:
                return False
        except OSError:
            return False
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(0.2)
            s.connect(str(sock))
            return True
        except OSError:
            return False
        finally:
            s.close()

    async def _spawn_backend(self, user: str, sock: Path) -> None:
        # (Re)create the per-user socket dir as root, owned by the user, 0700.
        udir = sock.parent
        e = pwd.getpwnam(user)
        try:
            # /run is tmpfs: after a reboot nothing else recreates the shared
            # socket root, so build the whole chain here — root-owned 0755
            # parents, then the per-user dir 0700 owned by the user.
            common.USER_SOCK_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(common.USER_SOCK_DIR, 0o755)
            udir.mkdir(mode=0o700, exist_ok=True)
            os.chown(udir, e.pw_uid, e.pw_gid)
            os.chmod(udir, 0o700)
        except OSError:
            pass
        if sock.exists() or sock.is_symlink():
            try:
                sock.unlink()
            except OSError:
                pass
        env = {
            "PATH": "/usr/bin:/bin",
            "KB_REPO": str(common.REPO_ROOT),
            "KB_RUN": str(common.RUN_DIR),
            "KB_ETC": str(common.ETC_DIR),
            # The per-user backend is what actually runs the SQL, so it needs
            # the database name too — otherwise KB_PG_DB is honoured by the
            # hub and the indexer but silently ignored where it matters.
            "KB_PG_DB": common.PG_DB,
            "PYTHONPATH": str(PLATFORM_ROOT),
            "HOME": pwd.getpwnam(user).pw_dir,
        }
        cmd = ["/usr/sbin/runuser", "-u", user, "--", common.VENV_PY,
               "-m", "kb_platform.user_server", "--uds", str(sock)]
        proc = subprocess.Popen(cmd, env=env, cwd=str(PLATFORM_ROOT),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Reap the runuser wrapper when it exits, or each backend restart leaves
        # a root zombie behind (Popen is never wait()ed otherwise).
        _reap_later(proc)
        for _ in range(100):
            if sock.exists():
                return
            await asyncio.sleep(0.05)
        raise web.HTTPBadGateway(text=f"backend for {user} did not start")

    def _syncd(self) -> aiohttp.ClientSession:
        if self._syncd_session is None or self._syncd_session.closed:
            connector = aiohttp.UnixConnector(path=str(common.SYNCD_SOCK))
            self._syncd_session = aiohttp.ClientSession(connector=connector)
        return self._syncd_session

    # --- auth routes --------------------------------------------------------
    # The HTML shell must NEVER be cached. Asset URLs inside it carry a ?v=
    # build stamp, so app.js and style.css bust themselves — but nothing busts
    # the document that names them. Without this header a browser applies
    # heuristic freshness (a fraction of the time since Last-Modified) and can
    # keep serving yesterday's app.html, which points at yesterday's bundle:
    # the user deploys, reloads, and sees no change.
    # The shell shipped with no security headers at all, so the browser had no
    # instructions to enforce: the whole app was frameable by any site
    # (clickjacking), and nothing bounded an injected script.
    #
    # Deliberately tight where it is free and loose only where the app needs
    # it. 'unsafe-inline' for STYLE only — CodeMirror and xterm inject
    # stylesheets at runtime; script-src stays 'self', which is what actually
    # matters. app.html carries exactly one script tag and it is external, so
    # nothing here needs a nonce. ws: for the CRDT and PTY sockets, blob:/data:
    # for pasted media, frame-src 'self' for the artifact iframes.
    SHELL_CSP = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "media-src 'self' blob:; font-src 'self' data:; "
        "connect-src 'self' ws: wss:; frame-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
    )
    NO_STORE = {
        "Cache-Control": "no-store, must-revalidate",
        # SHELL_CSP is NOT applied yet — enabling it broke 6 of 8 browser tests
        # including every artifact one. The headers below are the ones that are
        # safe unconditionally; the CSP needs the artifact iframe's opaque
        # origin worked out first. Deliberately left defined and unused rather
        # than deleted, so the next attempt starts from the real policy.
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        # frame-ancestors above is the modern control; this covers older
        # browsers that never implemented it.
        "X-Frame-Options": "SAMEORIGIN",
    }

    async def login_page(self, request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "login.html", headers=self.NO_STORE)

    async def do_login(self, request: web.Request) -> web.Response:
        data = await request.post()
        user = str(data.get("username", "")).strip()
        pw = str(data.get("password", ""))
        now = time.monotonic()
        keys = _login_keys(request, user)
        wait = self._throttle.retry_after(keys, now)
        if wait:
            # Deliberately the same message whether the account or the source is
            # locked: telling an attacker WHICH one tripped tells them whether
            # the username exists.
            return web.json_response(
                {"error": f"too many failed sign-ins \u2014 try again in "
                          f"{wait // 60 + 1} min"},
                status=429, headers={"Retry-After": str(wait)})
        if not pam_auth.authenticate(user, pw):
            for k in self._throttle.record_failure(keys, now):
                print(f"kb-hub: login locked {k} for {int(LOGIN_LOCK)}s after "
                      f"{LOGIN_MAX_FAILS} failures", file=sys.stderr, flush=True)
            # Every wrong answer costs this, lock or no lock — otherwise the
            # first LOGIN_MAX_FAILS guesses are free and as fast as the network.
            await asyncio.sleep(LOGIN_FAIL_DELAY)
            _audit("login", user, ok=False, source=_login_source(request))
            return web.json_response({"error": "invalid credentials"}, status=401)
        self._throttle.clear(keys)
        _audit("login", user, source=_login_source(request))
        tok = common.make_token(self.key, {"user": user})
        resp = web.json_response({"ok": True, "user": user})
        resp.set_cookie(common.COOKIE_NAME, tok, httponly=True, samesite="Lax",
                        max_age=common.SESSION_TTL, path="/")
        return resp

    async def do_logout(self, request: web.Request) -> web.Response:
        resp = web.HTTPFound("/login")
        resp.del_cookie(common.COOKIE_NAME, path="/")
        return resp

    async def index(self, request: web.Request) -> web.Response:
        if not self.current_user(request):
            raise web.HTTPFound("/login")
        return web.FileResponse(STATIC_DIR / "app.html", headers=self.NO_STORE)

    async def deep_link(self, request: web.Request) -> web.Response:
        """Repo paths ARE routes: /company/notes.md serves the app, which opens
        that file — so a document's URL can be pasted straight to a colleague.
        Unauthenticated visitors go through login and land on the file after."""
        if not self.current_user(request):
            raise web.HTTPFound("/login?next=" + urllib.parse.quote(request.rel_url.raw_path))
        return web.FileResponse(STATIC_DIR / "app.html", headers=self.NO_STORE)

    async def vc_proxy(self, request: web.Request) -> web.Response:
        """Version-history reads (log/show/diff/activity) — proxied to syncd
        with the caller's hub-verified identity; syncd re-checks per file that
        the caller can READ it right now (kernel-evaluated, as them)."""
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        op = request.match_info["op"]
        if op not in ("log", "show", "diff", "activity"):
            return web.json_response({"error": "unknown history operation"}, status=404)
        token = common.make_token(self.key, {"user": user}, ttl=60)
        url = f"http://kb/vc/{op}"
        if request.query_string:
            url += "?" + request.query_string
        try:
            async with self._syncd().get(url, headers={"X-KB-Auth": token}) as resp:
                return web.json_response(await resp.json(), status=resp.status)
        except (aiohttp.ClientError, ValueError) as e:
            return web.json_response({"error": f"syncd error: {e}"}, status=502)

    async def presence(self, request: web.Request) -> web.Response:
        """Who has which doc open right now (from syncd), permission-filtered
        for the requesting user — feeds the file tree's presence avatars."""
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        uid_gid = pam_auth.user_uid_gid(user)
        if not uid_gid:
            return web.json_response({"error": "forbidden"}, status=403)
        token = common.make_token(self.key, {"user": user, "uid": uid_gid[0],
                                             "gids": self.gids_for(user)}, ttl=common.SESSION_TTL)
        try:
            async with self._syncd().get("http://kb/presence",
                                         headers={"X-KB-Auth": token}) as resp:
                return web.json_response(await resp.json(), status=resp.status)
        except aiohttp.ClientError as e:
            return web.json_response({"error": f"syncd error: {e}"}, status=502)

    # --- proxies ------------------------------------------------------------
    async def proxy_http(self, request: web.Request) -> web.StreamResponse:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        sess = await self.ensure_backend(user)
        url = "http://kb" + request.rel_url.raw_path_qs
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in _HOP and k.lower() not in _ASSERTED}
        headers["X-KB-User"] = user
        body = await request.read()
        # /api/events is a server-sent event stream: it never ends on its own,
        # so it gets no client timeout and is PIPED chunk by chunk instead of
        # buffered — a buffered body would reach the browser only when the
        # backend closed the stream, i.e. never.
        stream = request.path == "/api/events"
        extra = {"timeout": aiohttp.ClientTimeout(total=None, sock_read=None)} if stream else {}
        # …but never past the session: the cookie was checked once, on the way
        # in, and a stream that outlived it would keep an expired tab looking
        # alive. Ending it at `exp` makes the client reconnect, meet a 401, and
        # bounce to login by itself — what the 4 s poll used to do.
        ttl = None
        if stream:
            tok = common.read_token(self.key, request.cookies.get(common.COOKIE_NAME, ""))
            ttl = max(1.0, float((tok or {}).get("exp", 0)) - time.time())
        try:
            async with sess.request(request.method, url, headers=headers, data=body,
                                    allow_redirects=False, **extra) as resp:
                if stream and resp.headers.get("Content-Type", "").startswith("text/event-stream"):
                    sr = web.StreamResponse(status=resp.status, headers={
                        k: v for k, v in resp.headers.items() if k.lower() not in _HOP})
                    await sr.prepare(request)

                    async def pipe():
                        async for chunk in resp.content.iter_any():
                            await sr.write(chunk)
                    try:
                        await asyncio.wait_for(pipe(), timeout=ttl)
                    except (asyncio.TimeoutError, ConnectionResetError, asyncio.CancelledError,
                            RuntimeError):
                        pass
                    return sr
                out_body = await resp.read()
                out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}
                # After the status is known, and after the body is already
                # buffered — the audit changes nothing about what is served,
                # and `bytes` is the count we were going to hold anyway, so
                # nothing here weakens the response or its streaming.
                hit = _access_event(request, resp.status)
                if hit:
                    _audit_access(hit[0], user, hit[1], _login_source(request),
                                  bytes=len(out_body))
                return web.Response(status=resp.status, body=out_body, headers=out_headers)
        except aiohttp.ClientError as e:
            return web.json_response({"error": f"backend error: {e}"}, status=502)

    async def proxy_pty(self, request: web.Request) -> web.StreamResponse:
        user = self.current_user(request)
        if not user:
            return web.Response(status=401)
        sess = await self.ensure_backend(user)
        # forward the query string — ?session=<id> is how a client reattaches
        # to its still-running shell after a page refresh
        url = "http://kb/pty" + ("?" + request.query_string if request.query_string else "")
        return await self._bridge_ws(request, sess, url, {"X-KB-User": user})

    async def doc_epoch(self, request: web.Request) -> web.Response:
        """The doc's CRDT lineage id (from syncd) — the editor fetches this and
        presents it when joining the live session; stale lineages are refused."""
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        uid_gid = pam_auth.user_uid_gid(user)
        if not uid_gid:
            return web.json_response({"error": "forbidden"}, status=403)
        token = common.make_token(self.key, {"user": user, "uid": uid_gid[0],
                                             "gids": self.gids_for(user)}, ttl=common.SESSION_TTL)
        rel = request.query.get("path", "").strip("/")
        try:
            async with self._syncd().get("http://kb/epoch/" + rel,
                                         headers={"X-KB-Auth": token}) as resp:
                return web.json_response(await resp.json(), status=resp.status)
        except aiohttp.ClientError as e:
            return web.json_response({"error": f"syncd error: {e}"}, status=502)

    async def proxy_doc(self, request: web.Request) -> web.StreamResponse:
        user = self.current_user(request)
        if not user:
            return web.Response(status=401)
        uid_gid = pam_auth.user_uid_gid(user)
        if not uid_gid:
            return web.Response(status=403)
        uid, _ = uid_gid
        token = common.make_token(self.key, {"user": user, "uid": uid,
                                             "gids": self.gids_for(user)}, ttl=common.SESSION_TTL)
        path = request.match_info["path"]
        # Normalized BEFORE the query string is glued on, so ?e=<epoch> can
        # never reach the audit line. None means the path does not resolve into
        # the repo — syncd is about to refuse it anyway, and an unresolvable
        # path is not something to record as an open.
        rel = _audit_path(path)
        source = _login_source(request)
        # forward the query string — ?e=<epoch> is the lineage gate
        if request.query_string:
            path += "?" + request.query_string
        # Only once syncd has ACCEPTED the socket: it answers 403 (no read, or
        # a secret), 404 (bad path) or 409 (stale lineage) before the handshake,
        # and each of those raises out of ws_connect instead. So this fires per
        # accepted session join — never per CRDT frame, never on a refusal.
        on_accept = (lambda: _audit_access("document.open", user, rel, source)) if rel else None
        return await self._bridge_ws(request, self._syncd(), f"http://kb/ws/doc/{path}",
                                     {"X-KB-Auth": token}, on_accept=on_accept)

    # --- privileged filesystem admin (runs as root; carefully authorized) ---
    # These are the ONLY places the hub mutates the filesystem. Every handler
    # re-derives the caller from the signed cookie and checks Unix authorization
    # (write-to-parent for create; own-or-admin for property changes) before
    # doing anything. All operations are bounded to the repo tree.

    def _create_inheriting(self, rel: str, data: bytes | None, exclusive: bool = False,
                           owner: tuple[int, int] | None = None,
                           force_mode: int | None = None,
                           creator: int | None = None) -> str:
        """Create a file with its parent's audience and write `data` to it.
        Thin wrapper over `_open_inheriting` — see there for the symlink and
        mode rules. Returns the owner's name."""
        own, fd = self._open_inheriting(rel, exclusive, owner, force_mode, creator)
        try:
            if data:
                os.write(fd, data)
        finally:
            os.close(fd)
        return own

    def _open_inheriting(self, rel: str, exclusive: bool = False,
                         owner: tuple[int, int] | None = None,
                         force_mode: int | None = None,
                         creator: int | None = None) -> tuple[str, int]:
        """Create a file (owned by its PARENT's owner/group; needs root) WITHOUT
        following any symlink in the path — the parent dir is reached via
        openat/O_NOFOLLOW and the file is opened O_NOFOLLOW under that dir fd, so
        a symlink planted by a user cannot redirect this root write. Returns the
        owner's name and an OPEN WRITE FD, which the caller must close.
        `owner`/`force_mode` override inheritance outright. `creator` overrides
        only the UID — used inside `_secrets/`, where the person who put the
        credential there stays the one who controls it (nothing else may widen
        it, not even an admin), while its GROUP and MODE still follow the
        folder. That is what makes sharing a `_secrets` folder mean something:
        while the folder is closed, so is the key; once its owner shares the
        folder, keys added to it are readable by exactly that audience.

        The fd is the point for a chunked upload: it is opened once, under a
        verified dir fd, and every later append goes to that inode no matter
        what happens to the name in a folder the uploader can also write."""
        parent_rel = os.path.dirname(rel)
        name = os.path.basename(rel)
        if not name or "/" in name or name in (".", ".."):
            raise OSError("bad name")
        pfd = common.opendir_beneath(parent_rel)
        try:
            pst = os.fstat(pfd)
            own = owner or (pst.st_uid if creator is None else creator, pst.st_gid)
            flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
            flags |= os.O_EXCL if exclusive else os.O_TRUNC
            # common.birth_mode, not a local rule. The old one started from a
            # hardcoded 0o644 and only ever ADDED bits, so a new document was
            # born world-readable no matter how closed its folder was — 0o644
            # inside a private 0700 folder, 0o664 inside a 2770 team folder. It
            # also read st_mode's group bits, which on an ACL-bearing folder are
            # the MASK rather than the group's own permission.
            secret = common.born_closed(pfd, rel)
            mode = (force_mode if force_mode is not None
                    else common.birth_mode(pfd, False, rel))
            # Where the folder carries a default ACL the kernel's inheritance is
            # the correct answer, and a restrictive create mode would clamp its
            # mask to nothing — voiding every entry it just granted.
            inherit_acl = force_mode is None and not secret and common.inherits_acl(pfd)
            if inherit_acl:
                old_umask = os.umask(0)
                try:
                    fd = os.open(name, flags, 0o666, dir_fd=pfd)
                finally:
                    os.umask(old_umask)
            else:
                fd = os.open(name, flags, mode, dir_fd=pfd)
            try:
                os.fchown(fd, *own)
                if not inherit_acl:
                    os.fchmod(fd, mode)   # the create mode was cut by our umask
            except OSError:
                os.close(fd)
                raise
        finally:
            os.close(pfd)
        try:
            return pwd.getpwuid(own[0]).pw_name, fd
        except KeyError:
            return str(own[0]), fd

    async def fs_newfile(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        data = await request.json()
        rel = data.get("path", "")
        p = common.resolve_repo_path(rel)
        if p is None:
            return web.json_response({"error": "bad path"}, status=400)
        if p.exists():
            return web.json_response({"error": "already exists"}, status=409)
        # Classify the RESOLVED path, never the raw string: `_secrets/../x.sh`
        # would otherwise skip the extension check, and a path reaching a
        # _secrets dir through a symlink would miss the born-private rule.
        rel_resolved = str(p.relative_to(common.REPO_ROOT.resolve()))
        secret = common.is_secret_path(rel_resolved)
        if not secret and not (rel_resolved.endswith(".md") or rel_resolved.endswith(".html")):
            return web.json_response({"error": "name must end in .md or .html"}, status=400)
        uid, gids = _uid_gids(user)
        if not p.parent.is_dir() or not _fs_can(p.parent, uid, gids, need_write=True):
            return web.json_response({"error": "no write access to folder"}, status=403)
        rel_clean = rel_resolved
        try:
            # A secret stays its creator's to control (only the owner may widen
            # one), but takes the audience of the `_secrets` folder it lands in
            # — which is owner-only until that folder is deliberately shared.
            owner = self._create_inheriting(
                rel_clean, None, exclusive=True,
                creator=uid if secret else None)
        except FileExistsError:
            return web.json_response({"error": "already exists"}, status=409)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=500)
        # report the canonical path we actually created, not the raw request
        return web.json_response({"ok": True, "path": rel_clean, "owner": owner})

    async def fs_upload(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        rel_dir = request.query.get("dir", "")
        d = common.resolve_repo_path(rel_dir)
        if d is None or not d.is_dir():
            return web.json_response({"error": "bad folder"}, status=400)
        uid, gids = _uid_gids(user)
        if not _fs_can(d, uid, gids, need_write=True):
            return web.json_response({"error": "no write access to folder"}, status=403)
        reader = await request.multipart()
        field = await reader.next()
        name = os.path.basename(field.filename or "upload.bin")
        chunks = bytearray()
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            chunks += chunk
        rel_clean = str(d.relative_to(common.REPO_ROOT.resolve()) / name)
        secret = common.is_secret_path(rel_clean)
        try:
            owner = self._create_inheriting(
                rel_clean, bytes(chunks), creator=uid if secret else None)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"ok": True, "path": rel_clean, "owner": owner})

    # ---- chunked uploads ---------------------------------------------------
    # The single-shot `fs_upload` above still serves scripts and small posts,
    # but the UI no longer uses it: a browser slices the file and drives the
    # three calls below, so no request is ever bigger than one chunk. See
    # uploads.py for why (the edge, not this platform, is what capped uploads).

    def _upload_dir(self, user: str, rel_dir: str):
        """Resolve + authorise an upload target for this user. Returns the
        directory, or an error response."""
        d = common.resolve_repo_path(rel_dir)
        if d is None or not d.is_dir():
            return None, web.json_response({"error": "bad folder"}, status=400)
        uid, gids = _uid_gids(user)
        if not _fs_can(d, uid, gids, need_write=True):
            return None, web.json_response({"error": "no write access to folder"},
                                           status=403)
        return d, None

    async def fs_upload_begin(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "bad request"}, status=400)
        rel_dir = str(data.get("dir", ""))
        d, err = self._upload_dir(user, rel_dir)
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
        self._uploads.sweep()
        if self._uploads.full():
            return web.json_response({"error": "too many uploads in flight"},
                                     status=503)
        uploads.sweep_orphans(d)
        dir_rel = str(d.relative_to(common.REPO_ROOT.resolve()))
        rel_final = str(Path(dir_rel) / name)
        secret = common.is_secret_path(rel_final)
        spool = d / uploads.spool_name()
        try:
            uid = pwd.getpwnam(user).pw_uid
            # The spool is born exactly as the finished file must be — owner,
            # mode and inherited ACL — because the last step is a rename inside
            # this same folder, which carries the inode over untouched.
            _owner, fd = self._open_inheriting(
                str(Path(dir_rel) / spool.name), exclusive=True,
                creator=uid if secret else None)
        except (OSError, KeyError) as e:
            return web.json_response({"error": str(e)}, status=500)
        sess = self._uploads.add(user=user, dir_rel=dir_rel, name=name,
                                 spool=spool, size=size, fd=fd)
        return web.json_response(uploads.begin_payload(sess))

    async def fs_upload_chunk(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        sess = self._uploads.get(request.query.get("id", ""), user)
        if sess is None:
            return web.json_response({"error": "unknown upload"}, status=404)
        try:
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            return web.json_response({"error": "bad offset"}, status=400)
        return await uploads.receive(request, sess, offset)

    async def fs_upload_finish(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "bad request"}, status=400)
        sid = str(data.get("id", ""))
        sess = self._uploads.get(sid, user)
        if sess is None:
            return web.json_response({"error": "unknown upload"}, status=404)
        err = uploads.complete_error(sess)
        if err:
            return err
        # Re-check write access at the end too: a session can outlive the share
        # that authorised it, and the rename is the write that counts.
        _d, aerr = self._upload_dir(user, sess.dir_rel)
        if aerr:
            self._uploads.drop(sid)
            return aerr
        try:
            pfd = common.opendir_beneath(sess.dir_rel)
        except OSError as e:
            self._uploads.drop(sid)
            return web.json_response({"error": str(e)}, status=500)
        try:
            if not uploads.same_inode(sess.fd, sess.spool_name, pfd):
                self._uploads.drop(sid)
                return web.json_response({"error": "upload file vanished"}, status=409)
            os.rename(sess.spool_name, sess.name, src_dir_fd=pfd, dst_dir_fd=pfd)
        except OSError as e:
            self._uploads.drop(sid)
            return web.json_response({"error": str(e)}, status=500)
        finally:
            os.close(pfd)
        self._uploads.drop(sid, unlink=False)   # the spool IS the file now
        rel_clean = str(Path(sess.dir_rel) / sess.name)
        return web.json_response({"ok": True, "path": rel_clean, "size": sess.size,
                                  "owner": _owner_name(common.REPO_ROOT / rel_clean)})

    async def fs_upload_abort(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        try:
            data = await request.json()
        except ValueError:
            data = {}
        sid = str(data.get("id", ""))
        if self._uploads.get(sid, user) is None:
            return web.json_response({"error": "unknown upload"}, status=404)
        self._uploads.drop(sid)
        return web.json_response({"ok": True})

    async def fs_props_get(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        p = common.resolve_repo_path(request.query.get("path", ""))
        if p is None or not p.exists():
            return web.json_response({"error": "not found"}, status=404)
        st = os.lstat(p)
        uid, gids = _uid_gids(user)
        # Authentication is not authorisation. This returns owner, group, mode
        # and the full named-ACL list — the company's whole sharing topology —
        # plus a 404-vs-200 existence oracle. Its sibling fs_share_get has
        # always gated on readability; this one never did.
        if not _fs_can(p, uid, gids, False) and not _is_admin(user):
            return web.json_response({"error": "no access"}, status=403)
        return web.json_response({
            "path": str(p.relative_to(common.REPO_ROOT)),
            "owner": _owner_name(p) or str(st.st_uid),
            "group": (grp.getgrgid(st.st_gid).gr_name if _grp_ok(st.st_gid) else str(st.st_gid)),
            "mode": oct(stat.S_IMODE(st.st_mode))[2:],
            "is_dir": stat.S_ISDIR(st.st_mode),
            "acls": _parse_acl(p),
            "can_edit": _owns_or_admin(p, user),
            "access": {"read": _fs_can(p, uid, gids, False), "write": _fs_can(p, uid, gids, True)},
        })

    async def fs_props_set(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        data = await request.json()
        p = common.resolve_repo_path(data.get("path", ""))
        if p is None or not p.exists():
            return web.json_response({"error": "not found"}, status=404)
        if not _owns_or_admin(p, user):
            return web.json_response({"error": "only the owner (or an admin) can change this"},
                                    status=403)
        rel = str(p.relative_to(common.REPO_ROOT.resolve()))
        # _secrets/ files ARE shareable through this endpoint — that is the
        # documented design (tests/cli/test_secrets.py: "shared with the normal
        # permissions UI"), and the containment guarantee is about git, the index
        # and the CRDT relay, not about ACLs. What must NOT happen is the ADMIN
        # override reaching them: ADMIN_GROUP is "sudo" here, so _owns_or_admin
        # let any platform admin widen someone else's secret from the root hub,
        # silently and with no share record. The owner keeps full control.
        if (common.is_secret_path(rel) and _owner_name(p) != user
                and (data.get("owner") or data.get("group") or data.get("acl_add")
                     or data.get("visibility") in ("team", "company"))):
            return web.json_response(
                {"error": "only the owner can widen access to a file under _secrets/"},
                status=403)
        # Reach the target without following symlinks and pin it by a real fd, so
        # a swapped path can't redirect the root chown/setfacl. All mutations act
        # on the fd (fchown / xattr), never on a re-traversable path string.
        try:
            pfd = common.opendir_beneath(os.path.dirname(rel))
        except OSError:
            return web.json_response({"error": "not found"}, status=404)
        tfd = None
        try:
            name = os.path.basename(rel)
            lst = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            if stat.S_ISLNK(lst.st_mode):
                return web.json_response({"error": "refusing to modify a symlink"}, status=400)
            is_dir = stat.S_ISDIR(lst.st_mode)
            oflags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if is_dir else 0)
            tfd = os.open(name, oflags, dir_fd=pfd)
            if data.get("owner"):
                if not _NAME_RE.match(data["owner"]):
                    return web.json_response({"error": "bad user name"}, status=400)
                # Giving a file AWAY forges provenance: the old shape let any
                # owner set st_uid to root or another account while keeping write
                # access via group or ACL, producing a file that DISPLAYS as
                # someone else's and is still attacker-controlled. Only an admin
                # may reassign, and never below the human uid range.
                if data["owner"] != user:
                    if not _is_admin(user):
                        return web.json_response(
                            {"error": "only an admin can change the owner"}, status=403)
                    if pwd.getpwnam(data["owner"]).pw_uid < 1000:
                        return web.json_response(
                            {"error": "refusing to hand a file to a system account"},
                            status=400)
                os.fchown(tfd, pwd.getpwnam(data["owner"]).pw_uid, -1)
            if data.get("group"):
                if not _NAME_RE.match(data["group"]):
                    return web.json_response({"error": "bad group name"}, status=400)
                # SECURITY: authorization for chgrp is GROUP MEMBERSHIP, not inode
                # ownership. The old shape gated only on _owns_or_admin(inode), so
                # owning any throwaway folder let a non-member chgrp it onto
                # any team-*/proj-* group; combined with _share_apply's group
                # reuse that rewrote the whole roster (see _share_apply). The
                # kernel enforces exactly this rule for non-root chgrp — the hub
                # runs as root, so it must re-derive it.
                if not _is_admin(user):
                    _, cgids = _uid_gids(user)
                    if grp.getgrnam(data["group"]).gr_gid not in cgids:
                        return web.json_response(
                            {"error": "you can only set a group you belong to"},
                            status=403)
                os.fchown(tfd, -1, grp.getgrnam(data["group"]).gr_gid)
            if data.get("visibility"):
                # base-mode presets — the "who can even see this" dial the modal
                # was missing: private = owner + explicit ACL grantees only;
                # team = the file's group; company = everyone can read.
                vis = data["visibility"]
                modes = {"private": (0o700, 0o600),
                         "team":    (0o2770, 0o660),
                         "company": (0o2775, 0o664)}
                if vis not in modes:
                    return web.json_response({"error": "visibility must be private/team/company"}, status=400)
                # A folder inherits its parent's DEFAULT acl, and that is what
                # decides the audience of files created in it later. Setting the
                # folder private used to rewrite only the ACCESS acl, so the
                # folder itself went private while every document created or
                # copied into it afterwards was still born world-readable from
                # the stale `default:other::r-x`. Verified: a copy into a
                # "private" folder landed `other::r--`. /fs/share's private
                # branch has always cleared both; this is that, kept narrow —
                # widening presets have no such hazard, so they are left alone.
                if vis == "private" and is_dir:
                    try:
                        os.removexattr(tfd, _ACL_DEFAULT)
                    except OSError:
                        pass
                os.fchmod(tfd, modes[vis][0 if is_dir else 1])
                # With an extended ACL present, chmod's group bits set the MASK,
                # not the real group:: entry — a stale group::--- would keep
                # denying group members regardless of mode. Repair it explicitly
                # (setfacl also recalculates the mask, so named ACL grantees
                # keep their access under "private" — exactly the semantics).
                # Plain-mode files need no repair (chmod IS group:: there).
                try:
                    os.getxattr(f"/proc/self/fd/{tfd}", "system.posix_acl_access")
                    has_acl = True
                except OSError:
                    has_acl = False
                if has_acl:
                    gperm = "---" if vis == "private" else ("rwx" if is_dir else "rw-")
                    _acl_apply_fd(tfd, is_dir, ["-m", f"g::{gperm}"])
                # "Private" must not also mean "not searchable". kb-indexer runs
                # as kbindexer; a file it cannot read has NO rows in kb.blocks,
                # so making a note private used to drop it out of search for its
                # own owner. RLS still decides who may see those rows, so this
                # grant widens nothing — it only keeps the index complete.
                if vis == "private" and not common.is_secret_path(rel):
                    _grant_indexer(tfd, is_dir)
            shared_with = []   # (flag, name) grantees to make the path reachable for
            for a in data.get("acl_add", []):
                kind, aname, perms = a.get("type"), a.get("name", ""), a.get("perms", "")
                if kind not in ("user", "group") or not _NAME_RE.match(aname) or not _PERM_RE.match(perms):
                    return web.json_response({"error": "bad acl entry"}, status=400)
                flag = "u" if kind == "user" else "g"
                _acl_apply_fd(tfd, is_dir, ["-m", f"{flag}:{aname}:{perms}"])
                if "r" in perms:
                    shared_with.append((flag, aname))
            for a in data.get("acl_remove", []):
                kind, aname = a.get("type"), a.get("name", "")
                if kind not in ("user", "group") or not _NAME_RE.match(aname):
                    return web.json_response({"error": "bad acl entry"}, status=400)
                _acl_apply_fd(tfd, is_dir, ["-x", f"{'u' if kind == 'user' else 'g'}:{aname}"])
            # Advanced is the other way to widen a `_secrets` folder, and it has
            # to record the same decision the panel does — otherwise keys added
            # after a raw ACL grant would still be born owner-only and the grant
            # would look like it had done nothing.
            if common.is_secret_root(rel):
                if data.get("acl_add") or data.get("group") or \
                        data.get("visibility") in ("team", "company"):
                    common.set_secrets_shared(tfd, True)
                elif data.get("visibility") == "private":
                    common.set_secrets_shared(tfd, False)
        except KeyError:
            return web.json_response({"error": "no such user or group"}, status=400)
        except (OSError, subprocess.SubprocessError) as e:
            return web.json_response({"error": str(e)}, status=500)
        finally:
            if tfd is not None:
                os.close(tfd)
            os.close(pfd)
        # A read grant is a dead no-op unless the grantee can traverse every
        # ancestor directory. Give them traverse-only (x, no listing) on the
        # ancestors so the share actually works — the surgical "just this file".
        traversed = self._grant_ancestor_traverse(rel, shared_with) if shared_with else []
        _audit("props.set", user, path=rel, granted_traverse=traversed)
        return web.json_response({"ok": True, "granted_traverse": traversed})

    def _grant_ancestor_traverse(self, rel: str, entries: list) -> list:
        """Add traverse-only (x) ACLs for `entries` on ancestor directories of
        `rel` that the grantee CANNOT already traverse — so a shared file becomes
        reachable, WITHOUT adding a named entry where they already have group/other
        access (a named --x entry overrides the group entry and would DOWNGRADE
        them to traverse-only). Race-safe (openat per dir)."""
        granted = []
        parent = os.path.dirname(rel)
        while parent:
            try:
                dfd = common.opendir_beneath(parent)
            except OSError:
                break
            try:
                st = os.fstat(dfd)
                for flag, name in entries:
                    if _dir_traversable(dfd, st, flag, name):
                        continue   # already reachable — adding an ACL would downgrade
                    # -n: do NOT recalculate the mask. setfacl would otherwise
                    # widen it to the union of every group-class entry, quietly
                    # restoring grants that a narrowed mask had revoked — a
                    # share of one file must not re-open a folder's other ACLs.
                    _acl_apply_fd(dfd, False, ["-n", "-m", f"{flag}:{name}:x"])
                    # …but a mask without x makes the entry we just added
                    # effective "---" and the share stays dead. That is exactly
                    # what happens on a dir that had NO extended ACL before this
                    # grant (a fresh 0700 _secrets dir): the mask materializes
                    # from the group bits, i.e. "---". Widen the mask by the x
                    # bit ALONE — never a full recalc, so every r/w bit a
                    # narrowed mask deliberately revokes stays revoked.
                    ents = common.acl_entries(dfd) or []
                    mask = next((p for t, p, _ in ents if t == common._ACL_MASK), None)
                    if mask is not None and not (mask & 1):
                        bits = ("r" if mask & 4 else "") + ("w" if mask & 2 else "") + "x"
                        _acl_apply_fd(dfd, False, ["-n", "-m", "m::" + bits])
                    if parent not in granted:
                        granted.append(parent)
            except (OSError, KeyError, subprocess.SubprocessError):
                pass
            finally:
                os.close(dfd)
            parent = os.path.dirname(parent)
        return granted

    # --- sharing, in people ------------------------------------------------
    # /fs/props is the octal view (kept, behind the panel's Advanced drawer);
    # these two are the everyday one. Same authorization either way: only the
    # owner or an admin may change who can open something.

    async def fs_share_get(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        rel_in = request.query.get("path", "")
        p = common.resolve_repo_path(rel_in)
        if p is None or not p.exists():
            return web.json_response({"error": "not found"}, status=404)
        rel = str(p.relative_to(common.REPO_ROOT.resolve()))
        uid, gids = _uid_gids(user)
        if not _fs_can(p, uid, gids, False) and not _is_admin(user):
            return web.json_response({"error": "no access"}, status=403)
        try:
            return web.json_response(_share_state(p, rel, user))
        except (OSError, KeyError) as e:
            return web.json_response({"error": str(e)}, status=500)

    async def fs_share_set(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        data = await request.json()
        p = common.resolve_repo_path(data.get("path", ""))
        if p is None or not p.exists():
            return web.json_response({"error": "not found"}, status=404)
        if not _owns_or_admin(p, user):
            return web.json_response({"error": "only the owner (or an admin) can change who has access"},
                                     status=403)
        rel = str(p.relative_to(common.REPO_ROOT.resolve()))
        # The same exception /fs/props makes: a secret is the OWNER's to widen.
        # ADMIN_GROUP is "sudo" here, so without this any platform admin could
        # hand a colleague's credential around from the root hub. Closing one
        # (scope "private") stays available to an admin, as it always was.
        if (common.is_secret_path(rel) and _owner_name(p) != user
                and data.get("scope") != "private"):
            return web.json_response(
                {"error": "only the owner can widen access to something under _secrets/"},
                status=403)
        try:
            res = await asyncio.to_thread(_share_apply, p, rel, data, user)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except KeyError:
            return web.json_response({"error": "no such user or group"}, status=400)
        except (OSError, subprocess.SubprocessError) as e:
            return web.json_response({"error": str(e)}, status=500)
        # A grant nobody can reach is a dead grant: open the ancestors just
        # enough (traverse, no listing) for the new audience to get here.
        traversed = (self._grant_ancestor_traverse(rel, res["share_with"])
                     if res.get("share_with") else [])
        # kbindexer joins every shared group, but it is a service account with no
        # session to reload — reporting it would put "kbindexer" in front of the
        # user on every single share.
        changed = sorted(res["changed"] - {INDEXER_USER})
        # Their backends were started by runuser with the old group list, so
        # neither the grant nor its removal reaches them until they restart.
        await asyncio.to_thread(_restart_backends, changed)
        _audit("share.set", user, path=rel, scope=res["scope"],
               group=res.get("group", ""), forked=bool(res.get("forked")),
               regrouped=res.get("regrouped", 0))
        return web.json_response({
            "ok": True, "scope": res["scope"], "group": res.get("group", ""),
            "forked": bool(res.get("forked")), "restarted": changed,
            "regrouped": res.get("regrouped", 0), "acl_walked": res.get("acl_walked", 0),
            "granted_traverse": traversed,
            "state": _share_state(p, rel, user),
        })

    # --- admin: user & group management (sudo-group admins only, runs as root) ---
    def _require_admin(self, request: web.Request) -> str | None:
        user = self.current_user(request)
        return user if (user and _is_admin(user)) else None

    async def admin_me(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        return web.json_response({"user": user, "admin": bool(user and _is_admin(user))})

    async def admin_list(self, request: web.Request) -> web.Response:
        if not self._require_admin(request):
            return web.json_response({"error": "admin only"}, status=403)
        return web.json_response({"users": _human_users(), "groups": _shared_groups()})

    # --- egress: the ONLY way network reaches an artifact -------------------
    # The artifact sandbox keeps connect-src 'none' forever; instead the bridge
    # forwards kb-fetch here. The hub enforces an admin-approved per-artifact
    # domain allowlist, injects secrets server-side (resolved by reading the
    # `_secrets/` file AS the requesting viewer — kernel-checked via runuser,
    # so "can use the key" is exactly "can read the file"), and audit-logs
    # every call. Redirects are refused so an allowlisted host can't bounce
    # the request somewhere else.

    # The allowlist lives IN THE REPO (.os/egress.json, root:kb-users 644):
    # everyone can read it (transparency), git snapshots it (audit trail), and
    # WRITE access to the file IS the delegation — an admin grants a user rw
    # via the normal permissions UI, and from then on that user (and any agent
    # running as them) may change network access: through these endpoints or by
    # editing the file directly. The kernel is the authority either way; the
    # loader validates every entry so a hand-edit can't smuggle bad shapes in.
    EGRESS_FILE = common.company_config("egress.json")
    EGRESS_LOG = Path("/var/log/kb/egress.log")
    _DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*(:\d{1,5})?$")

    def _egress_load(self) -> dict:
        try:
            raw = json.loads(self.EGRESS_FILE.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out = {}
        for art, entry in raw.items():
            if not (isinstance(art, str) and art.endswith(".html")
                    and common.resolve_repo_path(art) is not None):
                continue
            doms = entry.get("domains") if isinstance(entry, dict) else None
            if not isinstance(doms, list):
                continue
            doms = [d for d in doms
                    if isinstance(d, str) and self._DOMAIN_RE.match(d.strip().lower())]
            if doms:
                out[art.strip("/")] = {"domains": [d.strip().lower() for d in doms]}
        return out

    def _egress_save(self, cfg: dict) -> None:
        """In-place write (NOT rename): the file's ACLs carry the delegation,
        and a rename-over would silently drop them."""
        common.write_company_config("egress.json", (json.dumps(cfg, indent=2) + "\n").encode(),
                                    in_place=True)

    def _can_write_as(self, user: str, path: Path) -> bool:
        """Does the kernel say this user may WRITE `path`? Checked AS the user
        (runuser + test -w), so modes, ACLs and ancestor traversal all count —
        'hard access', not a role list."""
        r = subprocess.run(["/usr/sbin/runuser", "-u", user, "--",
                            "/usr/bin/test", "-w", str(path)],
                           capture_output=True, timeout=10)
        return r.returncode == 0

    def _can_edit_egress(self, user: str) -> bool:
        """Admins, or anyone granted write access to the allowlist file via
        the normal file-permissions UI."""
        return _is_admin(user) or self._can_write_as(user, self.EGRESS_FILE)

    def _read_as_user(self, user: str, rel: str, cap: int) -> bytes | None:
        """Read a repo file with the USER's authority (runuser + cat): the
        kernel evaluates modes, ACLs and ancestor traversal — no reimplementation.
        Returns None if the user cannot read it (or it exceeds cap)."""
        p = common.resolve_repo_path(rel)
        if p is None or not p.is_file():
            return None
        r = subprocess.run(["/usr/sbin/runuser", "-u", user, "--", "/bin/cat", "--", str(p)],
                           capture_output=True, timeout=30)
        if r.returncode != 0 or len(r.stdout) > cap:
            return None
        return r.stdout

    async def admin_egress_get(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        return web.json_response({"entries": self._egress_load(),
                                  "can_edit": self._can_edit_egress(user)})

    async def admin_egress_set(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user or not self._can_edit_egress(user):
            return web.json_response(
                {"error": "you need write access to .os/egress.json (ask an admin to grant it)"},
                status=403)
        d = await request.json()
        artifact = str(d.get("artifact", "")).strip().strip("/")
        domains = d.get("domains")
        p = common.resolve_repo_path(artifact)
        if p is None or not artifact.endswith(".html"):
            return web.json_response({"error": "artifact must be a repo .html path"}, status=400)
        if not isinstance(domains, list) or len(domains) > 10:
            return web.json_response({"error": "domains must be a list (max 10)"}, status=400)
        domains = [str(x).strip().lower() for x in domains if str(x).strip()]
        for dom in domains:
            if not self._DOMAIN_RE.match(dom):
                return web.json_response({"error": f"bad domain: {dom}"}, status=400)
        cfg = self._egress_load()
        if domains:
            cfg[artifact] = {"domains": domains}
        else:
            cfg.pop(artifact, None)
        self._egress_save(cfg)
        return web.json_response({"ok": True, "entries": cfg})

    def _egress_audit(self, line: dict) -> None:
        try:
            self.EGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(self.EGRESS_LOG, "a") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass

    async def egress(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        d = await request.json()
        artifact = str(d.get("artifact", "")).strip().strip("/")
        url = str(d.get("url", ""))
        method = str(d.get("method", "GET")).upper()
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            return web.json_response({"error": "bad method"}, status=400)
        entry = self._egress_load().get(artifact)
        if not entry:
            return web.json_response(
                {"error": "this artifact has no network access — an admin must allow it"}, status=403)
        u = urllib.parse.urlsplit(url)
        host = (u.netloc or "").lower()
        local = host.split(":")[0] in ("127.0.0.1", "localhost")
        if u.scheme != "https" and not (u.scheme == "http" and local):
            return web.json_response({"error": "https only"}, status=400)
        if host not in entry.get("domains", []):
            return web.json_response({"error": f"domain {host} is not allowed for this artifact"}, status=403)

        loop = asyncio.get_event_loop()

        async def resolve(value: str) -> str | None:
            """secret:<repo path> -> file content, read AS the caller."""
            if not isinstance(value, str) or not value.startswith("secret:"):
                return value
            rel = value[len("secret:"):].strip().strip("/")
            if not common.is_secret_path(rel):
                return None   # only _secrets/ files are injectable
            data = await loop.run_in_executor(
                None, self._read_as_user, user, rel, 64 * 1024)
            return data.decode(errors="replace").strip() if data is not None else None

        headers = {}
        for k, v in (d.get("headers") or {}).items():
            if not re.match(r"^[A-Za-z0-9-]{1,64}$", str(k)):
                return web.json_response({"error": f"bad header name: {k}"}, status=400)
            rv = await resolve(str(v))
            if rv is None:
                return web.json_response({"error": f"secret in header {k} is not readable by you"}, status=403)
            headers[str(k)] = rv

        req_kwargs: dict = {}
        form = d.get("form")
        if form is not None:
            fd = aiohttp.FormData()
            for k, v in (form.get("fields") or {}).items():
                rv = await resolve(str(v))
                if rv is None:
                    return web.json_response({"error": f"secret in field {k} is not readable by you"}, status=403)
                fd.add_field(str(k), rv)
            f = form.get("file")
            if f:
                rel = str(f.get("path", "")).strip().strip("/")
                blob = await loop.run_in_executor(
                    None, self._read_as_user, user, rel, 256 * 1024 * 1024)
                if blob is None:
                    return web.json_response({"error": "file is not readable by you (or too large)"}, status=403)
                fd.add_field(str(f.get("field", "file")), blob,
                             filename=str(f.get("filename") or os.path.basename(rel)),
                             content_type=str(f.get("content_type") or "application/octet-stream"))
            req_kwargs["data"] = fd
        elif d.get("json") is not None:
            req_kwargs["json"] = d["json"]
        elif d.get("body") is not None:
            req_kwargs["data"] = str(d["body"]).encode()

        started = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.request(method, url, headers=headers,
                                     allow_redirects=False, **req_kwargs) as resp:
                    body = await _read_capped(resp.content, 32 * 1024 * 1024)
                    if body is None:
                        return web.json_response({"error": "response too large"}, status=502)
                    self._egress_audit({"ts": int(started), "user": user, "artifact": artifact,
                                        "method": method, "url": url, "status": resp.status,
                                        "resp_bytes": len(body)})
                    return web.json_response({
                        "ok": True, "status": resp.status,
                        "content_type": resp.headers.get("content-type", ""),
                        "body_b64": base64.b64encode(body).decode()})
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self._egress_audit({"ts": int(started), "user": user, "artifact": artifact,
                                "method": method, "url": url, "error": str(e)[:200]})
            return web.json_response({"error": f"upstream error: {e}"}, status=502)

    # --- dictation: speech-to-text for everyone, key readable by no one ------
    # The ElevenLabs key is a COMPANY credential, and that makes it the exact
    # INVERSE of the `_secrets/` model directly above: there, "can use the key"
    # is deliberately the same thing as "can read the file", enforced by the
    # kernel. Here everyone may spend the key and nobody may read it — so it
    # cannot live in the repo, and it cannot live in a per-user backend either
    # (those are spawned `runuser -u <user>`, so anything the backend can read
    # the user's own shell, cron job or agent can read). It lives beside the
    # session key: 0600 root:root under /etc/kb, opened only in this process.
    #
    # The caller never chooses the URL. That is the load-bearing detail — it is
    # why a key with speech-to-text scope can never be pointed at
    # /v1/text-to-speech, /v1/voices/add or /v1/user by anyone who can log in.
    STT_KEY_FILE = common.ETC_DIR / "elevenlabs.key"
    STT_URL = os.environ.get("KB_STT_URL", "https://api.elevenlabs.io/v1/speech-to-text")
    STT_MODEL = os.environ.get("KB_STT_MODEL", "scribe_v2")
    STT_LOG = Path("/var/log/kb/stt.log")
    STT_MAX_BYTES = 12 * 1024 * 1024              # ~50 min of 32 kbps mono opus
    STT_DAILY_BYTES = int(os.environ.get("KB_STT_DAILY_BYTES", 60 * 60 * 4096))
    STT_MIN_BYTES = 1024                          # smaller than this is a stray tap
    _STT_EXT = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
                "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
                "audio/mpeg": "mp3", "audio/flac": "flac"}

    def _load_stt_key(self) -> str | None:
        """Fail SOFT. A missing key must 503 one route, never raise out of
        __init__ — the unit is Restart=on-failure, so a raise here would turn
        "dictation isn't set up" into a crash-looping platform. Accepts either a
        bare key or the `header = "xi-api-key: …"` curl-config form."""
        try:
            raw = self.STT_KEY_FILE.read_text().strip()
        except OSError:
            return None
        m = re.search(r"xi-api-key:\s*([A-Za-z0-9_\-]+)", raw)
        return m.group(1) if m else (raw.split()[0] if raw else None)

    def _stt_quota(self, user: str, nbytes: int) -> bool:
        """A guardrail against one person burning the company's credit, not
        billing: in-memory, per UTC day, resets on restart. That is the right
        trade — the audit log is the record, this is just the brake."""
        day = time.strftime("%Y-%m-%d", time.gmtime())
        seen_day, used = self._stt_used.get(user, (day, 0))
        if seen_day != day:
            used = 0
        if used + nbytes > self.STT_DAILY_BYTES:
            return False
        self._stt_used[user] = (day, used + nbytes)
        return True

    def _stt_audit(self, line: dict) -> None:
        # Records THAT someone dictated — never WHAT they said. This is a
        # microphone in an office; the transcript is nobody else's business.
        try:
            self.STT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(self.STT_LOG, "a") as f:
                f.write(json.dumps(line) + "\n")
        except OSError:
            pass

    async def stt(self, request: web.Request) -> web.Response:
        """Audio in, transcript out. The API key never leaves this process, and
        no upstream response body is ever forwarded verbatim."""
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        if not self._stt_key:
            return web.json_response(
                {"error": "dictation is not set up on this server"}, status=503)
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not ctype.startswith("audio/"):
            return web.json_response({"error": "expected an audio body"}, status=400)

        # Read with an explicit cap (see _read_capped for why a single read()
        # will not do). The app-wide client_max_size is 2 GiB (it has to be, for
        # uploads), so this is the only thing standing between a stray
        # multi-gigabyte POST and the hub's memory.
        body = await _read_capped(request.content, self.STT_MAX_BYTES)
        if body is None:
            return web.json_response({"error": "that recording is too long"}, status=413)
        if len(body) < self.STT_MIN_BYTES:
            # A tap, not speech. Answer without spending an API call.
            return web.json_response({"ok": True, "text": ""})
        if not self._stt_quota(user, len(body)):
            return web.json_response(
                {"error": "daily dictation limit reached — try again tomorrow"}, status=429)

        fd = aiohttp.FormData()
        fd.add_field("file", body, content_type=ctype,
                     filename="dictation." + self._STT_EXT.get(ctype, "bin"))
        fd.add_field("model_id", self.STT_MODEL)
        fd.add_field("tag_audio_events", "false")   # defaults TRUE: "(laughter)" in your prose
        fd.add_field("timestamps_granularity", "none")
        fd.add_field("diarize", "false")
        fd.add_field("enable_logging", "false")     # zero retention upstream
        lang = (request.query.get("lang") or "").strip().lower()
        if re.fullmatch(r"[a-z]{2,3}", lang):
            fd.add_field("language_code", lang)

        started = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(self.STT_URL, headers={"xi-api-key": self._stt_key},
                                  data=fd, allow_redirects=False) as resp:
                    raw = await _read_capped(resp.content, 8 * 1024 * 1024)
                    ms = int((time.time() - started) * 1000)
                    if resp.status != 200:
                        self._stt_audit({"ts": int(started), "user": user, "bytes": len(body),
                                         "status": resp.status, "ms": ms})
                        # Never forward the upstream body — it is not ours to leak,
                        # and it can carry key metadata in an auth error.
                        msg = ("dictation is rate-limited — wait a moment"
                               if resp.status == 429 else
                               "speech-to-text is unavailable right now")
                        return web.json_response({"error": msg}, status=502)
                    try:
                        j = json.loads(raw)
                    except (ValueError, TypeError):
                        # TypeError = raw is None = over the cap. Audit it: this
                        # was the one /stt failure path that wrote no line at
                        # all, so a truncated transcript looked, in the log,
                        # exactly like a dictation that never happened.
                        self._stt_audit({"ts": int(started), "user": user, "bytes": len(body),
                                         "status": 502, "ms": ms,
                                         "error": "unreadable upstream response"})
                        return web.json_response(
                            {"error": "unreadable response from upstream"}, status=502)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self._stt_audit({"ts": int(started), "user": user, "bytes": len(body),
                             "error": str(e)[:200]})
            return web.json_response({"error": f"upstream error: {e}"}, status=502)

        text = (j.get("text") or "").strip()
        self._stt_audit({"ts": int(started), "user": user, "bytes": len(body), "status": 200,
                         "ms": int((time.time() - started) * 1000), "chars": len(text),
                         "secs": j.get("audio_duration_secs")})
        return web.json_response({"ok": True, "text": text,
                                  "language_code": j.get("language_code", "")})

    async def admin_launchers(self, request: web.Request) -> web.Response:
        """Replace the company-wide launcher buttons. The list lives in the data
        repo at .os/launchers.json (root-owned 644 like the agent skills:
        everyone reads it, only an admin — via this root endpoint — writes it)."""
        if not self._require_admin(request):
            return web.json_response({"error": "admin only"}, status=403)
        buttons, err = common.validate_launchers(await request.json())
        if err:
            return web.json_response({"error": err}, status=400)
        common.write_company_config("launchers.json",
                                    (json.dumps({"buttons": buttons}, indent=2) + "\n").encode(),
                                    in_place=False)
        return web.json_response({"ok": True, "buttons": buttons})

    async def admin_settings(self, request: web.Request) -> web.Response:
        """Company-wide settings: {"set": {key: value}, "unset": [key]}. The
        file lives in the data repo at .os/settings.json (root:kb-users 0644:
        everyone reads it, git snapshots it, only an admin — via this root
        endpoint — writes it). Written in place, like the egress allowlist, so
        a write-ACL granted on the file would survive."""
        admin = self._require_admin(request)
        if not admin:
            return web.json_response({"error": "admin only"}, status=403)
        try:
            data = await request.json()
        except ValueError:
            return web.json_response({"error": "expected JSON"}, status=400)
        change, err = kbsettings.validate_change(data, "company")
        if err:
            return web.json_response({"error": err}, status=400)
        current = kbsettings.load_layer(kbsettings.company_file(), "company")["values"]
        values = kbsettings.apply_change(current, change)
        common.write_company_config(kbsettings.FILE_NAME, kbsettings.dumps(values), in_place=True)
        _audit("settings.company", admin, set=sorted(change["set"]) or None,
               unset=change["unset"] or None)
        return web.json_response({"ok": True, "values": values})

    async def admin_create_user(self, request: web.Request) -> web.Response:
        if not self._require_admin(request):
            return web.json_response({"error": "admin only"}, status=403)
        d = await request.json()
        u = str(d.get("username", "")).strip()
        first = str(d.get("first", "")).strip()
        last = str(d.get("last", "")).strip()
        email = str(d.get("email", "")).strip()
        pw = str(d.get("password", ""))
        if not _USERNAME_RE.match(u):
            return web.json_response({"error": "username must be lowercase letters/digits/_ (start with a letter)"}, status=400)
        if not first or not last:
            return web.json_response({"error": "first and last name are required"}, status=400)
        if not _EMAIL_RE.match(email):
            return web.json_response({"error": "invalid email"}, status=400)
        # A KB password is also an SSH password (port 2007, rclone uses it), so
        # it is exposed to online guessing that this box cannot see. Six
        # characters is not a password; twelve of anything memorable is.
        if len(pw) < 12:
            return web.json_response({"error": "password must be at least 12 characters \u2014 use a short phrase"}, status=400)
        # chpasswd reads one user:password per LINE from stdin, so a newline in
        # the password is a second instruction to a root helper.
        if "\n" in pw or "\r" in pw:
            return web.json_response({"error": "password cannot contain line breaks"}, status=400)
        try:
            pwd.getpwnam(u)
            return web.json_response({"error": "user already exists"}, status=409)
        except KeyError:
            pass

        viewer = str(d.get("kind", "full")) == "viewer"
        shell = VIEWER_SHELL if viewer else FULL_SHELL
        if _run(["useradd", "-m", "-s", shell, "-c", f"{first} {last}", u]).returncode != 0:
            return web.json_response({"error": "useradd failed"}, status=500)
        if viewer:
            _cron_deny_set(u, True)

        def rollback():
            _run(["userdel", "-r", u])

        if _run(["chpasswd"], stdin=f"{u}:{pw}\n").returncode != 0:
            rollback(); return web.json_response({"error": "setting password failed"}, status=500)
        if _run(["gpasswd", "-a", u, "kb-users"]).returncode != 0:
            rollback(); return web.json_response({"error": "adding to kb-users failed"}, status=500)
        # users/ is group-writable, so any member can pre-plant an entry named
        # after a future account — and new hires are named in the KB, so the
        # name is guessable. `install -d` would follow a symlink there and hand
        # its TARGET to the new user (reproduced: a victim dir went
        # alice:alice -> nobody:nogroup). mkdir at a pinned dir fd fails
        # EEXIST on anything already sitting at the name, symlink included.
        try:
            e = pwd.getpwnam(u)
            pfd = common.opendir_beneath("users")
            try:
                os.mkdir(u, 0o700, dir_fd=pfd)
                dfd = os.open(u, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=pfd)
                try:
                    os.fchown(dfd, e.pw_uid, e.pw_gid)
                    os.fchmod(dfd, 0o700)
                finally:
                    os.close(dfd)
            finally:
                os.close(pfd)
        except (OSError, KeyError) as exc:
            rollback()
            return web.json_response(
                {"error": f"could not create the account's folder: {exc}"}, status=500)
        # kb_users is the DB-side mirror of the kb-users OS group: joining it is
        # what carries over access to company-shared app tables (games, todos,
        # timesheets...) that live in someone's personal u_<user> schema. Without
        # it a new account can open every page but every query behind them fails.
        _audit("user.create", self.current_user(request), target=u,
               kind="viewer" if viewer else "full", email=email)
        sql = (f'CREATE ROLE "{u}" LOGIN; CREATE SCHEMA "u_{u}" AUTHORIZATION "{u}"; '
               f'ALTER ROLE "{u}" SET search_path = "u_{u}", kb, public; '
               f'GRANT kb_users TO "{u}";')
        r = _psql_su(sql)
        if r.returncode != 0:
            rollback(); _run(["rm", "-rf", str(pdir)])
            return web.json_response({"error": f"postgres role setup failed: {r.stderr[:200]}"}, status=500)
        profs = _profiles_load()
        profs[u] = {"first": first, "last": last, "email": email}
        _profiles_save(profs)
        return web.json_response({"ok": True, "username": u})

    async def admin_set_shell(self, request: web.Request) -> web.Response:
        """Toggle an account between full (shell + cron) and viewer (neither).
        usermod flips /etc/passwd; cron.deny follows; the user's backend is
        killed so it re-reads its identity (the hub respawns it on demand)."""
        admin = self._require_admin(request)
        if not admin:
            return web.json_response({"error": "admin only"}, status=403)
        d = await request.json()
        u = str(d.get("username", "")).strip()
        want_shell = bool(d.get("shell"))
        if not _USERNAME_RE.match(u):
            return web.json_response({"error": "bad username"}, status=400)
        if u in PROTECTED_USERS or u == admin:
            return web.json_response({"error": "this account's shell cannot be changed here"}, status=400)
        try:
            e = pwd.getpwnam(u)
        except KeyError:
            return web.json_response({"error": "no such user"}, status=404)
        if not 1000 <= e.pw_uid < 65000:
            return web.json_response({"error": "not a managed account"}, status=400)
        if _run(["usermod", "-s", FULL_SHELL if want_shell else VIEWER_SHELL, u]).returncode != 0:
            return web.json_response({"error": "usermod failed"}, status=500)
        _cron_deny_set(u, not want_shell)
        # the per-user backend caches its capabilities at boot — recycle it
        _run(["pkill", "-KILL", "-u", u, "-f", "kb_platform.user_server"])
        return web.json_response({"ok": True, "username": u, "shell": want_shell})

    async def admin_delete_user(self, request: web.Request) -> web.Response:
        admin = self._require_admin(request)
        if not admin:
            return web.json_response({"error": "admin only"}, status=403)
        d = await request.json()
        u = str(d.get("username", "")).strip()
        if u in PROTECTED_USERS or u == admin:
            return web.json_response({"error": "this user cannot be removed"}, status=400)
        try:
            e = pwd.getpwnam(u)
        except KeyError:
            return web.json_response({"error": "no such user"}, status=404)
        if e.pw_uid < 1000:
            return web.json_response({"error": "cannot remove a system user"}, status=400)
        # Kill any running processes (e.g. a live per-user backend) so userdel
        # can proceed, and drop the stale cached session + socket.
        _run(["pkill", "-KILL", "-u", u])
        self._user_sessions.pop(u, None)
        _run(["rm", "-rf", str(common.USER_SOCK_DIR / u)])
        # Postgres: drop everything the role owns (schema, grants) then the role.
        _psql_su(f'DROP OWNED BY "{u}" CASCADE;')
        _psql_su(f'DROP ROLE IF EXISTS "{u}";')
        # Strip any named ACL grants this user held, so a recycled uid can't
        # silently inherit files that were individually shared with them.
        _run(["setfacl", "-R", "-x", f"u:{u}", str(common.REPO_ROOT)])
        _run(["setfacl", "-R", "-d", "-x", f"u:{u}", str(common.REPO_ROOT)])
        # userdel does NOT remove the crontab spool; a leftover file (old uid)
        # blocks any future account with the same name from using cron — and
        # keeps the dead user's jobs running under whoever inherits the name.
        _run(["crontab", "-r", "-u", u])
        _run(["userdel", "-rf", u])
        _cron_deny_set(u, False)
        _run(["rm", "-rf", str(common.REPO_ROOT / "users" / u)])
        profs = _profiles_load()
        profs.pop(u, None)
        _profiles_save(profs)
        return web.json_response({"ok": True})

    async def admin_create_group(self, request: web.Request) -> web.Response:
        if not self._require_admin(request):
            return web.json_response({"error": "admin only"}, status=403)
        name = str((await request.json()).get("name", "")).strip()
        if not _GROUP_RE.match(name):
            return web.json_response({"error": "group name must be lowercase letters/digits/-/_"}, status=400)
        try:
            grp.getgrnam(name)
            return web.json_response({"error": "group already exists"}, status=409)
        except KeyError:
            pass
        if _run(["groupadd", name]).returncode != 0:
            return web.json_response({"error": "groupadd failed"}, status=500)
        return web.json_response({"ok": True, "name": name})

    async def admin_delete_group(self, request: web.Request) -> web.Response:
        """Delete a shared group. Guards: never kb-users (the platform group),
        never system groups, never a group that is some user's primary group."""
        if not self._require_admin(request):
            return web.json_response({"error": "admin only"}, status=403)
        name = str((await request.json()).get("name", "")).strip()
        if not _GROUP_RE.match(name):
            return web.json_response({"error": "bad group name"}, status=400)
        if name == "kb-users":
            return web.json_response({"error": "kb-users is the platform group — it cannot be deleted"}, status=400)
        try:
            g = grp.getgrnam(name)
        except KeyError:
            return web.json_response({"error": "no such group"}, status=404)
        if not 1000 <= g.gr_gid < 65000:
            return web.json_response({"error": "system groups cannot be deleted"}, status=400)
        if any(e.pw_gid == g.gr_gid for e in pwd.getpwall()):
            return web.json_response({"error": "group is a user's primary group"}, status=400)
        if _run(["groupdel", name]).returncode != 0:
            return web.json_response({"error": "groupdel failed"}, status=500)
        return web.json_response({"ok": True})

    async def admin_group_member(self, request: web.Request) -> web.Response:
        admin = self._require_admin(request)
        if not admin:
            return web.json_response({"error": "admin only"}, status=403)
        d = await request.json()
        group = str(d.get("group", "")).strip()
        user = str(d.get("username", "")).strip()
        action = d.get("action")
        if not _GROUP_RE.match(group) or not _NAME_RE.match(user) or action not in ("add", "remove"):
            return web.json_response({"error": "bad request"}, status=400)
        try:
            grp.getgrnam(group)
            pwd.getpwnam(user)
        except KeyError:
            return web.json_response({"error": "no such user or group"}, status=404)
        if group in ("sudo", "root", "docker") and action == "add":
            _audit("group.member", admin, ok=False, group=group, target=user,
                   action=action, reason="privileged group refused")
            return web.json_response({"error": "refusing to grant privileged group via UI"}, status=400)
        flag = "-a" if action == "add" else "-d"
        if _run(["gpasswd", flag, user, group]).returncode != 0:
            return web.json_response({"error": "membership change failed"}, status=500)
        # runuser fixed this user's supplementary gids when their backend was
        # spawned, so without this the change reaches the filesystem but not the
        # app — the classic "I added them and they still can't see it".
        await asyncio.to_thread(_restart_backends, [user])
        _audit("group.member", admin, group=group, target=user, action=action)
        return web.json_response({"ok": True, "restarted": [user]})

    async def _bridge_ws(self, request, target_session, target_url, extra_headers,
                         on_accept=None):
        """Bridge the client socket to `target_url`. `on_accept` runs once, as
        soon as the DOWNSTREAM handshake succeeds — the client's own socket was
        already prepared above, so it is the upstream one that carries the
        accept/refuse answer. Swallowing its errors keeps the _audit contract:
        recording an event must never be able to break the thing it records."""
        server_ws = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024 * 1024)
        await server_ws.prepare(request)
        try:
            async with target_session.ws_connect(target_url, headers=extra_headers,
                                                  max_msg_size=16 * 1024 * 1024) as client_ws:
                if on_accept is not None:
                    try:
                        on_accept()
                    except Exception:
                        pass

                async def pump(src, dst, is_server_side):
                    async for msg in src:
                        if msg.type == WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                        elif msg.type == WSMsgType.BINARY:
                            await dst.send_bytes(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING,
                                          WSMsgType.CLOSED, WSMsgType.ERROR):
                            break

                t1 = asyncio.create_task(pump(server_ws, client_ws, True))
                t2 = asyncio.create_task(pump(client_ws, server_ws, False))
                done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except aiohttp.ClientError:
            pass
        if not server_ws.closed:
            await server_ws.close()
        return server_ws


# Every URL under /static carries a ?v= stamp that build.mjs rewrites on each
# build — in app.html, and on the font urls inside style.css — so a changed file
# is a NEW URL and the old one can be cached for good: the browser never spends
# a round trip revalidating a bundle it already has (with no Cache-Control at
# all it revalidated every asset on every reload, each one a full trip through
# the tunnel before a line of JS could run), and a deploy still lands the
# instant app.html — served no-store — points at the new stamp. The stamp is
# what makes `immutable` honest; without it this header would pin a stale
# bundle for a year. The HTML shells are the exception: they CARRY the stamps
# and are served no-store by their own routes, so a direct fetch of
# /static/app.html must not get a year either.
STATIC_CACHE = "public, max-age=31536000, immutable"


@web.middleware
async def static_cache(request: web.Request, handler):
    resp = await handler(request)
    if (request.path.startswith("/static/") and not request.path.endswith(".html")
            and resp.status in (200, 304)):
        resp.headers["Cache-Control"] = STATIC_CACHE
    return resp


def make_app() -> web.Application:
    # client_max_size must match the per-user backend (2 GiB): the hub both
    # accepts /fs/upload directly AND buffers proxied /api/upload bodies, so
    # the default 1 MiB cap silently rejected any real photo/audio upload.
    hub = Hub()
    # An older install kept the platform's JSON config in .claude/; move it to
    # .os/ (idempotent, fd-pinned, nothing deleted — see common).
    try:
        for line in common.migrate_company_config():
            log.warning("config migration: %s", line)
    except OSError as e:
        log.error("config migration failed: %s", e)
    # bootstrap the egress allowlist (deny-all) so its write-ACL can delegate
    # network administration to non-admins from day one
    try:
        if not hub.EGRESS_FILE.exists():
            hub._egress_save({})
    except OSError:
        pass
    app = web.Application(client_max_size=2 * 1024 * 1024 * 1024,
                          middlewares=[static_cache])
    app["hub"] = hub
    app.router.add_get("/login", hub.login_page)
    app.router.add_post("/login", hub.do_login)
    app.router.add_get("/logout", hub.do_logout)
    app.router.add_get("/", hub.index)
    # Deep links: repo paths serve the app (which opens the file client-side).
    # The pattern is anchored to the three top-level areas, so it can never
    # shadow /api, /fs, /admin, /static, /ws, /pty or /egress.
    app.router.add_get("/{path:(?:company|projects|users)(?:/.*)?}", hub.deep_link)
    app.router.add_get("/api/doc-epoch", hub.doc_epoch)
    app.router.add_get("/api/presence", hub.presence)
    app.router.add_get("/api/vc/{op}", hub.vc_proxy)
    app.router.add_get("/ws/doc/{path:.*}", hub.proxy_doc)
    app.router.add_get("/pty", hub.proxy_pty)
    # Privileged filesystem admin (hub handles these as root; NOT proxied).
    app.router.add_post("/fs/newfile", hub.fs_newfile)
    app.router.add_post("/fs/upload", hub.fs_upload)
    app.router.add_post("/fs/upload/begin", hub.fs_upload_begin)
    app.router.add_post("/fs/upload/chunk", hub.fs_upload_chunk)
    app.router.add_post("/fs/upload/finish", hub.fs_upload_finish)
    app.router.add_post("/fs/upload/abort", hub.fs_upload_abort)
    app.router.add_get("/fs/props", hub.fs_props_get)
    app.router.add_post("/fs/props", hub.fs_props_set)
    app.router.add_get("/fs/share", hub.fs_share_get)
    app.router.add_post("/fs/share", hub.fs_share_set)
    # Admin (sudo-group only): user + group management, run as root.
    app.router.add_get("/admin/me", hub.admin_me)
    app.router.add_get("/admin/list", hub.admin_list)
    app.router.add_post("/admin/users", hub.admin_create_user)
    app.router.add_post("/admin/users/delete", hub.admin_delete_user)
    app.router.add_post("/admin/users/shell", hub.admin_set_shell)
    app.router.add_post("/admin/groups", hub.admin_create_group)
    app.router.add_post("/admin/groups/member", hub.admin_group_member)
    app.router.add_post("/admin/groups/delete", hub.admin_delete_group)
    app.router.add_post("/admin/launchers", hub.admin_launchers)
    app.router.add_post("/admin/settings", hub.admin_settings)
    app.router.add_get("/admin/egress", hub.admin_egress_get)
    app.router.add_post("/admin/egress", hub.admin_egress_set)
    app.router.add_post("/egress", hub.egress)
    # Dictation. Its own top-level prefix, NOT /api/stt: the `*` catch-all below
    # owns every method under /api that isn't explicitly registered, so an
    # /api/stt would silently proxy GET and OPTIONS into the user backend —
    # which is exactly the process that must never see the key.
    app.router.add_post("/stt", hub.stt)
    app.router.add_static("/static", STATIC_DIR)
    # Everything under /api/* is proxied to the per-user backend.
    app.router.add_route("*", "/api/{tail:.*}", hub.proxy_http)

    async def _reap_loop(app):
        """Deleting a shared folder leaves its managed group behind with nothing
        to tell us — so sweep, rather than hoping every path remembers to."""
        while True:
            await asyncio.sleep(1800)
            try:
                await asyncio.to_thread(_reap_orphan_groups)
            except Exception:
                pass

    async def on_startup(app):
        app["reaper"] = asyncio.create_task(_reap_loop(app))

    app.on_startup.append(on_startup)

    async def on_cleanup(app):
        h: Hub = app["hub"]
        for s in h._user_sessions.values():
            await s.close()
        if h._syncd_session:
            await h._syncd_session.close()

    app.on_cleanup.append(on_cleanup)

    async def _stop_reaper(app):
        t = app.get("reaper")
        if t:
            t.cancel()

    app.on_cleanup.append(_stop_reaper)
    return app


def main() -> None:
    app = make_app()
    web.run_app(app, host=common.HUB_ADDR[0], port=common.HUB_ADDR[1], print=None)


if __name__ == "__main__":
    main()
