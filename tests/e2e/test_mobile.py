"""Mobile layout (phone viewport, touch): the sidebar is an over-content drawer,
topbar actions collapse into the ⋯ menu, tree actions open via the per-row ⋯
toggle, and modals become full-width bottom sheets. Every desktop feature must
stay reachable with a finger."""
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
    page.wait_for_selector("#terminal-panel:not([hidden])")
    deadline = time.time() + 8
    while time.time() < deadline and "$" not in page.inner_text("#terminal"):
        page.wait_for_timeout(150)


def wait_for_text(page, needle, secs=8):
    deadline = time.time() + secs
    while time.time() < deadline:
        if needle in page.inner_text("#terminal"):
            return True
        page.wait_for_timeout(150)
    return False


def test_drawer_boots_open_and_closes_on_file_open(browser):
    ctx, page = m_login(browser)
    # the file list is the mobile home screen
    assert nav_open(page)
    assert page.locator("#nav-btn").is_visible()
    assert not page.locator("#whoami").is_visible()      # desktop chrome is gone
    assert page.locator(".brand-word").is_visible()             # the name stays on phones
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
    row = page.locator(f'.tree-item[data-path="{AREA}"]')
    row.locator(".tmore").click()
    page.wait_for_selector('[data-testid="ctx-menu"]')
    page.click('.ctx-item:has-text("New file")')
    dlg_fill(page, name)
    page.wait_for_selector(f'.tab.active[data-path="{doc(name)}"]', timeout=8000)
    assert not nav_open(page)                             # creating lands you in the doc
    # delete it again from the tree
    page.click("#nav-btn")
    row = page.locator(f'.tree-item[data-path="{doc(name)}"]')
    row.locator(".tmore").click()
    page.wait_for_selector('[data-testid="ctx-menu"]')
    page.click('.ctx-item.danger:has-text("Delete")')
    dlg_ok(page)
    page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', state="detached", timeout=8000)
    ctx.close()


def test_terminal_open_from_menu(browser):
    ctx, page = m_login(browser)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal-panel:not([hidden])")
    assert not nav_open(page)                             # drawer got out of the way
    # the panel fits the phone viewport
    box = page.locator("#terminal-panel").bounding_box()
    assert box["width"] == 390 and box["y"] + box["height"] <= 844 + 1, box
    page.click("#term-hide")
    ctx.close()


def test_terminal_keybar_sends_keys(browser):
    """Touch keybar: visible on a phone, and its keys really reach the pty —
    ↑ recalls shell history, ^C cancels the recalled line."""
    ctx, page = m_login(browser)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal-panel:not([hidden])")
    bar = page.locator('[data-testid="term-keys"]')
    assert bar.is_visible()

    # a ~48-column phone terminal wraps lines mid-token; strip the wrap
    # newlines before matching (a wrapped row is full, so the join is exact)
    def term_text():
        return page.inner_text("#terminal").replace("\n", "")

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
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
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
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
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
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
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
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
    ctx.close()


def test_drawer_never_traps_a_full_screen_terminal(browser):
    """A full-screen terminal covers the topbar, so a drawer opened over it must
    still be dismissable — and a restored terminal must not get one on top of it
    in the first place."""
    ctx, page = m_login(browser)
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal-panel:not([hidden])")
    assert page.evaluate("() => document.body.classList.contains('term-max')")
    # the drawer opened the way "reveal in tree" opens it, over the terminal
    page.evaluate("() => document.body.classList.add('nav-open')")
    page.wait_for_timeout(300)                      # the slide-in transition
    assert page.evaluate("() => document.elementFromPoint(370, 500).id") == "scrim"
    page.mouse.click(370, 500)
    page.wait_for_function("() => !document.body.classList.contains('nav-open')")
    # …and a reload comes back to the terminal, not to the file list over it
    page.reload()
    page.wait_for_selector("#terminal-panel:not([hidden])")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    page.wait_for_timeout(300)
    assert not nav_open(page)
    page.evaluate("() => window.__kbterm.focus()")   # a restored terminal doesn't grab focus
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
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
        if "64;" in page.inner_text("#terminal").replace("\n", ""):
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
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
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
    the mic sits in the topbar (not behind the user menu), and pressing it must
    not blur the editor — that closed the phone keyboard and dropped the selection."""
    ctx, page = m_login(browser)
    assert page.locator("#mic-btn").is_visible()       # no menu needed
    assert not page.locator('[data-testid="user-menu"]').is_visible()
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    wait_path(page, doc("overview.md"))
    page.click(".cm-content")
    page.wait_for_timeout(200)
    in_editor = "() => !!(document.activeElement && document.activeElement.closest('.cm-editor'))"
    assert page.evaluate(in_editor)
    box = page.locator("#mic-btn").bounding_box()
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
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
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
    # finger down just inside the bottom edge, slide OUT (8px — under the
    # 10px slop), lift outside the panel
    x, y = b["x"] + b["w"] / 2, b["y"] + b["h"] - 3
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart",
             "touchPoints": [{"x": x, "y": y}]})
    time.sleep(0.03)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchMove",
             "touchPoints": [{"x": x, "y": y + 8}]})
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
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
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
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
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
    before = page.inner_text("#terminal")
    pt = find_word_pt(page, "grab_in_altscreen")
    assert pt
    long_press(page, cdp, pt["x"], pt["y"])
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.__kbterm.getSelection()") == "grab_in_altscreen"
    assert page.inner_text("#terminal") == before, \
        "a selection gesture must send the app nothing (cat echoed stray bytes)"
    page.click('#term-keys button[data-k="cc"]')
    page.keyboard.type(r"printf '\e[?1003l\e[?1049l'")
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
    ctx.close()
