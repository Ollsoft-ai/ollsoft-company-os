"""The trash: a delete is a move, so it can come back.

Deleting puts a file or folder into the `.trash/` of the folder that owns its
audience — `company/.trash/`, `projects/<p>/.trash/`, `users/<u>/.trash/` —
with a `meta.json` saying where it came from. Restoring is the move back. A
secret is never copied aside; it goes for good.
"""
import json
import time

import httpx
import pytest
from conftest import BASE, CREDS, dlg_ok, login, user_menu
from kbenv import U, doc, full


def api(user):
    c = httpx.Client(base_url=BASE, timeout=20)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def write(c, path, text="# probe\n"):
    r = c.post("/api/artifact/write", json={"path": path, "content": text})
    assert r.status_code == 200, r.text


def trash(c):
    r = c.get("/api/fs/trash")
    assert r.status_code == 200, r.text
    return r.json()


def test_a_deleted_file_waits_in_the_trash_and_comes_back():
    c = api("alice")
    path = doc(f"trash-{int(time.time())}.md")
    write(c, path, "# keep me\n")
    r = c.post("/api/fs/delete", json={"path": path})
    assert r.status_code == 200, r.text
    ident = r.json()["trashed"]
    assert ident.startswith("company/"), ident          # the area's own trash, not somewhere else
    assert not full(path).exists()

    entry = next(e for e in trash(c)["entries"] if e["id"] == ident)
    assert entry["path"] == path and entry["deletedBy"] == U("alice") and not entry["dir"]

    r = c.post("/api/fs/restore", json={"id": ident})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == path and r.json()["renamed"] is False
    assert full(path).read_text() == "# keep me\n"
    assert all(e["id"] != ident for e in trash(c)["entries"])
    c.post("/api/fs/delete", json={"path": path, "permanent": True})


def test_a_folder_goes_whole_and_a_taken_name_is_not_overwritten():
    c = api("alice")
    stamp = int(time.time())
    folder = doc(f"trashdir-{stamp}")
    assert c.post("/api/fs/mkdir", json={"path": folder}).status_code == 200
    write(c, f"{folder}/inside.md", "# inside\n")
    r = c.post("/api/fs/delete", json={"path": folder})
    ident = r.json()["trashed"]
    assert r.json()["was_dir"] is True
    assert not full(folder).exists()

    # something takes the name back while it sits in the trash
    assert c.post("/api/fs/mkdir", json={"path": folder}).status_code == 200
    write(c, f"{folder}/other.md", "# other\n")
    r = c.post("/api/fs/restore", json={"id": ident})
    assert r.status_code == 200, r.text
    assert r.json()["renamed"] is True
    restored = r.json()["path"]
    assert restored != folder and (full(restored) / "inside.md").is_file()
    assert (full(folder) / "other.md").is_file()        # nothing was written over
    for p in (restored, folder):
        c.post("/api/fs/delete", json={"path": p, "permanent": True})


def test_a_secret_is_deleted_outright_and_never_copied_aside():
    c = api("alice")
    path = doc(f"_secrets/trash-{int(time.time())}.md")
    write(c, path, "token\n")
    r = c.post("/api/fs/delete", json={"path": path})
    assert r.status_code == 200, r.text
    assert "trashed" not in r.json(), "a secret must not be kept in the trash"
    assert all(e["path"] != path for e in trash(c)["entries"])


def test_only_your_own_and_the_shared_trash_are_listed():
    """bob's deletions in his own folder are his; alice never sees them."""
    b = api("bob")
    path = f"users/{U('bob')}/trash-{int(time.time())}.md"
    write(b, path, "# mine\n")
    ident = b.post("/api/fs/delete", json={"path": path}).json()["trashed"]
    assert ident.startswith(f"users/{U('bob')}/")
    assert any(e["id"] == ident for e in trash(b)["entries"])
    assert all(e["id"] != ident for e in trash(api("carol"))["entries"])
    b.post("/api/fs/trash-purge", json={"id": ident})


def test_a_deleted_document_leaves_the_search_index():
    """`.trash/` is a dot-directory: the tree hides it like `.claude/`, and
    the indexer skips it — so a deleted document stops turning up in search
    while the file itself is still there to restore."""
    c = api("alice")
    marker = f"trashmarker{int(time.time())}"
    path = doc(f"{marker}.md")
    write(c, path, f"# {marker}\nsomething findable\n")
    found = False
    for _ in range(40):
        if any(marker in json.dumps(h) for h in c.get("/api/search", params={"q": marker}).json().get("results", [])):
            found = True
            break
        time.sleep(0.5)
    assert found, "the document never reached the index"
    ident = c.post("/api/fs/delete", json={"path": path}).json()["trashed"]
    for _ in range(40):
        hits = c.get("/api/search", params={"q": marker}).json().get("results", [])
        if not any(marker in json.dumps(h) for h in hits):
            break
        time.sleep(0.5)
    else:
        pytest.fail("a trashed document is still in search")
    c.post("/api/fs/trash-purge", json={"id": ident})


def test_the_ui_moves_to_the_trash_with_an_undo_and_restores_from_the_view(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = login(ctx, "alice")
    c = api("alice")
    name = f"trash-ui-{int(time.time())}.md"
    write(c, doc(name), "# probe\n")
    try:
        page.reload()
        page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=15000)
        page.locator(f'.tree-item[data-path="{doc(name)}"]').first.click(button="right")
        page.wait_for_selector('[data-testid="ctx-menu"]')
        page.click('.ctx-item.danger:has-text("Delete")')
        page.wait_for_selector(".modal-card")
        assert "to the trash" in page.text_content(".modal-card")    # not "Delete for good"
        dlg_ok(page)
        page.wait_for_selector('[data-testid="toast-act"]', timeout=8000)
        assert "Moved to the trash" in page.text_content('[data-testid="toast"]')
        # the count appears on the menu item, which opens the list
        user_menu(page)
        page.wait_for_function(
            "() => (document.querySelector('#trash-btn .trash-row-n') || {}).textContent",
            timeout=8000)
        page.click('[data-testid="trash-btn"]')
        row = page.locator('[data-testid="trash-item"]', has_text=name)
        row.wait_for(timeout=8000)
        assert doc(name) in row.text_content()                        # where it came from
        row.locator('[data-testid="trash-restore"]').click()
        page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=10000)
        assert row.count() == 0, "the entry leaves the list once it is back"
    finally:
        c.post("/api/fs/delete", json={"path": doc(name), "permanent": True})
        ctx.close()


def test_undo_in_the_toast_puts_it_straight_back(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = login(ctx, "alice")
    c = api("alice")
    name = f"trash-undo-{int(time.time())}.md"
    write(c, doc(name), "# probe\n")
    try:
        page.reload()
        page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=15000)
        page.locator(f'.tree-item[data-path="{doc(name)}"]').first.click(button="right")
        page.wait_for_selector('[data-testid="ctx-menu"]')
        page.click('.ctx-item.danger:has-text("Delete")')
        dlg_ok(page)
        page.click('[data-testid="toast-act"]')
        page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=10000)
        assert full(doc(name)).is_file()
    finally:
        c.post("/api/fs/delete", json={"path": doc(name), "permanent": True})
        ctx.close()
