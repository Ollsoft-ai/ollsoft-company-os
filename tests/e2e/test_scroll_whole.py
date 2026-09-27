"""A note on a touch screen is drawn WHOLE while it is read.

CodeMirror draws the lines on screen plus ~1000px either side and guesses the
height of everything else. On a phone a fling outran that window — blank
patches while scrolling, the whole screen at worst — and the guesses made the
page the wrong length and jump under the thumb. `richview.drawWhole` turns on
the mode CodeMirror prints in (an internal flag: this file is what notices if
an upgrade moves it), and app.js keeps it on while the keyboard is down and
off while typing, where a whole drawing re-lays-out the note on every key.

Also here: nothing listens for touch on the whole page non-passively. Such a
listener makes every scroll of every note wait for the main thread.
"""
import time

import httpx
import pytest

from conftest import BASE, CREDS, login
from kbenv import U, doc as kbdoc

PHONE = dict(viewport={"width": 390, "height": 844}, is_mobile=True,
             has_touch=True, device_scale_factor=3)


def _note(sections):
    parts = []
    for i in range(sections):
        parts.append(f"## Section {i}\n\n"
                     "A paragraph long enough to wrap two or three times on a phone, "
                     "which is what makes the height of a line something to measure "
                     "rather than something to know.\n\n- one\n- two\n")
        if i % 20 == 5:
            parts.append("| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n")
        if i % 20 == 10:
            parts.append("```\ncode line\n# not a heading\n```\n")
    return "\n".join(parts)


LONG = _note(120)            # ~30 KB: many screens, far past CodeMirror's window
HUGE = _note(360)            # past the 64K-character cap


def api():
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="module")
def notes():
    assert len(HUGE) > 64 * 1024 > len(LONG)
    c = api()
    stamp = int(time.time())
    made = {}
    for name, body in (("long", LONG), ("huge", HUGE)):
        rel = kbdoc(f"scrollwhole_{name}_{stamp}.md")
        assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
        assert c.post("/api/artifact/write", json={"path": rel, "content": body}).status_code == 200
        made[name] = rel
    yield made
    for rel in made.values():
        c.post("/api/fs/delete", json={"path": rel})


def open_note(browser, rel, **ctx_kw):
    ctx = browser.new_context(**ctx_kw)
    page = login(ctx, "alice")
    page.goto(f"{BASE}/{rel}")
    page.wait_for_function("(p) => window.__kbpath === p && window.__kbsynced && "
                           "window.__kbview.state.doc.length > 1000", arg=rel, timeout=20000)
    return ctx, page


WHOLE = ("() => { const v = window.__kbview; "
         "return v.viewport.from === 0 && v.viewport.to === v.state.doc.length; }")
# a line that reads as a heading, drawn without the heading's class and not in a code block
RAW_HEADINGS = ("() => [...__kbview.contentDOM.querySelectorAll('.cm-line')].filter(l => "
                "/^#{1,6} /.test(l.textContent) && !l.classList.contains('cm-h') && "
                "!l.classList.contains('cm-codeblock')).length")
CARET_Y = ("() => { const v = __kbview; const c = v.coordsAtPos(v.state.selection.main.head); "
           "return c ? Math.round(c.top) : null; }")


def test_a_phone_draws_the_whole_note_rich_to_the_end(browser, notes):
    ctx, page = open_note(browser, notes["long"], **PHONE)
    try:
        page.wait_for_function(WHOLE, timeout=10000)
        # every heading is a heading, down to the last one: the parse is forced
        # to the end, and the rich layer follows the parse, not just the viewport
        page.wait_for_function(f"() => ({RAW_HEADINGS})() === 0", timeout=10000)
        assert page.evaluate("() => __kbview.contentDOM.querySelectorAll('.cm-table-wrap').length") == 6
        # nothing is guessed, so the page is its real length from the start and
        # stays it while you travel through it
        h0 = page.evaluate("() => __kbview.scrollDOM.scrollHeight")
        for frac in (0.25, 0.5, 0.75, 1.0):
            page.evaluate("(f) => { const s = __kbview.scrollDOM; s.scrollTop = f * s.scrollHeight; }", frac)
            page.wait_for_timeout(150)
            assert page.evaluate("() => __kbview.scrollDOM.scrollHeight") == h0
        # the end is drawn: CodeMirror has no coordinates for a position it has not
        assert page.evaluate("() => __kbview.coordsAtPos(__kbview.state.doc.length) !== null")
    finally:
        ctx.close()


def test_the_keyboard_draws_a_window_and_closing_it_the_whole_note_again(browser, notes):
    ctx, page = open_note(browser, notes["long"], **PHONE)
    try:
        page.wait_for_function(WHOLE, timeout=10000)
        page.evaluate("() => { __kbview.scrollDOM.scrollTop = 6000; }")
        page.wait_for_timeout(200)
        box = page.evaluate("() => { const r = __kbview.scrollDOM.getBoundingClientRect(); "
                            "return [r.x + 80, r.y + 160]; }")
        page.touchscreen.tap(*box)
        page.wait_for_function("() => __kbview.hasFocus", timeout=4000)
        y0 = page.evaluate(CARET_Y)
        # the keyboard: the visual viewport loses its height while the note has focus
        page.set_viewport_size({"width": 390, "height": 500})
        page.wait_for_function("() => document.body.classList.contains('kb-up')", timeout=4000)
        page.wait_for_function("() => __kbview.viewport.to < __kbview.state.doc.length", timeout=4000)
        assert abs(page.evaluate(CARET_Y) - y0) <= 2     # nothing moved under the caret
        page.keyboard.type("typed ")
        assert "typed " in page.evaluate("() => __kbview.state.doc.lineAt(__kbview.state.selection.main.head).text")
        # Android's back button: the keyboard goes, the focus stays
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("() => !document.body.classList.contains('kb-up')", timeout=4000)
        page.wait_for_function(WHOLE, timeout=10000)
        assert abs(page.evaluate(CARET_Y) - y0) <= 2
    finally:
        ctx.close()


def test_a_note_past_the_cap_stays_windowed(browser, notes):
    ctx, page = open_note(browser, notes["huge"], **PHONE)
    try:
        page.wait_for_timeout(1500)
        assert not page.evaluate(WHOLE)
    finally:
        ctx.close()


def test_a_desktop_keeps_codemirrors_window(browser, notes):
    ctx, page = open_note(browser, notes["long"], viewport={"width": 1400, "height": 900})
    try:
        page.wait_for_timeout(1500)
        assert not page.evaluate(WHOLE)
    finally:
        ctx.close()


def test_no_page_wide_listener_holds_scrolling_back(browser, notes):
    ctx, page = open_note(browser, notes["long"], **PHONE)
    try:
        cdp = ctx.new_cdp_session(page)
        # which script added a listener: only the app's own count — Playwright
        # injects a (blocking) touch listener of its own, with no URL
        urls = {}
        cdp.on("Debugger.scriptParsed", lambda e: urls.__setitem__(e["scriptId"], e["url"]))
        cdp.send("Debugger.enable")
        page.wait_for_timeout(300)
        blocking = []
        for expr in ("document", "window"):
            obj = cdp.send("Runtime.evaluate", {"expression": expr})["result"]["objectId"]
            for ls in cdp.send("DOMDebugger.getEventListeners", {"objectId": obj})["listeners"]:
                url = urls.get(ls.get("scriptId"), "")
                if (ls["type"] in ("touchstart", "touchmove", "wheel", "mousewheel")
                        and not ls["passive"] and "/static/" in url):
                    blocking.append((expr, ls["type"], url.split("/static/")[1], ls.get("columnNumber")))
        assert urls, "no scripts seen — the listener check would pass vacuously"
        assert blocking == []
    finally:
        ctx.close()
