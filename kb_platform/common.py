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
import grp
import pwd
import re
import secrets
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
        # compare_digest REFUSES non-ASCII str and raises TypeError. The cookie
        # is attacker-controlled, so compare BYTES — a mangled or hand-crafted
        # signature has to come back False, never escape as a 500.
        got = sig.encode("utf-8", "surrogateescape")
    except (ValueError, TypeError, UnicodeError):
        return None
    if not hmac.compare_digest(_sign(key, raw).encode(), got):
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


def open_pinned(path, is_dir: bool) -> int | None:
    """An fd for `path`, refusing symlinks. Returns None if the final component
    is a symlink, if the path is gone, or if it is not the kind asked for.

    This exists because of an asymmetry in the stdlib that has already cost us
    a root escalation: `os.chown` takes `follow_symlinks=False` and this module
    uses it, but `os.chmod` has no such parameter and Linux has no `lchmod`. A
    path-based chmod on anything a user can influence is therefore a write to
    whatever that path resolves to — and the hub runs as root.

    Every root-side mutation of a repo path must open once through here and
    then act on the FD (`os.fchmod`, `os.fchown`, `os.setxattr(fd, ...)`),
    never on the path. That also closes the TOCTOU between checking a path and
    acting on it, because there is only one traversal.

    O_NONBLOCK matters as much as O_NOFOLLOW: without it a FIFO planted in a
    user's own folder would hang the root daemon on open().
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if is_dir:
        flags |= os.O_DIRECTORY
    try:
        return os.open(path, flags)
    except OSError:
        return None          # ELOOP (symlink), ENOENT, ENOTDIR — all "skip it"


def validate_launchers(data) -> tuple[list | None, str | None]:
    """Validate a {"buttons": [...]} pinned list (company or personal).
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
        if not 1 <= len(label) <= 48 or any(ord(c) < 32 for c in label):
            return None, "label must be 1-48 printable characters"
        if kind not in ("file", "folder", "term"):
            return None, "kind must be 'file', 'folder' or 'term'"
        if not 1 <= len(target) <= 300 or any(ord(c) < 32 for c in target):
            return None, "target must be one line of at most 300 characters"
        out.append({"label": label, "kind": kind, "target": target})
    return out, None


# --- Platform config directory (.os/) ---------------------------------------
# The platform's own JSON config — launcher buttons, the artifact egress
# allowlist, settings — lives in <REPO>/.os/ at company level (root:kb-users
# 2755: everyone reads it, root writes it, or whoever holds a write ACL on a
# given file) and in users/<name>/.os/ at personal level (0700, the user's
# own). Neither is knowledge: is_hidden_rel keeps every dot-directory out of
# the index and the tree hides it unless asked. The company dir IS snapshotted
# (.gitignore: !.os/*.json) — config changes are auditable history.
#
# Until 2026-09 these files sat in .claude/, which is Claude Code's discovery
# path. CLAUDE.md and skills/ stay there — that is what Claude Code reads; the
# platform's own state does not belong to one agent. migrate_company_config
# (root, at hub start) and migrate_user_config (as the user, lazily) move what
# an older install left behind.
CONFIG_DIRNAME = ".os"
LEGACY_CONFIG_DIRNAME = ".claude"
MIGRATED_CONFIG_FILES = ("launchers.json", "egress.json")
GITIGNORE_CONFIG_RULE = "!.os/*.json"
COMPANY_CONFIG_DIR = REPO_ROOT / CONFIG_DIRNAME
_ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")


def company_config(name: str) -> Path:
    """<REPO>/.os/<name> — a company-wide config file."""
    return COMPANY_CONFIG_DIR / name


def user_config(user: str, name: str) -> Path:
    """<REPO>/users/<user>/.os/<name> — that person's own config file."""
    return REPO_ROOT / "users" / user / CONFIG_DIRNAME / name


def load_config_json(path) -> object | None:
    """Tolerant read: a missing, unreadable or malformed file is None. The
    caller validates the shape — every config file has a validating loader,
    so a hand-edit can never smuggle a bad shape in."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _check_config_name(name: str) -> None:
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"bad config file name: {name!r}")


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data):]


def write_company_config(name: str, data: bytes, *, in_place: bool) -> None:
    """ROOT: write <REPO>/.os/<name>. The directory is reached through
    opendir_beneath (no symlink component) and the file through O_NOFOLLOW;
    every mutation acts on an fd, never on a path (the hub runs as root).

    in_place=True  — ftruncate + write on the EXISTING inode. Its ACLs
                     survive, and for a file whose write-ACL is a delegation
                     (egress.json) that is the whole point. A crash mid-write
                     leaves a truncated file, which every validating loader
                     reads as empty: fail closed, not open.
    in_place=False — a fresh .<name>.<rand>.kbtmp renamed over the file:
                     atomic, ACLs reset (right for admin-only files such as
                     launchers.json).
    Mode 0644 on create; ownership root:kb-users re-asserted on every write
    (skipped when not root, so unit tests can exercise the code path)."""
    _check_config_name(name)
    dfd = opendir_beneath(CONFIG_DIRNAME)
    try:
        if in_place:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
            try:
                os.ftruncate(fd, 0)
                _write_all(fd, data)
                if os.geteuid() == 0:
                    os.fchown(fd, 0, grp.getgrnam("kb-users").gr_gid)
                os.fsync(fd)
            finally:
                os.close(fd)
            return
        tmp = f".{name}.{os.urandom(6).hex()}.kbtmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
        try:
            try:
                _write_all(fd, data)
                if os.geteuid() == 0:
                    os.fchown(fd, 0, grp.getgrnam("kb-users").gr_gid)
                os.fchmod(fd, 0o644)          # umask-proof
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


def remove_company_config(name: str) -> bool:
    """ROOT: delete <REPO>/.os/<name> if present (fd-pinned like the writer).
    Returns whether something was removed."""
    _check_config_name(name)
    dfd = opendir_beneath(CONFIG_DIRNAME)
    try:
        try:
            os.unlink(name, dir_fd=dfd)
            return True
        except FileNotFoundError:
            return False
    finally:
        os.close(dfd)


def _open_user_config_dir(user: str) -> int:
    """AS THE USER: users/<user>/.os as a dir fd, created 0700 if absent.
    A home can carry named ACL entries and a default ACL (shared homes do), and
    a freshly made .os/ would inherit them — personal config is nobody else's,
    so inherited ACLs are stripped and the mode pinned, on the fd, every time.
    Idempotent. Raises OSError; the caller maps it to 403/400."""
    hfd = os.open(REPO_ROOT / "users" / user, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir(CONFIG_DIRNAME, 0o700, dir_fd=hfd)
        except FileExistsError:
            pass
        dfd = os.open(CONFIG_DIRNAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=hfd)
    finally:
        os.close(hfd)
    try:
        for attr in _ACL_XATTRS:
            try:
                os.removexattr(dfd, attr)
            except OSError:
                pass                  # no ACL there, or a filesystem without them
        os.fchmod(dfd, 0o700)
    except OSError:
        os.close(dfd)
        raise
    return dfd


def write_user_config(user: str, name: str, data: bytes) -> None:
    """AS THE USER: atomically write users/<user>/.os/<name>, 0600. The tmp is
    a .kbtmp so the tree, syncd, the indexer and kb-convert all skip it."""
    _check_config_name(name)
    dfd = _open_user_config_dir(user)
    try:
        tmp = f".{name}.{os.urandom(6).hex()}.kbtmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            try:
                _write_all(fd, data)
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


def migrate_user_config(user: str, name: str, legacy_name: str) -> bool:
    """AS THE USER, lazily: if users/<user>/.os/<name> is absent and the
    older install's users/<user>/<legacy_name> is a regular file, move it into
    place (a rename: same inode, same content). Two lstats when there is
    nothing to do. Returns True if something moved."""
    _check_config_name(name)
    home = REPO_ROOT / "users" / user
    try:
        os.lstat(user_config(user, name))
        return False                          # already there, whatever it is
    except FileNotFoundError:
        pass
    try:
        if not stat_mod.S_ISREG(os.lstat(home / legacy_name).st_mode):
            return False
    except FileNotFoundError:
        return False
    hfd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        dfd = _open_user_config_dir(user)
        try:
            os.rename(legacy_name, name, src_dir_fd=hfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
    finally:
        os.close(hfd)
    return True


def migrate_company_config() -> list[str]:
    """ROOT, at hub start, idempotent: create <REPO>/.os and move an older
    install's .claude/{launchers,egress}.json into it — a rename, so the
    inode (and the egress file's delegation ACL) travels with it — then
    un-ignore .os/*.json so git keeps snapshotting the config. Returns what it
    did, one line each, for the hub to log. Nothing is ever deleted: a legacy
    file that would collide is left where it is and reported.

    Everything acts on fds. The repo root is group-writable, so a member could
    pre-plant a `.os` entry — that is refused and reported, never chmod'ed."""
    done: list[str] = []
    rfd = opendir_beneath("")
    try:
        try:
            os.mkdir(CONFIG_DIRNAME, 0o755, dir_fd=rfd)
            done.append(f"created {CONFIG_DIRNAME}/")
        except FileExistsError:
            pass
        try:
            dfd = os.open(CONFIG_DIRNAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rfd)
        except OSError as e:
            done.append(f"refusing: {CONFIG_DIRNAME} is not a plain directory "
                        f"({e.strerror}); pre-planted? nothing touched")
            return done
        try:
            if os.geteuid() == 0:
                st = os.fstat(dfd)
                gid = grp.getgrnam("kb-users").gr_gid
                if (st.st_uid, st.st_gid) != (0, gid):
                    os.fchown(dfd, 0, gid)
                    done.append(f"chowned {CONFIG_DIRNAME}/ to root:kb-users")
                if stat_mod.S_IMODE(st.st_mode) != 0o2755:
                    os.fchmod(dfd, 0o2755)
                    done.append(f"set {CONFIG_DIRNAME}/ to 2755")
            try:
                ofd = os.open(LEGACY_CONFIG_DIRNAME, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=rfd)
            except OSError:
                ofd = None                    # fresh install: nothing to migrate
            if ofd is not None:
                try:
                    for name in MIGRATED_CONFIG_FILES:
                        try:
                            old = os.stat(name, dir_fd=ofd, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if not stat_mod.S_ISREG(old.st_mode):
                            done.append(f"refusing: {LEGACY_CONFIG_DIRNAME}/{name} is not a regular file")
                            continue
                        try:
                            os.stat(name, dir_fd=dfd, follow_symlinks=False)
                            done.append(f"both {LEGACY_CONFIG_DIRNAME}/{name} and {CONFIG_DIRNAME}/{name} "
                                        f"exist; {CONFIG_DIRNAME}/ wins, remove the old one by hand")
                            continue
                        except FileNotFoundError:
                            pass
                        os.rename(name, name, src_dir_fd=ofd, dst_dir_fd=dfd)
                        done.append(f"moved {LEGACY_CONFIG_DIRNAME}/{name} -> {CONFIG_DIRNAME}/{name}")
                finally:
                    os.close(ofd)
        finally:
            os.close(dfd)
        # git keeps snapshotting the config from its new home. .gitignore is
        # written by install.sh only when absent, so an upgraded box needs the
        # rule appended here (the same way syncd appends **/_secrets/).
        try:
            fd = os.open(".gitignore", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=rfd)
        except OSError:
            fd = None
        if fd is not None:
            with os.fdopen(fd) as f:
                lines = f.read().splitlines()
            if GITIGNORE_CONFIG_RULE not in lines:
                fd = os.open(".gitignore", os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, dir_fd=rfd)
                with os.fdopen(fd, "w") as f:
                    f.write(f"\n# platform config (launchers, egress, settings) lives in {CONFIG_DIRNAME}/\n"
                            f"{GITIGNORE_CONFIG_RULE}\n")
                done.append(f"added {GITIGNORE_CONFIG_RULE} to .gitignore")
    finally:
        os.close(rfd)
    return done


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


# ── the inbox: what happened while you were elsewhere ────────────────────────
# One append-only file per person, `users/<u>/.os/inbox.jsonl`, written by
# whichever process actually saw the event: syncd when a document you were
# mentioned in is committed, the hub when something is shared with you. A file
# and not a table, because everything here is a file — an agent can read it,
# the backup already covers it, and no schema has to migrate. Root writes it
# (only root can reach into someone else's 0700 folder) and chowns it to them,
# so their own backend can mark things read.
INBOX_FILE = "inbox.jsonl"
INBOX_MAX = 500                       # oldest lines fall off the top
MENTION_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_][A-Za-z0-9_-]*)")


def inbox_path(user: str) -> Path:
    return user_config(user, INBOX_FILE)


def read_inbox(user: str) -> list[dict]:
    """Every event, oldest first. A line that is not JSON is skipped, never
    fatal: this file is also editable by the person it belongs to."""
    try:
        raw = inbox_path(user).read_text()
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and isinstance(ev.get("id"), str):
            out.append(ev)
    return out[-INBOX_MAX:]


def add_inbox_event(user: str, event: dict) -> bool:
    """Append one event to `user`'s inbox, as root, leaving the file theirs.

    Best-effort in the same way attribution is: a notification must never be
    able to break the thing it is about.
    """
    try:
        info = pwd.getpwnam(user)
    except KeyError:
        return False
    try:
        d = REPO_ROOT / "users" / user / CONFIG_DIRNAME
        if not d.is_dir():
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.geteuid() == 0:
                os.chown(d, info.pw_uid, info.pw_gid)
        p = d / INBOX_FILE
        keep = read_inbox(user)
        keep.append({**event, "id": event.get("id") or secrets.token_hex(8),
                     "at": int(event.get("at") or time.time()), "read": False})
        tmp = d / (INBOX_FILE + ".kbtmp")
        with open(tmp, "w") as f:
            for ev in keep[-INBOX_MAX:]:
                f.write(json.dumps(ev) + "\n")
        os.chmod(tmp, 0o600)
        if os.geteuid() == 0:
            os.chown(tmp, info.pw_uid, info.pw_gid)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


# ── the trash ────────────────────────────────────────────────────────────────
# A delete is a move, not an unlink: the thing goes into the `.trash/` of the
# folder that owns its audience, so restoring it is a move back and nothing
# ever crosses a permission boundary on the way. A dot-directory, so the tree
# hides it, the indexer skips it and search never turns up a deleted document
# — while an agent with a shell can read it like any other folder.
TRASH_DIRNAME = ".trash"
# Nothing in the trash is ever removed on a timer: a knowledgebase that
# quietly eats what you deleted a month ago is worse than a folder that grows.
# It empties when someone empties it (krystof, 2026-09-21).


def trashable(rel: str) -> bool:
    """Can this be moved into a `.trash/` instead of removed?

    Not a top-level area (nothing to be trashed beside), not a secret (a
    readable copy waiting in a trash is exactly what `_secrets/` exists to
    prevent), and not something already in a trash. Everything else goes
    into a `.trash/` in its OWN folder, which is why the permissions take
    care of themselves: the folder's group, setgid bit and default ACL are
    inherited by the new directory, and a rename carries the file's own
    owner, mode and ACLs untouched. Nothing crosses an audience boundary
    because nothing leaves the folder.
    """
    parts = [seg for seg in rel.strip("/").split("/") if seg]
    return len(parts) >= 2 and not is_secret_path(rel) and not is_trash_path(rel)


def is_trash_path(rel: str) -> bool:
    """Inside somebody's `.trash/` — already deleted, never deleted again into
    itself, and never restored onto its own path."""
    return TRASH_DIRNAME in rel.strip("/").split("/")


def is_secret_path(rel: str) -> bool:
    """True if the repo-relative path lives inside a `_secrets/` folder.
    Secrets are ordinary kernel-protected files, but the platform treats them
    specially everywhere content could escape the permission check: git
    history, the search index, and the CRDT relay."""
    return "_secrets" in rel.strip("/").split("/")


# Set on a `_secrets` FOLDER when its owner deliberately shares it. Nothing
# else may be read as that intent: two of this box's six `_secrets` folders are
# group-open and one carries the company's default ACL, none of it a decision
# anybody made about credentials — they were made in a terminal, or inherited
# from the project around them. So a key is born owner-only wherever it lands,
# and follows its folder only where this says the owner asked for it.
SECRETS_SHARED_XATTR = "user.kb_secrets_shared"


def secrets_folder_shares(parent) -> bool:
    """Has the `_secrets` folder a child is being born into been shared?
    `parent` is a path or an open dir fd — whichever the caller already has."""
    try:
        return bool(os.getxattr(parent, SECRETS_SHARED_XATTR))
    except OSError:
        return False


def set_secrets_shared(target, on: bool) -> None:
    """Mark (or unmark) a `_secrets` folder as deliberately shared. Best effort:
    a filesystem without user xattrs simply keeps every key owner-only."""
    try:
        if on:
            os.setxattr(target, SECRETS_SHARED_XATTR, b"1")
        else:
            os.removexattr(target, SECRETS_SHARED_XATTR)
    except OSError:
        pass


def born_closed(parent, path) -> bool:
    """Must this child be born owner-only, ignoring the folder it lands in?

    Two cases: the `_secrets` folder itself (a folder of credentials never
    starts out as wide as the project around it), and anything inside a
    `_secrets` folder that has not been shared. Share the folder and this goes
    false — which is what makes the share mean anything for keys added later.
    """
    rel = str(path)
    if is_secret_root(rel):
        return True
    return is_secret_path(rel) and not secrets_folder_shares(parent)


def is_secret_root(rel: str) -> bool:
    """True for a `_secrets` FOLDER itself, as opposed to what is inside it.

    The distinction is the whole sharing story. The folder is born closed —
    owner-only, whatever the folder it sits in, so a key dropped into a
    company-wide project is private the moment it exists. Its contents then
    inherit the folder, which by default says exactly the same thing… but
    follows it once the owner deliberately shares the folder with a team.
    Forcing every file inside to 0600 forever (what this used to do) made that
    share silently do nothing, and there was no other way to hand a colleague a
    credential."""
    return os.path.basename(str(rel).rstrip("/")) == "_secrets"


# --- Kernel-exact access evaluation (for ROOT daemons only) ------------------
# hub and syncd run as root but act on behalf of users; the kernel won't answer
# "may THIS user read that?" for them (os.access answers for root), so they must
# re-derive it. Mode bits alone are NOT enough: POSIX ACLs grant access that
# never shows in st_mode, and on ACL-bearing files st_mode's group bits are the
# ACL *mask*, so bare bits both under-grant (a named u:alice:rw entry) and
# over-grant (mask rwx over a real group::--- entry). These two functions mirror
# the kernel's algorithm — acl(5) "ACCESS CHECK ALGORITHM" — exactly.
ACL_XATTR = "system.posix_acl_access"
ACL_DEFAULT_XATTR = "system.posix_acl_default"
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
    return parse_acl_bytes(raw)


def parse_acl_bytes(raw: bytes) -> list[tuple[int, int, int]] | None:
    """The on-disk POSIX ACL xattr as [(tag, perms, qualifier)]. Split out so
    the DEFAULT acl (a different xattr) can be read with the same parser."""
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
    out group-wide access the kernel would refuse.

    Symlinks are REFUSED (ELOOP). Note that O_PATH|O_NOFOLLOW does not do this
    for us: that combination deliberately opens the link itself, and a symlink's
    own mode is 0777, so treating it as the object would grant everyone
    everything — the check below is what makes this safe, not O_NOFOLLOW.
    """
    fd = os.open(path, os.O_PATH | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if stat_mod.S_ISLNK(st.st_mode):
            raise OSError(errno.ELOOP, "refusing to evaluate a symlink", str(path))
        # procfs magic-link resolves back to the pinned inode, so this reads the
        # xattr of exactly the object we fstat'ed (getxattr rejects O_PATH fds).
        return st, acl_entries(f"/proc/self/fd/{fd}", follow=True)
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


def can(path, uid: int, gids, want: int) -> bool:
    """May this user perform `want` (R=4 / W=2 / X=1) on `path`?

    THE access evaluator for privileged code. `unix_access` answers for a
    single inode; this adds the part a single-inode check silently omits and
    the kernel does not — execute on every ancestor directory up to
    REPO_ROOT.

    That omission was a live over-grant: files inside a `drwxrws---` project
    folder are often world-readable, so an inode-only check told the hub that
    any employee could read them while the kernel refused. Verified against
    `runuser -u <user> -- test -r` in both directions before this landed.

    Symlinks are refused (stat_and_acl pins the inode). The caller must still
    do the operation itself as the user, or through a pinned fd.
    """
    if not _inode_can(path, uid, gids, want):
        return False
    root = REPO_ROOT.resolve()
    parent = Path(path).resolve().parent
    while parent != root:
        if root not in parent.parents:
            return False              # escaped the repo tree
        if not _inode_can(parent, uid, gids, 1):
            return False
        parent = parent.parent
    return True


def _inode_can(path, uid: int, gids, want: int) -> bool:
    """`want` on ONE inode, ignoring how it is reached. Kernel-exact including
    POSIX ACLs: the share feature grants by named entry, so bare mode bits both
    under-grant (named users) and over-grant (st_mode's group bits are the
    mask)."""
    try:
        st, entries = stat_and_acl(path)   # one pinned inode; symlink -> ELOOP
    except OSError:
        return False
    return unix_access(st, entries, uid, gids, want)


def read_audience(st, entries) -> tuple[bool, bool, list]:
    """Who can read this inode: (world, owning_group, [named qualifiers]).

    Named ACL entries count, and the mask is applied. Anything that reports an
    inode's breadth must use this: the share panel grants by NAMED entry, so a
    mode-bits-only reading calls every shared document "private".
    """
    mode = stat_mod.S_IMODE(st.st_mode)
    if entries is None:
        return bool(mode & 0o004), bool(mode & 0o040), []
    group_obj, mask, other = 0, 7, mode & 7
    named: dict = {}
    for tag, perm, qual in entries:
        if tag == _ACL_GROUP_OBJ:
            group_obj = perm
        elif tag in (_ACL_GROUP, _ACL_USER):
            named[(tag, qual)] = perm
        elif tag == _ACL_MASK:
            mask = perm
        elif tag == _ACL_OTHER:
            other = perm
    return (bool(other & 4),
            bool(group_obj & mask & 4),
            [(tag, q) for (tag, q), pm in named.items() if pm & mask & 4])


def human_readers(named) -> list:
    """Drop service accounts from a read_audience() named list.

    Every file the share panel touches carries a named grant for the indexer
    (hub._grant_indexer) — a PRIVATE file included, because search has to read
    it. Counting that as a reader makes everything look shared, which is
    exactly what it did: "private" started reporting as "people" and the move
    warning fired on every drag.
    """
    out = []
    for tag, qual in named:
        try:
            if tag == _ACL_USER:
                name = pwd.getpwuid(qual).pw_name
                if name in PROTECTED_USERS or qual < 1000:
                    continue
            elif tag == _ACL_GROUP:
                if grp.getgrgid(qual).gr_name == INDEXER_USER or qual < 1000:
                    continue
        except KeyError:
            pass
        out.append((tag, qual))
    return out


def birth_mode(parent, is_dir: bool, child: str | None = None, *,
               closed_secret_root: bool = True) -> int:
    """The mode a newly created child of `parent` should end up with: the same
    audience as the folder it lands in.

    A process-wide restrictive umask is NOT enough. It used to be safe because
    every shared folder carried a default ACL that re-granted the group, but a
    group-owned folder (chgrp + setgid, no ACL) has no default ACL — so a new
    file there is born 0600 and the team it was written for, plus the search
    indexer, silently cannot read it. Mirror the parent's group/other bits
    instead (execute stripped for files, setgid kept for directories) so the
    audience of a document is the audience of its folder, ACLs or not.

    `_secrets/` is the exception, and born_closed draws the line: the folder
    itself is always born owner-only, and so is everything inside it until its
    owner shares that folder — after which keys follow it like any other file,
    which is the whole point of being able to share one.
    """
    if closed_secret_root and born_closed(parent, child if child is not None else parent):
        return 0o700 if is_dir else 0o600
    try:
        pst = os.stat(parent)
        pm = stat_mod.S_IMODE(pst.st_mode)
        # On a directory carrying an extended ACL, st_mode's group bits are the
        # MASK — the union of every named grant — not the folder's own group
        # permission. Taking them literally is the very mistake this module
        # exists to correct, so read the real group:: entry when there is one.
        entries = acl_entries(parent)
        if entries:
            for tag, perm, _q in entries:
                if tag == _ACL_GROUP_OBJ:
                    pm = (pm & ~0o070) | (perm << 3)
                    break
    except OSError:
        return 0o700 if is_dir else 0o600
    if is_dir:
        return (pm & 0o2777) | 0o700          # keep setgid; owner always full
    return (pm & 0o666) | 0o600               # rw for whoever the folder is for


def inherits_acl(parent) -> bool:
    """Does `parent` carry a default ACL, i.e. will the kernel give a new child
    an access ACL of its own? Where it does, the kernel's answer is the correct
    one and we must not chmod over it."""
    try:
        os.getxattr(parent, ACL_DEFAULT_XATTR)
        return True
    except OSError:
        return False


def create_with_mode(path, data: bytes | None = None, *, exclusive: bool = False):
    """Create `path` as this (unprivileged) user with the audience of its folder.

    Two regimes, because the kernel already handles one of them:
      * parent HAS a default ACL — let inheritance do its job, and create with a
        permissive mode under umask 0. A restrictive create mode would clamp the
        inherited ACL's MASK to nothing, silently voiding every entry it just
        granted (exactly the failure that hid documents from the indexer).
      * parent has none — the mode bits are the whole story: birth_mode().
    """
    fd = open_with_mode(path, exclusive=exclusive)
    try:
        if data:
            os.write(fd, data)
    finally:
        os.close(fd)


def open_with_mode(path, *, exclusive: bool = False) -> int:
    """`create_with_mode`, but handing back the OPEN WRITE FD instead of writing
    in one shot — the same two regimes, decided once. A chunked upload needs the
    fd: it is opened when the upload starts and appended to for as long as the
    upload runs. Caller closes."""
    parent = os.path.dirname(str(path)) or "."
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    if inherits_acl(parent) and not born_closed(parent, path):
        old = os.umask(0)
        try:
            return os.open(path, flags, 0o666)
        finally:
            os.umask(old)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, birth_mode(parent, False, str(path)))
    except OSError:
        os.close(fd)
        raise
    return fd


def mkdir_with_mode(path) -> None:
    """mkdir as this user with the audience of its parent — see create_with_mode."""
    parent = os.path.dirname(str(path)) or "."
    # A subfolder of a shared `_secrets` folder carries the mark on, so that
    # keys born one level deeper follow the same decision.
    carry_mark = is_secret_path(str(path)) and secrets_folder_shares(parent)
    if inherits_acl(parent) and not born_closed(parent, path):
        old = os.umask(0)
        try:
            os.mkdir(path, 0o777)
        finally:
            os.umask(old)
        if carry_mark:
            set_secrets_shared(path, True)
        return
    os.mkdir(path, 0o700)
    os.chmod(path, birth_mode(parent, True, str(path)))
    if carry_mark:
        set_secrets_shared(path, True)


def reset_to_parent_audience(path, is_dir: bool, *,
                             secret_root_follows: bool = False) -> None:
    """Make `path` look exactly as if it had just been created where it sits —
    used after a copy, which otherwise carries the SOURCE's ACL and mode into a
    folder with a different audience, and after a move, which carries the whole
    inode over untouched.

    The chgrp is not optional. setgid only fires when the kernel CREATES a
    child, so a file renamed into a team folder keeps the group it arrived
    with: the team it now lives with cannot read it, and the team it came from
    still can. Only a setgid parent is followed — elsewhere the group is
    nobody's business but the owner's.

    `secret_root_follows` is the share panel asking, in so many words, for a
    `_secrets` folder to take the audience of the folder it sits in. A move or
    a copy never asks for that: a folder of credentials dragged into a team
    folder must not quietly become that team's, so there the born-closed rule
    stands and only an explicit share may widen it."""
    parent = os.path.dirname(str(path)) or "."
    # One traversal, then every mutation goes through the fd. A symlink here
    # used to redirect a ROOT chmod onto any inode on the box: the share
    # panel's "inherit" walks a folder its owner controls, and os.chmod
    # follows symlinks with no way to say otherwise. See open_pinned().
    fd = open_pinned(path, is_dir)
    if fd is None:
        return            # symlink, gone, or not the kind we were told: skip it
    try:
        try:
            pst = os.stat(parent)
            if pst.st_mode & stat_mod.S_ISGID and os.fstat(fd).st_gid != pst.st_gid:
                os.fchown(fd, -1, pst.st_gid)
        except OSError:
            pass      # not ours to regroup; the mode work below still applies
        try:
            dflt = os.getxattr(parent, ACL_DEFAULT_XATTR)
        except OSError:
            dflt = None
        for attr in (ACL_XATTR, ACL_DEFAULT_XATTR) if is_dir else (ACL_XATTR,):
            try:
                os.removexattr(fd, attr)
            except OSError:
                pass
        if dflt and (secret_root_follows or not born_closed(parent, path)):
            try:                   # reproduce the kernel's inheritance
                os.setxattr(fd, ACL_XATTR, dflt)
                if is_dir:
                    os.setxattr(fd, ACL_DEFAULT_XATTR, dflt)
                else:
                    # On create the kernel intersects the inherited ACL with the
                    # create mode, which for a regular file carries no execute
                    # bit. chmod with an ACL present rewrites owner/mask/other,
                    # which is exactly that intersection — without it a copied
                    # document comes out executable and mask-widened.
                    os.fchmod(fd, stat_mod.S_IMODE(os.fstat(fd).st_mode) & 0o666)
                return
            except OSError:
                pass
        try:
            os.fchmod(fd, birth_mode(parent, is_dir, str(path),
                                     closed_secret_root=not secret_root_follows))
        except OSError:
            pass
    finally:
        os.close(fd)


def effective_mode(st, entries) -> int:
    """st_mode with the REAL group:: permission restored. On an ACL-bearing
    inode st_mode's group bits are the MASK — the union of every named grant —
    so reading them literally over-reports what the owning group itself has."""
    mode = stat_mod.S_IMODE(st.st_mode)
    for tag, perm, _q in entries or ():
        if tag == _ACL_GROUP_OBJ:
            return (mode & ~0o070) | (perm << 3)
    return mode


EVERYONE_GROUP = os.environ.get("KB_EVERYONE_GROUP", "kb-users")
try:
    EVERYONE_GID = grp.getgrnam(EVERYONE_GROUP).gr_gid
except KeyError:
    EVERYONE_GID = -1

INDEXER_USER = "kbindexer"
try:
    INDEXER_UID = pwd.getpwnam(INDEXER_USER).pw_uid
except KeyError:
    INDEXER_UID = -1


def readership_key(st, entries) -> tuple:
    """WHO CAN READ this, as a comparable value: (the owning group when the
    group may read, whether everyone may read, the named readers). Two inodes
    with the same key are open to exactly the same people.

    One definition on purpose: the share panel uses it to decide whether
    something merely follows the folder above it, and the file tree uses it to
    mark the ones that do not. Two separate notions of "same audience" disagree
    the moment they drift, and the disagreement lands in front of the user as a
    marked row whose panel insists it is inherited.

    Read only, also on purpose. Comparing write or execute bits makes an
    ordinary 0644 file differ from the 2775 folder holding it — that is every
    file in the knowledgebase, and a marker on everything is a marker on
    nothing. The indexer's own grant is ignored for the same reason: it is on
    everything the index can see and says nothing about who a document is for.
    """
    mode = effective_mode(st, entries)
    mask, named = 7, set()
    for tag, perm, _q in entries or ():
        if tag == _ACL_MASK:
            mask = perm
    for tag, perm, qual in entries or ():
        if tag == _ACL_USER and perm & mask & 4 and qual != INDEXER_UID:
            named.add(("u", qual))
        elif tag == _ACL_GROUP and perm & mask & 4:
            named.add(("g", qual))
    group_reads = bool((mode >> 3) & 4)
    # "Everyone" has two spellings and they mean the same people: world-readable
    # (o+r), or readable by the group that every employee is in. A folder at 2770
    # kb-users is not one bit narrower than its 2775 kb-users parent, and a 0664
    # file inside it is not one bit wider — treating the spellings as different
    # audiences marked all three of them as disagreeing with each other while the
    # share panel called every one of them "Everyone at Ollsoft".
    everyone = bool(mode & 4) or (group_reads and st.st_gid == EVERYONE_GID)
    if everyone:
        # Once everyone can read it, which group carries them stops narrowing
        # anything, and a named reader on top of it adds nobody.
        return (None, True, frozenset())
    return (st.st_gid if group_reads else None, False, frozenset(named))


def reset_audience_tree(root, *, secret_root_follows: bool = False) -> None:
    """reset_to_parent_audience over a whole tree, top-down so that each level
    is fixed before its children read it as their parent.

    Used after a copy (the copy carries the SOURCE's mode and ACL into a folder
    with a different audience) and by the share panel's "inherit from the folder
    above". Best-effort per item: a tree that lands slightly tight is
    recoverable, one that stays private inside a team folder is the bug."""
    root = Path(root)
    is_dir = root.is_dir()
    # Only the root of the walk can be the `_secrets` folder being shared; its
    # children follow it the ordinary way once it has moved.
    reset_to_parent_audience(root, is_dir, secret_root_follows=secret_root_follows)
    if not is_dir:
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in dirnames:
            reset_to_parent_audience(Path(dirpath) / n, True)
        for n in filenames:
            reset_to_parent_audience(Path(dirpath) / n, False)


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
