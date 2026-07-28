"""Shared config, paths, and HMAC signing for the KB platform.

Everything here is deliberately dependency-light so every process (hub, user
backend, syncd, indexer) can import it without pulling heavy deps.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import stat as stat_mod
import time
from pathlib import Path

# --- Canonical paths -------------------------------------------------------
REPO_ROOT = Path(os.environ.get("KB_REPO", "/srv/kb"))
RUN_DIR = Path(os.environ.get("KB_RUN", "/run/kb"))
ETC_DIR = Path(os.environ.get("KB_ETC", "/etc/kb"))
USER_SOCK_DIR = RUN_DIR / "users"
SYNCD_SOCK = RUN_DIR / "syncd.sock"
# Version-history query socket (world-connectable; the caller's identity comes
# from SO_PEERCRED — the kernel says who is asking, no token needed).
VC_SOCK = RUN_DIR / "vc.sock"
# Attribution hints: per-user backends drop a tiny file here after each API
# write/delete/rename, and syncd folds it into the next git commit. The DIR is
# root-owned 1733 (sticky, world-writable); each hint file is owned by the
# writing user, so st_uid is KERNEL-VERIFIED authorship — a user cannot forge a
# hint as someone else.
ATTRIB_DIR = RUN_DIR / "attrib"
HUB_ADDR = ("127.0.0.1", int(os.environ.get("KB_HUB_PORT", "8300")))
SESSION_KEY_FILE = ETC_DIR / "session.key"

# Postgres
PG_DB = os.environ.get("KB_PG_DB", "kb")

# --- Deployment identity ---------------------------------------------------
# The platform's own OS group. Every human account joins it; it owns the shared
# parts of the repo. Treated as a fixed platform constant (like `www-data`), not
# a tuning knob — the install script creates it.
KB_GROUP = "kb-users"

# Members of this OS group are platform admins (may edit any file's permissions
# and manage accounts). Debian/Ubuntu use `sudo`; RHEL-family use `wheel`.
ADMIN_GROUP = os.environ.get("KB_ADMIN_GROUP", "sudo")

# Accounts the admin UI refuses to modify or delete. System accounts are always
# included; KB_PROTECTED_USERS adds site-specific ones (comma-separated) — set it
# to protect the founding admin from being locked out or deleted by another admin.
PROTECTED_USERS = {"root", "kbindexer", "postgres", "nobody"} | {
    u.strip() for u in os.environ.get("KB_PROTECTED_USERS", "").split(",") if u.strip()
}

# The venv python used to spawn per-user backends.
VENV_PY = os.environ.get("KB_VENV_PY", str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"))

COOKIE_NAME = "kb_session"
SESSION_TTL = 12 * 3600  # 12h


def load_session_key() -> bytes:
    """Root-readable HMAC key shared by hub + syncd to sign identity tokens."""
    return SESSION_KEY_FILE.read_bytes().strip()


def _sign(key: bytes, payload: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def make_token(key: bytes, data: dict, ttl: int = SESSION_TTL, now: float | None = None) -> str:
    """Compact signed token: base of json + expiry + hmac. Not encrypted, just authenticated."""
    now = time.time() if now is None else now
    body = dict(data)
    body["exp"] = int(now + ttl)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    payload = raw.hex()
    sig = _sign(key, raw)
    return f"{payload}.{sig}"


def read_token(key: bytes, token: str, now: float | None = None) -> dict | None:
    """Verify + decode a token. Returns the dict payload or None if invalid/expired."""
    now = time.time() if now is None else now
    try:
        payload, sig = token.split(".", 1)
        raw = bytes.fromhex(payload)
    except (ValueError, TypeError):
        return None
    expect = _sign(key, raw)
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict) or body.get("exp", 0) < now:
        return None
    return body


# --- Path safety -----------------------------------------------------------
def resolve_repo_path(rel: str) -> Path | None:
    """Resolve a repo-relative path, refusing anything that escapes REPO_ROOT.

    Note: this is defence-in-depth only. The real guarantee is that the process
    doing the open runs as the target OS user, so the kernel enforces access
    regardless of this check. But we still reject traversal early for clean errors.
    """
    rel = rel.lstrip("/")
    try:
        candidate = (REPO_ROOT / rel).resolve()
    except (OSError, RuntimeError):
        return None
    root = REPO_ROOT.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def opendir_beneath(rel: str, root: Path | None = None) -> int:
    """Open the directory at repo-relative `rel`, refusing ANY symlink component
    (openat + O_NOFOLLOW at every level, rooted at REPO_ROOT). This is how
    privileged (root) code must reach a directory: it defeats both final- and
    intermediate-component symlink attacks. Returns a dir fd — caller must close.
    """
    base = (root or REPO_ROOT).resolve()
    parts = [p for p in rel.lstrip("/").split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise OSError("path traversal")
    dfd = os.open(str(base), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in parts:
            nfd = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        return dfd
    except OSError:
        os.close(dfd)
        raise


def validate_launchers(data) -> tuple[list | None, str | None]:
    """Validate a {"buttons": [...]} launcher list (company or personal).
    Returns (clean_buttons, None) or (None, error). A button opens a repo file
    or types a command into a fresh terminal AS the clicking user, so the only
    server-side concerns are shape, size and single-line-ness."""
    if not isinstance(data, dict):
        return None, "expected an object"
    btns = data.get("buttons")
    if not isinstance(btns, list):
        return None, "buttons must be a list"
    if len(btns) > 30:
        return None, "too many buttons (max 30)"
    out = []
    for b in btns:
        if not isinstance(b, dict):
            return None, "each button must be an object"
        label = str(b.get("label", "")).strip()
        kind = b.get("kind")
        target = str(b.get("target", "")).strip()
        if not 1 <= len(label) <= 24 or any(ord(c) < 32 for c in label):
            return None, "label must be 1-24 printable characters"
        if kind not in ("file", "term"):
            return None, "kind must be 'file' or 'term'"
        if not 1 <= len(target) <= 300 or any(ord(c) < 32 for c in target):
            return None, "target must be one line of at most 300 characters"
        out.append({"label": label, "kind": kind, "target": target})
    return out, None


def is_versioned_path(rel: str) -> bool:
    """True if this path belongs in git history: documents (.md) and artifacts
    (.html), never secrets. Attachments, binaries and machinery stay out —
    history is for the content people write, and it stays slim."""
    if is_secret_path(rel):
        return False
    return rel.endswith(".md") or rel.endswith(".html")


def write_attrib_hint(op: str, *rel_paths: str) -> None:
    """Record "I (the calling OS user) just did `op` to these paths" for the
    version history. Runs AS the user (in their backend), so the hint file's
    st_uid is the kernel's word on who acted. Best-effort: history attribution
    must never make the actual file operation fail."""
    paths = [p for p in rel_paths if p and is_versioned_path(p)]
    if not paths:
        return
    try:
        fn = ATTRIB_DIR / f"{os.getpid()}-{time.time_ns():x}.json"
        with open(fn, "w") as f:
            json.dump({"op": op, "paths": paths}, f)
    except OSError:
        pass


def is_secret_path(rel: str) -> bool:
    """True if the repo-relative path lives inside a `_secrets/` folder.
    Secrets are ordinary kernel-protected files, but the platform treats them
    specially everywhere content could escape the permission check: git
    history, the search index, and the CRDT relay."""
    return "_secrets" in rel.strip("/").split("/")


# --- Kernel-exact access evaluation (for ROOT daemons only) ------------------
# hub and syncd run as root but act on behalf of users; the kernel won't answer
# "may THIS user read that?" for them (os.access answers for root), so they must
# re-derive it. Mode bits alone are NOT enough: POSIX ACLs grant access that
# never shows in st_mode, and on ACL-bearing files st_mode's group bits are the
# ACL *mask*, so bare bits both under-grant (a named u:alice:rw entry) and
# over-grant (mask rwx over a real group::--- entry). These two functions mirror
# the kernel's algorithm — acl(5) "ACCESS CHECK ALGORITHM" — exactly.
ACL_XATTR = "system.posix_acl_access"
_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ, _ACL_GROUP, _ACL_MASK, _ACL_OTHER = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20)


def acl_entries(target, follow: bool = False) -> list[tuple[int, int, int]] | None:
    """Parsed `system.posix_acl_access` xattr as [(tag, perms, qualifier)], or
    None when the file has no extended ACL (evaluate classic mode bits then).
    `target` is a path or an open fd. On an unexpected read error returns [] —
    evaluating with empty entries can only under-grant.
    """
    try:
        if isinstance(target, int):
            raw = os.getxattr(target, ACL_XATTR)
        else:
            raw = os.getxattr(target, ACL_XATTR, follow_symlinks=follow)
    except OSError as e:
        if e.errno in (errno.ENODATA, errno.ENOTSUP):
            return None
        return []
    if len(raw) < 4 or int.from_bytes(raw[:4], "little") != 2:
        return None
    return [(int.from_bytes(raw[o:o + 2], "little"),
             int.from_bytes(raw[o + 2:o + 4], "little"),
             int.from_bytes(raw[o + 4:o + 8], "little"))
            for o in range(4, len(raw) - 7, 8)]


def stat_and_acl(path):
    """(lstat, access-ACL) of the SAME inode, pinned by an O_PATH fd.

    Reading the two separately by path is a TOCTOU hole: swap the file for a
    symlink between them and getxattr returns "no ACL" for the symlink while
    the stat still describes the real file — the evaluator then falls back to
    st_mode's group bits, which on an ACL-bearing file are the MASK, and hands
    out group-wide access the kernel would refuse. O_NOFOLLOW additionally
    means a symlink can never be the target (raises ELOOP).
    """
    fd = os.open(path, os.O_PATH | os.O_NOFOLLOW)
    try:
        # procfs magic-link resolves back to the pinned inode, so this reads the
        # xattr of exactly the object we fstat'ed (getxattr rejects O_PATH fds).
        return os.fstat(fd), acl_entries(f"/proc/self/fd/{fd}", follow=True)
    finally:
        os.close(fd)


def unix_access(st, entries, uid: int, gids, want: int) -> bool:
    """May the user (uid + supplementary gids) perform `want` (an R=4/W=2/X=1
    bitmask) on a file with this lstat result and these acl_entries()? Pure
    function; ancestor traversal is the caller's job. Owner and other are never
    capped by the mask; named users, the owning group and named groups are; a
    user who matches ANY group-class entry never falls through to other."""
    mode = stat_mod.S_IMODE(st.st_mode)
    if entries is None:
        if st.st_uid == uid:
            bits = mode >> 6
        elif st.st_gid in gids:
            bits = mode >> 3
        else:
            bits = mode
        return (bits & want) == want
    owner, group_obj, mask, other = (mode >> 6) & 7, 0, 7, mode & 7
    named_u, named_g = {}, {}
    for tag, perm, qual in entries:
        if tag == _ACL_USER_OBJ:
            owner = perm
        elif tag == _ACL_USER:
            named_u[qual] = perm
        elif tag == _ACL_GROUP_OBJ:
            group_obj = perm
        elif tag == _ACL_GROUP:
            named_g[qual] = perm
        elif tag == _ACL_MASK:
            mask = perm
        elif tag == _ACL_OTHER:
            other = perm
    if st.st_uid == uid:
        return (owner & want) == want
    if uid in named_u:
        return (named_u[uid] & mask & want) == want
    in_group_class = st.st_gid in gids
    if in_group_class and (group_obj & mask & want) == want:
        return True
    for gid, perm in named_g.items():
        if gid in gids:
            in_group_class = True
            if (perm & mask & want) == want:
                return True
    if in_group_class:
        return False
    return (other & want) == want


def birth_mode(parent, is_dir: bool, child: str | None = None) -> int:
    """The mode a newly created child of `parent` should end up with: the same
    audience as the folder it lands in.

    A process-wide restrictive umask is NOT enough. It used to be safe because
    every shared folder carried a default ACL that re-granted the group, but a
    group-owned folder (chgrp + setgid, no ACL) has no default ACL — so a new
    file there is born 0600 and the team it was written for, plus the search
    indexer, silently cannot read it. Mirror the parent's group/other bits
    instead (execute stripped for files, setgid kept for directories) so the
    audience of a document is the audience of its folder, ACLs or not.

    Secrets are the one exception and are born owner-only regardless of where
    they sit: `_secrets/` folders are themselves listable by the team, so
    inheriting their mode would publish the very thing that must not be.
    """
    if is_secret_path(str(child if child is not None else parent)):
        return 0o700 if is_dir else 0o600
    try:
        pm = stat_mod.S_IMODE(os.stat(parent).st_mode)
    except OSError:
        return 0o700 if is_dir else 0o600
    if is_dir:
        return (pm & 0o2777) | 0o700          # keep setgid; owner always full
    return (pm & 0o666) | 0o600               # rw for whoever the folder is for


def create_with_mode(path, data: bytes | None = None, *, exclusive: bool = False):
    """Create `path` as this (unprivileged) user with birth_mode(). Separate
    chmod after creation, because the process umask would mask an open() mode.
    The brief window between the two is more restrictive, never less."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        if data:
            os.write(fd, data)
        os.fchmod(fd, birth_mode(os.path.dirname(str(path)) or ".", False, str(path)))
    finally:
        os.close(fd)


def mkdir_with_mode(path) -> None:
    """mkdir as this user with birth_mode() — see create_with_mode."""
    os.mkdir(path, 0o700)
    os.chmod(path, birth_mode(os.path.dirname(str(path)) or ".", True, str(path)))


# --- Derived text sidecars ---------------------------------------------------
# kb-convert (convert.py) shadows every office/PDF binary with a hidden,
# read-only markdown extraction: `report.docx` -> `.report.docx.md`, in the
# same directory. The dot prefix hides it wherever the platform already hides
# dot-entries (tree, quick-open); these helpers carve out the ONE exception
# that still lets sidecar CONTENT into the index and content search.
DERIVED_SOURCE_SUFFIXES = (".docx", ".pptx", ".xlsx", ".pdf", ".doc", ".ppt", ".xls")


def is_derived_sidecar(rel: str) -> bool:
    """True for a kb-convert sidecar basename: `.<name>.<source-ext>.md`."""
    name = rel.rstrip("/").rsplit("/", 1)[-1]
    if not (name.startswith(".") and name.endswith(".md")):
        return False
    return name[1:-3].lower().endswith(DERIVED_SOURCE_SUFFIXES)


def is_hidden_rel(rel: str) -> bool:
    """True for paths the index must skip as machinery: anything under a
    dot-DIRECTORY (.claude/, .git/) and any dot-FILE — except derived sidecars,
    which are dot-files precisely so the tree hides them while their content
    stays searchable."""
    parts = rel.strip("/").split("/")
    if any(seg.startswith(".") for seg in parts[:-1]):
        return True
    return parts[-1].startswith(".") and not is_derived_sidecar(rel)
