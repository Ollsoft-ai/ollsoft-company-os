import json
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

# Serve the frontend straight out of this checkout instead of the deployed copy,
# so you can test an edit without redeploying. Enable with KB_DEV_BUNDLE=1.
# Derived from the repo layout — never a hardcoded home directory.
DEV = os.environ.get(
    "KB_DEV_BUNDLE_DIR",
    str(Path(__file__).resolve().parents[2] / "frontend" / "static"),
)
_MIME = {".js": "application/javascript", ".css": "text/css", ".html": "text/html",
         ".svg": "image/svg+xml", ".woff2": "font/woff2"}


def _use_dev_bundle(page):
    def serve(route):
        p = re.sub(r"\?.*$", "", route.request.url.split(BASE, 1)[-1])
        f = DEV + "/app.html" if p == "/" else DEV + p[len("/static"):]
        try:
            body = open(f, "rb").read()
        except OSError:
            route.continue_(); return
        route.fulfill(status=200,
                      content_type=_MIME.get("." + f.rsplit(".", 1)[-1], "application/octet-stream"),
                      body=body)
    page.route(BASE + "/", serve)
    page.route(re.compile(re.escape(BASE) + r"/static/.*"), serve)

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks"])
        yield b
        b.close()


def login(context, user):
    page = context.new_page()
    if os.environ.get("KB_DEV_BUNDLE"):
        _use_dev_bundle(page)
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', CREDS[user])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    return page


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


def open_doc(page, path):
    page.click(f'.tree-item[data-path="{path}"]')
    page.wait_for_function("() => window.__kbview && window.__kbpath")
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
