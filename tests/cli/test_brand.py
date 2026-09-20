"""The brand is a company setting: a product name (brand.name) and a logo
(brand.logo, a file in .os/ set only through its upload endpoint). Both reach
the app and the sign-in page — which shows them before anyone is signed in,
so /brand/logo is public and the login page substitutes the name on the way
out. Admin only; the company layer is REAL shared config, restored after."""
import struct
import zlib

import httpx
import pytest
from kbenv import BASE, CREDS, U

PNG = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
       + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) & 0xffffffff))
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'


def cl(user=None):
    c = httpx.Client(base_url=BASE, timeout=30)
    if user:
        c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


@pytest.fixture(scope="module", autouse=True)
def _restore_brand():
    a = cl("alice")
    before = a.get("/api/settings").json()["company"]["values"]
    yield
    a.post("/admin/brand/logo", json={"reset": True})
    now = a.get("/api/settings").json()["company"]["values"]
    todo = [k for k in now if k.startswith("brand.") and k != "brand.logo"]
    if todo:
        a.post("/admin/settings", json={"unset": todo})
    keep = {k: v for k, v in before.items() if k == "brand.name"}
    if keep:
        a.post("/admin/settings", json={"set": keep})


def test_name_reaches_the_app_and_the_sign_in_page():
    a = cl("alice")
    assert a.post("/admin/settings", json={"set": {"brand.name": "Acme <OS>"}}).status_code == 200
    assert cl("bob").get("/api/settings").json()["effective"]["brand.name"] == "Acme <OS>"
    html = cl().get("/login").text
    assert '<small data-brand="name">Acme &lt;OS&gt;</small>' in html       # escaped
    assert "<title>Acme &lt;OS&gt; · Sign in</title>" in html
    assert 'src="/brand/logo"' in html
    assert a.post("/admin/settings", json={"set": {"brand.name": "   "}}).status_code == 400
    assert a.post("/admin/settings", json={"unset": ["brand.name"]}).status_code == 200
    assert '<small data-brand="name">Company OS</small>' in cl().get("/login").text


def test_logo_upload_reset_and_serving():
    a = cl("alice")
    # the value is a file: it cannot be set as a plain value
    assert a.post("/admin/settings", json={"set": {"brand.logo": "logo.png"}}).status_code == 400
    # default: the built-in mark, public, as an image with no script allowed
    r = cl().get("/brand/logo")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers.get("content-security-policy", "")
    # a PNG
    r = a.post("/admin/brand/logo", files={"file": ("mark.png", PNG, "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["values"]["brand.logo"] == "logo.png"
    r = cl().get("/brand/logo")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert cl("bob").get("/api/settings").json()["effective"]["brand.logo"] == "logo.png"
    assert cl("bob").get("/api/settings").json()["logoRev"] > 0
    # an SVG replaces it (and the PNG is gone, not orphaned)
    r = a.post("/admin/brand/logo", files={"file": ("mark.svg", SVG, "image/svg+xml")})
    assert r.status_code == 200, r.text
    assert cl().get("/brand/logo").content == SVG
    assert cl("bob").get("/api/settings").json()["effective"]["brand.logo"] == "logo.svg"
    # reset: back to the built-in mark
    assert a.post("/admin/brand/logo", json={"reset": True}).status_code == 200
    assert cl("bob").get("/api/settings").json()["effective"]["brand.logo"] == ""
    assert cl().get("/brand/logo").headers["content-type"].startswith("image/svg+xml")
    assert cl().get("/brand/logo").content != SVG


def test_logo_rules():
    a = cl("alice")
    assert cl("bob").post("/admin/brand/logo", files={"file": ("x.png", PNG, "image/png")}).status_code == 403
    assert cl("bob").post("/admin/brand/logo", json={"reset": True}).status_code == 403
    assert a.post("/admin/brand/logo", files={"file": ("x.gif", b"GIF89a....", "image/gif")}).status_code == 400
    bad = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert a.post("/admin/brand/logo", files={"file": ("x.svg", bad, "image/svg+xml")}).status_code == 400
    big = b'<svg xmlns="http://www.w3.org/2000/svg">' + b"<!--" + b"x" * (600 * 1024) + b"--></svg>"
    assert a.post("/admin/brand/logo", files={"file": ("x.svg", big, "image/svg+xml")}).status_code == 413
    assert a.post("/admin/brand/logo", json={"nope": 1}).status_code == 400
    assert cl("bob").get("/api/settings").json()["effective"]["brand.logo"] == ""
