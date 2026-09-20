"""Dictation in the browser: the button, the key, and where the words land.

The network is stubbed, not the browser: chromium gets a fake microphone (so
getUserMedia resolves without a prompt and MediaRecorder produces real webm), and
`page.route` answers /stt with a canned transcript. That exercises the whole
client path — capture, keybinding, focus routing, insertion — without spending a
cent or depending on ElevenLabs being reachable.

Every test that writes into a document gets its OWN document and its OWN phrase.
Sharing either makes "did the text arrive?" unanswerable, because a previous
test's insertion is still sitting there.
"""
import json
import os
from pathlib import Path
import re
import uuid

import httpx
import pytest

from conftest import BASE, CREDS, login, user_menu
from kbenv import U, doc as kbdoc

SPOKEN = "the sync daemon owns the merge"
HOLD_MS = 800          # comfortably past dictation.js's 450 ms hold threshold


@pytest.fixture(scope="module")
def mic_browser(browser):
    """A SECOND chromium, launched from the shared session browser's own
    browser_type so it rides the same Playwright connection. This module needs the
    fake audio device (`--use-fake-device-for-media-stream`), which the shared
    browser doesn't have — and opening a nested `sync_playwright()` inside the
    session fixture's live context raises "Sync API inside the asyncio loop"."""
    b = browser.browser_type.launch(headless=True, args=[
        "--no-sandbox",
        "--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
    ])
    yield b
    b.close()


@pytest.fixture(scope="module")
def api():
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    yield c


@pytest.fixture(scope="module")
def ctx(mic_browser):
    """Microphone permission pre-granted: a real prompt would steal focus and the
    keyup, which is exactly the failure dictation.js's warm-up avoids in prod."""
    c = mic_browser.new_context(permissions=["microphone"])
    yield c
    c.close()


@pytest.fixture
def page(ctx):
    p = ctx.new_page()
    _serve_dev(p)
    p.goto(BASE + "/login")
    p.fill('input[name="username"]', U("alice"))
    p.fill('input[name="password"]', CREDS["alice"])
    p.click('button[type="submit"]')
    p.wait_for_url(BASE + "/")
    p.wait_for_selector('[data-testid="tree"] .tree-item', timeout=15000)
    stub(p, {"ok": True, "text": SPOKEN})
    # Content search is not under test here, and it is expensive for real: the
    # palette fires /api/search for any 2+ character query, and that scan can
    # take ~20 s on a large corpus — during which the per-user backend (sync
    # psycopg on the event loop) answers nothing, so the NEXT test's page load
    # times out. Dictating into the palette types a query; answer it locally.
    p.route("**/api/search*", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"results": [], "files": [], "db": True})))
    yield p
    p.close()


@pytest.fixture
def doc(api):
    """A fresh, empty document per test."""
    path = kbdoc(f"kbtest_dict_{os.getpid()}_{uuid.uuid4().hex[:8]}.md")
    api.post("/api/file", json={"path": path})
    api.post("/api/artifact/write", json={"path": path, "content": "# scratch\n\nbaseline\n"})
    yield path
    api.post("/api/fs/delete", json={"path": path})



# ── running against the dev bundle ───────────────────────────────────────────
# The hub serves static assets from /opt, which only a root `deploy.sh` updates.
# With KB_DEV_BUNDLE=1 these tests serve frontend/static straight from the source
# tree instead, so the client can be iterated on without root. Self-contained on
# purpose: it must not depend on a conftest helper that may or may not be there.
# Serve the frontend from this checkout instead of the deployed copy, so an
# edit can be tested without redeploying. Derived from the repo layout —
# never a hardcoded home directory, which only exists on one machine.
DEV = os.environ.get(
    "KB_DEV_BUNDLE_DIR",
    str(Path(__file__).resolve().parents[2] / "frontend" / "static"),
)
_MIME = {"js": "application/javascript", "css": "text/css", "html": "text/html",
         "svg": "image/svg+xml", "woff2": "font/woff2", "map": "application/json"}


def _serve_dev(page):
    if not os.environ.get("KB_DEV_BUNDLE"):
        return

    def send(route, path):
        try:
            body = open(path, "rb").read()
        except OSError:
            route.continue_()
            return
        route.fulfill(status=200, body=body,
                      content_type=_MIME.get(path.rsplit(".", 1)[-1], "application/octet-stream"),
                      headers={"cache-control": "no-store"})

    def static(route):
        rel = route.request.url.split(BASE, 1)[-1].split("?")[0]
        send(route, DEV + rel[len("/static"):])

    def document(route):
        # Every app URL — "/" and every deep link — is served the same app.html.
        rel = route.request.url.split(BASE, 1)[-1].split("?")[0]
        # /static is excluded here as well as handled below: Playwright matches
        # routes most-recently-added first, so this catch-all would otherwise
        # shadow the static one and serve app.html in place of app.js.
        if rel.startswith(("/static", "/login", "/logout", "/api", "/stt",
                           "/fs", "/admin", "/ws", "/pty", "/egress")):
            route.continue_()
            return
        send(route, DEV + "/app.html")

    page.route(lambda url: str(url).startswith(BASE), document)
    page.route(re.compile(re.escape(BASE) + r"/static/.*"), static)


def stub(page, payload, status=200):
    page.route("**/stt*", lambda r: r.fulfill(
        status=status, content_type="application/json", body=json.dumps(payload)))


def dictate(page, hold_ms=HOLD_MS):
    """Hold F9 long enough to read as a hold rather than a tap, then let go.

    The hold is measured from when capture actually STARTS (the indicator
    appearing), not from the keydown: on a cold mic getUserMedia can take
    longer than the whole hold, and a keyup that lands mid-warm-up is a
    zero-length capture — correctly discarded as too short, which is not what
    these tests mean to exercise."""
    page.keyboard.down("F9")
    recording(page)
    page.wait_for_timeout(hold_ms)
    page.keyboard.up("F9")


def recording(page):
    page.wait_for_selector('[data-testid="ptt"]', state="visible", timeout=6000)


def not_recording(page):
    # state="hidden" is the point: `[data-testid="ptt"][hidden]` can never be
    # "visible", so waiting on that selector the default way never resolves.
    page.wait_for_selector('[data-testid="ptt"]', state="hidden", timeout=15000)


def show_terminal(page):
    """Ctrl+` TOGGLES, and terminals survive a reload — so a restored session may
    already have the panel up, in which case pressing the key would hide it."""
    if page.locator("#terminal-panel").is_hidden():
        page.keyboard.press("Control+`")
    page.wait_for_selector("#terminal-panel", state="visible", timeout=10000)
    page.wait_for_function("() => window.__kbterm && window.__kbterm.buffer", timeout=10000)
    page.wait_for_timeout(1500)          # let the shell paint its prompt
    page.click("#terminal .xterm-screen")


def leave_terminal(page):
    """Ctrl+P and ? are deliberately NOT term:true — inside a terminal they belong
    to readline, and the app-level binding correctly does not fire. Terminals
    survive a reload, so a restored session can leave one focused; these tests
    have to step out of it first."""
    if page.locator("#terminal-panel").is_visible():
        page.keyboard.press("Control+`")
        page.wait_for_selector("#terminal-panel", state="hidden", timeout=6000)
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.wait_for_timeout(150)


def term_dump(page):
    return page.evaluate("""() => { const b = window.__kbterm.buffer.active; let s = '';
      for (let i = 0; i < b.length; i++) s += b.getLine(i).translateToString(true) + '\\n';
      return s; }""")


def prompt_count(page):
    """How many shell prompts are on screen. Unchanged across a dictation is the
    cleanest possible proof that nothing was executed."""
    return page.evaluate("""() => { const b = window.__kbterm.buffer.active; let n = 0;
      for (let i = 0; i < b.length; i++)
        if (/\\$ /.test(b.getLine(i).translateToString(true))) n++;
      return n; }""")


def open_doc(page, path):
    page.goto(BASE + "/" + path)
    page.wait_for_selector(".cm-editor", timeout=10000)
    page.wait_for_function("() => window.__kbview && window.__kbsynced", timeout=12000)
    page.click(".cm-content")
    page.wait_for_timeout(200)


def doc_text(page):
    return page.evaluate("window.__kbview.state.doc.toString()")


# ---- the affordance ---------------------------------------------------------

def test_mic_button_present(page):
    b = page.locator('[data-testid="mic-btn"]')
    assert b.count() == 1
    assert b.is_visible()
    assert b.get_attribute("aria-pressed") == "false"


def test_indicator_shows_while_recording(page):
    page.keyboard.down("F9")
    recording(page)
    assert page.locator('[data-testid="mic-btn"]').get_attribute("aria-pressed") == "true"
    assert "Listening" in page.locator("#ptt-status").inner_text()
    page.wait_for_timeout(HOLD_MS)      # make it a hold, so releasing ends it
    page.keyboard.up("F9")
    not_recording(page)


def test_hold_and_release_stops(page):
    dictate(page)
    not_recording(page)


def test_tap_latches_and_second_tap_finishes(page):
    """A quick tap must NOT stop on keyup — it latches, and the next press ends
    it. This is the gesture that makes dictating a long paragraph bearable."""
    page.keyboard.press("F9")               # down+up inside the hold threshold
    recording(page)
    page.wait_for_timeout(900)              # well past it; a hold would have ended
    assert page.locator('[data-testid="ptt"]').is_visible(), "a tap should have latched"
    page.keyboard.press("F9")               # second press finishes
    not_recording(page)


def test_autorepeat_does_not_toggle(page):
    """Holding a key emits keydown ~30x/s. If each one were treated as a press,
    recording would flicker on and off — hence the e.repeat guard."""
    page.keyboard.down("F9")
    recording(page)
    for _ in range(12):
        page.keyboard.down("F9")            # simulated auto-repeat
        page.wait_for_timeout(100)
    assert page.locator('[data-testid="ptt"]').is_visible(), "auto-repeat stopped it"
    page.keyboard.up("F9")                  # >1s held, so this reads as a release
    not_recording(page)


def test_blur_stops_a_stranded_recording(page):
    """Alt-Tab mid-hold means the keyup never arrives. Without the blur guard the
    recording would run all the way to the 10-minute cap."""
    page.keyboard.down("F9")
    recording(page)
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    not_recording(page)
    page.keyboard.up("F9")


def test_blur_does_not_stop_a_latched_recording(page):
    """Mobile browsers blur the window for their own chrome — Firefox for
    Android does it for the mic-permission doorhanger and its "recording"
    notification, moments after recording starts. Only a KEY-held recording
    (the stranded-keyup case) may end on blur; a latched one keeps going."""
    page.keyboard.press("F9")               # tap -> latched
    recording(page)
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    page.wait_for_timeout(600)
    assert page.locator('[data-testid="ptt"]').is_visible(), "blur killed a latched recording"
    page.keyboard.press("F9")               # second tap finishes
    not_recording(page)


def test_late_duplicate_release_does_not_stop_a_latch(page):
    """A touch tap on the mic delivers pointerup AND lostpointercapture — and
    Firefox for Android can deliver the second one late, >450 ms after the
    press, where it used to read as a hold ending and stopped the recording an
    instant after the tap latched it. A release must count exactly once."""
    b = page.locator('[data-testid="mic-btn"]')
    b.dispatch_event("pointerdown")
    b.dispatch_event("pointerup")           # a real tap: released immediately
    recording(page)
    page.wait_for_timeout(700)              # well past the 450 ms hold threshold
    b.dispatch_event("lostpointercapture")  # the straggler
    page.wait_for_timeout(400)
    assert page.locator('[data-testid="ptt"]').is_visible(), \
        "a duplicate release ended the latched recording"
    page.keyboard.press("F9")
    not_recording(page)


def test_alt_k_alias_survives_releasing_the_modifier_first(page):
    """Alt released before K delivers a keyup with altKey already false. Matching
    the keyup on the physical code rather than re-testing the combo is what keeps
    that from stranding the recording."""
    page.keyboard.down("Alt")
    page.keyboard.down("K")
    recording(page)
    page.wait_for_timeout(HOLD_MS)
    page.keyboard.up("Alt")                 # modifier first, on purpose
    page.keyboard.up("K")
    not_recording(page)


# ---- where the words go -----------------------------------------------------

def test_text_lands_in_the_editor(page, doc):
    open_doc(page, doc)
    dictate(page)
    page.wait_for_function("t => window.__kbview.state.doc.toString().includes(t)",
                           arg=SPOKEN, timeout=15000)


def test_editor_insertion_is_one_undo_step(page, doc):
    """userEvent 'input.paste' rather than 'input.type': adjacent input.type
    transactions merge into one undo step, so a dictation followed by typing would
    collapse together and a single Ctrl+Z would eat both."""
    phrase = "zebras calibrate the quiet ledger"
    stub(page, {"ok": True, "text": phrase})
    open_doc(page, doc)
    before = doc_text(page)
    assert phrase not in before
    dictate(page)
    page.wait_for_function("t => window.__kbview.state.doc.toString().includes(t)",
                           arg=phrase, timeout=15000)
    page.keyboard.press("Control+z")
    page.wait_for_function("b => window.__kbview.state.doc.toString() === b",
                           arg=before, timeout=8000)


def test_text_lands_in_the_terminal_without_a_newline(page):
    """Dictating a command must never run it: a mis-transcription has no undo."""
    show_terminal(page)
    dictate(page)
    page.wait_for_function(
        "t => { const b = window.__kbterm.buffer.active;"
        "  return b.getLine(b.cursorY).translateToString(true).includes(t); }",
        arg=SPOKEN, timeout=15000)
    # The prompt has not advanced: the text sits on the command line, un-executed,
    # waiting for the human to press Return.
    line = page.evaluate(
        "(() => { const b = window.__kbterm.buffer.active;"
        "  return b.getLine(b.cursorY).translateToString(true); })()")
    assert SPOKEN in line


def test_control_characters_are_stripped_from_a_transcript(page):
    r"""The transcript is third-party text on its way to a shell. An ESC must
    never survive: a hallucinated "\x1b[201~" would otherwise terminate xterm's
    bracketed paste and hand the remainder to the shell as typed input.

    dictation.js strips C0 controls before insertion and term.paste() rewrites any
    that got through to U+241B. This asserts the first layer, because that is the
    one that has to hold."""
    stub(page, {"ok": True, "text": "ls\u001b[201~; echo pwned"})
    show_terminal(page)
    dictate(page)
    page.wait_for_timeout(3000)
    dump = page.evaluate("""() => {
      const b = window.__kbterm.buffer.active; let s = '';
      for (let i = 0; i < b.length; i++) s += b.getLine(i).translateToString(true) + '\\n';
      return s; }""")
    assert "\u001b" not in dump, "an ESC reached the terminal"
    assert "␛" not in dump, "nothing should even have reached term.paste()'s fallback"
    assert "[201~" in dump, "the sanitized text should still arrive, just inert"
    # Nothing executed: "pwned" may appear inside the un-run command line, never
    # alone on a line, which is what command output would look like.
    assert not any(ln.strip() == "pwned" for ln in dump.splitlines()), "the escape broke out"


def test_a_carriage_return_in_a_transcript_does_not_press_enter(page):
    r"""The subtlest way a dictated command could run itself. xterm's paste path
    folds \r\n to \r and passes a LONE \r straight through, and \r in a PTY *is*
    Enter — so an un-sanitized transcript containing one would submit the command
    line. sanitize() folds every CR to \n first.

    With bracketed paste on (bash/readline, and most TUIs) the newline arrives as
    a literal character and the command line simply spans two rows; with it off,
    insertDictation collapses newlines to spaces. Either way nothing runs, which is
    what this asserts — the prompt count is the invariant, not the cursor row,
    because a multi-row command line moves the cursor without executing.
    """
    stub(page, {"ok": True, "text": "echo dictated\rwhoami"})
    show_terminal(page)
    prompts_before = prompt_count(page)
    dictate(page)
    page.wait_for_function(
        "() => { const b = window.__kbterm.buffer.active;"
        "  for (let i = 0; i < b.length; i++)"
        "    if (b.getLine(i).translateToString(true).includes('dictated')) return true;"
        "  return false; }", timeout=15000)
    page.wait_for_timeout(2000)
    dump = term_dump(page)
    assert prompt_count(page) == prompts_before, \
        f"a new prompt appeared, so something executed:\n{dump}"
    # `echo dictated` never ran, so "dictated" never appears as output on its own
    # line; and `whoami` never ran either.
    assert not any(ln.strip() == "dictated" for ln in dump.splitlines()), dump
    assert "\u001b" not in dump


def test_text_lands_in_the_command_palette(page):
    """The palette is a modal, and wireShortcuts() deliberately gives up the
    keyboard while a modal is open. Dictation is the one exception, because the
    palette is a text field and speaking into it is half the point."""
    leave_terminal(page)
    page.keyboard.press("Control+p")
    inp = page.locator('[data-testid="palette-input"]')
    inp.wait_for(timeout=6000)
    # Wait for the palette to actually OWN the focus. Dictation resolves its
    # target when recording starts, so dictating a moment too early sends the
    # words to whatever was focused before — a real race, not a flake.
    page.wait_for_function(
        "() => document.activeElement && "
        "document.activeElement.classList.contains('palette-input')", timeout=6000)
    dictate(page)
    for _ in range(60):
        if SPOKEN in (inp.input_value() or ""):
            break
        page.wait_for_timeout(250)
    assert SPOKEN in inp.input_value()


# ---- failure is visible, never silent ---------------------------------------

def test_server_503_shows_a_toast_and_inserts_nothing(page, doc):
    stub(page, {"error": "dictation is not set up on this server"}, status=503)
    open_doc(page, doc)
    before = doc_text(page)
    dictate(page)
    page.wait_for_selector('[data-testid="toast"]', timeout=15000)
    assert "not set up" in page.locator('[data-testid="toast"]').first.inner_text()
    assert doc_text(page) == before


def test_empty_transcript_says_so(page, doc):
    stub(page, {"ok": True, "text": ""})
    open_doc(page, doc)
    dictate(page)
    page.wait_for_selector('[data-testid="toast"]', timeout=15000)
    assert "Nothing was said" in page.locator('[data-testid="toast"]').first.inner_text()


def test_too_short_is_discarded_before_upload(page, doc):
    """A latch immediately stopped produces almost no audio; the client must not
    upload it at all — that request would be billed for nothing.

    The positive assertion (the "Too short" toast) is what keeps this test
    honest: on a cold mic the second press lands while getUserMedia is still
    resolving, and without it the test would pass vacuously with the recording
    never finished at all."""
    calls = []
    page.route("**/stt*", lambda r: (calls.append(1), r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"ok": True, "text": SPOKEN}))))
    open_doc(page, doc)
    page.keyboard.press("F9")     # latch
    page.keyboard.press("F9")     # stop immediately: under the 300 ms floor
    page.wait_for_selector('[data-testid="toast"]', timeout=8000)
    toasts = page.locator('[data-testid="toast"]').all_inner_texts()
    assert any("Too short" in t for t in toasts), toasts
    assert calls == [], "a sub-300ms recording should never be uploaded"


# ---- the last 24 hours are recoverable --------------------------------------

def test_transcript_lands_in_history(page, doc):
    """Every transcript that comes back is kept for a day, so a deleted or
    misrouted dictation is recoverable from the topbar's Dictation history."""
    phrase = "recoverable " + uuid.uuid4().hex[:8]
    stub(page, {"ok": True, "text": phrase})
    open_doc(page, doc)
    dictate(page)
    not_recording(page)
    page.wait_for_function(
        "t => window.__kbview.state.doc.toString().includes(t)", arg=phrase, timeout=15000)
    user_menu(page); page.click('[data-testid="dict-hist-btn"]')
    page.wait_for_selector('[data-testid="dh-list"] .dh-item', timeout=6000)
    assert phrase in page.locator('[data-testid="dh-list"]').inner_text()


def test_failed_transcription_keeps_the_audio_and_history_rescues_it(page, doc):
    """The reason the vault exists: five minutes of speech cannot be re-spoken.
    When /stt fails, the recording must survive — listed in Dictation history —
    and one click there must send the SAME audio again once the server is back."""
    stub(page, {"error": "upstream exploded"}, status=502)
    open_doc(page, doc)
    before = doc_text(page)
    dictate(page)
    page.wait_for_selector('[data-testid="toast"]', timeout=15000)
    assert doc_text(page) == before, "a failed transcription must insert nothing"

    user_menu(page); page.click('[data-testid="dict-hist-btn"]')
    page.wait_for_selector('[data-testid="dh-list"] .dh-rec', timeout=6000)
    row = page.locator('[data-testid="dh-list"] .dh-rec').first
    assert "not transcribed" in row.inner_text()
    assert row.locator('[data-testid="dh-download"]').count() == 1

    # The server recovers; "transcribe" on the kept recording finishes the job.
    phrase = "rescued " + uuid.uuid4().hex[:8]
    stub(page, {"ok": True, "text": phrase})
    row.locator('[data-testid="dh-transcribe"]').click()
    page.wait_for_function("t => window.__kbview.state.doc.toString().includes(t)",
                           arg=phrase, timeout=15000)
    # And once transcribed, it is no longer listed as stranded.
    user_menu(page); page.click('[data-testid="dict-hist-btn"]')
    page.wait_for_selector('[data-testid="dh-list"] .dh-item', timeout=6000)
    assert phrase in page.locator('[data-testid="dh-list"]').inner_text()


def test_a_recording_survives_the_tab_dying_mid_recording(page):
    """The worst case: the tab is gone WHILE the words are being spoken — no
    stop, no upload, nothing. Chunks are vaulted to IndexedDB every second, so
    after a reload the recording is in Dictation history, ready to transcribe
    or download, at most one second short."""
    leave_terminal(page)
    n_before = page.evaluate(
        """() => new Promise((res) => { const rq = indexedDB.open('kbDictAudio', 1);
             rq.onupgradeneeded = () => { rq.result.createObjectStore('recs', {keyPath: 'id'});
               rq.result.createObjectStore('chunks', {keyPath: ['rid','seq']}); };
             rq.onsuccess = () => { const t = rq.result.transaction('recs');
               const c = t.objectStore('recs').count();
               c.onsuccess = () => res(c.result); };
             rq.onerror = () => res(-1); })""")
    page.keyboard.press("F9")               # latch, so nothing stops it
    recording(page)
    page.wait_for_timeout(3500)             # a few one-second slices reach disk
    page.reload()                           # the "crash": no finish, no upload
    page.wait_for_selector('[data-testid="tree"] .tree-item', timeout=15000)

    user_menu(page); page.click('[data-testid="dict-hist-btn"]')
    page.wait_for_selector('[data-testid="dh-list"] .dh-rec', timeout=6000)
    row = page.locator('[data-testid="dh-list"] .dh-rec').first
    assert "not transcribed" in row.inner_text()
    assert row.locator('[data-testid="dh-transcribe"]').count() == 1
    n_after = page.evaluate(
        """() => new Promise((res) => { const rq = indexedDB.open('kbDictAudio', 1);
             rq.onsuccess = () => { const t = rq.result.transaction('recs');
               const c = t.objectStore('recs').count();
               c.onsuccess = () => res(c.result); }; })""")
    assert n_after == n_before + 1, "the interrupted recording should be vaulted"


def test_the_original_audio_of_a_transcript_is_downloadable(page, doc):
    """A successful dictation keeps its audio for a day too — the transcript row
    in history offers the original recording as a file."""
    phrase = "audible " + uuid.uuid4().hex[:8]
    stub(page, {"ok": True, "text": phrase})
    open_doc(page, doc)
    dictate(page)
    page.wait_for_function("t => window.__kbview.state.doc.toString().includes(t)",
                           arg=phrase, timeout=15000)
    user_menu(page); page.click('[data-testid="dict-hist-btn"]')
    page.wait_for_selector('[data-testid="dh-list"] .dh-item', timeout=6000)
    rows = page.locator('[data-testid="dh-list"] .dh-item')
    assert phrase in rows.first.inner_text() or phrase in page.locator('[data-testid="dh-list"]').inner_text()
    with page.expect_download(timeout=10000) as dl:
        page.locator('.dh-item:not(.dh-rec) .dh-download').first.click()
    name = dl.value.suggested_filename
    assert name.startswith("dictation-") and name.rsplit(".", 1)[-1] in ("webm", "ogg")


def test_deleting_a_kept_recording_is_permanent(page):
    """The ✕ on a stranded recording removes it from the vault, not just the
    modal — reopening the history must not resurrect it."""
    leave_terminal(page)
    user_menu(page); page.click('[data-testid="dict-hist-btn"]')
    # The list is filled asynchronously (the vault is IndexedDB) — wait for the
    # render, not just the modal: rows and the empty-state note are appended in
    # one synchronous pass, so any child means the list is complete.
    page.wait_for_selector('[data-testid="dh-list"] > *', timeout=6000)
    # Earlier tests strand recordings on purpose; clear every one of them.
    while page.locator('[data-testid="dh-list"] .dh-rec').count():
        page.locator('[data-testid="dh-list"] .dh-rec .dh-delete').first.click()
        page.wait_for_timeout(150)
    page.locator(".modal-close").click()
    page.wait_for_timeout(800)              # let the IndexedDB deletes commit
    user_menu(page); page.click('[data-testid="dict-hist-btn"]')
    page.wait_for_selector('[data-testid="dh-list"] > *', timeout=6000)
    assert page.locator('[data-testid="dh-list"] .dh-rec').count() == 0


def test_history_expires_after_a_day(page):
    """Entries older than 24 h are filtered on read — the store is a safety net,
    not an archive."""
    page.evaluate("""() => localStorage.setItem('kbDictHistory', JSON.stringify([
      {t: Date.now() - 1000, text: 'fresh entry'},
      {t: Date.now() - 25 * 3600 * 1000, text: 'stale entry'}]))""")
    leave_terminal(page)
    user_menu(page); page.click('[data-testid="dict-hist-btn"]')
    page.wait_for_selector('[data-testid="dh-list"] > *', timeout=6000)
    body = page.locator('[data-testid="dh-list"]').inner_text()
    assert "fresh entry" in body
    assert "stale entry" not in body


# ---- documentation cannot drift from behaviour ------------------------------

def test_shortcut_sheet_lists_dictation(page):
    """The sheet renders from the same BINDINGS table the dispatcher reads, so this
    asserts the key and its documentation cannot disagree."""
    leave_terminal(page)
    page.keyboard.press("?")
    page.wait_for_selector(".modal-overlay", timeout=6000)
    body = page.locator(".modal-overlay").inner_text()
    # Group headings are uppercased by CSS and inner_text() returns rendered text.
    assert "dictation" in body.lower()
    assert "F9" in body and "Alt+K" in body


def test_dictation_is_in_the_command_palette(page):
    page.keyboard.press("Control+Shift+p")
    inp = page.locator('[data-testid="palette-input"]')
    inp.wait_for(timeout=6000)
    inp.fill(">dicta")   # ">" is what selects command mode
    page.wait_for_timeout(500)
    listing = page.locator('[data-testid="palette-list"]').inner_text().lower()
    assert "dictate" in listing


HIDE = """(hidden) => {
  Object.defineProperty(document, 'hidden', {value: hidden, configurable: true});
  Object.defineProperty(document, 'visibilityState',
                        {value: hidden ? 'hidden' : 'visible', configurable: true});
  document.dispatchEvent(new Event('visibilitychange'));
}"""


def test_latched_recording_survives_going_home(page):
    """A latched recording keeps running when the app leaves the screen —
    dictating a note while reading another app is half the point of latching,
    and the phone's mic-in-use dot is then telling the truth. No stop, no
    spurious 'stopped' toast on return, and the second tap still finishes."""
    page.keyboard.press("F9")                    # tap → latched recording
    recording(page)
    page.evaluate(HIDE, True)
    page.wait_for_timeout(800)
    assert page.locator('[data-testid="ptt"]').is_visible(), "hiding must not stop a latch"
    assert page.evaluate("() => window.__kbmicLive()"), "the capture must stay open"
    page.evaluate(HIDE, False)
    page.wait_for_timeout(400)
    toasts = page.locator('[data-testid="toast"]').all_inner_texts()
    assert not any("left the screen" in t for t in toasts), toasts
    assert page.locator('[data-testid="ptt"]').is_visible()
    page.keyboard.press("F9")                    # finish normally
    not_recording(page)


def test_leaving_the_app_with_an_idle_mic_releases_it(page):
    """When nothing is recording, the warm mic (kept for latency) is released
    the moment the app leaves the screen — a live track lights Android's
    system-wide mic dot even disabled, and a green dot with nothing recording
    reads as eavesdropping."""
    dictate(page)                                # a full recording, then idle
    not_recording(page)
    assert page.evaluate("() => window.__kbmicLive()"), "idle keeps the mic warm on screen"
    page.evaluate(HIDE, True)
    page.wait_for_function("() => !window.__kbmicLive()", timeout=8000)
    page.evaluate(HIDE, False)


def test_a_recording_killed_off_screen_explains_on_return(page):
    """The bounded cases — the 10 min cap, or the OS reclaiming the microphone in
    the background — finish the recording while nobody is looking. The finish
    must release the mic, and the return to the app must say what happened:
    a toast fired off-screen would have expired unseen. Emulated by ending the
    tracks, exactly what the OS does when it takes the device."""
    page.keyboard.press("F9")
    recording(page)
    page.evaluate(HIDE, True)
    page.wait_for_timeout(300)
    page.evaluate("""() => {                    /* the OS reclaims the mic */
      window.__kbdictStream && window.__kbdictStream()
        .getAudioTracks().forEach((t) => { t.stop(); t.dispatchEvent(new Event('ended')); });
    }""")
    not_recording(page)
    page.wait_for_function("() => !window.__kbmicLive()", timeout=8000)
    page.evaluate(HIDE, False)
    page.wait_for_selector('[data-testid="toast"]', timeout=8000)
    toasts = page.locator('[data-testid="toast"]').all_inner_texts()
    assert any("left the screen" in t for t in toasts), toasts
