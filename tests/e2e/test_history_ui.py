"""The version-history panel in the editor: open a doc, make versions, see them
attributed, view a diff, and restore an old one — all through the real UI."""
import json
import time

import httpx
import pytest
from conftest import BASE, CREDS, login, open_doc, doc_text
from kbenv import U, doc

TAG = str(int(time.time()))
DOC = doc(f"hist_{TAG}.md")


def api(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def backend_v(c):
    r = c.get("/api/cron")
    return r.json().get("v", 0) if r.status_code == 200 else 0


@pytest.fixture(scope="module")
def seeded():
    c = api("alice")
    if backend_v(c) < 10:
        pytest.skip("backend predates versioning — restart backends first")
    assert c.post("/api/file", json={"path": DOC}).status_code == 200
    c.post("/api/artifact/write", json={"path": DOC, "content": "# plan\n\nfirst draft\n"})
    time.sleep(6)
    c.post("/api/artifact/write", json={"path": DOC, "content": "# plan\n\nsecond draft, changed\n"})
    # wait until two versions exist
    deadline = time.time() + 25
    while time.time() < deadline:
        if len(c.get("/api/vc/log", params={"path": DOC}).json().get("entries", [])) >= 2:
            break
        time.sleep(1)
    yield c
    c.post("/api/fs/delete", json={"path": DOC})


def test_history_panel_lists_versions_with_authors(browser, seeded):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, DOC)
    page.wait_for_selector('#doc-history:not([hidden])')
    page.click('#doc-history')
    page.wait_for_selector('[data-testid="vh-list"] .vh-item')
    items = page.locator('[data-testid="vh-item"]')
    assert items.count() >= 2, "both saves should appear as versions"
    # each row names the author
    assert "alice" in page.locator('[data-testid="vh-list"]').inner_text()
    # newest is marked current
    assert "current" in items.first.inner_text()
    ctx.close()


def test_history_shows_diff_and_restores(browser, seeded):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, DOC)
    assert "second draft" in doc_text(page)
    page.click('#doc-history')
    page.wait_for_selector('[data-testid="vh-item"]')
    # click the OLDEST version -> its diff renders, restore becomes available
    page.locator('[data-testid="vh-item"]').last.click()
    page.wait_for_selector('[data-testid="vh-diff"] .vh-add, [data-testid="vh-diff"] .vh-del', timeout=8000)
    page.wait_for_selector('[data-testid="vh-restore"]:not([hidden])')
    page.click('[data-testid="vh-restore"]')
    page.click('[data-testid="dlg-ok"]')          # confirm restore
    # the live editor converges back to the first draft
    page.wait_for_function(
        "() => window.__kbview && window.__kbview.state.doc.toString().includes('first draft')",
        timeout=10000)
    assert "second draft" not in doc_text(page)
    ctx.close()
