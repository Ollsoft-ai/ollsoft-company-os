"""Read-only multiplayer viewing + live filesystem tree.

A file you can read but not write (company config, someone else's read-only
share) must still open in the live editor — you see its content and any live
updates — but you cannot change it, and the daemon refuses to persist an edit
from a read-only join even if the client bypasses the editor.
"""
import time

import httpx
from conftest import BASE, CREDS, expand_folder, login
from kbenv import AREA, U, doc

RO_DOC = ".claude/CLAUDE.md"   # owned root:kb-users 644 -> read-only to everyone


def get_file(user, path):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c.get("/api/file", params={"path": path}).json()


def test_readonly_file_opens_and_shows_content(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click('[data-testid="hidden-toggle"]')   # dot-entries hidden by default
    expand_folder(page, ".claude")                # .claude auto-collapses
    page.click(f'.tree-item[data-path="{RO_DOC}"]')
    page.wait_for_function("() => window.__kbview && window.__kbview.state.doc.length > 0", timeout=10000)
    text = page.evaluate("() => window.__kbview.state.doc.toString()")
    assert "knowledgebase" in text.lower(), "read-only viewer must see the file content"
    # editor is not editable, and the badge says read-only
    assert page.evaluate("() => window.__kbview.state.readOnly") is True
    page.wait_for_selector('#access-badge.ro')
    ctx.close()


def test_readonly_edit_is_not_persisted(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    before = get_file("alice", RO_DOC)["content"]
    page.click('[data-testid="hidden-toggle"]')   # dot-entries hidden by default
    expand_folder(page, ".claude")                # .claude auto-collapses
    page.click(f'.tree-item[data-path="{RO_DOC}"]')
    page.wait_for_function("() => window.__kbydoc && window.__kbview.state.doc.length > 0", timeout=10000)
    # Bypass the read-only editor and mutate the CRDT text directly, as a hostile
    # read-only client would. The daemon must DROP this update.
    page.evaluate("() => window.__kbydoc.getText('content').insert(0, 'HOSTILE_EDIT ')")
    time.sleep(2.5)
    after = get_file("alice", RO_DOC)["content"]
    assert after == before, "a read-only client must not be able to change the file"
    assert "HOSTILE_EDIT" not in after
    ctx.close()


def test_live_tree_shows_newly_created_file(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.wait_for_selector(f'.tree-item[data-path="{AREA}"]')
    name = f"live_{int(time.time())}.md"
    # create a file out-of-band (as if a colleague made/shared it)
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    assert c.post("/fs/newfile", json={"path": doc(f"{name}")}).status_code == 200
    # the tree polls; the new file should appear on its own within a few seconds
    page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=8000)
    ctx.close()
    c.post("/api/fs/delete", json={"path": doc(f"{name}")})
