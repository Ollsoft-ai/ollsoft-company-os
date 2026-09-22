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
import json
import os
import pwd
import secrets
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


def mount_share(sid: str, src: Path, kind: str, mode: str) -> None:
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
    source = src if kind == "folder" else src.parent
    if is_mounted(target):
        return
    r = _run("/usr/bin/mount", "--bind", str(source), str(target))
    if r.returncode != 0:
        raise OSError(f"mount failed: {r.stderr.strip()}")
    if mode != "edit":                      # a view share is read-only in the kernel
        r = _run("/usr/bin/mount", "-o", "remount,ro,bind", str(target))
        if r.returncode != 0:
            _run("/usr/bin/umount", "-l", str(target))
            raise OSError(f"read-only remount failed: {r.stderr.strip()}")


def _inode(p: Path) -> int | None:
    try:
        return p.stat().st_ino
    except OSError:
        return None


def mount_healthy(row: dict) -> bool:
    """Is the container looking at the REAL thing right now?

    Not "is something mounted": the file the share serves has to be the same
    inode as the file in the knowledgebase. An atomic replace leaves the old
    mount in place and perfectly mounted, pointing at nothing anyone else can
    see."""
    base = DATA / row["id"]
    if not is_mounted(base) and not any(
            is_mounted(p) for p in (base.iterdir() if base.is_dir() else [])):
        return False
    live = common.REPO_ROOT / row["path"]
    if row.get("kind") == "folder":
        return _inode(base) == _inode(live)
    served = base / os.path.basename(row["path"])
    served_ino, live_ino = _inode(served), _inode(live)
    return served_ino is not None and served_ino == live_ino


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
    share must not depend on a file happening to be world-readable.

    A single-file share also needs to REACH its file, because what is mounted
    is the folder (see mount_share). It gets `--x` there: search, never read.
    With that, `open("<folder>/<name>")` works and `ls <folder>` does not, so
    a sibling is protected by the kernel and not merely by our routing."""
    rights = "rwX" if mode == "edit" else "rX"
    _acl(target, kind, ["-m", f"u:{SHARE_USER}:{rights}"])
    if kind == "folder":                    # …and for whatever is written later
        _acl(target, kind, ["-d", "-m", f"u:{SHARE_USER}:{rights}"])
    else:
        _run("/usr/bin/setfacl", "-m", f"u:{SHARE_USER}:--x", str(target.parent))


def _other_file_shares_in(folder: Path, except_id: str = "") -> bool:
    for row in _read():
        if row.get("id") == except_id or row.get("kind") == "folder":
            continue
        if (common.REPO_ROOT / row["path"]).parent == folder:
            return True
    return False


def revoke_acl(target: Path, kind: str, sid: str = "") -> None:
    _acl(target, kind, ["-x", f"u:{SHARE_USER}"])
    if kind == "folder":
        _acl(target, kind, ["-d", "-x", f"u:{SHARE_USER}"])
    elif not _other_file_shares_in(target.parent, sid):
        # the search bit on the folder goes too, unless another link needs it
        _run("/usr/bin/setfacl", "-x", f"u:{SHARE_USER}", str(target.parent))


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
    _write([r for r in rows if r["id"] != sid])     # before the ACL: the folder's
    p = common.REPO_ROOT / row["path"]              # search bit is kept only for
    if p.exists():                                  # shares that are still live
        revoke_acl(p, row.get("kind", "file"), sid)
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
            if p.exists():
                revoke_acl(p, row.get("kind", "file"), row["id"])
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
                mount_share(row["id"], p, row.get("kind", "file"), row.get("mode", "view"))
                grant(p, row.get("kind", "file"), row.get("mode", "view"))
                write_conf_again(row)
                out.append(f"remounted {row['id']} ({row['path']}) — it was not serving the live file")
            except OSError as e:
                out.append(f"could not remount {row['id']}: {e}")
        try:
            if regrant(row):
                out.append(f"re-granted {row['id']} ({row['path']}) — its ACL had been rewritten")
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
