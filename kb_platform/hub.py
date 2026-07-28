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
import os
import pwd
import re
import socket
import stat
import subprocess
import time
import urllib.parse
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

from . import common, pam_auth

PLATFORM_ROOT = Path(os.environ.get("KB_PLATFORM_ROOT", "/opt/kb-platform"))
STATIC_DIR = PLATFORM_ROOT / "frontend" / "static"

_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer",
        "upgrade", "proxy-authorization", "proxy-authenticate", "content-length",
        "content-encoding"}

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
    os.access). Kernel-exact including POSIX ACLs — the platform's own share
    feature grants by ACL, so bare mode bits routinely lie here."""
    try:
        st, entries = common.stat_and_acl(path)   # one pinned inode; symlink -> ELOOP
    except OSError:
        return False
    return common.unix_access(st, entries, uid, gids, 2 if need_write else 4)


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
    m = st.st_mode
    if entries is None:
        if st.st_gid == gid:
            return bool((m >> 3) & 1)
        return bool(m & 1)
    # Mirror the kernel for a hypothetical user whose only relevant group is
    # `name`: EVERY matching group-class entry contributes and any grant wins
    # (a named entry for the owning group is legal and must be unioned, not
    # shadowed); if no group-class entry matches, `other` decides.
    group_obj, mask, named_g, other = 0, 7, {}, m & 7
    for tag, perm, qual in entries:
        if tag == common._ACL_GROUP_OBJ:
            group_obj = perm
        elif tag == common._ACL_GROUP:
            named_g[qual] = perm
        elif tag == common._ACL_MASK:
            mask = perm
        elif tag == common._ACL_OTHER:
            other = perm
    granted, matched = 0, False
    if st.st_gid == gid:
        granted, matched = granted | group_obj, True
    if gid in named_g:
        granted, matched = granted | named_g[gid], True
    if matched:
        return bool(granted & mask & 1)
    return bool(other & 1)


def _grp_ok(gid: int) -> bool:
    try:
        grp.getgrgid(gid)
        return True
    except KeyError:
        return False


_ACL_ACCESS = "system.posix_acl_access"
_ACL_DEFAULT = "system.posix_acl_default"


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
        os.setxattr(tfd, _ACL_ACCESS, os.getxattr(tmpf, _ACL_ACCESS))
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


class Hub:
    def __init__(self):
        self.key = common.load_session_key()
        self._user_sessions: dict[str, aiohttp.ClientSession] = {}
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        self._syncd_session: aiohttp.ClientSession | None = None
        self._stt_key = self._load_stt_key()
        self._stt_used: dict[str, tuple[str, int]] = {}   # user -> (utc day, bytes)

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
        asyncio.get_event_loop().run_in_executor(None, proc.wait)
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
    NO_STORE = {"Cache-Control": "no-store, must-revalidate"}

    async def login_page(self, request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "login.html", headers=self.NO_STORE)

    async def do_login(self, request: web.Request) -> web.Response:
        data = await request.post()
        user = str(data.get("username", "")).strip()
        pw = str(data.get("password", ""))
        if not pam_auth.authenticate(user, pw):
            return web.json_response({"error": "invalid credentials"}, status=401)
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
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        headers["X-KB-User"] = user
        body = await request.read()
        try:
            async with sess.request(request.method, url, headers=headers, data=body,
                                    allow_redirects=False) as resp:
                out_body = await resp.read()
                out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP}
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
        # forward the query string — ?e=<epoch> is the lineage gate
        if request.query_string:
            path += "?" + request.query_string
        return await self._bridge_ws(request, self._syncd(), f"http://kb/ws/doc/{path}",
                                     {"X-KB-Auth": token})

    # --- privileged filesystem admin (runs as root; carefully authorized) ---
    # These are the ONLY places the hub mutates the filesystem. Every handler
    # re-derives the caller from the signed cookie and checks Unix authorization
    # (write-to-parent for create; own-or-admin for property changes) before
    # doing anything. All operations are bounded to the repo tree.

    def _inherit_mode(self, parent_mode: int) -> int:
        mode = 0o644
        if parent_mode & 0o020:      # parent group-writable -> file group rw
            mode |= 0o060
        elif parent_mode & 0o040:    # parent group-readable -> file group r
            mode |= 0o040
        if parent_mode & 0o004:      # parent other-readable -> file other r
            mode |= 0o004
        return mode

    def _create_inheriting(self, rel: str, data: bytes | None, exclusive: bool = False,
                           owner: tuple[int, int] | None = None,
                           force_mode: int | None = None) -> str:
        """Create a file (owned by its PARENT's owner/group; needs root) WITHOUT
        following any symlink in the path — the parent dir is reached via
        openat/O_NOFOLLOW and the file is opened O_NOFOLLOW under that dir fd, so
        a symlink planted by a user cannot redirect this root write. Returns owner.
        `owner`/`force_mode` override inheritance — used for `_secrets/` files,
        which must be born private (creator-owned, 0600), never group-open."""
        parent_rel = os.path.dirname(rel)
        name = os.path.basename(rel)
        if not name or "/" in name or name in (".", ".."):
            raise OSError("bad name")
        pfd = common.opendir_beneath(parent_rel)
        try:
            pst = os.fstat(pfd)
            own = owner or (pst.st_uid, pst.st_gid)
            mode = force_mode if force_mode is not None else self._inherit_mode(pst.st_mode)
            flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
            flags |= os.O_EXCL if exclusive else os.O_TRUNC
            fd = os.open(name, flags, mode, dir_fd=pfd)
            try:
                if data:
                    os.write(fd, data)
                os.fchown(fd, *own)
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
        finally:
            os.close(pfd)
        try:
            return pwd.getpwuid(own[0]).pw_name
        except KeyError:
            return str(own[0])

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
        rel_clean = str(p.relative_to(common.REPO_ROOT.resolve()))
        try:
            # secrets are born private: creator-owned, 0600 — shared deliberately
            owner = self._create_inheriting(
                rel_clean, None, exclusive=True,
                owner=(uid, pwd.getpwnam(user).pw_gid) if secret else None,
                force_mode=0o600 if secret else None)
        except FileExistsError:
            return web.json_response({"error": "already exists"}, status=409)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"ok": True, "path": rel, "owner": owner})

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
                rel_clean, bytes(chunks),
                owner=(uid, pwd.getpwnam(user).pw_gid) if secret else None,
                force_mode=0o600 if secret else None)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"ok": True, "path": rel_clean, "owner": owner})

    async def fs_props_get(self, request: web.Request) -> web.Response:
        user = self.current_user(request)
        if not user:
            return web.json_response({"error": "unauthenticated"}, status=401)
        p = common.resolve_repo_path(request.query.get("path", ""))
        if p is None or not p.exists():
            return web.json_response({"error": "not found"}, status=404)
        st = os.lstat(p)
        uid, gids = _uid_gids(user)
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
                os.fchown(tfd, pwd.getpwnam(data["owner"]).pw_uid, -1)
            if data.get("group"):
                if not _NAME_RE.match(data["group"]):
                    return web.json_response({"error": "bad group name"}, status=400)
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
                    _acl_apply_fd(dfd, False, ["-m", f"{flag}:{name}:x"])  # access ACL only
                    if parent not in granted:
                        granted.append(parent)
            except (OSError, KeyError, subprocess.SubprocessError):
                pass
            finally:
                os.close(dfd)
            parent = os.path.dirname(parent)
        return granted

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

    # The allowlist lives IN THE REPO (.claude/egress.json, root:kb-users 644):
    # everyone can read it (transparency), git snapshots it (audit trail), and
    # WRITE access to the file IS the delegation — an admin grants a user rw
    # via the normal permissions UI, and from then on that user (and any agent
    # running as them) may change network access: through these endpoints or by
    # editing the file directly. The kernel is the authority either way; the
    # loader validates every entry so a hand-edit can't smuggle bad shapes in.
    EGRESS_FILE = common.REPO_ROOT / ".claude" / "egress.json"
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
        data = (json.dumps(cfg, indent=2) + "\n").encode()
        pfd = common.opendir_beneath(".claude")
        try:
            fd = os.open("egress.json", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644, dir_fd=pfd)
            try:
                os.ftruncate(fd, 0)
                os.write(fd, data)
                os.fchown(fd, 0, grp.getgrnam("kb-users").gr_gid)
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(pfd)

    def _can_edit_egress(self, user: str) -> bool:
        """Admins, or anyone the kernel says may WRITE the allowlist file
        (granted via the normal file-permissions UI). Checked AS the user, so
        ACLs and group membership all count — 'hard access', not a role list."""
        if _is_admin(user):
            return True
        r = subprocess.run(["/usr/sbin/runuser", "-u", user, "--",
                            "/usr/bin/test", "-w", str(self.EGRESS_FILE)],
                           capture_output=True, timeout=10)
        return r.returncode == 0

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
                {"error": "you need write access to .claude/egress.json (ask an admin to grant it)"},
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
                    body = await resp.content.read(32 * 1024 * 1024 + 1)
                    if len(body) > 32 * 1024 * 1024:
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

        # Read with an explicit cap. NOT `request.content.read(cap + 1)`: that
        # returns whatever is buffered, not the number of bytes asked for, so a
        # single read both fails to detect an oversize body AND silently truncates
        # it into the upstream call. The app-wide client_max_size is 2 GiB (it has
        # to be, for uploads), so this is the only thing standing between a stray
        # multi-gigabyte POST and the hub's memory.
        buf = bytearray()
        while True:
            chunk = await request.content.read(64 * 1024)
            if not chunk:
                break
            buf += chunk
            if len(buf) > self.STT_MAX_BYTES:
                return web.json_response({"error": "that recording is too long"}, status=413)
        body = bytes(buf)
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
                    raw = await resp.content.read(8 * 1024 * 1024 + 1)
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
                    except ValueError:
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
        repo at .claude/launchers.json (root-owned 644 like the agent skills:
        everyone reads it, only an admin — via this root endpoint — writes it)."""
        if not self._require_admin(request):
            return web.json_response({"error": "admin only"}, status=403)
        buttons, err = common.validate_launchers(await request.json())
        if err:
            return web.json_response({"error": err}, status=400)
        path = common.REPO_ROOT / ".claude" / "launchers.json"
        tmp = path.with_name(".launchers.json.tmp")
        tmp.write_text(json.dumps({"buttons": buttons}, indent=2) + "\n")
        os.chmod(tmp, 0o644)
        os.chown(tmp, 0, grp.getgrnam("kb-users").gr_gid)
        os.replace(tmp, path)
        return web.json_response({"ok": True, "buttons": buttons})

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
        if len(pw) < 6:
            return web.json_response({"error": "password must be at least 6 characters"}, status=400)
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
        pdir = common.REPO_ROOT / "users" / u
        _run(["install", "-d", "-m", "700", "-o", u, "-g", u, str(pdir)])
        # kb_users is the DB-side mirror of the kb-users OS group: joining it is
        # what carries over access to company-shared app tables (games, todos,
        # timesheets...) that live in someone's personal u_<user> schema. Without
        # it a new account can open every page but every query behind them fails.
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
        if not self._require_admin(request):
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
            return web.json_response({"error": "refusing to grant privileged group via UI"}, status=400)
        flag = "-a" if action == "add" else "-d"
        if _run(["gpasswd", flag, user, group]).returncode != 0:
            return web.json_response({"error": "membership change failed"}, status=500)
        return web.json_response({"ok": True})

    async def _bridge_ws(self, request, target_session, target_url, extra_headers):
        server_ws = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024 * 1024)
        await server_ws.prepare(request)
        try:
            async with target_session.ws_connect(target_url, headers=extra_headers,
                                                  max_msg_size=16 * 1024 * 1024) as client_ws:
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


def make_app() -> web.Application:
    # client_max_size must match the per-user backend (2 GiB): the hub both
    # accepts /fs/upload directly AND buffers proxied /api/upload bodies, so
    # the default 1 MiB cap silently rejected any real photo/audio upload.
    hub = Hub()
    # bootstrap/migrate the egress allowlist into the repo, where its write-ACL
    # can delegate network administration to non-admins
    try:
        if not hub.EGRESS_FILE.exists():
            cfg = {}
            try:
                cfg = json.loads((common.ETC_DIR / "egress.json").read_text())
            except (OSError, ValueError):
                pass
            hub._egress_save(cfg if isinstance(cfg, dict) else {})
    except OSError:
        pass
    app = web.Application(client_max_size=2 * 1024 * 1024 * 1024)
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
    app.router.add_get("/fs/props", hub.fs_props_get)
    app.router.add_post("/fs/props", hub.fs_props_set)
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

    async def on_cleanup(app):
        h: Hub = app["hub"]
        for s in h._user_sessions.values():
            await s.close()
        if h._syncd_session:
            await h._syncd_session.close()

    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    app = make_app()
    web.run_app(app, host=common.HUB_ADDR[0], port=common.HUB_ADDR[1], print=None)


if __name__ == "__main__":
    main()
