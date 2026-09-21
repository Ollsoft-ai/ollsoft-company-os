"""Public links — the host half.

A public share is three things on this box, all created by root and none of
them visible to the container that serves them:

  1. a row in `/var/lib/kb-shares/shares.json` (root 0600) — the real path,
     who made it, when it dies, the hashed token and password;
  2. a directory `/srv/kb-public/data/<id>` which is a BIND MOUNT of the
     shared file or folder, mounted read-only unless the share may be edited;
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
import json
import os
import pwd
import secrets
import subprocess
import time
from pathlib import Path

from . import common

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


def hash_password(pw: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(),
            "hash": hashlib.scrypt(pw.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()}


# ---- mounts -----------------------------------------------------------------
def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=30)


def is_mounted(target: Path) -> bool:
    return _run("/usr/bin/mountpoint", "-q", str(target)).returncode == 0


def mount_share(sid: str, src: Path, kind: str, mode: str) -> None:
    """Put exactly this file or folder in front of the container.

    A folder is bound onto `data/<id>`; a FILE is bound onto
    `data/<id>/<name>` so the siblings it lives with are never exposed.
    """
    target = DATA / sid
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o755)
    if kind == "file":
        target = target / src.name
        target.touch(exist_ok=True)
    if is_mounted(target):
        return
    r = _run("/usr/bin/mount", "--bind", str(src), str(target))
    if r.returncode != 0:
        raise OSError(f"mount failed: {r.stderr.strip()}")
    if mode != "edit":                      # a view share is read-only in the kernel
        r = _run("/usr/bin/mount", "-o", "remount,ro,bind", str(target))
        if r.returncode != 0:
            _run("/usr/bin/umount", "-l", str(target))
            raise OSError(f"read-only remount failed: {r.stderr.strip()}")


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
        "expires": int(row.get("expires") or 0),
        "title": row.get("title", ""),
        "name": os.path.basename(row["path"]),
        "token_hash": token_hash,
        "pw": row.get("pw"),
    }))
    os.chmod(p, 0o640)
    try:
        os.chown(p, 0, pwd.getpwnam(SHARE_USER).pw_gid)
    except (KeyError, OSError):
        pass


def remove_conf(sid: str) -> None:
    try:
        (CONF / (sid + ".json")).unlink()
    except OSError:
        pass


# ---- who the container is allowed to read as --------------------------------
def _acl(target: Path, kind: str, spec: list[str]) -> None:
    args = ["/usr/bin/setfacl"]
    if kind == "folder":
        args.append("-R")
    args += spec + [str(target)]
    _run(*args)


def grant(target: Path, kind: str, mode: str) -> None:
    """Let the container's account in — and only onto this subtree. Mode bits
    alone would not do: a 0640 document is unreadable to it, and a public
    share must not depend on a file happening to be world-readable."""
    rights = "rwX" if mode == "edit" else "rX"
    _acl(target, kind, ["-m", f"u:{SHARE_USER}:{rights}"])
    if kind == "folder":                    # …and for whatever is written later
        _acl(target, kind, ["-d", "-m", f"u:{SHARE_USER}:{rights}"])


def revoke_acl(target: Path, kind: str) -> None:
    _acl(target, kind, ["-x", f"u:{SHARE_USER}"])
    if kind == "folder":
        _acl(target, kind, ["-d", "-x", f"u:{SHARE_USER}"])


# ---- the operations the hub calls -------------------------------------------
def create(rel: str, *, by: str, mode: str = "view", days: int = DEFAULT_DAYS,
           password: str = "", title: str = "") -> dict:
    """Make a share and hand back the one-time URL (the token is never stored
    in the clear, so this is the only moment it exists)."""
    p = common.resolve_repo_path(rel)
    if p is None or not p.exists():
        raise ValueError("no such path")
    rel = str(p.relative_to(common.REPO_ROOT.resolve()))
    if common.is_secret_path(rel):
        raise ValueError("a secret cannot be shared publicly")
    if common.is_trash_path(rel):
        raise ValueError("that is in the trash")
    if "/" not in rel:
        raise ValueError("share something inside an area, not the area itself")
    if mode not in MODES:
        raise ValueError("mode must be view or edit")
    days = max(1, min(int(days or DEFAULT_DAYS), MAX_DAYS))
    kind = "folder" if p.is_dir() else "file"

    sid = base64.b32encode(secrets.token_bytes(10)).decode().lower().rstrip("=")
    token = secrets.token_urlsafe(24)
    row = {"id": sid, "path": rel, "kind": kind, "mode": mode, "by": by,
           "created": int(time.time()), "expires": int(time.time()) + days * 86400,
           "title": title[:120] or os.path.basename(rel),
           "pw": hash_password(password) if password else None}
    grant(p, kind, mode)
    try:
        mount_share(sid, p, kind, mode)
    except OSError:
        revoke_acl(p, kind)
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
    p = common.REPO_ROOT / row["path"]
    if p.exists():
        revoke_acl(p, row.get("kind", "file"))
    _write([r for r in rows if r["id"] != sid])
    return _pub(row)


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
    """Put the container's ACL entry back if something took it away.

    It happens: the platform rewrites a folder's ACLs whenever its audience
    changes, an agent runs setfacl, a backup is restored. The entry is the
    only thing standing between a live link and a 404, and re-stating it is
    idempotent — so the sweep does it on every pass rather than trusting that
    nothing else ever touches the tree. Returns True if it was missing.
    """
    p = common.REPO_ROOT / row["path"]
    if not p.exists():
        return False
    try:
        r = _run("/usr/bin/getfacl", "-p", "-c", str(p))
        had = f"user:{SHARE_USER}:" in (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        had = False
    if had:
        return False
    grant(p, row.get("kind", "file"), row.get("mode", "view"))
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
            if p.exists():
                revoke_acl(p, row.get("kind", "file"))
            out.append(f"expired {row['id']} ({row['path']})")
            continue
        if not p.exists():
            unmount_share(row["id"])
            remove_conf(row["id"])
            out.append(f"gone {row['id']} ({row['path']})")
            continue
        target = DATA / row["id"]
        if row.get("kind") == "file":
            target = target / os.path.basename(row["path"])
        if not is_mounted(target):
            try:
                mount_share(row["id"], p, row.get("kind", "file"), row.get("mode", "view"))
                out.append(f"remounted {row['id']} ({row['path']})")
            except OSError as e:
                out.append(f"could not remount {row['id']}: {e}")
        try:
            if regrant(row):
                out.append(f"re-granted {row['id']} ({row['path']}) — its ACL had been rewritten")
        except (OSError, subprocess.SubprocessError) as e:
            out.append(f"could not re-grant {row['id']}: {e}")
        keep.append(row)
    if len(keep) != len(_read()):
        _write(keep)
    return out


if __name__ == "__main__":       # `python -m kb_platform.publicshare` — the sweep timer
    for line in sweep():
        print("kb-shares:", line)
