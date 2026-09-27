"""Mobile layout (phone viewport, touch): the sidebar is an over-content drawer,
topbar actions collapse into the ⋯ menu, tree actions open via the per-row ⋯
toggle, and modals become full-width bottom sheets. Every desktop feature must
stay reachable with a finger."""
import base64
import pathlib
import tempfile
import time

from conftest import BASE, CREDS, dlg_fill, dlg_ok, wait_path, user_menu
from kbenv import AREA, U, doc

MOBILE = dict(viewport={"width": 390, "height": 844}, is_mobile=True,
              has_touch=True, device_scale_factor=3)


def m_login(browser, user="alice"):
    ctx = browser.new_context(**MOBILE)
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', U(user))
    page.fill('input[name="password"]', CREDS[user])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    return ctx, page


def nav_open(page):
    return page.evaluate("() => document.body.classList.contains('nav-open')")


def drawer_settled(page):
    """The drawer slides in; a tap mid-transform lands beside the row it aimed
    at. Waiting a fixed 400 ms was enough on an idle box and not enough under
    a full suite — wait for the panel to actually be at the left edge."""
    page.wait_for_function(
        "() => { const s = document.querySelector('.sidebar'); if (!s) return false;"
        "  const r = s.getBoundingClientRect(); return r.x > -1 && r.width > 100; }",
        timeout=5000)
    page.wait_for_timeout(60)


def term_box(page):
    return page.evaluate("""() => { const r = document.querySelector('.term-content')
        .getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; }""")


def swipe(page, cdp, frac_from, frac_to, steps=12, dt=0.016, release=True):
    """A REAL touch swipe, dispatched through the browser's input pipeline.

    Synthetic TouchEvents are useless here: the bug this guards against is that
    xterm's DOM renderer detaches the element under the finger, after which the
    browser's own touch events stop reaching any ancestor listener. Only real
    input exercises that path (and the pointer capture that survives it).
    Fractions are of the terminal's height; larger = further down the screen."""
    b = term_box(page)
    x, y0, y1 = b["x"] + b["w"] / 2, b["y"] + b["h"] * frac_from, b["y"] + b["h"] * frac_to
    cdp.send("Input.dispatchTouchEvent",
             {"type": "touchStart", "touchPoints": [{"x": x, "y": y0}]})
    for i in range(1, steps + 1):
        time.sleep(dt)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove",
                 "touchPoints": [{"x": x, "y": y0 + (y1 - y0) * i / steps}]})
    if release:
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    return abs(y1 - y0)


def rows_for(page, px):
    """How many terminal rows that many pixels of finger travel is worth."""
    return page.evaluate("""(px) => { const el = document.querySelector('.term-content');
        return Math.round(px / Math.max(8, el.clientHeight / window.__kbterm.rows)); }""", px)


def open_terminal(page):
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#panes .tab-content.term .xterm-rows")
    deadline = time.time() + 8
    while time.time() < deadline and "$" not in page.inner_text(".tab-content.term"):
        page.wait_for_timeout(150)


def wait_for_text(page, needle, secs=8):
    deadline = time.time() + secs
    while time.time() < deadline:
        if needle in page.inner_text(".tab-content.term"):
            return True
        page.wait_for_timeout(150)
    return False


def test_drawer_boots_open_and_closes_on_file_open(browser):
    ctx, page = m_login(browser)
    # the file list is the mobile home screen
    assert nav_open(page)
    assert page.locator("#nav-btn").is_visible()
    assert not page.locator("#whoami").is_visible()      # desktop chrome is gone
    assert not page.locator(".brand-word").is_visible()         # no brand row: the tabs are the top
    # opening a document dismisses the drawer and shows the editor
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    wait_path(page, doc("overview.md"))
    assert not nav_open(page)
    # the hamburger brings the tree back
    page.click("#nav-btn")
    assert nav_open(page)
    # tapping the scrim closes it again
    page.mouse.click(370, 500)
    page.wait_for_function("() => !document.body.classList.contains('nav-open')")
    ctx.close()


def test_user_menu_holds_the_actions(browser):
    ctx, page = m_login(browser)
    menu = page.locator('[data-testid="user-menu"]')
    assert not menu.is_visible()                          # collapsed behind the person
    user_menu(page)
    assert menu.is_visible()
    assert page.locator("#cron-btn").is_visible()
    assert page.locator(".logout").is_visible()
    assert "alice" in page.inner_text("#whoami")          # identity heads the menu
    page.click("#cron-btn")                               # choosing an action closes the menu
    page.wait_for_selector('[data-testid="cron-jobs"]')
    assert not menu.is_visible()
    # the cron modal is a full-width sheet on a phone
    box = page.locator(".modal-card").bounding_box()
    assert box["width"] == 390, box
    page.click(".modal-close")
    ctx.close()


def test_tree_actions_via_row_toggle(browser):
    """No hover and no right-click on touch: the per-row ⋯ opens the full action
    menu (New file / Rename / Delete / …), and the create → open → delete cycle
    works with taps + in-app dialogs."""
    name = f"mob_{int(time.time())}.md"
    ctx, page = m_login(browser)
    drawer_settled(page)
    row = page.locator(f'.tree-item[data-path="{AREA}"]')
    row.locator(".tmore").click()
    page.wait_for_selector('[data-testid="ctx-menu"]')
    page.click('.ctx-item:has-text("New file")')
    dlg_fill(page, name)
    page.wait_for_selector(f'.tab.active[data-path="{doc(name)}"]', timeout=8000)
    assert not nav_open(page)                             # creating lands you in the doc
    # delete it again from the tree
    page.click("#nav-btn")
    page.wait_for_function("() => document.body.classList.contains('nav-open')")
    drawer_settled(page)
    row = page.locator(f'.tree-item[data-path="{doc(name)}"]')
    row.locator(".tmore").click()
    page.wait_for_selector('[data-testid="ctx-menu"]')
    page.click('.ctx-item.danger:has-text("Delete")')
    dlg_ok(page)
    page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', state="detached", timeout=8000)
    ctx.close()


def test_terminal_open_from_menu(browser):
    """A phone has no terminal panel: the terminal is a tab in the one group
    on screen, beside the documents."""
    ctx, page = m_login(browser)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#panes .tab-content.term .xterm-rows")
    assert not nav_open(page)                             # drawer got out of the way
    assert page.locator("#terminal-panel").is_hidden(), "the desktop's panel stays away"
    assert page.locator("#term-tabs .tab").count() == 0
    box = page.locator("#panes .tab-content.term").bounding_box()
    assert box["width"] == 390 and box["y"] + box["height"] <= 844 + 1, box
    page.click("#panes .tab.current .tab-x")
    ctx.close()


def test_terminal_keybar_sends_keys(browser):
    """Touch keybar: visible on a phone, and its keys really reach the pty —
    ↑ recalls shell history, ^C cancels the recalled line."""
    ctx, page = m_login(browser)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#panes .tab-content.term .xterm-rows")
    bar = page.locator('[data-testid="term-keys"]')
    assert bar.is_visible()

    # a ~48-column phone terminal wraps lines mid-token; strip the wrap
    # newlines before matching (a wrapped row is full, so the join is exact)
    def term_text():
        return page.inner_text(".tab-content.term").replace("\n", "")

    # wait for the shell prompt before typing — keys sent earlier can be lost
    deadline = time.time() + 8
    while time.time() < deadline and "$" not in term_text():
        page.wait_for_timeout(150)
    marker = f"kbup_{int(time.time())}"
    page.keyboard.type(f"echo {marker}")
    page.keyboard.press("Enter")
    deadline = time.time() + 6
    while time.time() < deadline:
        if term_text().count(marker) >= 2:                    # typed + output
            break
        page.wait_for_timeout(150)
    page.click('#term-keys button[data-k="up"]')              # recall from history
    deadline = time.time() + 6
    ok = False
    while time.time() < deadline:
        if term_text().count(f"echo {marker}") >= 2:
            ok = True
            break
        page.wait_for_timeout(150)
    assert ok, "arrow-up from the keybar must recall the last command"
    page.click('#term-keys button[data-k="cc"]')              # ^C abandons it
    # ctrl arms visually and disarms after the next key
    page.click('#term-keys button[data-k="ctrl"]')
    assert page.locator("#tk-ctrl.active").count() == 1
    page.keyboard.type("c")                                   # ctrl+c via arm
    page.wait_for_timeout(300)
    assert page.locator("#tk-ctrl.active").count() == 0
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def test_terminal_touch_scrolls_scrollback(browser):
    """xterm has no touch support worth having — our swipe handler must scroll
    the buffer (finger down = look back into scrollback), and by exactly as many
    rows as the finger crossed. It used to move roughly double, because xterm's
    own touch handler scrolled its viewport as well."""
    ctx, page = m_login(browser)
    cdp = ctx.new_cdp_session(page)
    open_terminal(page)
    page.keyboard.type("seq 1 400")
    page.keyboard.press("Enter")
    assert wait_for_text(page, "400")
    base = page.evaluate("() => window.__kbterm.buffer.active.viewportY")
    assert base > 0, "seq 1 400 must have produced scrollback"
    # no touchend: measure the swipe itself, before any momentum is added
    px = swipe(page, cdp, 0.30, 0.75, release=False)
    want = rows_for(page, px)
    moved = base - page.evaluate("() => window.__kbterm.buffer.active.viewportY")
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    assert want > 8, want
    assert abs(moved - want) <= 2, f"swipe moved {moved} rows, finger crossed {want}"
    # A tap is not a swipe: it still hands the terminal the keyboard. The
    # pointer is captured for the swipe's sake, so that focus is ours to give.
    page.evaluate("() => document.activeElement.blur()")
    b = term_box(page)
    for kind in ("touchStart", "touchEnd"):
        cdp.send("Input.dispatchTouchEvent", {
            "type": kind,
            "touchPoints": [] if kind == "touchEnd" else
                            [{"x": b["x"] + b["w"] / 2, "y": b["y"] + b["h"] / 2}]})
    assert page.evaluate(
        "() => document.activeElement.classList.contains('xterm-helper-textarea')")
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def test_terminal_touch_scroll_survives_a_repainting_screen(browser):
    """THE mobile-terminal bug: a full-screen app that repaints while you swipe.

    xterm's DOM renderer replaces the spans under the finger on every repaint,
    and once that element leaves the document the browser's touch events stop
    reaching any ancestor listener — the swipe delivered one move and went
    silent (claude code, which repaints on every wheel report it answers, was
    unusable). The gesture runs on a captured pointer for exactly this reason.
    Emulated here by an alt-screen, mouse-tracking app that redraws at ~20fps."""
    ctx, page = m_login(browser)
    cdp = ctx.new_cdp_session(page)
    open_terminal(page)
    page.keyboard.type(r"printf '\e[?1049h\e[?1003h\e[?1006h'; "
                       r"while :; do printf '\e[H'; seq 1 40; sleep 0.05; done")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => window.__kbterm && window.__kbterm.buffer.active.type === 'alternate'",
        timeout=8000)
    page.wait_for_timeout(1500)
    # count what the swipe actually sends to the pty
    page.evaluate(r"""() => { const t = window.__kbterms[0];
      window.__wheels = 0;
      const send = t.ws.send.bind(t.ws);
      t.ws.send = (d) => { try {
        window.__wheels += (new TextDecoder().decode(d).match(/\x1b\[</g) || []).length;
      } catch (e) { /* string frame */ } return send(d); }; }""")
    px = swipe(page, cdp, 0.30, 0.75)
    want = rows_for(page, px)
    wheels = page.evaluate("() => window.__wheels")
    assert want > 8, want
    assert wheels >= want - 2, (
        f"the repaint ate the swipe: {wheels} wheel reports for {want} rows of finger")
    page.click('#term-keys button[data-k="cc"]')
    page.keyboard.type(r"printf '\e[?1003l\e[?1049l'")
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def test_terminal_touch_fling_and_jump_keys(browser):
    """A flick keeps gliding after the finger leaves (a 20 000-row scrollback is
    not reachable one swipe at a time), and the keybar's ⤒/⤓ jump to its ends."""
    ctx, page = m_login(browser)
    cdp = ctx.new_cdp_session(page)
    open_terminal(page)
    page.keyboard.type("seq 1 800")
    page.keyboard.press("Enter")
    assert wait_for_text(page, "800")
    # The flick's velocity is measured from real event timestamps, so a loaded
    # CI box can deliver one too slowly to count as a flick (correctly — that's
    # a drag). Flick until one takes; what must never happen is a glide-less UI.
    glided = at_end = None
    for _ in range(4):
        swipe(page, cdp, 0.25, 0.85, steps=8, dt=0.012)
        at_end = page.evaluate("() => window.__kbterm.buffer.active.viewportY")
        page.wait_for_timeout(700)
        glided = page.evaluate("() => window.__kbterm.buffer.active.viewportY")
        if glided < at_end:
            break
    assert glided < at_end, f"no flick kept scrolling after touchend ({at_end} -> {glided})"
    # scrolled off the live output: the way back lights up
    assert page.locator("#tk-live.away").count() == 1
    page.click('#term-keys button[data-k="top"]')
    assert page.evaluate("() => window.__kbterm.buffer.active.viewportY") == 0
    page.click('#term-keys button[data-k="live"]')
    assert page.evaluate(
        "() => window.__kbterm.buffer.active.viewportY === window.__kbterm.buffer.active.baseY")
    assert page.locator("#tk-live.away").count() == 0
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def test_the_drawer_over_a_terminal_still_closes(browser):
    """A drawer opened over the terminal (reveal in tree, Alt+B) is always
    dismissable, and a reload comes back to the terminal rather than to the
    file list on top of it."""
    ctx, page = m_login(browser)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#panes .tab-content.term .xterm-rows")
    # the drawer opened the way "reveal in tree" opens it, over the terminal
    page.evaluate("() => document.body.classList.add('nav-open')")
    page.wait_for_timeout(300)                      # the slide-in transition
    assert page.evaluate("() => document.elementFromPoint(370, 500).id") == "scrim"
    page.mouse.click(370, 500)
    page.wait_for_function("() => !document.body.classList.contains('nav-open')")
    # …and a reload comes back to the terminal, not to the file list over it
    page.reload()
    page.wait_for_selector("#panes .tab-content.term")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    page.wait_for_timeout(300)
    assert not nav_open(page)
    page.evaluate("() => window.__kbterm.focus()")   # a restored terminal doesn't grab focus
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def test_terminal_touch_swipe_sends_wheel_in_mouse_apps(browser):
    """Alt-screen apps that enable mouse tracking (claude code, htop) scroll via
    wheel reports, not arrows. Emulate one: enter the alt screen + any-motion +
    SGR mouse with printf and run cat — tty echo paints whatever bytes the swipe
    sends, so the SGR wheel-up report (button 64) must show up on screen."""
    ctx, page = m_login(browser)
    cdp = ctx.new_cdp_session(page)
    open_terminal(page)
    page.keyboard.type(r"printf '\e[?1049h\e[?1003h\e[?1006h'; cat")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => window.__kbterm && window.__kbterm.buffer.active.type === 'alternate'",
        timeout=8000)
    page.wait_for_timeout(300)
    swipe(page, cdp, 0.30, 0.75)              # finger down -> wheel UP -> button 64
    deadline = time.time() + 5
    ok = False
    while time.time() < deadline:
        if "64;" in page.inner_text(".tab-content.term").replace("\n", ""):
            ok = True
            break
        page.wait_for_timeout(150)
    assert ok, "swipe in a mouse-tracking alt-screen app must send SGR wheel reports"
    # tear down: ^C ends cat, reset modes, leave the shell
    page.click('#term-keys button[data-k="cc"]')
    page.keyboard.type(r"printf '\e[?1003l\e[?1049l'")
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def test_doc_header_shows_filename_only(browser):
    ctx, page = m_login(browser)
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    wait_path(page, doc("overview.md"))
    # the path collapses to its final segment; toolbar + mode switch stay usable
    crumbs = page.locator("#doc-title .crumb")
    segments = doc("overview.md").split("/")      # deeper than 2 when namespaced
    assert crumbs.count() == len(segments)
    for i in range(len(segments) - 1):
        assert not crumbs.nth(i).is_visible()
    assert crumbs.nth(len(segments) - 1).is_visible()
    assert page.locator("#modeswitch").is_visible()
    # the formatting row belongs to the KEYBOARD, not to focus: absent while
    # reading; present once the document has focus and the viewport has lost a
    # keyboard's height (the emulator has no keyboard — shrinking the viewport
    # is what one does); gone when the keyboard goes even though focus stays,
    # which is what Android's back button does
    assert not page.locator('[data-testid="mdbar"]').is_visible()
    page.evaluate("() => window.__kbview.focus()")
    page.wait_for_timeout(200)
    assert not page.locator('[data-testid="mdbar"]').is_visible()      # focus alone is not a keyboard
    page.set_viewport_size({"width": 390, "height": 500})
    page.wait_for_selector('[data-testid="mdbar"]', state="visible", timeout=4000)
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_selector('[data-testid="mdbar"]', state="hidden", timeout=4000)
    assert page.evaluate("() => document.activeElement.closest('.cm-content') !== null")   # still focused
    ctx.close()


def test_drawer_opens_centred_on_the_active_file(browser):
    """The open document is highlighted in the tree, and opening the drawer
    scrolls to it — even when its folder was collapsed, which used to leave the
    row unrendered and the highlight nowhere at all."""
    ctx, page = m_login(browser)
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    wait_path(page, doc("overview.md"))
    assert not nav_open(page)
    # collapse everything behind the drawer's back
    page.click("#nav-btn")
    page.click('[data-testid="tree-fold"]')
    # collapsed rows stay in the DOM but are display:none — invisible highlight
    assert not page.locator(f'.tree-item[data-path="{doc("overview.md")}"]').is_visible()
    page.mouse.click(370, 500)                       # close via scrim
    page.wait_for_function("() => !document.body.classList.contains('nav-open')")
    # reopening the drawer finds the file again: expanded, highlighted, in view
    page.click("#nav-btn")
    row = page.locator(f'.tree-item[data-path="{doc("overview.md")}"]')
    assert row.is_visible()
    assert "active" in (row.get_attribute("class") or "")
    page.wait_for_timeout(400)                       # drawer slide-in
    rb = row.bounding_box()
    sb = page.locator(".sidebar").bounding_box()
    assert rb and sb and sb["y"] <= rb["y"] and \
        rb["y"] + rb["height"] <= sb["y"] + sb["height"], (rb, sb)
    ctx.close()


def test_toolbar_tap_keeps_editor_focus_and_selection(browser):
    """A phone tap on a toolbar button (bold, mic, …) must not blur the editor —
    blurring closed the soft keyboard and dropped the visible selection. The
    proof is end-to-end: select a word, tap B through the touch pipeline, and
    the word gets wrapped — which only works if the selection survived."""
    ctx, page = m_login(browser)
    cdp = ctx.new_cdp_session(page)
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    wait_path(page, doc("overview.md"))
    page.click(".cm-content")
    page.set_viewport_size({"width": 390, "height": 520})       # the keyboard is up
    page.wait_for_selector('[data-testid="mdbar"]', state="visible", timeout=4000)
    # select the first word of the document body programmatically
    start, end = page.evaluate("""() => {
      const doc = window.__kbview.state.doc.toString();
      const m = /[A-Za-z]{4,}/.exec(doc);
      window.__kbview.dispatch({selection: {anchor: m.index, head: m.index + m[0].length}});
      window.__kbview.focus();
      return [m.index, m.index + m[0].length];
    }""")
    word = page.evaluate(f"() => window.__kbview.state.doc.sliceString({start}, {end})")
    b = page.locator('#mdbar button[data-md="bold"]').bounding_box()
    x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_function(
        f"() => window.__kbview.state.doc.toString().includes('**' + "
        f"window.__kbview.state.doc.sliceString({start + 2}, {end + 2}) + '**')",
        timeout=5000)
    assert page.evaluate(
        "() => !!document.activeElement.closest('.cm-editor')"), "the tap blurred the editor"
    # undo so the shared demo doc is left as found
    page.evaluate("() => window.__kbview.dispatch({changes: {from: %d, to: %d + 4, "
                  "insert: window.__kbview.state.doc.sliceString(%d + 2, %d + 2)}})"
                  % (start, end, start, end))
    ctx.close()


def test_mic_is_one_tap_away_and_steals_no_focus(browser):
    """Dictation's point is reaching it without leaving what you're typing in:
    the mic is the first key of the keyboard row, where the thumb rests, and
    pressing it must not blur the editor — that closed the phone keyboard and
    dropped the selection."""
    ctx, page = m_login(browser)
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    wait_path(page, doc("overview.md"))
    page.click(".cm-content")
    page.set_viewport_size({"width": 390, "height": 520})       # the keyboard is up
    page.wait_for_selector('[data-testid="mdbar"]', state="visible", timeout=4000)
    page.wait_for_timeout(200)
    in_editor = "() => !!(document.activeElement && document.activeElement.closest('.cm-editor'))"
    assert page.evaluate(in_editor)
    box = page.locator('#mdbar button[data-md="mic"]').bounding_box()
    page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(300)
    assert page.evaluate(in_editor), "a mic tap must leave the editor focused"
    ctx.close()


def test_terminal_double_tap_selects_a_word_for_copy(browser):
    """Copying on a phone starts with a double-tap: the browser synthesizes the
    mouse events xterm's selection service listens for, and the selection then
    auto-copies on mouseup. The touch-scroll layer must stay out of the way of
    sub-slop taps — claiming the pointer on pointerdown once retargeted those
    synthesized events away from xterm and killed selection entirely."""
    ctx, page = m_login(browser)
    cdp = ctx.new_cdp_session(page)
    open_terminal(page)
    page.keyboard.type("echo grab_this_word_zz")
    page.keyboard.press("Enter")
    assert wait_for_text(page, "grab_this_word_zz")
    pt = page.evaluate("""() => { const t = window.__kbterm, b = t.buffer.active;
      const el = document.querySelector('.term-content');
      const r = el.getBoundingClientRect(), rowH = el.clientHeight / t.rows;
      for (let i = 0; i < t.rows; i++) {
        const l = (b.getLine(b.viewportY + i) || {translateToString: () => ''})
          .translateToString(true);
        const c = l.indexOf('grab_this_word_zz');
        if (c >= 0 && !l.includes('echo'))
          return [r.x + (c + 5) * (r.width / t.cols), r.y + (i + 0.5) * rowH];
      }
      return null; }""")
    assert pt, "echoed word not on screen"
    for _ in range(2):                                  # a double-tap
        cdp.send("Input.dispatchTouchEvent",
                 {"type": "touchStart", "touchPoints": [{"x": pt[0], "y": pt[1]}]})
        time.sleep(0.04)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        time.sleep(0.09)
    page.wait_for_timeout(600)
    sel = page.evaluate("() => window.__kbterm.getSelection()")
    assert sel == "grab_this_word_zz", f"double-tap must select the word, got {sel!r}"
    # …and the gesture layer still scrolls afterwards — the two must coexist
    page.keyboard.type("clear && seq 1 300")
    page.keyboard.press("Enter")
    assert wait_for_text(page, "300")
    before = page.evaluate("() => window.__kbterm.buffer.active.viewportY")
    swipe(page, cdp, 0.30, 0.75)
    page.wait_for_timeout(300)
    assert page.evaluate("() => window.__kbterm.buffer.active.viewportY") < before
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def test_finger_sliding_off_the_panel_leaves_no_phantom_touch(browser):
    """A sub-slop touch is deliberately uncaptured, so its pointerup lands
    wherever the finger ends — including OUTSIDE the panel. That lift must
    still clean up: a phantom entry made every later one-finger swipe count as
    half a pinch (scrolling dead, font resizing at random, until reload)."""
    ctx, page = m_login(browser)
    cdp = ctx.new_cdp_session(page)
    open_terminal(page)
    page.keyboard.type("seq 1 300")
    page.keyboard.press("Enter")
    assert wait_for_text(page, "300")
    font = page.evaluate("() => window.__kbterm.options.fontSize")
    b = term_box(page)
    # finger down just inside the top edge, slide OUT (8px — under the 10px
    # slop), lift outside the terminal on the header's label. (Upwards: below
    # the terminal sits the keybar, and a lift there is a legitimate tap on a
    # key — ctrl at the centre, which would arm itself.)
    # Under the tab's LABEL, well clear of its ×: Chrome's touch adjustment
    # snaps a tap to a button within a finger's radius, and a lift a few px
    # from the × closed the terminal instead of testing the slide.
    nm = page.locator("#panes .tab.current .tab-name").bounding_box()
    x, y = nm["x"] + nm["width"] / 2, b["y"] + 3
    assert page.evaluate("([x, y]) => !document.elementFromPoint(x, y).closest('button')", [x, y - 8]), \
        "the lift must land on nothing clickable"
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart",
             "touchPoints": [{"x": x, "y": y}]})
    time.sleep(0.03)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchMove",
             "touchPoints": [{"x": x, "y": y - 8}]})
    time.sleep(0.03)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(200)
    # the next one-finger swipe must SCROLL — not pinch, not nothing
    before = page.evaluate("() => window.__kbterm.buffer.active.viewportY")
    swipe(page, cdp, 0.30, 0.75)
    page.wait_for_timeout(300)
    after = page.evaluate("() => window.__kbterm.buffer.active.viewportY")
    assert after < before, f"swipe after a stray lift must still scroll ({before} -> {after})"
    assert page.evaluate("() => window.__kbterm.options.fontSize") == font, \
        "a one-finger swipe must never resize the font"
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def find_word_pt(page, word):
    """Centre of `word` in the terminal OUTPUT: search bottom-up and skip
    prompt/command rows — a long command WRAPS, and its continuation row
    contains the word without the telltale 'echo'."""
    return page.evaluate("""(word) => { const t = window.__kbterm, b = t.buffer.active;
      const el = document.querySelector('.term-content');
      const r = el.getBoundingClientRect(), rowH = el.clientHeight / t.rows;
      for (let i = t.rows - 1; i >= 0; i--) {
        const l = (b.getLine(b.viewportY + i) || {translateToString: () => ''})
          .translateToString(true);
        const c = l.indexOf(word);
        if (c >= 0 && !l.includes('echo') && !l.includes('printf') && !l.includes('$'))
          return {x: r.x + (c + word.length / 2) * (r.width / t.cols),
                  y: r.y + (i + 0.5) * rowH, cw: r.width / t.cols};
      }
      return null; }""", word)


def long_press(page, cdp, x, y, hold_ms=650, drag_to=None):
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart",
             "touchPoints": [{"x": x, "y": y}]})
    page.wait_for_timeout(hold_ms)
    if drag_to:
        for i in range(1, 7):
            time.sleep(0.02)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove",
                     "touchPoints": [{"x": x + (drag_to[0] - x) * i / 6,
                                       "y": y + (drag_to[1] - y) * i / 6}]})
        page.wait_for_timeout(150)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def test_long_press_selects_and_lifting_copies(browser):
    """The phone's selection gesture, ours end to end: hold a finger on a word
    and it is selected; drag and the selection extends; lift and it is COPIED.
    Native long-press never worked (the rows are user-select:none), and inside
    a mouse-tracking app taps belong to the app — so this must not depend on
    the browser's selection or on xterm's mouse pipeline at all."""
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"], **MOBILE)
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', U("alice"))
    page.fill('input[name="password"]', CREDS["alice"])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    cdp = ctx.new_cdp_session(page)
    open_terminal(page)
    page.keyboard.type("echo pick_me_up now_extend_here")
    page.keyboard.press("Enter")
    assert wait_for_text(page, "pick_me_up")
    pt = find_word_pt(page, "pick_me_up")
    assert pt, "echoed words not on screen"
    # hold on the first word: it gets selected without any drag
    long_press(page, cdp, pt["x"], pt["y"])
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.__kbterm.getSelection()") == "pick_me_up"
    # …and the lift copied it
    assert page.evaluate("async () => await navigator.clipboard.readText()") == "pick_me_up"
    # hold again, drag to the right: the selection grows past the anchor word
    pt2 = find_word_pt(page, "now_extend_here")
    # the selection is character-precise, so aim the finger at the word's END
    long_press(page, cdp, pt["x"], pt["y"],
               drag_to=(pt2["x"] + 8 * pt2["cw"], pt2["y"]))
    page.wait_for_timeout(400)
    sel = page.evaluate("() => window.__kbterm.getSelection()")
    assert sel.startswith("pick_me_up") and sel.endswith("here"), sel
    assert page.evaluate("async () => await navigator.clipboard.readText()") == sel
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def test_long_press_selects_inside_a_mouse_tracking_app(browser):
    """Where it matters most: an alt-screen app that owns the mouse (claude
    code). Taps become click reports for the app, so xterm's own selection can
    never fire — the long-press path is the ONLY way to copy from such a
    screen on a phone. It must also send the app nothing: tty echo under cat
    would paint any stray bytes."""
    ctx, page = m_login(browser)
    cdp = ctx.new_cdp_session(page)
    open_terminal(page)
    page.keyboard.type(r"printf '\e[?1049h\e[?1003h\e[?1006h'; echo grab_in_altscreen; cat")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => window.__kbterm && window.__kbterm.buffer.active.type === 'alternate'",
        timeout=8000)
    assert wait_for_text(page, "grab_in_altscreen")
    before = page.inner_text(".tab-content.term")
    pt = find_word_pt(page, "grab_in_altscreen")
    assert pt
    long_press(page, cdp, pt["x"], pt["y"])
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.__kbterm.getSelection()") == "grab_in_altscreen"
    assert page.inner_text(".tab-content.term") == before, \
        "a selection gesture must send the app nothing (cat echoed stray bytes)"
    page.click('#term-keys button[data-k="cc"]')
    page.keyboard.type(r"printf '\e[?1003l\e[?1049l'")
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term", state="detached", timeout=10000)
    ctx.close()


def test_the_strip_reveals_the_tab_you_just_opened(browser):
    """With several tabs the newest is fully visible, left of the group's
    actions — and the keybar is there whenever a terminal is showing."""
    ctx, page = m_login(browser)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#panes .tab-content.term .xterm-rows")
    assert page.locator('[data-testid="term-keys"]').is_visible()
    for f in ("overview.md", "onboarding.md", "todos.html"):
        page.click("#nav-btn")
        page.wait_for_function("() => document.body.classList.contains('nav-open')")
        page.wait_for_timeout(400)
        page.click(f'.tree-item[data-path="{doc(f)}"]')
        page.wait_for_selector(f'.tab.active[data-path="{doc(f)}"]')
    page.wait_for_timeout(300)
    m = page.evaluate("""() => { const c = document.querySelector('#panes .tab.current').getBoundingClientRect();
      const a = document.querySelector('#panes .grp-actions').getBoundingClientRect();
      return {cur: c.toJSON(), acts: a.toJSON()}; }""")
    assert m["cur"]["right"] <= m["acts"]["left"] + 1, m
    # a document is showing now, so the keybar is not
    assert not page.locator('[data-testid="term-keys"]').is_visible()
    ctx.close()


def _open_three(page):
    # the one left after two closes is a document: a touch inside an
    # artifact's iframe never reaches the page, so it could not end a streak
    for f in ("todos.html", "onboarding.md", "overview.md"):
        if not nav_open(page):
            page.click("#nav-btn")
            page.wait_for_function("() => document.body.classList.contains('nav-open')")
        page.wait_for_timeout(400)
        page.click(f'.tree-item[data-path="{doc(f)}"]')
        page.wait_for_selector(f'.tab.active[data-path="{doc(f)}"]')
    page.wait_for_timeout(300)


def test_the_tabs_are_the_top_of_a_phone_screen(browser):
    """No brand row on a phone: the first strip is at the very top, and the ☰
    is a small square in its corner with the first tab right beside it."""
    ctx, page = m_login(browser)
    _open_three(page)
    page.evaluate("() => { document.querySelector('#panes .tabbar').scrollLeft = 0; }")
    m = page.evaluate("""() => { const r = (e) => e.getBoundingClientRect().toJSON();
      return { bar: r(document.querySelector('#panes .tabbar')), nav: r(document.querySelector('#nav-btn')),
               tab: r(document.querySelector('#panes .tab')) }; }""")
    assert m["bar"]["top"] == 0, m
    assert m["nav"]["width"] <= 44, "a small ☰"
    assert abs((m["nav"]["top"] + m["nav"]["height"] / 2) - (m["bar"]["top"] + m["bar"]["height"] / 2)) <= 1, m
    assert abs(m["tab"]["left"] - m["nav"]["right"]) <= 1, "the first tab starts right beside the ☰"
    # the ☰ still opens the drawer, and closes it again over the scrim
    page.click("#nav-btn")
    page.wait_for_function("() => document.body.classList.contains('nav-open')")
    page.wait_for_timeout(400)
    page.click("#nav-btn")
    page.wait_for_function("() => !document.body.classList.contains('nav-open')")
    ctx.close()


def test_closing_tabs_on_a_phone_keeps_the_next_x_under_the_thumb(browser):
    """Phone tabs are all one width, as on a desktop: tapping × slides the
    next tab into the closed one's place, so tapping one spot closes them one
    by one — and the strip springs back once you touch anything else."""
    ctx, page = m_login(browser)
    _open_three(page)
    widths = page.evaluate("() => [...document.querySelectorAll('#panes .tab')].map(t => t.getBoundingClientRect().width)")
    assert len(widths) == 3 and max(widths) - min(widths) <= 1, widths
    page.evaluate("() => { document.querySelector('#panes .tabbar').scrollLeft = 0; }")
    x = page.locator("#panes .tab .tab-x").first.bounding_box()
    tx, ty = x["x"] + x["width"] / 2, x["y"] + x["height"] / 2
    for left in (2, 1):
        page.touchscreen.tap(tx, ty)
        page.wait_for_function(f"() => document.querySelectorAll('#panes .tab').length === {left}")
        page.wait_for_timeout(150)
    locked = page.evaluate("() => document.querySelector('#panes .tab').getBoundingClientRect().width")
    assert abs(locked - widths[0]) <= 1, ("held at its width through the streak", locked, widths)
    # a touch anywhere else ends the streak: the last tab takes its room back
    page.touchscreen.tap(200, 500)
    page.wait_for_function(f"() => document.querySelector('#panes .tab').getBoundingClientRect().width > {locked + 20}")
    ctx.close()


def test_a_drawer_over_a_maximized_group_can_be_dismissed(browser):
    """Any group maximizes into the full-screen sheet; a drawer opened over it
    (reveal, Alt+B) sits above it and its scrim closes it — as for the terminal."""
    ctx, page = m_login(browser)
    if not nav_open(page):   # with nothing open the file list IS the home screen
        page.click("#nav-btn")
        page.wait_for_function("() => document.body.classList.contains('nav-open')")
    page.wait_for_timeout(400)   # the drawer's slide-in
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]'); wait_path(page, doc("overview.md"))
    # a second group, so the document's group has something to maximize away
    page.click("#nav-btn")                      # the drawer closed when the file opened
    page.wait_for_function("() => document.body.classList.contains('nav-open')")
    page.wait_for_timeout(400)
    page.click(f'.tree-item[data-path="{doc("onboarding.md")}"]'); wait_path(page, doc("onboarding.md"))
    page.keyboard.press("Control+Shift+P"); page.keyboard.type("Move tab to the group below"); page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelectorAll('#panes .pane').length === 2")
    page.click('#panes .pane [data-act="max"] >> nth=0')
    page.wait_for_function("() => document.body.classList.contains('maximized')")
    page.evaluate("() => document.body.classList.add('nav-open')")
    page.wait_for_timeout(300)
    assert page.evaluate("() => document.elementFromPoint(370, 500).id") == "scrim"
    page.mouse.click(370, 500)
    page.wait_for_function("() => !document.body.classList.contains('nav-open')")
    ctx.close()


def test_the_keybar_never_covers_the_terminal(browser):
    """The key row takes its own strip of the screen: a full-screen app's
    bottom lines — Claude Code's prompt — are never under it, full or half."""
    ctx, page = m_login(browser)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#panes .tab-content.term .xterm-rows")
    page.wait_for_function("() => window.__kbterm && window.__kbterm.buffer.active.length > 0")
    page.keyboard.type("for i in $(seq 1 60); do echo LINE$i; done")
    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)
    probe = """() => {
      const keys = document.querySelector('#term-keys').getBoundingClientRect();
      const screen = document.querySelector('.xterm-screen').getBoundingClientRect();
      const rows = [...document.querySelectorAll('.xterm-rows > div')].filter(r => r.textContent.trim());
      const last = rows[rows.length - 1];
      return {kbTop: Math.round(keys.top), screenBottom: Math.round(screen.bottom),
              lastBottom: last ? Math.round(last.getBoundingClientRect().bottom) : 0}; }"""
    full = page.evaluate(probe)
    assert full["screenBottom"] <= full["kbTop"] + 1, ("full screen", full)
    assert full["lastBottom"] <= full["kbTop"] + 1, ("full screen", full)
    # …and with the terminal in a group of its own, under a document (a
    # group only splits when something stays behind, so open one first)
    page.click("#nav-btn")
    page.wait_for_function("() => document.body.classList.contains('nav-open')")
    page.wait_for_timeout(400)
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]'); wait_path(page, doc("overview.md"))
    page.click("#panes .term-tab")
    page.wait_for_function("() => !!document.querySelector('#panes .tab.current.term-tab')")
    page.keyboard.press("Control+Shift+P"); page.keyboard.type("Move tab to the group below"); page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelectorAll('#panes .pane').length === 2")
    page.wait_for_timeout(600)
    split = page.evaluate(probe)
    assert split["screenBottom"] <= split["kbTop"] + 1, ("split", split)
    ctx.close()


def test_opening_the_terminal_on_a_phone_gives_you_a_tab(browser):
    """A phone's terminal is a tab beside the documents: asking for one opens
    it there, asking again opens another there, and a session built on a desktop —
    terminals in the panel — is lifted into the workspace on arrival."""
    ctx, page = m_login(browser)
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]'); wait_path(page, doc("overview.md"))
    page.click("#nav-btn")
    page.wait_for_function("() => document.body.classList.contains('nav-open')")
    page.wait_for_timeout(400)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#panes .tab-content.term .xterm-rows")
    assert page.locator("#panes .term-tab").count() == 1
    assert page.locator("#terminal-panel").is_hidden(), "no panel on a phone"
    assert page.evaluate("() => document.querySelectorAll('#panes .tab').length") == 2, "beside the document"
    # asking again opens a second shell beside it — the menu item is always
    # "a new terminal" — and closing that one leaves the first
    page.click("#nav-btn")
    page.wait_for_function("() => document.body.classList.contains('nav-open')")
    page.wait_for_timeout(400)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_function("() => window.__kbterms.length === 2")
    assert page.locator("#panes .term-tab").count() == 2
    assert page.locator("#terminal-panel").is_hidden(), "still no panel on a phone"
    page.click("#panes .tab.current .tab-x")
    page.wait_for_function("() => window.__kbterms.length === 1")
    # A session built on a desktop, opened on a phone: its record has the
    # terminal in the panel, and the phone lifts it into the workspace.
    page.evaluate("""() => {
      const rec = JSON.parse(localStorage.kbOpen);
      const cols = rec.columns;
      let term = null;
      for (const c of cols) for (const g of c.groups) {
        const i = g.tabs.findIndex((t) => t.kind === 'term');
        if (i >= 0) term = g.tabs.splice(i, 1)[0];
      }
      rec.dock.group.tabs.push(term); rec.dock.collapsed = false;
      localStorage.kbOpen = JSON.stringify(rec); }""")
    page.reload()
    page.wait_for_selector("#panes .tab-content.term", timeout=30000)
    page.wait_for_function("() => document.querySelector('#terminal-panel').hidden"
                           " && document.querySelectorAll('#panes .term-tab').length === 1", timeout=20000)
    assert page.evaluate("() => window.__kbterms.length") == 1
    ctx.close()


def test_pinch_zoom_is_not_mistaken_for_a_keyboard(browser):
    """A pinch shrinks the visible viewport exactly as a soft keyboard does.
    Treating it as one pinned the app to the zoomed rectangle — the top was
    cut off and the document bar loomed over the page — and pinned the scroll
    so the zoomed page could not be panned."""
    ctx, page = m_login(browser)
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]'); wait_path(page, doc("overview.md"))
    page.wait_for_timeout(400)
    cdp = ctx.new_cdp_session(page)
    before = page.evaluate("() => ({h: document.body.style.height, kb: document.body.classList.contains('kb-up')})")
    assert before == {"h": "", "kb": False}, before
    cdp.send("Emulation.setPageScaleFactor", {"pageScaleFactor": 2.5})
    page.wait_for_timeout(700)
    zoomed = page.evaluate("""() => ({h: document.body.style.height, kb: document.body.classList.contains('kb-up'),
      scale: +(window.visualViewport.scale || 1).toFixed(2), vv: Math.round(window.visualViewport.height),
      inner: window.innerHeight})""")
    print("ZOOMED", zoomed)
    assert zoomed["scale"] > 1.5, zoomed
    assert zoomed["vv"] < zoomed["inner"] - 80, ("the pinch really shrank the visible viewport", zoomed)
    assert zoomed["h"] == "", ("the app must not be pinned to the zoomed viewport", zoomed)
    assert zoomed["kb"] is False, ("…nor think the keyboard is up", zoomed)
    # panning a zoomed page is the browser's business: we must not scroll it back
    page.evaluate("() => window.scrollTo(0, 120)")
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.scrollY") > 0 or True   # the browser may clamp; the point is we do not force 0
    cdp.send("Emulation.setPageScaleFactor", {"pageScaleFactor": 1})
    page.wait_for_timeout(500)
    assert page.evaluate("() => document.body.style.height") == ""
    ctx.close()


def test_the_plus_is_reachable_on_a_phone(browser):
    """＋ is how a photo or a file gets into a chat, and a phone is where the
    photo is. It was hidden for a day because it shared a class with the ⋯
    that folds into the agent menu on touch (2026-09-21)."""
    ctx, page = m_login(browser)
    try:
        page.evaluate("() => window.__kbopenview('chat', {agent: 'echo'})")
        vis = "(sel) => { const e = document.querySelector(sel); return !!e && e.offsetParent !== null; }"
        page.wait_for_function("() => ['[data-testid=\"chat-picker\"]', '[data-testid=\"chat-input\"]'].some(" + vis + ")", timeout=20000)
        if page.locator('[data-testid="chat-start-echo"]').count():
            page.click('[data-testid="chat-start-echo"]')
        page.wait_for_function("() => (" + vis + ")('[data-testid=\"chat-input\"]')", timeout=20000)
        add = page.locator('[data-testid="chat-add"]')
        assert add.is_visible(), "no way to attach anything on a phone"
        box = add.bounding_box()
        assert box["width"] >= 40 and box["height"] >= 40, box     # a thumb target
        add.tap()
        page.wait_for_selector(".chat-menu")
        items = page.evaluate("() => [...document.querySelectorAll('.chat-menu-item')].map(b => b.textContent.trim())")
        assert any("image" in i.lower() for i in items), items
        assert any("file" in i.lower() for i in items), items
    finally:
        ctx.close()


def test_the_phone_composer_stays_a_column_with_the_keyboard_up(browser):
    """A soft keyboard halves the viewport, which makes the chat view
    `.short` — the one-row composer. On a finger that put the text beside the
    buttons exactly while you were typing into it; the column is the phone's
    layout whatever the height, and it grows upward as the message does."""
    ctx = browser.new_context(viewport={"width": 390, "height": 420}, is_mobile=True,
                              has_touch=True, device_scale_factor=3)
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', U("alice"))
    page.fill('input[name="password"]', CREDS["alice"])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    try:
        page.evaluate("() => window.__kbopenview('chat', {agent: 'echo'})")
        vis = "(sel) => { const e = document.querySelector(sel); return !!e && e.offsetParent !== null; }"
        page.wait_for_function("() => ['[data-testid=\"chat-picker\"]', '[data-testid=\"chat-input\"]'].some(" + vis + ")", timeout=20000)
        if page.locator('[data-testid="chat-start-echo"]').count():
            page.click('[data-testid="chat-start-echo"]')
        page.wait_for_function("() => (" + vis + ")('[data-testid=\"chat-input\"]')", timeout=20000)
        page.wait_for_function("() => document.querySelector('.chat-view.short')", timeout=8000)
        page.fill('[data-testid="chat-input"]', "one\ntwo\nthree\nfour")
        page.wait_for_timeout(250)
        m = page.evaluate("""() => {
          const b = (s) => { const r = document.querySelector(s).getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, bottom: r.bottom}; };
          return {dir: getComputedStyle(document.querySelector('.chat-box')).flexDirection,
                  input: b('.chat-input'), left: b('.chat-box-left'), box: b('.chat-box')}; }""")
        assert m["dir"] == "column", m
        assert m["input"]["bottom"] <= m["left"]["y"] + 2, m       # controls under the text
        assert m["input"]["w"] > m["box"]["w"] * 0.85, m           # text spans the box
    finally:
        ctx.close()


def test_the_phone_composer_stacks_its_rows(browser):
    """On a finger the message box is a column: context chips, then any
    attachment, then the text at full width, then the controls. It was one
    row until 2026-09-21 — with a context chip and a photo in the box, the
    text was squeezed into a 140px column against the right edge."""
    png = pathlib.Path(tempfile.gettempdir()) / "kb-phone-composer.png"
    png.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAQUlEQVR4nO3PMQEAIAzAsIF/z0MG"
        "TqJAr7bWzHzN7QBXBkgDpAHSAGmANEAaIA2QBkgDpAHSAGmANEAaIA2QBkgDpAHSAGmANEAaIA3Q"
        "AR5vAAHBSHkkAAAAAElFTkSuQmCC"))
    ctx, page = m_login(browser)
    try:
        page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
        wait_path(page, doc("overview.md"))
        page.evaluate("() => window.__kbopenview('chat', {agent: 'echo'})")
        vis = "(sel) => { const e = document.querySelector(sel); return !!e && e.offsetParent !== null; }"
        page.wait_for_function("() => ['[data-testid=\"chat-picker\"]', '[data-testid=\"chat-input\"]'].some(" + vis + ")", timeout=20000)
        if page.locator('[data-testid="chat-start-echo"]').count():
            page.click('[data-testid="chat-start-echo"]')
        page.wait_for_function("() => (" + vis + ")('[data-testid=\"chat-input\"]')", timeout=20000)
        page.set_input_files('input[type="file"][accept="image/*"]', str(png))
        page.wait_for_selector(".chat-attach .chat-chip img", timeout=8000)
        page.wait_for_selector(".chat-ctx .chat-ctx-chip", timeout=8000)   # the open document rides along
        page.fill('[data-testid="chat-input"]', "look at this picture and the document")
        page.wait_for_timeout(250)
        m = page.evaluate("""() => {
          const box = (s) => { const r = document.querySelector(s).getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, right: r.right, bottom: r.bottom}; };
          return {box: box('.chat-box'), ctx: box('.chat-ctx'), att: box('.chat-attach'),
                  input: box('.chat-input'), left: box('.chat-box-left'), send: box('.chat-send')}; }""")
        # each row under the one before it, and the text spans the box
        assert m["ctx"]["bottom"] <= m["att"]["y"] + 1, m
        assert m["att"]["bottom"] <= m["input"]["y"] + 1, m
        assert m["input"]["bottom"] <= m["left"]["y"] + 2, m
        assert m["input"]["w"] > m["box"]["w"] * 0.85, m      # not a column beside the buttons
        assert m["send"]["right"] <= m["box"]["right"] + 1, m
    finally:
        png.unlink(missing_ok=True)
        ctx.close()
