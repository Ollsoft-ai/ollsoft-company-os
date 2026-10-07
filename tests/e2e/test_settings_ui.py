"""The settings dialog: rows generated from the registry, a pill per row that
says which layer the value came from, × to clear that layer; an admin's Company
tab edits the shared layer with the same rows. Your settings follow you to
every browser. The one shipped setting has a single option today, so the
value itself is driven through the store hook where a select could not fire."""
import httpx
import pytest
from conftest import BASE, CREDS, login, user_menu
from kbenv import U

KEY, ROW = "ui.theme", "set-ui-theme"
VAL = "deep-blue"


def api(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


@pytest.fixture(scope="module", autouse=True)
def _restore_company_layer():
    a = api("alice")
    before = a.get("/api/settings").json()["company"]["values"]
    yield
    now = a.get("/api/settings").json()["company"]["values"]
    if now:
        a.post("/admin/settings", json={"unset": list(now)})
    if before:
        a.post("/admin/settings", json={"set": before})


@pytest.fixture(autouse=True)
def _clean_slate():
    api("alice").post("/admin/settings", json={"unset": [KEY]})
    api("bob").post("/api/settings", json={"unset": [KEY]})
    yield
    api("bob").post("/api/settings", json={"unset": [KEY]})


def pill(page, testid):
    return page.inner_text(f'[data-testid="{testid}"]').strip().lower()


def wait_pill(page, testid, text):
    page.wait_for_function(
        "([t, want]) => (document.querySelector(`[data-testid=\"${t}\"]`) || {}).textContent"
        ".trim().toLowerCase() === want", arg=[testid, text], timeout=15000)


def test_your_setting_persists_roams_and_resets(browser):
    ctx, ctx2 = browser.new_context(), None
    try:
        page = login(ctx, "bob")
        page.wait_for_function("() => window.__kbsettings.state() !== null")
        assert page.evaluate("() => document.documentElement.dataset.theme") == VAL
        user_menu(page); page.click('[data-testid="settings-btn"]')
        page.wait_for_selector(f'[data-testid="{ROW}"]')
        assert pill(page, f"{ROW}-src") == "default"
        assert not page.is_visible(f'[data-testid="{ROW}-reset"]')
        assert page.locator(f'[data-testid="{ROW}-input"] option').count() >= 1
        page.evaluate(f"() => window.__kbsettings.set('{KEY}', '{VAL}')")
        wait_pill(page, f"{ROW}-src", "your setting")
        assert page.is_visible(f'[data-testid="{ROW}-reset"]')
        # survives a reload, and a second browser as the same person sees it
        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        page.wait_for_function(f"() => window.__kbsettings.source('{KEY}') === 'user'")
        ctx2 = browser.new_context()
        page2 = login(ctx2, "bob")
        page2.wait_for_function(f"() => window.__kbsettings.source('{KEY}') === 'user'")
        # × clears your layer; the other browser learns on its next fetch
        user_menu(page); page.click('[data-testid="settings-btn"]')
        page.wait_for_selector(f'[data-testid="{ROW}-reset"]')
        page.click(f'[data-testid="{ROW}-reset"]')
        wait_pill(page, f"{ROW}-src", "default")
        page2.evaluate("() => window.__kbsettings.fetch()")
        page2.wait_for_function(f"() => window.__kbsettings.source('{KEY}') === 'default'")
    finally:
        ctx.close()
        if ctx2:
            ctx2.close()


def test_admin_company_tab_sets_the_shared_default(browser):
    ctx = browser.new_context()
    try:
        page = login(ctx, "alice")
        page.wait_for_function("() => !document.querySelector('#admin-btn').hidden")   # isAdmin is known
        user_menu(page); page.click('[data-testid="settings-btn"]')
        page.click('[data-testid="settings-tab-company"]')
        page.wait_for_selector(f'[data-testid="{ROW}-co"]')
        assert pill(page, f"{ROW}-co-src") == "default"
        # left to the default, the control shows that default as a ghost — never "not set"
        sel = page.locator(f'[data-testid="{ROW}-co"]')
        assert "default" in sel.locator("option:checked").inner_text()
        assert "ghost" in (sel.get_attribute("class") or "")
        page.select_option(f'[data-testid="{ROW}-co"]', VAL)
        wait_pill(page, f"{ROW}-co-src", "company default")
        assert api("bob").get("/api/settings").json()["source"][KEY] == "company"
        page.click(f'[data-testid="{ROW}-co-reset"]')
        wait_pill(page, f"{ROW}-co-src", "default")
        assert api("bob").get("/api/settings").json()["source"][KEY] == "default"
    finally:
        ctx.close()


def test_non_admin_has_no_company_tab(browser):
    ctx = browser.new_context()
    try:
        page = login(ctx, "bob")
        user_menu(page); page.click('[data-testid="settings-btn"]')
        page.wait_for_selector(f'[data-testid="{ROW}"]')
        assert page.locator('[data-testid="settings-tab-company"]').count() == 0
    finally:
        ctx.close()
