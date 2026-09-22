"""The app in Firefox and Safari, not only in Chrome.

Everything else in this directory drives Chromium, including the "phone"
tests — which emulate a phone's viewport and touch, but not its ENGINE. Every
iPhone and iPad on the internet runs WebKit, so a WebKit-only failure (a
missing `:has()`, a `color-mix()` that does not resolve, a module that will
not parse) would reach krystof's phone and nothing here would have said a
word. This is a smoke pass: does it load, render, type and switch mode, and
does the console stay quiet.

A browser that is not installed skips rather than fails — CI has Chromium
only. Install the others with `python -m playwright install firefox webkit`
(and `install-deps webkit` as root, for GTK).
"""
import time

import httpx
import pytest

from conftest import BASE, CREDS
from kbenv import U, doc as kbdoc

BODY = """# Cross-browser

A **bold** word and a [link](other.md).

- [ ] a task

| What | Who |
| --- | --- |
| **ship** | you |
"""

ENGINES = ["chromium", "firefox", "webkit"]


@pytest.fixture                       # per test: each engine TYPES into it
def smoke_doc():
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    rel = kbdoc(f"xbrowser_{int(time.time() * 1000)}.md")
    assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
    assert c.post("/api/artifact/write",
                  json={"path": rel, "content": BODY}).status_code == 200
    yield rel
    c.post("/api/fs/delete", json={"path": rel, "permanent": True})


@pytest.mark.parametrize("engine", ENGINES)
def test_the_app_works_in(engine, smoke_doc, pw):
    try:
        browser = getattr(pw, engine).launch(
            headless=True, args=["--no-sandbox"] if engine == "chromium" else [])
    except Exception as e:                          # noqa: BLE001 — not installed here
        pytest.skip(f"{engine} is not installed: {str(e)[:80]}")
    ctx = browser.new_context()
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # Console noise that is the ENGINE talking, not the app failing: a
    # websocket cut when the page goes away, a viewport key Safari does
    # not know, a font preload it decided it did not need.
    noise = ("favicon", "connection to", "NetworkError", "interactive-widget",
             "preloaded using link preload", "Failed to load resource")
    page.on("console", lambda m: errors.append(f"console: {m.text}")
            if m.type == "error" and not any(n in m.text for n in noise) else None)
    try:
        page.goto(BASE + "/login")
        page.fill('input[name="username"]', U("alice"))
        page.fill('input[name="password"]', CREDS["alice"])
        page.click('button[type="submit"]')
        page.wait_for_selector('[data-testid="tree"] .tree-item', timeout=20000)

        page.goto(f"{BASE}/{smoke_doc}")
        page.wait_for_function(f"() => window.__kbpath === {smoke_doc!r}", timeout=20000)
        page.wait_for_selector(".cm-content", timeout=15000)

        # the rendered view, the parts that need modern CSS and DOM.
        # Wait rather than assert: WebKit paints the decorations a beat
        # after the content, and "not yet" is not "never".
        page.wait_for_selector("table.cm-table", timeout=15000)
        page.wait_for_selector(".cm-h1", timeout=10000)
        assert page.locator(".cm-task input[type=checkbox]").count() == 1
        assert page.locator('[data-cell="1,0"] .cm-cell-md strong').count() == 1, \
            "a table cell did not render its markdown"

        # typing reaches the document — through the real keyboard, at a
        # position we put the cursor on ourselves
        page.locator(".cm-content").click()
        page.evaluate("() => { window.__kbview.focus();"
                      "        window.__kbview.dispatch({selection:{anchor:0}}); }")
        page.keyboard.type("zz")
        page.wait_for_function(
            "() => window.__kbview.state.doc.toString().startsWith('zz')", timeout=8000)

        # and the source/rich switch, which is pure CSS + decorations
        page.click("#mode-source")
        page.wait_for_selector(".cm-lineNumbers", timeout=8000)
        page.click("#mode-rich")
        page.wait_for_selector("table.cm-table", timeout=8000)

        assert not errors, f"{engine}: {errors[:4]}"
    finally:
        ctx.close()
        browser.close()          # one browser per engine per test; pw itself is shared
