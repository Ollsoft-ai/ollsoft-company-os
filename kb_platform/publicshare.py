"""Public links — the host half.

A public share is three things on this box, all created by root and none of
them visible to the container that serves them:

  1. a row in `/var/lib/kb-shares/shares.json` (root 0600) — the real path,
     who made it, when it dies, the hashed token and password;
  2. a directory `/srv/kb-public/data/<id>` which is a BIND MOUNT — of the
     folder for a folder share, and of the file's FOLDER for a single-file
     share, mounted read-only unless the share may be edited;
  3. a file `/srv/kb-public/conf/<id>.json` (root:kbshare 0640) telling the
     container what may be done — and never where the thing really lives.

The container has `/data` and `/conf` and nothing else. Revoking is an
unmount, so a leaked link stops working within a second, whatever the
container believes. Expiry is enforced twice: the sweep unmounts, and the
container refuses a conf whose time has passed.

Everything here runs as root inside the hub, so it is deliberately small and
every path is checked against the repo before anything is mounted.
"""
from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
import pwd
import secrets
import stat
import subprocess
import time
from pathlib import Path

from . import common
from . import settings as kbsettings

SHARE_USER = "kbshare"                 # the account the container runs as
ROOT = Path(os.environ.get("KB_PUBLIC_ROOT", "/srv/kb-public"))
DATA = ROOT / "data"
CONF = ROOT / "conf"
STORE = Path(os.environ.get("KB_SHARE_STORE", "/var/lib/kb-shares/shares.json"))
DEFAULT_DAYS = 14
MAX_DAYS = 90
MODES = ("view", "edit")


# ---- the store --------------------------------------------------------------
def _read() -> list[dict]:
    try:
        data = json.loads(STORE.read_text())
    except (OSError, ValueError):
        return []
    return [s for s in data if isinstance(s, dict) and isinstance(s.get("id"), str)]


def _write(rows: list[dict]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STORE)


def _pub(row: dict) -> dict:
    """What a person may see about a share: never the token, never a hash."""
    return {"id": row["id"], "path": row["path"], "kind": row.get("kind", "file"),
            "mode": row.get("mode", "view"), "expires": row.get("expires", 0),
            "created": row.get("created", 0), "by": row.get("by", ""),
            "title": row.get("title", ""), "password": bool(row.get("pw")),
            "url": public_url(row["id"], None)}


def public_url(sid: str, token: str | None) -> str:
    base = os.environ.get("KB_SHARE_BASE", "").rstrip("/")
    return f"{base}/s/{sid}/{token}" if token else f"{base}/s/{sid}/…"


def company_look() -> dict:
    """The company's own theme, to travel with the link.

    A public page has no account behind it, so there is no personal `ui.theme`
    to honour — and asking the reader's device was the wrong answer (krystof,
    2026-09-22: the company-wide setting is the one to send). An admin who
    picks Light, or tints the accent, changes what every client sees next time
    they open a link."""
    try:
        layer = kbsettings.load_layer(kbsettings.company_file(), "company")["values"]
    except Exception:                          # noqa: BLE001 — a look is never worth a 500
        return {}
    look = {}
    theme = layer.get("ui.theme")
    if isinstance(theme, str) and theme:
        look["theme"] = theme
    custom = layer.get("ui.theme.custom")
    if isinstance(custom, dict) and custom:
        # already validated against THEME_TOKENS by load_layer; the container
        # writes them as CSS variables and validates again before it does
        look["tokens"] = {str(k): str(v) for k, v in list(custom.items())[:40]}
    return look


def hash_password(pw: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(),
            "hash": hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()}


# ---- mounts -----------------------------------------------------------------
def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=30)


def is_mounted(target: Path) -> bool:
    return _run("/usr/bin/mountpoint", "-q", str(target)).returncode == 0


def mount_share(sid: str, pin: "Pinned", mode: str) -> None:
    """Put exactly this file or folder in front of the container.

    A folder is bound onto `data/<id>`. A single FILE is bound **by its
    folder**, and the container is allowed at one name inside it.

    Binding the file itself was the obvious thing and it is wrong: a bind
    mount pins an INODE, and every careful writer on this box replaces a file
    rather than rewriting it — syncd flushes a document to a `.kbtmp` and
    renames it into place, and so do git, vim and half the tools an agent
    runs. The moment that happens the share is looking at an orphaned inode:
    reads return the old text, writes go into a file with no name, and both
    sides think they are fine. It cost krystof a set of edits on 2026-09-22
    before the mount table admitted it (`findmnt` printed the source with
    "//deleted" on the end).

    A directory inode is not replaced, so the mount survives. The siblings
    stay hidden by the kernel rather than by the mount: the container's
    account gets SEARCH (`--x`) on the folder and read/write on the one file,
    so it can open that name and cannot list the folder or open anything
    else — see grant().
    """
    target = DATA / sid
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o755)
    if is_mounted(target):
        return
    # Bind the folder behind the fd that was checked, not a path: the kernel
    # follows /proc/<pid>/fd/<n> to that exact directory, and --no-canonicalize
    # stops mount(8) turning it back into a name it would look up again.
    src = f"/proc/{os.getpid()}/fd/{pin.fd}"
    r = _run("/usr/bin/mount", "--no-canonicalize", "--bind", src, str(target))
    if r.returncode != 0:
        raise OSError(f"mount failed: {r.stderr.strip()}")
    if not _same(os.stat(target), os.fstat(pin.fd)):
        _run("/usr/bin/umount", "-l", str(target))
        raise OSError("the mount did not land on the folder that was checked")
    if mode != "edit":                      # a view share is read-only in the kernel
        r = _run("/usr/bin/mount", "-o", "remount,ro,bind", str(target))
        if r.returncode != 0:
            _run("/usr/bin/umount", "-l", str(target))
            raise OSError(f"read-only remount failed: {r.stderr.strip()}")


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def mount_healthy(row: dict) -> bool:
    """Is the container looking at the REAL thing right now?

    Not "is something mounted": the file the share serves has to be the same
    inode as the file in the knowledgebase. An atomic replace leaves the old
    mount in place and perfectly mounted, pointing at nothing anyone else can
    see. A path that now runs through a link is not healthy either."""
    base = DATA / row["id"]
    if not is_mounted(base) and not any(
            is_mounted(p) for p in (base.iterdir() if base.is_dir() else [])):
        return False
    try:
        pin = Pinned(row["path"])
    except ValueError:
        return False
    with pin:
        try:
            if pin.kind == "folder":
                return _same(os.stat(base), os.fstat(pin.fd))
            return _same(os.lstat(base / os.path.basename(row["path"])), os.fstat(pin.ffd))
        except OSError:
            return False


def unmount_share(sid: str) -> None:
    base = DATA / sid
    if not base.exists():
        return
    for p in sorted(base.iterdir(), reverse=True) if base.is_dir() else []:
        if is_mounted(p):
            _run("/usr/bin/umount", "-l", str(p))
    if is_mounted(base):
        _run("/usr/bin/umount", "-l", str(base))
    try:
        for p in base.iterdir():
            if p.is_file() and not is_mounted(p):
                p.unlink()
        base.rmdir()
    except OSError:
        pass


# ---- the container's view of a share ----------------------------------------
def write_conf(row: dict, token_hash: str) -> None:
    CONF.mkdir(parents=True, exist_ok=True)
    p = CONF / (row["id"] + ".json")
    p.write_text(json.dumps({
        "mode": row.get("mode", "view"),
        "kind": row.get("kind", "file"),     # a file share opens the file, not a list of one
        # a single-file share is mounted BY ITS FOLDER, so the container is
        # told the one name it may serve out of it (the kernel says the same
        # thing with the folder's ACL; this is the second lock)
        "only": None if row.get("kind") == "folder" else os.path.basename(row["path"]),
        "expires": int(row.get("expires") or 0),
        "title": row.get("title", ""),
        "name": os.path.basename(row["path"]),
        "token_hash": token_hash,
        "pw": row.get("pw"),
        **company_look(),            # theme (+ token overrides), refreshed by the sweep
    }))
    os.chmod(p, 0o640)
    try:
        os.chown(p, 0, pwd.getpwnam(SHARE_USER).pw_gid)
    except (KeyError, OSError):
        pass


def write_conf_again(row: dict) -> None:
    """Rewrite a conf keeping the token hash that is already in it — a remount
    must not invalidate the link somebody is holding."""
    p = CONF / (row["id"] + ".json")
    try:
        token_hash = json.loads(p.read_text()).get("token_hash", "")
    except (OSError, ValueError):
        return                              # no conf to keep: leave it alone
    write_conf(row, token_hash)


def remove_conf(sid: str) -> None:
    try:
        (CONF / (sid + ".json")).unlink()
    except OSError:
        pass


# ---- reaching the thing without following anything ---------------------------
# Every path here came from a person and is acted on by root, so it is never
# resolved: it is OPENED one component at a time, refusing any link on the
# way, and everything after — the owner check, the ACLs, the bind mount — acts
# on that one fd. A path that is checked and then used again by name is a path
# its owner can swap for a link in between.
#
# A folder link never reaches into SKIP_DIRS: credentials stay where the
# platform keeps them, and what someone deleted is not published. Sharing a
# folder with colleagues stops at `_secrets` too (hub._walk_repo); a public
# link, which needs no account at all, cannot be the exception.
#
# Only `_secrets` is also STRIPPED when found holding an entry. A file deleted
# from a shared folder keeps its entry inside `.trash/`, so restoring it (a
# rename) brings back a file the link can still serve; the container refuses
# `.trash` paths itself, so the entry there serves nothing.
SKIP_DIRS = ("_secrets", common.TRASH_DIRNAME)
STRIP_DIRS = ("_secrets",)


class NotAllowed(Exception):
    """The caller may not publish this (the owner check, asked of the pinned inode)."""


def _clean_rel(rel: str) -> str:
    parts = [p for p in str(rel or "").strip().strip("/").split("/") if p and p != "."]
    if not parts or ".." in parts:
        raise ValueError("no such path")
    return "/".join(parts)


class Pinned:
    """A share's target, held open without following links. `fd` is what gets
    mounted — the folder for a folder share, the file's folder for a file
    share — and `ffd` is the file itself (None for a folder)."""

    def __init__(self, rel: str):
        self.rel = _clean_rel(rel)
        parent, _, name = self.rel.rpartition("/")
        self.fd, self.ffd = -1, None
        try:
            dfd = common.opendir_beneath(parent)
        except OSError:
            raise ValueError("no such path, or a link on the way to it") from None
        held: int | None = dfd          # closed at the end unless it becomes self.fd
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                self.fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            elif stat.S_ISREG(st.st_mode):
                self.fd, held = dfd, None
                self.ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
                if not stat.S_ISREG(os.fstat(self.ffd).st_mode):
                    raise OSError("not a regular file")
            else:
                raise ValueError("only a file or a folder can be published")
        except OSError:
            self.close()
            raise ValueError("no such path, or it is a link") from None
        except ValueError:
            self.close()
            raise
        finally:
            if held is not None:
                os.close(held)

    @property
    def kind(self) -> str:
        return "folder" if self.ffd is None else "file"

    def stat(self) -> os.stat_result:
        return os.fstat(self.fd if self.ffd is None else self.ffd)

    def close(self) -> None:
        for fd in (self.ffd, self.fd):
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fd, self.ffd = -1, None

    def __enter__(self) -> "Pinned":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _walk(dfd: int, skip: bool = True):
    """Every folder and regular file below the open folder `dfd`, as (fd,
    is_dir), each opened O_NOFOLLOW and closed once the caller is done with it.
    Links are never entered or yielded; with `skip`, nothing in SKIP_DIRS is."""
    for _path, dirnames, filenames, dirfd in os.fwalk(".", dir_fd=dfd, follow_symlinks=False):
        if skip:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name, is_dir in [(d, True) for d in dirnames] + [(f, False) for f in filenames]:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if is_dir else 0)
            try:
                fd = os.open(name, flags, dir_fd=dirfd)
            except OSError:
                continue
            try:
                mode = os.fstat(fd).st_mode
                if stat.S_ISDIR(mode) if is_dir else stat.S_ISREG(mode):
                    yield fd, is_dir
            finally:
                os.close(fd)


def _has_entry(fd: int, is_dir: bool) -> bool:
    """Does the container's account have an ACL entry here (access, or default
    on a folder)?"""
    try:
        uid = pwd.getpwnam(SHARE_USER).pw_uid
    except KeyError:
        return False
    for xattr in (common.ACL_XATTR, common.ACL_DEFAULT_XATTR) if is_dir else (common.ACL_XATTR,):
        try:
            entries = common.parse_acl_bytes(os.getxattr(fd, xattr)) or []
        except OSError:
            continue
        if any(tag == 0x02 and q == uid for tag, _perms, q in entries):   # ACL_USER
            return True
    return False


def _apply(fd: int, is_dir: bool, args: list[str], strict: bool = False,
           cache: dict | None = None) -> None:
    try:
        common.acl_apply_fd(fd, is_dir, args, cache)
    except (OSError, subprocess.SubprocessError):
        if strict:
            raise


def strip_secrets(dfd: int) -> int:
    """Take the container's entries off everything under a `_secrets` below the
    open folder `dfd`. A `_secrets` made inside a shared folder after the link
    inherits the folder's default ACL, so this runs on every sweep, not only
    at grant time. Returns how many inodes it had to change."""
    n = 0
    drop = ["-x", f"u:{SHARE_USER}"]
    cache: dict = {}
    for _path, dirnames, _files, dirfd in os.fwalk(".", dir_fd=dfd, follow_symlinks=False):
        hits = [d for d in dirnames if d in STRIP_DIRS]
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for d in hits:
            try:
                sfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dirfd)
            except OSError:
                continue
            try:
                for fd, is_dir in itertools.chain([(sfd, True)], _walk(sfd, skip=False)):
                    if _has_entry(fd, is_dir):
                        _apply(fd, is_dir, drop, cache=cache)
                        n += 1
            finally:
                os.close(sfd)
    return n


def grant(pin: Pinned, mode: str) -> None:
    """Let the container's account in — and only onto this subtree. Mode bits
    alone would not do: a 0640 document is unreadable to it, and a public
    share must not depend on a file happening to be world-readable.

    A single-file share also needs to REACH its file, because what is mounted
    is the folder (see mount_share). It gets `--x` there: search, never read.
    With that, `open("<folder>/<name>")` works and `ls <folder>` does not, so
    a sibling is protected by the kernel and not merely by our routing."""
    spec = _spec(mode)
    if pin.kind == "folder":
        _apply(pin.fd, True, spec, strict=True)      # access + default: what is written later too
        cache: dict = {}
        for fd, is_dir in _walk(pin.fd):
            _apply(fd, is_dir, spec, cache=cache)
        strip_secrets(pin.fd)
    else:
        _apply(pin.ffd, False, spec, strict=True)
        _apply(pin.fd, False, ["-m", f"u:{SHARE_USER}:--x"], strict=True)


def _spec(mode: str) -> list[str]:
    return ["-m", f"u:{SHARE_USER}:{'rwX' if mode == 'edit' else 'rX'}"]


def heal(pin: Pinned, mode: str) -> int:
    """Grant whatever in a folder share lacks the container's entry — a file
    moved in from elsewhere carries its old ACL, a restore from the trash
    brings back one granted before, a setfacl or a backup drops entries —
    and strip any `_secrets` made since. Returns how many inodes it changed."""
    spec, cache, n = _spec(mode), {}, 0
    for fd, is_dir in itertools.chain([(pin.fd, True)], _walk(pin.fd)):
        if not _has_entry(fd, is_dir):
            _apply(fd, is_dir, spec, cache=cache)
            n += 1
    return n + strip_secrets(pin.fd)


def _other_file_shares_in(folder_rel: str, except_id: str = "") -> bool:
    for row in _read():
        if row.get("id") == except_id or row.get("kind") == "folder":
            continue
        if os.path.dirname(row["path"]) == folder_rel:
            return True
    return False


def revoke_acl(pin: Pinned, sid: str = "") -> None:
    drop = ["-x", f"u:{SHARE_USER}"]
    if pin.kind == "folder":
        # everything, SKIP_DIRS included: an entry an older grant left there goes too
        cache: dict = {}
        for fd, is_dir in itertools.chain([(pin.fd, True)], _walk(pin.fd, skip=False)):
            if _has_entry(fd, is_dir):
                _apply(fd, is_dir, drop, cache=cache)
    else:
        _apply(pin.ffd, False, drop)
        if not _other_file_shares_in(os.path.dirname(pin.rel), sid):
            # the search bit on the folder goes too, unless another link needs it
            _apply(pin.fd, False, drop)


# ---- the operations the hub calls -------------------------------------------
def create(rel: str, *, by: str, mode: str = "view", days: int = DEFAULT_DAYS,
           password: str = "", title: str = "", may_publish=None) -> dict:
    """Make a share and hand back the one-time URL (the token is never stored
    in the clear, so this is the only moment it exists).

    `may_publish(stat)` is the caller's authorization, asked about the very
    inode that is then granted and mounted — the hub's own check on the path
    string is only an early, friendly answer. NotAllowed if it says no."""
    with Pinned(rel) as pin:
        return _create(pin, by=by, mode=mode, days=days, password=password,
                       title=title, may_publish=may_publish)


def _create(pin: Pinned, *, by: str, mode: str, days: int, password: str, title: str,
            may_publish) -> dict:
    rel = pin.rel
    if common.is_secret_path(rel):
        raise ValueError("a secret cannot be shared publicly")
    if common.is_trash_path(rel):
        raise ValueError("that is in the trash")
    if "/" not in rel:
        raise ValueError("share something inside an area, not the area itself")
    if mode not in MODES:
        raise ValueError("mode must be view or edit")
    if may_publish is not None and not may_publish(pin.stat()):
        raise NotAllowed("only the owner (or an admin) can publish this")
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    kind = pin.kind

    sid = base64.b32encode(secrets.token_bytes(10)).decode().lower().rstrip("=")
    token = secrets.token_urlsafe(24)
    row = {"id": sid, "path": rel, "kind": kind, "mode": mode, "by": by,
           "created": int(time.time()), "expires": int(time.time()) + days * 86400,
           "title": title[:120] or os.path.basename(rel),
           "pw": hash_password(password) if password else None}
    grant(pin, mode)
    try:
        mount_share(sid, pin, mode)
    except OSError:
        revoke_acl(pin)
        raise
    write_conf(row, hashlib.sha256(token.encode()).hexdigest())
    rows = _read()
    rows.append(row)
    _write(rows)
    out = _pub(row)
    out["url"] = public_url(sid, token)
    return out


def revoke(sid: str) -> dict | None:
    rows = _read()
    row = next((r for r in rows if r["id"] == sid), None)
    if row is None:
        return None
    unmount_share(sid)
    remove_conf(sid)
    _write([r for r in rows if r["id"] != sid])     # before the ACL: the folder's
    _revoke_acl_at(row, sid)                        # search bit is kept only for
    return _pub(row)                                # shares that are still live


def _revoke_acl_at(row: dict, sid: str) -> None:
    try:
        pin = Pinned(row["path"])
    except ValueError:
        return                  # gone, or a link now: nothing of ours to take back there
    with pin:
        revoke_acl(pin, sid)


def covering(rel: str) -> list[dict]:
    """The live shares affected by a change at this path — the share itself,
    one above it, or ones below it."""
    rel = rel.strip("/")
    out = []
    for row in _read():
        p = row["path"]
        if p == rel or rel.startswith(p + "/") or p.startswith(rel + "/"):
            out.append(row)
    return out


def listing(user: str | None = None) -> list[dict]:
    rows = _read()
    if user is not None:
        rows = [r for r in rows if r.get("by") == user]
    return [_pub(r) for r in sorted(rows, key=lambda r: r.get("created", 0), reverse=True)]


def get(sid: str) -> dict | None:
    return next((r for r in _read() if r["id"] == sid), None)


def regrant(row: dict) -> bool:
    """Put the container's ACL entries back if something took them away (a
    folder: across the tree, and off any `_secrets` — see heal()).

    It happens: the platform rewrites a folder's ACLs whenever its audience
    changes, an agent runs setfacl, a backup is restored. The entry is the
    only thing standing between a live link and a 404, and re-stating it is
    idempotent — so the sweep does it on every pass rather than trusting that
    nothing else ever touches the tree. Returns True if it was missing.
    """
    try:
        pin = Pinned(row["path"])
    except ValueError:
        return False
    with pin:
        if pin.kind == "folder":
            return heal(pin, row.get("mode", "view")) > 0
        if _has_entry(pin.ffd, False):
            return False
        grant(pin, row.get("mode", "view"))
        return True


def refresh_look(row: dict) -> bool:
    """Put the company's current theme into a live link's conf. Cheap enough
    to do on every pass, and the only way a theme change reaches the links
    that already exist."""
    p = CONF / (row["id"] + ".json")
    try:
        conf = json.loads(p.read_text())
    except (OSError, ValueError):
        return False
    look = company_look()
    if conf.get("theme") == look.get("theme") and conf.get("tokens") == look.get("tokens"):
        return False
    write_conf(row, conf.get("token_hash", ""))
    return True


def sweep() -> list[str]:
    """Take down what has expired, put back what a reboot dropped, and
    re-state the ACL every live share depends on. Human-readable lines for
    the log."""
    out = []
    now = time.time()
    keep = []
    for row in _read():
        p = common.REPO_ROOT / row["path"]
        if row.get("expires") and now > row["expires"]:
            unmount_share(row["id"])
            remove_conf(row["id"])
            _revoke_acl_at(row, row["id"])
            out.append(f"expired {row['id']} ({row['path']})")
            continue
        if not p.exists():
            unmount_share(row["id"])
            remove_conf(row["id"])
            out.append(f"gone {row['id']} ({row['path']})")
            continue
        # Not "is it mounted" but "is it the RIGHT inode": an atomic replace
        # of the shared file leaves a perfectly mounted orphan behind, which
        # is how a set of edits through a link went nowhere on 2026-09-22.
        # This also migrates a share made before file shares were mounted by
        # their folder — the old mount is thrown away and made again.
        if not mount_healthy(row):
            try:
                unmount_share(row["id"])
                with Pinned(row["path"]) as pin:
                    mount_share(row["id"], pin, row.get("mode", "view"))
                    grant(pin, row.get("mode", "view"))
                write_conf_again(row)
                out.append(f"remounted {row['id']} ({row['path']}) — it was not serving the live file")
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                # a path that now runs through a link stays down: unmounted, not followed
                out.append(f"could not remount {row['id']}: {e}")
        try:
            if regrant(row):
                out.append(f"re-granted {row['id']} ({row['path']}) — entries were missing, or a _secrets had one")
        except (OSError, subprocess.SubprocessError) as e:
            out.append(f"could not re-grant {row['id']}: {e}")
        if refresh_look(row):
            out.append(f"restyled {row['id']} ({row['path']}) — the company theme changed")
        keep.append(row)
    if len(keep) != len(_read()):
        _write(keep)
    return out


if __name__ == "__main__":       # `python -m kb_platform.publicshare` — the sweep timer
    for line in sweep():
        print("kb-shares:", line)
