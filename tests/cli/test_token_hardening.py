"""Regression: a malformed session cookie must be rejected, never crash.

`hmac.compare_digest` refuses non-ASCII *str* and raises TypeError. The cookie
is attacker-controlled and kb-hub is internet-facing (cloudflared ->
127.0.0.1:8300), so a cookie shaped `<valid-hex>.<non-ascii>` used to escape
`read_token` as an unhandled 500 on every route behind `current_user` --
including `/`, which locks a browser out of reaching /login to clear it.
Journal showed 2 live hits on 2026-08-20.
"""
import socket

import pytest

from kb_platform import common

BASE = "http://127.0.0.1:8300"

# payload half must be valid hex or the token dies at bytes.fromhex() first.
BAD_SIGS = [
    "é",                      # latin-1
    "⚡️",                # emoji + variation selector
    "deàdbeef",               # non-ASCII smuggled mid-signature
    "\ud800",                      # lone surrogate (header-decode artifact)
]


@pytest.mark.parametrize("sig", BAD_SIGS)
def test_read_token_rejects_non_ascii_signature(sig):
    assert common.read_token(b"k" * 32, "00." + sig) is None


def test_read_token_still_accepts_a_real_token():
    tok = common.make_token(b"k" * 32, {"user": "alice"})
    assert common.read_token(b"k" * 32, tok)["user"] == "alice"
    assert common.read_token(b"other" * 8, tok) is None    # wrong key -> None


def _raw_get(cookie: bytes) -> bytes:
    """httpx/requests refuse to encode a non-ASCII Cookie header, so speak HTTP
    directly -- which is exactly what reaches the hub through the tunnel."""
    req = (b"GET / HTTP/1.1\r\nHost: localhost\r\nCookie: "
           + common.COOKIE_NAME.encode() + b"=" + cookie
           + b"\r\nConnection: close\r\n\r\n")
    try:
        s = socket.create_connection(("127.0.0.1", 8300), 5)
    except OSError:
        pytest.skip("kb-hub not running on 127.0.0.1:8300")
    with s:
        s.sendall(req)
        return s.recv(4096)


@pytest.mark.parametrize("sig", BAD_SIGS)
def test_hub_redirects_instead_of_500(sig):
    head = _raw_get(b"00." + sig.encode("utf-8", "surrogatepass"))
    status = head.split(b"\r\n", 1)[0]
    assert b"500" not in status, f"malformed cookie crashed the hub: {status!r}"
    assert b"302" in status and b"/login" in head
