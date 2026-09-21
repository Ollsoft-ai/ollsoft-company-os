"""Pinned items: admins pin company-wide shortcuts (marked "company"), every
user pins their own. A pin opens a file, reveals a folder, or types a command
into a fresh terminal running as the clicking user. They render as rows in the
sidebar's Pinned section — right-click a row (or a tree row) to pin, rename or
unpin. Company writes go through the hub (admin-only); personal lists live in
users/<u>/ (private)."""
import time

import httpx
import pytest
from conftest import BASE, CREDS, login, user_menu, wait_path
from kbenv import AREA, U, doc


def api(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def set_company(buttons):
    r = api("alice").post("/admin/launchers", json={"buttons": buttons})
    assert r.status_code == 200, r.text


def company_now():
    return api("alice").get("/api/launchers").json()["company"]


def drop_company(*labels):
    """Remove only what this test added, from the list as it is RIGHT NOW.

    The company list is real shared config on a live box, and `/admin/launchers`
    replaces it whole. Writing a snapshot taken minutes ago un-does anything
    that happened meanwhile; writing `[]` deletes someone's pins outright. Both
    happened on 2026-09-21: the box's `claude` pin vanished for an hour, and a
    module-scoped snapshot then recorded the emptiness as the truth to restore.
    Read, modify, write — always."""
    set_company([b for b in company_now() if b["label"] not in labels])


@pytest.fixture(scope="module", autouse=True)
def _company_list_is_left_as_found():
    """A canary, not a repair: if a test leaks a company pin, say so loudly
    instead of quietly rewriting shared config."""
    before = company_now()
    yield
    after = company_now()
    if after != before:
        set_company(before)
        raise AssertionError(f"a test changed the company pins: {before} -> {after}")


def test_company_button_reaches_everyone_and_opens_artifact(browser):
    set_company(company_now() + [{"label": "Pulse", "kind": "file", "target": doc("cron-demo/pulse.html")}])
    try:
        ctx = browser.new_context()
        page = login(ctx, "bob")          # NOT an admin — company buttons still show
        row = page.locator('#pins .pin-row.company', has_text="Pulse")
        row.wait_for(timeout=8000)
        assert "everyone" in (row.get_attribute("title") or "")   # whose pin it is, quietly
        row.click()
        wait_path(page, doc("cron-demo/pulse.html"), timeout=8000, kind="artifact")
        ctx.close()
    finally:
        drop_company("Pulse")


def test_admin_manages_company_buttons_via_modal(browser):
    label = f"co_{int(time.time())}"
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        user_menu(page)
        page.click('[data-testid="pins-btn"]')
        page.fill('[data-testid="lnch-label-company"]', label)
        page.select_option('[data-testid="lnch-kind-company"]', "file")
        page.fill('[data-testid="lnch-target-company"]', doc("overview.md"))
        page.click('[data-testid="lnch-add-company"]')
        page.wait_for_selector(f'#pins .pin-row.company:has-text("{label}")', timeout=8000)
        # remove it again from inside the modal
        page.locator(f'.launcher-card .lchip.company:has-text("{label}")').locator(".lx").click()
        page.wait_for_selector(f'#pins .pin-row.company:has-text("{label}")',
                               state="detached", timeout=8000)
    finally:
        drop_company(label)
        ctx.close()


def test_personal_term_button_runs_command_and_stays_private(browser):
    marker = f"lnchmk_{int(time.time())}"
    ctx = browser.new_context()
    page = login(ctx, "bob")
    try:
        user_menu(page)
        page.click('[data-testid="pins-btn"]')
        page.fill('[data-testid="lnch-label-mine"]', "Marker")
        page.select_option('[data-testid="lnch-kind-mine"]', "term")
        page.fill('[data-testid="lnch-target-mine"]', f"echo {marker}")
        page.click('[data-testid="lnch-add-mine"]')
        page.wait_for_selector('#pins .pin-row.mine:has-text("Marker")', timeout=8000)
        page.click(".launcher-card .modal-foot button")      # close the modal
        page.wait_for_selector(".modal-overlay", state="detached")
        page.click('#pins .pin-row.mine:has-text("Marker")')
        page.wait_for_selector("#terminal-panel:not([hidden])")
        deadline = time.time() + 8
        ok = False
        while time.time() < deadline:
            if page.inner_text("#terminal").replace("\n", "").count(marker) >= 2:
                ok = True                                    # typed line + its output
                break
            page.wait_for_timeout(150)
        assert ok, "the launcher must open a shell and run its command"
        # other users never see personal buttons
        other = api("carol").get("/api/launchers").json()
        assert all(b["label"] != "Marker" for b in other["mine"])
        page.keyboard.type("exit")
        page.keyboard.press("Enter")
        page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
    finally:
        api("bob").post("/api/launchers", json={"buttons": []})
        ctx.close()


def test_api_authz_and_validation():
    # non-admin cannot write company buttons
    r = api("bob").post("/admin/launchers",
                         json={"buttons": [{"label": "x", "kind": "term", "target": "id"}]})
    assert r.status_code == 403, r.text
    # multi-line targets are refused (both scopes share the validator)
    r = api("bob").post("/api/launchers",
                         json={"buttons": [{"label": "x", "kind": "term", "target": "a\nb"}]})
    assert r.status_code == 400, r.text
    r = api("bob").post("/api/launchers", json={"buttons": [{"label": "", "kind": "file", "target": "a"}]})
    assert r.status_code == 400, r.text
    # a valid personal write round-trips
    r = api("bob").post("/api/launchers",
                         json={"buttons": [{"label": "ok", "kind": "file", "target": doc("overview.md")}]})
    assert r.status_code == 200, r.text
    assert api("bob").get("/api/launchers").json()["mine"][0]["label"] == "ok"
    api("bob").post("/api/launchers", json={"buttons": []})


def test_pin_a_folder_from_the_tree_and_unpin_from_its_row(browser):
    """The way people actually pin: right-click the thing, not a dialog. The
    tree menu writes the personal list, the row appears in Pinned, and the
    row's own menu takes it away again."""
    ctx = browser.new_context()
    page = login(ctx, "bob")
    try:
        api("bob").post("/api/launchers", json={"buttons": []})
        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        page.locator(f'.tree-item[data-path="{AREA}"]').first.click(button="right")
        page.wait_for_selector('[data-testid="ctx-menu"]')
        page.click('.ctx-item:has-text("Pin to the sidebar")')
        row = page.locator('#pins .pin-row.mine[data-kind="folder"]')
        row.wait_for(timeout=8000)
        assert row.get_attribute("data-target") == AREA
        # the tree offers the reverse now, and so does the row
        page.locator(f'.tree-item[data-path="{AREA}"]').first.click(button="right")
        page.wait_for_selector('[data-testid="ctx-menu"]')
        assert page.locator('.ctx-item:has-text("Unpin from the sidebar")').count() == 1
        page.keyboard.press("Escape")
        row.click(button="right")
        page.wait_for_selector('[data-testid="ctx-menu"]')
        page.click('.ctx-item:has-text("Unpin")')
        row.wait_for(state="detached", timeout=8000)
        assert api("bob").get("/api/launchers").json()["mine"] == []
    finally:
        api("bob").post("/api/launchers", json={"buttons": []})
        ctx.close()


def test_nothing_pinned_draws_nothing(browser):
    """An empty section is noise: with no pins at all, the rows AND the
    "Pinned" heading are absent. The way back in is the tree's right-click,
    or "Pinned items" in the user menu."""
    before = company_now()              # …and put back at the end, read from NOW
    set_company([])                     # deliberately empty: that is the case under test
    api("bob").post("/api/launchers", json={"buttons": []})
    ctx = browser.new_context()
    page = login(ctx, "bob")
    try:
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        page.wait_for_timeout(600)
        assert not page.locator("#pins").is_visible()
        assert not page.locator("#pins-title").is_visible()
        # …and the dialog is still one click away, from the person
        user_menu(page)
        page.click('[data-testid="pins-btn"]')
        page.wait_for_selector(".launcher-card")
        page.fill('[data-testid="lnch-label-mine"]', "Shown")
        page.select_option('[data-testid="lnch-kind-mine"]', "file")
        page.fill('[data-testid="lnch-target-mine"]', doc("overview.md"))
        page.click('[data-testid="lnch-add-mine"]')
        page.wait_for_selector('#pins .pin-row.mine:has-text("Shown")', timeout=8000)
        assert page.locator("#pins-title").is_visible()      # the heading comes back with it
    finally:
        set_company(before)
        api("bob").post("/api/launchers", json={"buttons": []})
        ctx.close()


def test_an_admin_promotes_a_pin_to_the_whole_company_and_back(browser):
    """A pin you made for yourself can become everyone's without retyping it:
    the row's menu moves it between the two lists. Only an admin is offered
    the move, and the destination is written before the source is cleared."""
    label = f"promote_{int(time.time())}"
    before = company_now()
    ctx = browser.new_context()
    page = login(ctx, "alice")                      # alice is the admin
    try:
        api("alice").post("/api/launchers", json={
            "buttons": [{"label": label, "kind": "file", "target": doc("overview.md")}]})
        page.reload()
        row = page.locator(f'#pins .pin-row.mine:has-text("{label}")')
        row.wait_for(timeout=8000)
        row.click(button="right")
        page.wait_for_selector('[data-testid="ctx-menu"]')
        page.click('.ctx-item:has-text("Pin for everyone")')
        page.wait_for_selector(f'#pins .pin-row.company:has-text("{label}")', timeout=8000)
        assert page.locator(f'#pins .pin-row.mine:has-text("{label}")').count() == 0
        assert any(b["label"] == label for b in api("bob").get("/api/launchers").json()["company"])
        # …and back again
        page.locator(f'#pins .pin-row.company:has-text("{label}")').click(button="right")
        page.wait_for_selector('[data-testid="ctx-menu"]')
        page.click('.ctx-item:has-text("Keep it just for me")')
        page.wait_for_selector(f'#pins .pin-row.mine:has-text("{label}")', timeout=8000)
        # the move is two whole-list writes: wait for the second to land before
        # reading, or the assertion races the browser
        deadline = time.time() + 8
        while time.time() < deadline:
            if company_now() == before:
                break
            page.wait_for_timeout(200)
        assert company_now() == before, "the move must leave every other pin alone"
        # a non-admin is never offered the move
        other = browser.new_context()
        bpage = login(other, "bob")
        api("bob").post("/api/launchers", json={
            "buttons": [{"label": "just-mine", "kind": "file", "target": doc("overview.md")}]})
        bpage.reload()
        bpage.locator('#pins .pin-row.mine:has-text("just-mine")').click(button="right")
        bpage.wait_for_selector('[data-testid="ctx-menu"]')
        labels = bpage.evaluate("() => [...document.querySelectorAll('.ctx-item')].map(b => b.textContent.trim())")
        assert not any("everyone" in l for l in labels), labels
        api("bob").post("/api/launchers", json={"buttons": []})
        other.close()
    finally:
        drop_company(label)
        api("alice").post("/api/launchers", json={"buttons": []})
        ctx.close()
