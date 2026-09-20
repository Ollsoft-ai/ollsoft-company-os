"""The brand in the chrome: the product name next to the mark and in the tab
title, the logo served from /brand/logo, an admin's upload through the
Settings dialog, and the sign-in page wearing both."""
import struct
import zlib

import httpx
import pytest
from conftest import BASE, CREDS, login, user_menu
from kbenv import U

PNG = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
       + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) & 0xffffffff))


def api(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


@pytest.fixture(autouse=True)
def _reset():
    a = api("alice")
    a.post("/admin/settings", json={"unset": ["brand.name"]})
    a.post("/admin/brand/logo", json={"reset": True})
    yield
    a.post("/admin/settings", json={"unset": ["brand.name"]})
    a.post("/admin/brand/logo", json={"reset": True})


def test_name_and_logo_reach_the_chrome_and_the_sign_in_page(browser):
    a = api("alice")
    assert a.post("/admin/settings", json={"set": {"brand.name": "Acme OS"}}).status_code == 200
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        page.goto(BASE + "/login")
        assert page.text_content('[data-brand="name"]').strip() == "Acme OS"   # rendered uppercase by CSS
        assert "Acme OS" in page.title()
        page = login(ctx, "bob")
        page.wait_for_function("() => document.querySelector('.brand-word [data-brand=\"name\"]').textContent === 'Acme OS'", timeout=15000)
        assert page.title() == "Acme OS"
        assert page.get_attribute(".logo-word", "src") == "/brand/logo"
        # the admin uploads a logo through the dialog; bob's page follows on its own
        admin = login(browser.new_context(), "alice")
        admin.wait_for_function("() => !document.querySelector('#admin-btn').hidden")
        user_menu(admin); admin.click('[data-testid="settings-btn"]')
        admin.click('[data-testid="settings-tab-company"]')
        admin.set_input_files('[data-testid="set-brand-logo-file"]', {"name": "mark.png", "mimeType": "image/png", "buffer": PNG})
        admin.wait_for_function("() => (window.__kbsettings.get('brand.logo') || '') === 'logo.png'", timeout=10000)
        assert admin.get_attribute(".logo-word", "src").startswith("/brand/logo?v=")
        page.wait_for_function("() => document.querySelector('.logo-word').getAttribute('src').startsWith('/brand/logo?v=')", timeout=15000)
        assert page.evaluate("() => document.querySelector('.logo-word').classList.contains('custom')")
        # reset from the dialog: the built-in mark again, no inversion class
        admin.click('[data-testid="set-brand-logo-co-reset"]')
        admin.wait_for_function("() => (window.__kbsettings.get('brand.logo') || '') === ''", timeout=10000)
        page.wait_for_function("() => document.querySelector('.logo-word').getAttribute('src') === '/brand/logo'", timeout=15000)
        admin.context.close()
    finally:
        ctx.close()
