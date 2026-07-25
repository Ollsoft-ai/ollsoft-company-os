"""VS-Code-style UX in a real browser: editor tabs (open/switch/close),
tabbed terminals in a docked panel that actually disappears when closed,
and the cron panel."""
import time

from conftest import dlg_ok, login, open_doc


def test_editor_tabs_open_switch_close(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")

    open_doc(page, "company/overview.md")
    page.wait_for_selector('.tab.active[data-path="company/overview.md"]')
    open_doc(page, "company/onboarding.md")
    page.wait_for_selector('.tab.active[data-path="company/onboarding.md"]')
    assert page.locator("#tabbar .tab").count() == 2

    # switching back re-activates the existing live tab (no re-open)
    page.click('.tab[data-path="company/overview.md"]')
    page.wait_for_function("() => window.__kbpath === 'company/overview.md'")
    assert page.locator(".tab.active").get_attribute("data-path") == "company/overview.md"

    # close the active tab -> neighbour becomes active
    page.hover('.tab[data-path="company/overview.md"]')
    page.click('.tab[data-path="company/overview.md"] .tab-x')
    page.wait_for_function("() => window.__kbpath === 'company/onboarding.md'")
    assert page.locator("#tabbar .tab").count() == 1

    # close the last tab -> empty state, tab bar hides
    page.hover(".tab.active")
    page.click(".tab.active .tab-x")
    page.wait_for_selector("#tabbar", state="hidden")
    assert page.text_content("#doc-title") == "No document open"
    ctx.close()


def test_artifact_opens_as_tab_alongside_doc(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, "company/overview.md")
    page.click('.tree-item[data-path="company/todos.html"]')
    page.wait_for_selector('.tab.active[data-path="company/todos.html"]')
    assert page.locator("#tabbar .tab").count() == 2
    # the doc tab's editor stays mounted (live) while the artifact is active
    assert page.evaluate("() => !!document.querySelector('.cm-editor')")
    page.wait_for_selector("iframe.artifact-frame")
    ctx.close()


def test_terminal_panel_tabs_and_close_reclaims_space(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")

    # open the panel: one terminal appears and the panel is really visible
    page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    assert page.locator("#term-tabs .term-tab").count() == 1

    # a second terminal
    page.click('[data-testid="term-new"]')
    page.wait_for_function(
        "() => document.querySelectorAll('#term-tabs .term-tab').length === 2")

    # kill one via its tab ×
    page.locator("#term-tabs .term-tab").first.hover()
    page.locator("#term-tabs .term-tab .term-x").first.click()
    page.wait_for_function(
        "() => document.querySelectorAll('#term-tabs .term-tab').length === 1")

    # typing `exit` ends the shell; the server closes the ws; the tab retires
    # and — being the last one — the whole panel disappears from the layout.
    page.click("#terminal")
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
    # no overlay residue: the editor area is full-height again
    panel_box = page.locator("#terminal-panel").bounding_box()
    assert panel_box is None, "a hidden panel must not occupy space"
    ctx.close()


def test_terminal_panel_hide_keeps_shell_running(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    page.click("#terminal")
    page.keyboard.type("MARKER=alive_$$; echo start_$MARKER")
    page.keyboard.press("Enter")

    # hide the panel (▾) — the shell must keep running underneath
    page.click('[data-testid="term-hide"]')
    page.wait_for_selector("#terminal-panel", state="hidden")
    # re-open: same terminal, same shell state
    page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    page.click("#terminal")
    page.keyboard.type("echo again_$MARKER")
    page.keyboard.press("Enter")
    deadline = time.time() + 5
    got = False
    while time.time() < deadline:
        if "again_alive_" in page.inner_text("#terminal"):
            got = True
            break
        page.wait_for_timeout(150)
    assert got, "hiding the panel must not kill the shell"
    # clean up the shell
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
    ctx.close()


def test_cron_panel_add_and_remove(browser):
    marker = "kb-e2e-cron-marker"
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click('[data-testid="cron-btn"]')
    page.wait_for_selector('[data-testid="cron-jobs"]')

    page.fill('[data-testid="cron-schedule"]', "*/30 * * * *")
    page.fill('[data-testid="cron-command"]', f"echo {marker} > /dev/null")
    page.click('[data-testid="cron-add"]')
    page.wait_for_selector(f'.cron-cmd[title*="{marker}"]')

    # delete it again (in-app confirm dialog)
    row = page.locator(".cron-row", has=page.locator(f'.cron-cmd[title*="{marker}"]'))
    row.locator(".del-user").click()
    dlg_ok(page)
    page.wait_for_selector(f'.cron-cmd[title*="{marker}"]', state="detached")
    ctx.close()


def _open_terminal(page):
    page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    page.click("#terminal")
    page.wait_for_timeout(800)


def test_terminal_select_copies_to_clipboard(browser):
    """Selecting text in the terminal copies it to the clipboard on its own —
    no Ctrl+C needed (the "select = copy" convention). This is what a user means
    by "copy from the terminal"."""
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = ctx.new_page()
    page.goto("http://127.0.0.1:8300/login")
    import json
    creds = json.load(open("/tmp/kb-test-creds.json"))
    page.fill('input[name="username"]', "alice")
    page.fill('input[name="password"]', creds["alice"])
    page.click('button[type="submit"]')
    page.wait_for_url("http://127.0.0.1:8300/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    _open_terminal(page)
    page.keyboard.type("echo DRAGME_XYZ")
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)
    page.evaluate("() => navigator.clipboard.writeText('__none__')")

    box = page.locator("#terminal .xterm-screen").bounding_box()
    info = page.evaluate("""() => { const t=window.__kbterm,b=t.buffer.active; let row=-1;
      for(let i=0;i<b.length;i++){const l=b.getLine(i); if(l&&l.translateToString(true).includes('DRAGME_XYZ')){row=i;break;}}
      return {row, baseY:b.baseY, rows:t.rows}; }""")
    vrow = info["row"] - info["baseY"]
    rowh = box["height"] / info["rows"]
    y = box["y"] + vrow * rowh + rowh / 2
    page.mouse.move(box["x"] + 4, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.5, y, steps=8)
    page.mouse.up()
    page.wait_for_timeout(400)

    sel = page.evaluate("() => window.__kbterm.getSelection()")
    clip = page.evaluate("() => navigator.clipboard.readText()")
    assert "DRAGME_XYZ" in sel, sel
    assert clip == sel, (clip, sel)
    ctx.close()


def test_terminal_shift_drag_selects_inside_mouse_app(browser):
    """Inside a mouse-tracking app (like claude code) a plain drag is the app's
    to handle — but Shift+drag (Option on Mac) forces a local selection, which
    then auto-copies. Without this you cannot copy from claude code at all."""
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = ctx.new_page()
    page.goto("http://127.0.0.1:8300/login")
    import json
    creds = json.load(open("/tmp/kb-test-creds.json"))
    page.fill('input[name="username"]', "alice")
    page.fill('input[name="password"]', creds["alice"])
    page.click('button[type="submit"]')
    page.wait_for_url("http://127.0.0.1:8300/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    _open_terminal(page)
    # a fake mouse-tracking app: prints a marker, then reads mouse (SGR) via `cat`
    page.keyboard.type(r"printf 'MOUSEAPP_LINE_QQ\n'; printf '\e[?1000h\e[?1006h'; cat")
    page.keyboard.press("Enter")
    page.wait_for_timeout(700)

    box = page.locator("#terminal .xterm-screen").bounding_box()
    info = page.evaluate("""() => { const t=window.__kbterm,b=t.buffer.active; let row=-1;
      for(let i=0;i<b.length;i++){const l=b.getLine(i); if(l&&l.translateToString(true).includes('MOUSEAPP_LINE_QQ')){row=i;break;}}
      return {row, baseY:b.baseY, rows:t.rows}; }""")
    vrow = info["row"] - info["baseY"]
    rowh = box["height"] / info["rows"]
    y = box["y"] + vrow * rowh + rowh / 2

    # plain drag: the app owns the mouse -> no local selection
    page.mouse.move(box["x"] + 4, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.5, y, steps=6)
    page.mouse.up()
    page.wait_for_timeout(250)
    assert not page.evaluate("() => window.__kbterm.getSelection()"), "plain drag must not steal the app's mouse"

    # Shift+drag: forces a selection, which auto-copies
    page.evaluate("() => navigator.clipboard.writeText('__none__')")
    page.keyboard.down("Shift")
    page.mouse.move(box["x"] + 4, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.5, y, steps=6)
    page.mouse.up()
    page.keyboard.up("Shift")
    page.wait_for_timeout(400)
    sel = page.evaluate("() => window.__kbterm.getSelection()")
    clip = page.evaluate("() => navigator.clipboard.readText()")
    assert "MOUSEAPP_LINE_QQ" in sel, sel
    assert clip == sel, (clip, sel)
    ctx.close()


def test_terminal_paste_arrives_once_without_clipboard_read_permission(browser):
    """Ctrl+V and Ctrl+Shift+V must each paste exactly once, and must not
    depend on the clipboard READ permission.

    Reading the clipboard ourselves (navigator.clipboard.readText) broke it
    both ways: text copied in ANOTHER app made Chrome demand permission — it
    pops a little "Paste" chip and nothing ever arrives — while text copied
    inside the page landed twice, because Chrome ran its own paste as well.
    Handing the key back to the browser leaves exactly one paste path, and the
    native paste event carries the text with it, no permission needed."""
    ctx = browser.new_context(permissions=[])        # clipboard READ denied
    # fill the clipboard the way another application would: a real Ctrl+C
    helper = ctx.new_page()
    helper.goto("http://127.0.0.1:8300/login")
    helper.evaluate("""() => { const ta = document.createElement('textarea');
        ta.value = 'PASTED_ONCE_QQ'; document.body.appendChild(ta);
        ta.focus(); ta.select(); }""")
    helper.keyboard.press("Control+C")
    helper.wait_for_timeout(200)

    page = login(ctx, "alice")
    _open_terminal(page)
    page.evaluate("""() => { window.__sent = [];
                             window.__kbterm.onData(d => window.__sent.push(d)); }""")
    for key in ("Control+Shift+V", "Control+V"):
        page.evaluate("() => { window.__sent = []; }")
        page.keyboard.press(key)
        page.wait_for_timeout(700)
        sent = page.evaluate("() => window.__sent")
        copies = sum(s.count("PASTED_ONCE_QQ") for s in sent)
        assert copies == 1, f"{key} must paste exactly once, got {copies}: {sent}"
        # and it must never leak ^V into the shell alongside the paste
        assert not any("\x16" in s for s in sent), f"{key} sent a literal ^V: {sent}"
    ctx.close()
