"""ui.theme reaches everything: <html data-theme> before first paint, the
page's colours, the editor's highlight colours and a running terminal — and
switching back restores deep blue without a reload."""
import httpx
import pytest
from conftest import BASE, CREDS, login, open_doc, user_menu
from kbenv import U, doc

KEY = "ui.theme"


def api(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


@pytest.fixture(autouse=True)
def _reset():
    api("alice").post("/admin/settings", json={"unset": [KEY]})
    api("bob").post("/api/settings", json={"unset": [KEY]})
    yield
    api("bob").post("/api/settings", json={"unset": [KEY]})


def bg(page):
    return page.evaluate("() => getComputedStyle(document.body).backgroundColor")


def token(page, name):
    return page.evaluate("(n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim()", name)


def test_light_theme_lands_before_paint_and_switches_live(browser):
    b = api("bob")
    assert b.post("/api/settings", json={"set": {KEY: "light"}}).status_code == 200
    ctx = browser.new_context()
    try:
        page = login(ctx, "bob")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'light'", timeout=15000)
        light_bg = bg(page)
        assert light_bg == "rgb(255, 255, 255)", light_bg          # Notion's paper
        assert token(page, "--term-bg").lower() == "#f7f6f3"
        # the editor's highlight style follows the tokens
        open_doc(page, doc("overview.md"))
        page.wait_for_selector(".cm-editor")
        assert page.evaluate("() => getComputedStyle(document.querySelector('.cm-editor')).backgroundColor") == "rgb(255, 255, 255)"
        # a terminal opened under the light theme wears it
        user_menu(page); page.click('[data-testid="toggle-term"]')
        page.wait_for_function("() => window.__kbterms && window.__kbterms.length > 0", timeout=15000)
        assert page.evaluate("() => window.__kbterms[0].term.options.theme.background").lower() == "#f7f6f3"
        # switch to dark from the store: everything retints live, no reload
        page.evaluate(f"() => window.__kbsettings.set('{KEY}', 'dark')")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'dark'", timeout=10000)
        page.wait_for_function("() => getComputedStyle(document.body).backgroundColor === 'rgb(25, 25, 25)'", timeout=5000)
        page.wait_for_function("() => window.__kbterms[0].term.options.theme.background.toLowerCase() === '#191919'", timeout=5000)
        # and back to the default: deep blue
        page.evaluate(f"() => window.__kbsettings.unset('{KEY}')")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'deep-blue'", timeout=10000)
        page.wait_for_function("() => getComputedStyle(document.body).backgroundColor === 'rgb(13, 22, 38)'", timeout=5000)
        assert page.evaluate("() => window.__kbterms[0].term.options.theme.background").lower() == "#071019"
        # a second load starts light again only if it is set — it is not now
        page.reload(); page.wait_for_selector('[data-testid="tree"] .tree-item')
        assert page.evaluate("() => document.documentElement.dataset.theme") == "deep-blue"
    finally:
        ctx.close()


def test_company_default_theme_applies_to_everyone(browser):
    assert api("alice").post("/admin/settings", json={"set": {KEY: "dark"}}).status_code == 200
    ctx = browser.new_context()
    try:
        page = login(ctx, "bob")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'dark'", timeout=15000)
        assert bg(page) == "rgb(25, 25, 25)"
        user_menu(page); page.click('[data-testid="settings-btn"]')
        page.wait_for_selector('[data-testid="set-ui-theme-src"]')
        assert page.inner_text('[data-testid="set-ui-theme-src"]').strip().lower() == "company default"
        # the select shows friendly labels, values stay the keys
        labels = page.eval_on_selector_all('[data-testid="set-ui-theme-input"] option', "os => os.map(o => [o.value, o.textContent])")
        assert ["light", "Light"] in labels and ["deep-blue", "Deep blue"] in labels
    finally:
        api("alice").post("/admin/settings", json={"unset": [KEY]})
        ctx.close()
