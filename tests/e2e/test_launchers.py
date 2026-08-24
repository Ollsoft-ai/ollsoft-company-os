"""Launcher buttons: admins publish company-wide shortcuts (blue), every user
keeps personal ones (violet). A button either opens a file/artifact or types a
command into a fresh terminal running as the clicking user. Company writes go
through the hub (admin-only); personal lists live in users/<u>/ (private)."""
import time

import httpx
import pytest
from conftest import BASE, CREDS, login
from kbenv import U, doc


def api(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def set_company(buttons):
    r = api("alice").post("/admin/launchers", json={"buttons": buttons})
    assert r.status_code == 200, r.text


@pytest.fixture(scope="module", autouse=True)
def _restore_real_company_buttons():
    """The company launcher list is REAL shared config — snapshot it and put it
    back when this module is done, whatever the tests wrote in between."""
    original = api("alice").get("/api/launchers").json()["company"]
    yield
    set_company(original)


def test_company_button_reaches_everyone_and_opens_artifact(browser):
    set_company([{"label": "Pulse", "kind": "file", "target": doc("cron-demo/pulse.html")}])
    try:
        ctx = browser.new_context()
        page = login(ctx, "bob")          # NOT an admin — company buttons still show
        chip = page.locator('.launchbar .lchip.company', has_text="Pulse")
        chip.wait_for(timeout=8000)
        chip.click()
        page.wait_for_function(
            "() => window.__kbkind === 'artifact' && window.__kbpath === 'company/cron-demo/pulse.html'",
            timeout=8000)
        ctx.close()
    finally:
        set_company([])


def test_admin_manages_company_buttons_via_modal(browser):
    label = f"co_{int(time.time())}"
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        page.click('[data-testid="launcher-manage"]')
        page.fill('[data-testid="lnch-label-company"]', label)
        page.select_option('[data-testid="lnch-kind-company"]', "file")
        page.fill('[data-testid="lnch-target-company"]', doc("overview.md"))
        page.click('[data-testid="lnch-add-company"]')
        page.wait_for_selector(f'.launchbar .lchip.company:has-text("{label}")', timeout=8000)
        # remove it again from inside the modal
        page.locator(f'.launcher-card .lchip.company:has-text("{label}")').locator(".lx").click()
        page.wait_for_selector(f'.launchbar .lchip.company:has-text("{label}")',
                               state="detached", timeout=8000)
    finally:
        set_company([])
        ctx.close()


def test_personal_term_button_runs_command_and_stays_private(browser):
    marker = f"lnchmk_{int(time.time())}"
    ctx = browser.new_context()
    page = login(ctx, "bob")
    try:
        page.click('[data-testid="launcher-manage"]')
        page.fill('[data-testid="lnch-label-mine"]', "Marker")
        page.select_option('[data-testid="lnch-kind-mine"]', "term")
        page.fill('[data-testid="lnch-target-mine"]', f"echo {marker}")
        page.click('[data-testid="lnch-add-mine"]')
        page.wait_for_selector('.launchbar .lchip.mine:has-text("Marker")', timeout=8000)
        page.click(".launcher-card .modal-foot button")      # close the modal
        page.wait_for_selector(".modal-overlay", state="detached")
        page.click('.launchbar .lchip.mine:has-text("Marker")')
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
