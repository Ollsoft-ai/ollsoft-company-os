"""File-manager UI: collapsible tree, .claude visibility, access badge, create
via the folder button, and the permissions modal (add an ACL end to end)."""
import time

import httpx
from conftest import BASE, CREDS, dlg_fill, dlg_ok, login
from kbenv import AREA, U, doc, home

TAG = str(int(time.time()))


def props(user, path):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c.get("/fs/props", params={"path": path}).json()


def test_claude_visible_and_folders_collapse(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    # dot-entries are HIDDEN by default; the sidebar `.*` toggle reveals them
    assert page.locator('.tree-item[data-path=".claude"]').count() == 0
    page.click('[data-testid="hidden-toggle"]')
    page.wait_for_selector('.tree-item[data-path=".claude"]')
    child = '.tree-item[data-path=".claude/skills"]'
    # machinery folders (names starting with . or _) start COLLAPSED by default
    assert not page.locator(child).is_visible()
    page.locator('.tree-item[data-path=".claude"] .tlabel').first.click()   # expand
    assert page.locator(child).is_visible()
    page.locator('.tree-item[data-path=".claude"] .tlabel').first.click()   # collapse again
    assert not page.locator(child).is_visible()
    ctx.close()


def test_collapse_and_expand_all(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    inner = f'.tree-item[data-path="{doc("overview.md")}"]'
    assert page.locator(inner).is_visible()               # expanded by default
    page.click('[data-testid="tree-fold"]')               # collapse all
    assert not page.locator(inner).is_visible()
    assert page.locator(f'.tree-item[data-path="{AREA}"]').is_visible()
    page.click('[data-testid="tree-fold"]')               # now it expands all
    assert page.locator(inner).is_visible()
    ctx.close()


def test_access_badge_reflects_permissions(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    page.wait_for_selector('#access-badge:not([hidden])')
    assert "write" in page.inner_text('#access-badge')
    ctx.close()


def test_create_file_via_folder_button(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    name = f"uicreate_{TAG}.md"
    page.hover(f'.tree-item[data-path="{AREA}"]')
    page.click(f'.tree-item[data-path="{AREA}"] .tbtn[title="New file here"]')
    dlg_fill(page, name)
    page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=6000)
    # created in company/ -> inherits root ownership
    assert props("alice", doc(f"{name}"))["owner"] == "root"
    # remove it again (company/ is group-writable, so the parent-write check passes)
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    c.post("/api/fs/delete", json={"path": doc(f"{name}")})
    ctx.close()


def test_permissions_modal_adds_acl(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    # alice owns his private file, so he can edit its ACLs.
    target = home("alice", "private.md")
    page.hover(f'.tree-item[data-path="{target}"]')
    page.click(f'.tree-item[data-path="{target}"] .tbtn[title="Permissions"]')
    page.wait_for_selector('#pm-addacl')
    page.select_option('#pm-type', 'user')
    page.select_option('#pm-name', 'bob')   # picker, fed by /api/principals
    page.select_option('#pm-perms', 'r')
    page.click('#pm-addacl')
    page.click('#pm-save')
    # sharing a file inside a 0700 home auto-grants ancestor traverse; the app
    # explains that in its own dialog — acknowledge it
    dlg_ok(page)
    page.wait_for_selector('.modal-overlay', state='detached', timeout=5000)
    acls = props("alice", target)["acls"]
    assert any(a["type"] == "user" and a["name"] == "bob" for a in acls), acls
    # Clean up: undo the share AND the auto-granted ancestor traverse, so the
    # "bob can't read alice's private" invariant is restored for other tests.
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    for pth in (target, home("alice"), "users"):
        c.post("/fs/props", json={"path": pth, "acl_remove": [{"type": "user", "name": U("bob")}]})
    ctx.close()


def test_machinery_folders_collapsed_by_default(browser):
    """Folders whose name starts with '_' or '.' (attachments' _files/,
    _secrets/, .claude/) hold machinery, not notes — they open collapsed so the
    tree stays about your files. Expanding one sticks (the poll won't refold it)."""
    import time as _t
    import httpx
    tag = str(int(_t.time()))
    base = doc(f"mach_{tag}")
    c = httpx.Client(base_url="http://127.0.0.1:8300", timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    c.post("/api/fs/mkdir", json={"path": base})
    c.post("/api/fs/mkdir", json={"path": f"{base}/_files"})
    c.post("/api/fs/mkdir", json={"path": f"{base}/notes"})
    c.post("/api/file", json={"path": f"{base}/_files/a.md"})
    c.post("/api/file", json={"path": f"{base}/notes/b.md"})
    ctx = browser.new_context()
    try:
        page = login(ctx, "alice")
        page.wait_for_selector(f'.tree-item[data-path="{base}/_files"]', timeout=8000)
        # _files starts closed; a normal folder (notes) starts open
        assert not page.locator(f'.tree-item[data-path="{base}/_files/a.md"]').is_visible()
        assert page.locator(f'.tree-item[data-path="{base}/notes/b.md"]').is_visible()
        # expanding _files reveals its child and survives a tree poll cycle
        page.locator(f'.tree-item[data-path="{base}/_files"] .tlabel').first.click()
        assert page.locator(f'.tree-item[data-path="{base}/_files/a.md"]').is_visible()
        page.wait_for_timeout(4600)
        assert page.locator(f'.tree-item[data-path="{base}/_files/a.md"]').is_visible(), \
            "the poll must not refold a folder the user opened"
    finally:
        c.post("/api/fs/delete", json={"path": base})
        ctx.close()
