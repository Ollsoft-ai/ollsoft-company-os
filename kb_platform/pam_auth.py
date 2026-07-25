"""PAM authentication for the hub.

Uses python-pam (pure-Python ctypes against libpam.so). We authenticate against
the system's pam_unix stack, so a KB user is exactly a Linux user with a password.
The hub is the ONLY component that ever sees a password; everything downstream
trusts kernel identity + signed tokens.
"""
from __future__ import annotations

import pwd

import pam as _pam

_PAM = _pam.pam()


def authenticate(username: str, password: str, service: str = "login") -> bool:
    """Return True iff (username, password) is valid per PAM.

    We only allow real, human-style accounts (uid 1000..64999), so a
    system/service account can never authenticate through the web.
    """
    if not username or not password:
        return False
    try:
        entry = pwd.getpwnam(username)
    except KeyError:
        return False
    # human account = uid range (kbindexer et al. sit below 1000, nobody above
    # 64999). A nologin shell is deliberately ALLOWED: "viewer" accounts sign
    # into the webapp with no shell — pty/cron are gated on the shell instead.
    if not 1000 <= entry.pw_uid < 65000 or username == "nobody":
        return False
    return bool(_PAM.authenticate(username, password, service=service))


def user_uid_gid(username: str) -> tuple[int, int] | None:
    try:
        e = pwd.getpwnam(username)
    except KeyError:
        return None
    return (e.pw_uid, e.pw_gid)
