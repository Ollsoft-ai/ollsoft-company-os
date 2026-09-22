import pytest
from playwright.sync_api import sync_playwright

# kbenv is resolved LAZILY, never at import of this file. This is an "initial"
# conftest whenever pytest is given `tests/e2e` as its argument, and pytest loads
# those during pre-parse — before pytest_configure, which is where the root
# conftest seeds the fixtures. Importing kbenv here would read the credentials
# file a moment before it is written, and every e2e run would die in collection.
# tests/cli has no conftest of its own, which is why this only showed up here.
#
# Test modules still do `from conftest import BASE, CREDS`, and that keeps
# working: PEP 562 module __getattr__ fires when THEY are imported, which is
# during collection — after the fixtures exist.
def __getattr__(name):
    if name in ("BASE", "CREDS", "U", "L", "NS", "AREA", "REPO",
                "doc", "proj", "home", "full", "people"):
        import kbenv
        return getattr(kbenv, name)
    raise AttributeError(f"module 'conftest' has no attribute {name!r}")


@pytest.fixture(scope="session")
def pw():
    """The one Playwright instance for the whole run. A second
    `sync_playwright()` in the same thread throws ("it looks like you are
    using Playwright Sync API inside the asyncio loop"), so a test that wants
    Firefox or WebKit borrows this rather than opening its own."""
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(pw):
    b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    yield b
    b.close()


def login(context, user):
    from kbenv import AREA, BASE, CREDS, U
    page = context.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', U(user))
    page.fill('input[name="password"]', CREDS[user])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    # This run's documents live one level deeper than company/, so open that
    # folder before handing the page over. company/ itself is expanded by
    # default, which is the behaviour these tests were written against; without
    # this every test that reaches a fixture through the tree would have to
    # expand it by hand.
    try:
        expand_folder(page, AREA)
    except Exception:
        pass          # a test that never touches the tree should not fail here
    return page


def user_menu(page):
    """Open the user menu — the person at the bottom left of the sidebar, where
    the actions live: settings, admin, cron, terminal, dictation history,
    shortcuts, sign out. On a phone the sidebar is a drawer, so open that
    first. Choosing an action closes the menu again, so call this before each
    click on one of those buttons."""
    if page.locator('[data-testid="user-menu"]').is_visible():
        return
    if not page.locator('[data-testid="user-btn"]').is_visible():
        page.click('[data-testid="nav-btn"]')
        page.wait_for_selector('[data-testid="user-btn"]', state="visible")
    page.click('[data-testid="user-btn"]')
    page.wait_for_selector('[data-testid="user-menu"]', state="visible")


def dlg_fill(page, text):
    """Fill the app's own prompt dialog (replaced native prompt()) and confirm."""
    page.fill('[data-testid="dlg-input"]', text)
    page.click('[data-testid="dlg-ok"]')


def dlg_ok(page):
    """Confirm the app's own confirm/alert dialog (replaced native confirm())."""
    page.click('[data-testid="dlg-ok"]')


def expand_folder(page, path):
    """Expand a collapsed tree folder so its children become clickable. Folders
    whose name starts with '.' or '_' auto-collapse by default, so a test that
    reaches a child inside one must expand it first. No-op if already open."""
    caret = page.locator(f'.tree-item[data-path="{path}"] .caret').first
    caret.wait_for(timeout=8000)
    if "open" not in (caret.get_attribute("class") or ""):
        page.locator(f'.tree-item[data-path="{path}"] .tlabel').first.click()
        page.wait_for_timeout(150)


def wait_path(page, path, timeout=None, kind=None):
    """Wait until THIS path is the open tab.

    The path is passed as an argument rather than baked into the JS source: a
    literal 'company/overview.md' in here is a fixture path from before
    namespacing, and it can never become true again on a namespaced run.
    """
    page.wait_for_function(
        "([p, k]) => window.__kbpath === p && (!k || window.__kbkind === k)",
        arg=[path, kind], timeout=timeout)


def open_doc(page, path):
    page.click(f'.tree-item[data-path="{path}"]')
    wait_path(page, path)
    # wait until the CRDT text is populated from the server seed
    page.wait_for_function(
        "() => window.__kbview && window.__kbview.state.doc.length > 0", timeout=8000)


def doc_text(page):
    return page.evaluate("() => window.__kbview.state.doc.toString()")


def insert_at(page, pos, text):
    page.evaluate(
        "([p,t]) => window.__kbview.dispatch({changes:{from:p,insert:t}})", [pos, text])


def insert_at_end(page, text):
    page.evaluate(
        "(t) => { const v=window.__kbview; v.dispatch({changes:{from:v.state.doc.length,insert:t}}); }", text)
