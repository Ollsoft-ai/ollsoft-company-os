"""Shared config, paths, and HMAC signing for the KB platform.

Everything here is deliberately dependency-light so every process (hub, user
backend, syncd, indexer) can import it without pulling heavy deps.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
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
