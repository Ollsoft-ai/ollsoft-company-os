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
    """xterm has no touch support of its own — our swipe handler must scroll
    the buffer (finger down = look up into scrollback)."""
    ctx, page = m_login(browser)
    page.click("#more-btn")
    page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal-panel:not([hidden])")
    deadline = time.time() + 8
    while time.time() < deadline and "$" not in page.inner_text("#terminal"):
        page.wait_for_timeout(150)
    page.keyboard.type("seq 1 300")
    page.keyboard.press("Enter")
    deadline = time.time() + 6
    while time.time() < deadline and "300" not in page.inner_text("#terminal"):
        page.wait_for_timeout(150)
    base = page.evaluate("() => window.__kbterm.buffer.active.viewportY")
    assert base > 0, "seq 1 300 must have produced scrollback"
    moved = page.evaluate("""() => {
      const el = document.querySelector('.term-content');
      const mk = (type, y) => new TouchEvent(type, {
        bubbles: true, cancelable: true,
        touches: [new Touch({ identifier: 1, target: el, clientX: 150, clientY: y })] });
      el.dispatchEvent(mk('touchstart', 300));
      el.dispatchEvent(mk('touchmove', 420));   // finger drags DOWN -> view scrolls UP
      return window.__kbterm.buffer.active.viewportY;
    }""")
    assert moved < base, f"swipe must scroll into scrollback (was {base}, now {moved})"
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
    page.click("#more-btn")
    page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal-panel:not([hidden])")
    deadline = time.time() + 8
    while time.time() < deadline and "$" not in page.inner_text("#terminal"):
        page.wait_for_timeout(150)
    page.keyboard.type(r"printf '\e[?1049h\e[?1003h\e[?1006h'; cat")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => window.__kbterm && window.__kbterm.buffer.active.type === 'alternate'",
        timeout=8000)
    page.wait_for_timeout(300)
    page.evaluate("""() => {
      const el = document.querySelector('.term-content');
      const mk = (type, y) => new TouchEvent(type, { bubbles: true, cancelable: true,
        touches: [new Touch({ identifier: 1, target: el, clientX: 150, clientY: y })] });
      el.dispatchEvent(mk('touchstart', 300));
      el.dispatchEvent(mk('touchmove', 420));   // finger down -> wheel UP -> button 64
    }""")
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
