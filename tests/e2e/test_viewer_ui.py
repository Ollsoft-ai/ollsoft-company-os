"""Viewer accounts in the browser: Terminal and Cron vanish everywhere,
terminal-kind launcher chips are hidden, and documents still open editable."""
import time

import httpx
import pytest
from conftest import BASE, CREDS, user_menu, wait_path
from kbenv import U, doc

VU = f"vwui{int(time.time()) % 100000}"
PW = "ViewPassphrase01"


def api(user, password=None):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": password or CREDS[user]})
    return c


@pytest.fixture(scope="module")
def viewer():
    a = api("alice")
    r = a.post("/admin/users", json={"username": U(VU), "first": "View", "last": "Only",
                                     "email": f"{VU}@example.com", "password": PW,
                                     "kind": "viewer"})
    assert r.status_code == 200, r.text
    yield VU
    a.post("/admin/users/delete", json={"username": U(VU)})


def test_viewer_browser_experience(browser, viewer):
    a = api("alice")
    original = a.get("/api/launchers").json()["company"]
    a.post("/admin/launchers", json={"buttons": [
        {"label": "Shell thing", "kind": "term", "target": "id"},
        {"label": "Overview", "kind": "file", "target": doc("overview.md")}]})
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        page.goto(BASE + "/login")
        page.fill('input[name="username"]', U(viewer))
        page.fill('input[name="password"]', PW)
        page.click('button[type="submit"]')
        page.wait_for_url(BASE + "/")
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        # no execution affordances anywhere — not even inside the user menu
        user_menu(page)
        assert page.locator('[data-testid="toggle-term"]').is_hidden()
        assert page.locator('[data-testid="cron-btn"]').is_hidden()
        page.wait_for_selector('.launchbar .lchip.company')          # file chip shows
        assert page.locator('.launchbar .lchip.company', has_text="Overview").count() == 1
        assert page.locator('.launchbar .lchip.company', has_text="Shell thing").count() == 0
        # documents still open — and are editable (kb-users group write)
        page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
        wait_path(page, doc("overview.md"))
        page.wait_for_selector('#access-badge:not([hidden])')
        assert "write" in page.get_attribute('#access-badge', 'title')      # the pen; words on hover
    finally:
        a.post("/admin/launchers", json={"buttons": original})
        ctx.close()
