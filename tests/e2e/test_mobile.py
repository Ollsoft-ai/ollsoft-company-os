"""Mobile layout (phone viewport, touch): the sidebar is an over-content drawer,
topbar actions collapse into the ⋯ menu, tree actions open via the per-row ⋯
toggle, and modals become full-width bottom sheets. Every desktop feature must
stay reachable with a finger."""
import time

from conftest import BASE, CREDS, dlg_fill, dlg_ok

MOBILE = dict(viewport={"width": 390, "height": 844}, is_mobile=True,
              has_touch=True, device_scale_factor=3)


def m_login(browser, user="alice"):
    ctx = browser.new_context(**MOBILE)
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', user)
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
    page.click("#more-btn")
    page.click('[data-testid="toggle-term"]')
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
    assert not page.locator(".brand-word").is_visible()
    # opening a document dismisses the drawer and shows the editor
    page.click('.tree-item[data-path="company/overview.md"]')
    page.wait_for_function("() => window.__kbview && window.__kbpath === 'company/overview.md'")
    assert not nav_open(page)
    # the hamburger brings the tree back
    page.click("#nav-btn")
    assert nav_open(page)
    # tapping the scrim closes it again
    page.mouse.click(370, 500)
    page.wait_for_function("() => !document.body.classList.contains('nav-open')")
    ctx.close()


def test_topbar_menu_holds_the_actions(browser):
    ctx, page = m_login(browser)
    menu = page.locator("#topbar-actions")
    assert not menu.is_visible()                          # collapsed behind ⋯
    page.click("#more-btn")
    assert menu.is_visible()
    assert page.locator("#cron-btn").is_visible()
    assert page.locator(".logout").is_visible()
    assert "alice" in page.inner_text("#whoami-m")      # identity moved into the menu
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
    row = page.locator('.tree-item[data-path="company"]')
    row.locator(".tmore").click()
    page.wait_for_selector('[data-testid="ctx-menu"]')
    page.click('.ctx-item:has-text("New file")')
    dlg_fill(page, name)
    page.wait_for_selector(f'.tab.active[data-path="company/{name}"]', timeout=8000)
    assert not nav_open(page)                             # creating lands you in the doc
    # delete it again from the tree
    page.click("#nav-btn")
    row = page.locator(f'.tree-item[data-path="company/{name}"]')
    row.locator(".tmore").click()
    page.wait_for_selector('[data-testid="ctx-menu"]')
    page.click('.ctx-item.danger:has-text("Delete")')
    dlg_ok(page)
    page.wait_for_selector(f'.tree-item[data-path="company/{name}"]', state="detached", timeout=8000)
    ctx.close()


def test_terminal_open_from_menu(browser):
    ctx, page = m_login(browser)
    page.click("#more-btn")
    page.click('[data-testid="toggle-term"]')
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
    page.click("#more-btn")
    page.click('[data-testid="toggle-term"]')
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
    page.click("#more-btn")
    page.click('[data-testid="toggle-term"]')
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
    page.click('.tree-item[data-path="company/overview.md"]')
    page.wait_for_function("() => window.__kbview && window.__kbpath === 'company/overview.md'")
    # the path collapses to its final segment; toolbar + mode switch stay usable
    crumbs = page.locator("#doc-title .crumb")
    assert crumbs.count() == 2
    assert not crumbs.nth(0).is_visible()
    assert crumbs.nth(1).is_visible()
    assert page.locator("#modeswitch").is_visible()
    assert page.locator('[data-testid="mdbar"]').is_visible()
    ctx.close()


def test_drawer_opens_centred_on_the_active_file(browser):
    """The open document is highlighted in the tree, and opening the drawer
    scrolls to it — even when its folder was collapsed, which used to leave the
    row unrendered and the highlight nowhere at all."""
    ctx, page = m_login(browser)
    page.click('.tree-item[data-path="company/overview.md"]')
    page.wait_for_function("() => window.__kbview && window.__kbpath === 'company/overview.md'")
    assert not nav_open(page)
    # collapse everything behind the drawer's back
    page.click("#nav-btn")
    page.click('[data-testid="tree-fold"]')
    # collapsed rows stay in the DOM but are display:none — invisible highlight
    assert not page.locator('.tree-item[data-path="company/overview.md"]').is_visible()
    page.mouse.click(370, 500)                       # close via scrim
    page.wait_for_function("() => !document.body.classList.contains('nav-open')")
    # reopening the drawer finds the file again: expanded, highlighted, in view
    page.click("#nav-btn")
    row = page.locator('.tree-item[data-path="company/overview.md"]')
    assert row.is_visible()
    assert "active" in (row.get_attribute("class") or "")
    page.wait_for_timeout(400)                       # drawer slide-in
    rb = row.bounding_box()
    sb = page.locator(".sidebar").bounding_box()
    assert rb and sb and sb["y"] <= rb["y"] and \
        rb["y"] + rb["height"] <= sb["y"] + sb["height"], (rb, sb)
    ctx.close()
