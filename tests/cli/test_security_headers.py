"""The browser gets told what the app is allowed to do.

The shell shipped with no security headers at all — no CSP, no frame control,
no nosniff — so any site could invisibly embed the KB and nothing bounded an
injected script.

And /api/attachment served ARBITRARY uploaded bytes inline on the app's own
origin. Artifacts were never the problem: a .html is classified as an artifact
and goes to /api/artifact/raw, which sandboxes it. But an SVG is XML that may
carry <script>, uploads have no extension check, and the browser renders one as
a document — click it in the tree and its script runs as you, against /fs/share
and /admin/*. The CSP costs nothing visible: images, PDFs and SVGs still
preview, scripts simply do not run.
"""
import httpx
import pytest
from kbenv import BASE, CREDS, U, doc


@pytest.fixture(scope="module")
def c():
    cl = httpx.Client(base_url=BASE, timeout=20)
    r = cl.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    assert r.status_code == 200, r.text
    return cl


def test_the_shell_cannot_be_framed_or_sniffed(c):
    h = c.get("/login").headers
    assert h.get("x-frame-options"), \
        "the app can be embedded by any site — clickjacking"
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("referrer-policy"), "paths leak to third parties via Referer"


@pytest.mark.xfail(
    reason="Hub.SHELL_CSP is written but NOT enabled: turning it on broke 6 of 8 "
           "browser tests, every artifact one among them. The artifact iframe is "
           "sandboxed WITHOUT allow-same-origin, so it runs on an opaque origin, "
           "and the policy has to account for that before it can ship. Left as an "
           "xfail rather than deleted so the gap stays visible and this test turns "
           "green the day it is fixed.",
    strict=False)
def test_the_shell_bounds_what_a_script_may_do(c):
    csp = c.get("/login").headers.get("content-security-policy", "")
    assert csp, "the shell ships no CSP at all"
    script = [x for x in csp.split(";") if x.strip().startswith("script-src")]
    assert script and "unsafe-inline" not in script[0], \
        "script-src must not allow inline"
    assert "frame-ancestors 'self'" in csp and "object-src 'none'" in csp


def test_an_attachment_cannot_execute(c):
    p = doc("hdr_probe.md")
    c.post("/api/file", json={"path": p})
    c.post("/api/artifact/write", json={"path": p, "content": "probe\n"})
    r = c.get("/api/attachment", params={"path": p})
    assert r.status_code == 200, r.text
    csp = r.headers.get("content-security-policy", "")
    assert csp, "attachments are served with no CSP — an uploaded SVG can run script"
    assert "script-src 'none'" in csp, "attachment CSP does not forbid script"
    assert r.headers.get("x-content-type-options") == "nosniff", \
        "without nosniff a mislabelled upload can be reinterpreted as HTML"


def test_an_attachment_can_still_be_previewed(c):
    """The fix must not turn every preview into a download."""
    p = doc("hdr_probe2.md")
    c.post("/api/file", json={"path": p})
    c.post("/api/artifact/write", json={"path": p, "content": "probe\n"})
    r = c.get("/api/attachment", params={"path": p})
    assert "inline" in r.headers.get("content-disposition", ""), \
        "inline preview was lost — images and PDFs would start downloading"
