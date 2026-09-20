"""Loading affordances: the app must never look frozen, and must never claim a
result it does not have yet.

The reported symptom: opening a document showed an empty pane, and searching
showed "No matches", with no way to tell "nothing here" from "still working".
Both are answered now — a spinner over the tab until the CRDT session syncs,
and a "Searching documents…" row until the content query lands.

Latency is applied with CDP Network.emulateNetworkConditions. NOT by sleeping
inside a page.route handler: a blocking sleep there stalls Playwright's own
event loop, so the driver cannot observe the page during the delay it created
(an 8 s delay measured as instantaneous while that mistake was in place).
"""
import time

import httpx
import pytest
from conftest import BASE, CREDS, login, open_doc, user_menu
from kbenv import U, doc

LAT = 1200          # ms added to every request while a slow phase is under test
USER = "alice"      # the same identity creates the fixture doc and views it


def login(ctx, user):
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', U(user))
    page.fill('input[name="password"]', CREDS[user])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    return page


@pytest.fixture
def slow(browser):
    """A logged-in page plus a switch for per-request latency."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, USER)
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")

    def set_latency(ms):
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": ms,
            "downloadThroughput": -1, "uploadThroughput": -1})

    yield page, set_latency
    ctx.close()


@pytest.fixture
def scratch_doc():
    """Our own document — never assert against whatever happens to live in
    company/ on this box.

    Created through the PRODUCT's API as USER, not with Path.write_text: the
    suite runs as krystof here and as alice on CI, and this box's umask is
    0077, so a plain write produces a 0600 file owned by the runner that the
    logged-in user cannot read — it then never appears in their tree and the
    test times out looking for it. Going through the API makes ownership and
    mode correct by construction, whoever runs the suite."""
    name = f"kbtest_ux_{int(time.time() * 1000)}"
    rel = doc(f"{name}.md")
    c = httpx.Client(base_url=BASE, timeout=30)
    assert c.post("/login", data={"username": U(USER),
                                  "password": CREDS[USER]}).status_code == 200
    assert c.post("/fs/newfile", json={"path": rel}).status_code == 200
    assert c.post("/api/artifact/write",
                  json={"path": rel,
                        "content": "# probe\n\n" + "Body line.\n" * 30}).status_code == 200
    try:
        yield name
    finally:
        c.post("/api/fs/delete", json={"path": rel})


def open_palette(page, query):
    page.keyboard.press("Control+p")
    page.wait_for_selector('[data-testid="palette"]')
    page.fill('[data-testid="palette-input"]', query)


# --- search ----------------------------------------------------------------

def test_search_shows_pending_instead_of_claiming_no_matches(slow):
    """THE reported bug: a query whose only hits are inside documents showed
    "No matches" for as long as the round trip took."""
    page, set_latency = slow
    set_latency(LAT)
    open_palette(page, "zebrafish")     # lives only in document CONTENT
    snap = page.wait_for_function("""() => {
      const e = document.querySelector('[data-testid=palette-searching]');
      if (!e || parseFloat(getComputedStyle(e).opacity) < 0.9) return null;
      return {txt: e.innerText, role: e.getAttribute('role'),
              spinner: !!e.querySelector('.upspin'),
              busy: document.querySelector('[data-testid=palette-kind]')
                      .classList.contains('busy'),
              list: document.querySelector('[data-testid=palette-list]').innerText};
    }""", timeout=15000).json_value()
    assert "Searching" in snap["txt"], snap["txt"]
    assert "No matches" not in snap["list"], "must not claim an answer it lacks"
    assert snap["spinner"], "a pending state needs a visible spinner"
    assert snap["role"] == "status", "screen readers must hear it too"
    assert snap["busy"], "the FIND label should read as busy"


def test_search_pending_clears_and_settles(slow):
    """It must resolve both ways: to results, and to a real 'No matches'."""
    page, set_latency = slow
    set_latency(LAT)
    open_palette(page, "onboarding")
    page.wait_for_selector('[data-testid="palette-searching"]', timeout=15000)
    page.wait_for_selector('[data-testid="palette-searching"]', state="detached",
                           timeout=30000)
    assert page.locator('[data-testid="palette-item"]').count() > 0
    assert not page.eval_on_selector('[data-testid="palette-kind"]',
                                     "e => e.classList.contains('busy')")

    page.fill('[data-testid="palette-input"]', "zzqqxxnomatchzz")
    page.wait_for_function("""() => {
      const l = document.querySelector('[data-testid=palette-list]');
      return l && l.innerText.includes('No matches');
    }""", timeout=30000)


def test_fast_search_does_not_flash_a_spinner(slow):
    """A spinner that appears and vanishes within a frame reads as jank. The
    indicator is held invisible for ~180ms before it fades in."""
    page, set_latency = slow
    set_latency(0)
    open_palette(page, "onboarding")
    time.sleep(0.12)
    op = page.eval_on_selector_all('[data-testid="palette-searching"]',
                                   "els => els.map(e => getComputedStyle(e).opacity)")
    assert all(float(o) < 0.5 for o in op), f"spinner flashed: {op}"


# --- opening documents -----------------------------------------------------

def test_doc_open_shows_a_named_loading_state(slow, scratch_doc):
    page, set_latency = slow
    # The palette's filename matches come from the client's copy of the tree,
    # which refreshes on a poll — so wait for our just-created file to actually
    # be known before searching for it. (Do this at full speed; latency goes on
    # only for the interaction under test.)
    page.wait_for_selector(f'.tree-item[data-path$="{scratch_doc}.md"]', timeout=30000)
    set_latency(LAT)
    open_palette(page, scratch_doc)
    sel = f'[data-testid="palette-item"][data-path$="{scratch_doc}.md"]'
    page.wait_for_selector(sel, timeout=25000)
    page.click(sel)
    snap = page.wait_for_function("""() => {
      const e = document.querySelector('[data-testid=tab-loading]');
      if (!e || parseFloat(getComputedStyle(e).opacity) < 0.9) return null;
      const r = e.getBoundingClientRect();
      return {txt: e.innerText, w: r.width, h: r.height,
              spinner: !!e.querySelector('.upspin'), role: e.getAttribute('role')};
    }""", timeout=20000).json_value()
    assert "Opening" in snap["txt"] and scratch_doc in snap["txt"], snap["txt"]
    assert snap["w"] > 200 and snap["h"] > 200, "must cover the pane, not hide in a corner"
    assert snap["spinner"] and snap["role"] == "status"


def test_doc_open_loading_clears_and_content_arrives(slow, scratch_doc):
    page, set_latency = slow
    # The palette's filename matches come from the client's copy of the tree,
    # which refreshes on a poll — so wait for our just-created file to actually
    # be known before searching for it. (Do this at full speed; latency goes on
    # only for the interaction under test.)
    page.wait_for_selector(f'.tree-item[data-path$="{scratch_doc}.md"]', timeout=30000)
    set_latency(LAT)
    open_palette(page, scratch_doc)
    sel = f'[data-testid="palette-item"][data-path$="{scratch_doc}.md"]'
    page.wait_for_selector(sel, timeout=25000)
    page.click(sel)
    page.wait_for_selector('[data-testid="tab-loading"]', timeout=20000)
    page.wait_for_selector('[data-testid="tab-loading"]', state="detached", timeout=45000)
    # the overlay must not clear before there is something behind it
    assert page.eval_on_selector_all(
        ".cm-content", "els => els.some(e => e.innerText.length > 20)")


def test_reduced_motion_still_communicates_but_does_not_spin(browser):
    """prefers-reduced-motion must not mean 'no feedback' — only 'no motion'."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900},
                              reduced_motion="reduce")
    try:
        page = login(ctx, USER)
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": LAT,
            "downloadThroughput": -1, "uploadThroughput": -1})
        open_palette(page, "onboarding")
        page.wait_for_function("""() => {
          const e = document.querySelector('[data-testid=palette-searching]');
          return e && parseFloat(getComputedStyle(e).opacity) > 0.9;
        }""", timeout=15000)
        assert page.eval_on_selector(
            '.palette-searching .upspin',
            "e => getComputedStyle(e).animationName") == "none"
    finally:
        ctx.close()


# --- boot -------------------------------------------------------------------

def test_boot_restores_tabs_and_terminal_before_the_tree_arrives(browser, scratch_doc):
    """The tree is the slow boot request (a permission-checked walk of the
    repo) and it used to be awaited in front of restoreSession(), which reads
    nothing but localStorage. Hold the tree indefinitely and the tabs and the
    terminal must still come back — and when the tree finally lands, nothing
    already on screen may move."""
    path = doc(f"{scratch_doc}.md")          # the fixture yields the bare name
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, USER)
    try:
        page.wait_for_selector(f'.tree-item[data-path="{path}"]', timeout=30000)
        open_doc(page, path)
        user_menu(page); page.click('[data-testid="toggle-term"]')
        page.wait_for_selector("#terminal .xterm-rows", timeout=10000)
        page.wait_for_timeout(500)              # let saveSession() record both

        held = []
        def hold(route):
            # the FIRST tree request is held until the test lets go; later
            # polls (there are none until boot finishes) pass through
            if held:
                route.continue_()
            else:
                held.append(route)
        page.route("**/api/tree*", hold)
        page.reload()
        page.wait_for_selector(f'.tab[data-path="{path}"]', timeout=4000)
        page.wait_for_selector("#terminal-panel:not([hidden])", timeout=4000)
        assert held, "the tree request never left — this proves nothing"
        assert page.locator("#tree .tree-item").count() == 0, \
            "the tree is on screen, so the tabs were not measured ahead of it"
        before = page.locator("#editor").bounding_box()

        held[0].continue_()
        page.wait_for_selector(f'.tree-item[data-path="{path}"]', timeout=15000)
        after = page.locator("#editor").bounding_box()
        assert before == after, f"the editor moved when the tree landed: {before} -> {after}"
        # and the tree caught up with the restored tab: highlighted and revealed
        row = page.locator(f'.tree-item[data-path="{path}"]')
        assert "active" in (row.get_attribute("class") or "")
        assert row.is_visible()
    finally:
        ctx.close()


def test_the_tree_is_pushed_not_polled(browser):
    """Nothing polls: with the event stream up, an idle tab asks for the tree
    zero times. And when a catch-up IS asked for (here: the `online` wake), it
    carries the ETag back and, with nothing changed, is a 304 — no body."""
    ctx = browser.new_context()
    page = login(ctx, USER)
    try:
        page.wait_for_function("() => window.__kbevents && window.__kbevents.mode === 'sse'",
                               timeout=15000)
        n = page.evaluate("() => window.__kbtreefetches || 0")
        page.wait_for_timeout(9000)                       # two old poll periods
        assert page.evaluate("() => window.__kbtreefetches || 0") == n, "the tree was polled"
        with page.expect_response(lambda r: "/api/tree" in r.url and r.status == 304,
                                  timeout=8000) as got:
            page.evaluate("() => window.dispatchEvent(new Event('online'))")
        req = got.value.request
        assert req.headers.get("if-none-match"), "the catch-up did not send the ETag back"
        assert got.value.headers.get("etag") == req.headers.get("if-none-match")
    finally:
        ctx.close()


def _hold(page, pattern):
    """Route `pattern` so the FIRST matching request is held until the test
    lets go; later ones pass. Returns the list the held route lands in."""
    held = []
    def handler(route):
        if held:
            route.continue_()
        else:
            held.append(route)
    page.route(pattern, handler)
    return held


def test_tabs_restore_before_whoami_answers_and_you_are_still_you(browser, scratch_doc):
    """Phase 1 of the restore needs nothing but localStorage — not even your
    name. Hold whoami: the tab must come back anyway. Then release it: the
    collaboration session must learn who you are (a tab restored early used
    to announce itself as "user" for as long as it stayed open)."""
    path = doc(f"{scratch_doc}.md")
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, USER)
    try:
        page.wait_for_selector(f'.tree-item[data-path="{path}"]', timeout=30000)
        open_doc(page, path)
        page.wait_for_timeout(400)
        held = _hold(page, "**/api/whoami")
        page.reload()
        page.wait_for_selector(f'.tab[data-path="{path}"]', timeout=3000)
        assert held, "whoami was never asked — this proves nothing"
        assert page.evaluate("() => window.__kbuser") is None, \
            "whoami answered before the tab appeared — the ordering was not exercised"
        held[0].continue_()
        # the self avatar carries the awareness name ("<name> (you)"): it must
        # become yours, and "user" — the name announced before whoami — must
        # not survive anywhere in the row
        page.wait_for_function(
            "(u) => { const a = document.querySelector('#presence .presence-avatar.self');"
            "         return a && a.title.startsWith(u + ' (you)'); }", arg=U(USER), timeout=10000)
        titles = page.evaluate(
            "() => [...document.querySelectorAll('#presence .presence-avatar')].map(a => a.title)")
        assert not any(t.startswith("user") for t in titles), titles
    finally:
        ctx.close()


def test_terminal_restores_without_waiting_for_the_documents(browser, scratch_doc):
    """The terminals used to queue behind every document's websocket. Hold the
    documents' first round trip and the terminal must still come back."""
    path = doc(f"{scratch_doc}.md")
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, USER)
    try:
        page.wait_for_selector(f'.tree-item[data-path="{path}"]', timeout=30000)
        open_doc(page, path)
        user_menu(page); page.click('[data-testid="toggle-term"]')
        page.wait_for_selector("#terminal .xterm-rows", timeout=10000)
        page.wait_for_timeout(500)
        held = _hold(page, "**/fs/props*")        # mountDoc awaits this first
        page.reload()
        page.wait_for_selector("#terminal-panel:not([hidden]) .xterm-rows", timeout=4000)
        assert held, "the document never asked for its props — nothing was held"
        assert page.evaluate("() => !window.__kbview"), \
            "the document mounted before the terminal — the wait was not exercised"
        held[0].continue_()
        page.wait_for_function("() => !!window.__kbview", timeout=10000)
    finally:
        ctx.close()


def test_xterm_is_fetched_only_when_a_terminal_opens(browser):
    """A quarter of the bundle, in its own chunk: a session that never opens a
    shell never downloads it, and the first shell fetches it exactly then."""
    ctx = browser.new_context()
    chunks = []
    # esbuild also emits a few-hundred-byte helper chunk both halves share,
    # which the entry loads at boot; the terminal's own chunk is the `term-` one
    ctx.on("request", lambda r: chunks.append(r.url) if "/static/chunks/term-" in r.url else None)
    page = login(ctx, USER)
    try:
        page.wait_for_timeout(800)
        assert not chunks, f"xterm was fetched before any terminal was opened: {chunks}"
        user_menu(page); page.click('[data-testid="toggle-term"]')
        page.wait_for_selector("#terminal .xterm-rows", timeout=10000)
        assert any("term-" in u for u in chunks), f"no terminal chunk was fetched: {chunks}"
    finally:
        ctx.close()
