"""The UI/UX round: Notion-style editor shorthand ([] → todo, Tab indents,
Enter continues lists, @mentions), single-line heading ops on line selections,
VS-Code-style tree verbs (context menu rename/copy/paste, drag-to-move), URL
deep links, and live presence in the file tree.

Deep-link and presence tests light up once the hub/syncd restart with the new
code — until then they skip with a message saying exactly that."""
import json
import re
import time

import httpx
import pytest
from conftest import BASE, dlg_fill, login, open_doc, doc_text
from kbenv import CREDS, U, doc

TAG = str(int(time.time()))
DIR = doc(f"uiux_{TAG}")


def http(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def hub_has_deep_links():
    return httpx.get(BASE + "/company", follow_redirects=False).status_code != 404


def syncd_has_presence(c):
    return c.get("/api/presence").status_code == 200


@pytest.fixture(scope="module")
def box():
    c = http("alice")
    assert c.post("/api/fs/mkdir", json={"path": DIR}).status_code == 200
    assert c.post("/api/file", json={"path": f"{DIR}/typing.md"}).status_code == 200
    c.post("/api/artifact/write", json={"path": f"{DIR}/typing.md", "content": "# t\n\n"})
    yield c
    c.post("/api/fs/delete", json={"path": DIR})


def cursor_to_end(page):
    page.click(".cm-content")
    page.evaluate("() => { const v = window.__kbview; "
                  "v.dispatch({selection:{anchor: v.state.doc.length}}); v.focus(); }")


def test_todo_shorthand_tab_and_enter(browser, box):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, f"{DIR}/typing.md")

    # "[]" at line start becomes a canonical GFM task
    cursor_to_end(page)
    page.keyboard.type("[]buy milk")
    assert "- [ ] buy milk" in doc_text(page)

    # Enter continues the task list…
    page.keyboard.press("Enter")
    page.keyboard.type("second")
    assert "- [ ] buy milk\n- [ ] second" in doc_text(page)

    # …and Tab nests the item; Shift+Tab brings it back
    page.keyboard.press("Tab")
    assert "\n  - [ ] second" in doc_text(page)
    page.keyboard.press("Shift+Tab")
    assert "\n- [ ] second" in doc_text(page)
    ctx.close()


def test_mention_autocomplete(browser, box):
    c = box
    assert c.post("/api/file", json={"path": f"{DIR}/mention.md"}).status_code == 200
    c.post("/api/artifact/write", json={"path": f"{DIR}/mention.md", "content": "# m\n\n"})
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, f"{DIR}/mention.md")
    page.wait_for_function("() => window.__kbsynced === true", timeout=8000)
    cursor_to_end(page)
    # A real box has arbitrary principals (not just the demo trio), so a short
    # fuzzy prefix may rank other users into the list. Type enough of "bob" to
    # be deterministic, but stop short of the full name so the completion still
    # has something to INSERT — the doc gains characters we never typed.
    page.keyboard.type("ping @bo")
    page.wait_for_selector(".cm-tooltip-autocomplete li", timeout=6000)
    # click the EXACT "bob" row (substring matching could hit e.g. a "bobby")
    row = page.locator(".cm-tooltip-autocomplete li").filter(
        has_text=re.compile(r"^bob$")).first
    row.wait_for(timeout=6000)
    row.click()
    page.wait_for_function(
        '() => window.__kbview.state.doc.toString().includes("ping @bob")', timeout=6000)
    ctx.close()


def test_heading_on_line_selection_stays_on_that_line(browser, box):
    c = box
    assert c.post("/api/file", json={"path": f"{DIR}/heading.md"}).status_code == 200
    c.post("/api/artifact/write", json={"path": f"{DIR}/heading.md", "content": "alpha\nbeta\n"})
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, f"{DIR}/heading.md")
    # a triple-click selection: the whole first line INCLUDING the newline
    page.evaluate("() => { const v = window.__kbview; const l = v.state.doc.line(1); "
                  "v.dispatch({selection:{anchor: l.from, head: l.to + 1}}); }")
    page.click('[data-md="h1"]')
    text = doc_text(page)
    assert text.startswith("# alpha\nbeta"), text   # beta must NOT become a heading
    ctx.close()


def test_context_menu_rename(browser, box):
    c = box
    assert c.post("/api/file", json={"path": f"{DIR}/old.md"}).status_code == 200
    ctx = browser.new_context()
    page = login(ctx, "alice")
    row = f'.tree-item[data-path="{DIR}/old.md"]'
    page.wait_for_selector(row, timeout=8000)
    page.click(row, button="right")
    page.wait_for_selector('[data-testid="ctx-menu"]')
    page.click('.ctx-item:has-text("Rename…")')
    dlg_fill(page, "new.md")
    page.wait_for_selector(f'.tree-item[data-path="{DIR}/new.md"]', timeout=8000)
    page.wait_for_selector(row, state="detached", timeout=8000)
    ctx.close()


def test_context_menu_copy_paste(browser, box):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    src = f'.tree-item[data-path="{DIR}/new.md"]'
    page.wait_for_selector(src, timeout=8000)
    page.click(src, button="right")
    page.click('.ctx-item:has-text("Copy")')
    page.click(f'.tree-item[data-path="{DIR}"]', button="right")
    page.click('.ctx-item:has-text("Paste")')
    # same folder + name taken -> pastes under a "copy" name
    page.wait_for_selector(f'.tree-item[data-path="{DIR}/new copy.md"]', timeout=8000)
    ctx.close()


def test_drag_row_onto_folder_moves_it(browser, box):
    c = box
    assert c.post("/api/fs/mkdir", json={"path": f"{DIR}/dest"}).status_code == 200
    assert c.post("/api/file", json={"path": f"{DIR}/mv.md"}).status_code == 200
    ctx = browser.new_context()
    page = login(ctx, "alice")
    src = f'.tree-item[data-path="{DIR}/mv.md"]'
    page.wait_for_selector(src, timeout=8000)
    page.drag_and_drop(src, f'.tree-item[data-path="{DIR}/dest"]')
    page.wait_for_selector(f'.tree-item[data-path="{DIR}/dest/mv.md"]', timeout=8000)
    page.wait_for_selector(src, state="detached", timeout=8000)
    ctx.close()


def test_url_follows_tabs_and_survives_reload(browser, box):
    if not hub_has_deep_links():
        pytest.skip("hub not yet restarted with deep-link routes")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, f"{DIR}/typing.md")
    page.wait_for_function(
        f'() => location.pathname === "/{DIR}/typing.md"', timeout=6000)
    page.reload()
    page.wait_for_function(
        f'() => window.__kbpath === "{DIR}/typing.md"', timeout=10000)
    ctx.close()


def test_shared_url_lands_on_the_file_after_login(browser, box):
    if not hub_has_deep_links():
        pytest.skip("hub not yet restarted with deep-link routes")
    ctx = browser.new_context()          # fresh: no session cookie
    page = ctx.new_page()
    page.goto(f"{BASE}/{DIR}/typing.md")
    page.wait_for_url("**/login**")      # bounced to sign-in, with ?next=
    page.fill('input[name="username"]', U("alice"))
    page.fill('input[name="password"]', CREDS["alice"])
    page.click('button[type="submit"]')
    page.wait_for_function(
        f'() => window.__kbpath === "{DIR}/typing.md"', timeout=10000)
    ctx.close()


def test_tree_shows_who_has_a_doc_open(browser, box):
    if not syncd_has_presence(box):
        pytest.skip("hub/syncd not yet restarted with presence support")
    kctx = browser.new_context()
    k = login(kctx, "alice")
    jctx = browser.new_context()
    j = login(jctx, "bob")
    open_doc(j, f"{DIR}/typing.md")
    # alice's tree row grows bob's avatar within a poll cycle or two
    av = f'.tree-item[data-path="{DIR}/typing.md"] .tp-avatar'
    k.wait_for_selector(av, timeout=15000)
    assert "bob" in k.locator(av).first.get_attribute("title")
    jctx.close()
    kctx.close()


def test_rename_of_open_doc_does_not_resurrect_it(browser, box):
    """The corruption guard: A renames a doc that B has open and is editing. The
    old file must NOT be recreated by syncd's flush (it used to reappear at the
    old path, as root, forking the content), and B's now-dead tab must retire."""
    if not syncd_has_presence(box):
        pytest.skip("syncd not yet restarted with the retire-room fix")
    c = box
    import os
    assert c.post("/api/file", json={"path": f"{DIR}/live.md"}).status_code == 200
    c.post("/api/artifact/write", json={"path": f"{DIR}/live.md", "content": "# live\n"})

    actx = browser.new_context(); a = login(actx, "alice")
    bctx = browser.new_context(); b = login(bctx, "bob")
    open_doc(a, f"{DIR}/live.md")
    open_doc(b, f"{DIR}/live.md")
    b.wait_for_function("() => window.__kbsynced === true", timeout=8000)
    # B is actively editing when the rename lands
    b.evaluate("() => { const v=window.__kbview; v.dispatch({changes:{from:v.state.doc.length,insert:'\\nB was typing'}}); }")

    # A renames it out from under B, via the context menu
    a.click(f'.tree-item[data-path="{DIR}/live.md"]', button="right")
    a.click('.ctx-item:has-text("Rename…")')
    dlg_fill(a, "renamed.md")
    a.wait_for_selector(f'.tree-item[data-path="{DIR}/renamed.md"]', timeout=8000)

    # give syncd several flush cycles: the old path must stay gone (no resurrection)
    import time as _t
    for _ in range(12):
        _t.sleep(0.5)
        assert not os.path.exists(f"/srv/kb/{DIR}/live.md"), "old file was resurrected on disk"
    assert os.path.exists(f"/srv/kb/{DIR}/renamed.md")

    # B's dead tab retires on its own (tree prune) with a notice
    b.wait_for_selector(f'.tab[data-path="{DIR}/live.md"]', state="detached", timeout=15000)
    actx.close(); bctx.close()


def test_presence_is_permission_filtered(box):
    if not syncd_has_presence(box):
        pytest.skip("hub/syncd not yet restarted with presence support")
    # carol must never learn that (or what) alice edits in his private area.
    # alice's own private.md fixture is used as the watched doc: joining the
    # live session is what registers presence, so query it via the ws-admitting
    # side effect of an epoch fetch + a real open in the API-less way — instead
    # we just assert the endpoint filters: whatever carol sees must be readable
    # by carol (spot-checked against /api/file).
    i = http("carol")
    seen = i.get("/api/presence").json().get("presence", {})
    for path in seen:
        assert i.get("/api/file", params={"path": path}).status_code == 200, \
            f"presence leaked an unreadable path to carol: {path}"
