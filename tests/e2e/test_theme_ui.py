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
    api("alice").post("/admin/settings", json={"unset": [KEY, "ui.theme.custom"]})
    api("bob").post("/api/settings", json={"unset": [KEY, "ui.theme.custom"]})
    yield
    api("bob").post("/api/settings", json={"unset": [KEY, "ui.theme.custom"]})


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
        assert page.evaluate("() => getComputedStyle(document.body).fontSize") == "16px"       # Notion's 16px
        assert token(page, "--content-max") == "708px"                                          # Notion's column
        assert page.evaluate("() => getComputedStyle(document.body).fontFamily").startswith("ui-sans-serif")
        assert token(page, "--panel").lower() == "#f9f8f7"                                      # the sidebar Notion's shell paints
        assert page.evaluate("() => getComputedStyle(document.querySelector('.pane-title')).textTransform") == "none"
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
        assert page.evaluate("() => getComputedStyle(document.body).color") == "rgb(211, 211, 211)"   # white at 81%
        # and back to the default: deep blue
        page.evaluate(f"() => window.__kbsettings.unset('{KEY}')")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'deep-blue'", timeout=10000)
        page.wait_for_function("() => getComputedStyle(document.body).backgroundColor === 'rgb(13, 22, 38)'", timeout=5000)
        assert page.evaluate("() => window.__kbterms[0].term.options.theme.background").lower() == "#071019"
        assert page.evaluate("() => getComputedStyle(document.body).fontSize") == "15px"       # deep blue keeps its numbers
        assert '"IBM Plex Sans"' in page.evaluate("() => getComputedStyle(document.body).fontFamily")
        # a second load starts light again only if it is set — it is not now
        page.reload(); page.wait_for_selector('[data-testid="tree"] .tree-item')
        assert page.evaluate("() => document.documentElement.dataset.theme") == "deep-blue"
    finally:
        ctx.close()


def test_overrides_paint_live_and_are_editable_in_the_dialog(browser):
    K = "ui.theme.custom"
    b = api("bob")
    ctx = browser.new_context()
    try:
        page = login(ctx, "bob")
        page.wait_for_function("() => window.__kbsettings.state() !== null")
        # from the API: the background token is overridden, the page follows
        assert b.post("/api/settings", json={"set": {K: {"bg": "#123456", "font-size": "18px"}}}).status_code == 200
        page.wait_for_function("() => getComputedStyle(document.body).backgroundColor === 'rgb(18, 52, 86)'", timeout=10000)
        assert page.evaluate("() => getComputedStyle(document.body).fontSize") == "18px"        # the base size follows
        # from the dialog: pick the accent, the token lands on <html>
        user_menu(page); page.click('[data-testid="settings-btn"]')
        page.click('[data-testid="set-ui-theme-custom-input"]')
        page.fill('[data-testid="set-ui-theme-custom-accent"]', "#00ff00")
        page.wait_for_function("() => (window.__kbsettings.get('ui.theme.custom') || {}).accent === '#00ff00'", timeout=10000)
        assert page.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()") == "#00ff00"
        assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(18, 52, 86)"   # kept
        # reset the whole thing: the theme as shipped
        page.click('[data-testid="set-ui-theme-custom-reset"]')
        page.wait_for_function("() => getComputedStyle(document.body).backgroundColor === 'rgb(13, 22, 38)'", timeout=10000)
    finally:
        b.post("/api/settings", json={"unset": [K]})
        ctx.close()


def _column(page):
    return page.evaluate("""() => { const l = document.querySelector('.cm-rich .cm-line'); const cs = getComputedStyle(l);
      const c = document.querySelector('.cm-content').getBoundingClientRect().width;
      return [Math.round(parseFloat(cs.paddingLeft)), Math.round(c - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight))]; }""")


def test_the_document_reads_like_a_page_at_every_width(browser):
    """Notion's framing: on a wide window the text sits in a 708px column with
    paper either side; as the pane narrows the gutter follows the pane instead
    of squeezing the text; a phone gets the floor. Deep blue keeps its inset."""
    from conftest import open_doc
    from kbenv import doc as kbdoc
    b = api("bob")
    assert b.post("/api/settings", json={"set": {KEY: "light"}}).status_code == 200
    try:
        for width, want in ((1900, "wide"), (1300, "wide"), (900, "narrow")):
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            page = login(ctx, "bob")
            page.wait_for_function("() => document.documentElement.dataset.theme === 'light'", timeout=15000)
            open_doc(page, kbdoc("overview.md"))
            page.wait_for_selector(".cm-rich .cm-line")
            gutter, text = _column(page)
            if want == "wide":
                assert abs(text - 708) <= 2, (width, gutter, text)             # the column
                assert gutter >= 96, (width, gutter, text)                     # paper either side
            else:
                assert 24 <= gutter < 96, (width, gutter, text)                # follows the pane…
                assert text > 500, (width, gutter, text)                       # …instead of squeezing the text
            ctx.close()
        b.post("/api/settings", json={"unset": [KEY]})
        ctx = browser.new_context(viewport={"width": 1900, "height": 900})
        page = login(ctx, "bob")
        page.wait_for_function("() => document.documentElement.dataset.theme === 'deep-blue'", timeout=15000)
        open_doc(page, kbdoc("overview.md"))
        page.wait_for_selector(".cm-rich .cm-line")
        gutter, text = _column(page)
        assert gutter == 33 and text > 1400, (gutter, text)                   # deep blue: its 2.2rem inset, the whole width
        ctx.close()
    finally:
        b.post("/api/settings", json={"unset": [KEY]})


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
