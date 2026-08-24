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
from conftest import BASE, CREDS
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
    assert c.post("/login", data={"username": USER,
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
