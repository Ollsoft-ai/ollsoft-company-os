"""VS-Code-style UX in a real browser: editor tabs (open/switch/close),
tabbed terminals in a docked panel that actually disappears when closed,
and the cron panel."""
import time

from conftest import BASE, dlg_ok, login, open_doc, wait_path, user_menu
from kbenv import CREDS, U, doc


def test_editor_tabs_open_switch_close(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")

    open_doc(page, doc("overview.md"))
    page.wait_for_selector(f'.tab.active[data-path="{doc("overview.md")}"]')
    open_doc(page, doc("onboarding.md"))
    page.wait_for_selector(f'.tab.active[data-path="{doc("onboarding.md")}"]')
    assert page.locator("#tabbar .tab").count() == 2

    # switching back re-activates the existing live tab (no re-open)
    page.click(f'.tab[data-path="{doc("overview.md")}"]')
    wait_path(page, doc("overview.md"))
    assert page.locator(".tab.active").get_attribute("data-path") == doc("overview.md")

    # close the active tab -> neighbour becomes active
    page.hover(f'.tab[data-path="{doc("overview.md")}"]')
    page.click(f'.tab[data-path="{doc("overview.md")}"] .tab-x')
    wait_path(page, doc("onboarding.md"))
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
    open_doc(page, doc("overview.md"))
    page.click(f'.tree-item[data-path="{doc("todos.html")}"]')
    page.wait_for_selector(f'.tab.active[data-path="{doc("todos.html")}"]')
    assert page.locator("#tabbar .tab").count() == 2
    # the doc tab's editor stays mounted (live) while the artifact is active
    assert page.evaluate("() => !!document.querySelector('.cm-editor')")
    page.wait_for_selector("iframe.artifact-frame")
    ctx.close()


def test_terminal_panel_tabs_and_close_reclaims_space(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")

    # open the panel: one terminal appears and the panel is really visible
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    assert page.locator("#term-tabs .term-tab").count() == 1

    # a second terminal (Ctrl+Shift+` — there is no per-strip ＋ any more)
    page.keyboard.press("Control+Shift+Backquote")
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
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    page.click("#terminal")
    page.keyboard.type("MARKER=alive_$$; echo start_$MARKER")
    page.keyboard.press("Enter")

    # hide the panel (▾) — the shell must keep running underneath
    page.click('[data-testid="term-hide"]')
    page.wait_for_selector("#terminal-panel", state="hidden")
    # re-open: same terminal, same shell state
    user_menu(page); page.click('[data-testid="toggle-term"]')
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
    user_menu(page); page.click('[data-testid="cron-btn"]')
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
    user_menu(page); page.click('[data-testid="toggle-term"]')
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
    creds = CREDS
    page.fill('input[name="username"]', U("alice"))
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
    creds = CREDS
    page.fill('input[name="username"]', U("alice"))
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
    # The marker is on the screen TWICE: once in the line the shell echoed back
    # ("…$ printf 'MOUSEAPP_LINE_QQ\n'; …") and once as the output. Scanning
    # top-down for the first line that *contains* it finds the echo, and then a
    # half-width drag selects the prompt instead — which is exactly what this
    # test did until the account names grew a namespace and pushed the marker
    # past the halfway point. Match the OUTPUT line: bottom-up, exact.
    info = page.evaluate("""() => { const t=window.__kbterm,b=t.buffer.active; let row=-1;
      for(let i=b.length-1;i>=0;i--){const l=b.getLine(i);
        if(l&&l.translateToString(true).trim()==='MOUSEAPP_LINE_QQ'){row=i;break;}}
      return {row, baseY:b.baseY, rows:t.rows}; }""")
    assert info["row"] >= 0, "the marker never reached the screen"
    vrow = info["row"] - info["baseY"]
    rowh = box["height"] / info["rows"]
    y = box["y"] + vrow * rowh + rowh / 2
    # Across the whole row, not half of it: how far along the line the marker
    # sits depends on how long this run's prompt is, which is not this test's
    # subject.
    drag_to = box["x"] + box["width"] - 6

    # plain drag: the app owns the mouse -> no local selection
    page.mouse.move(box["x"] + 4, y)
    page.mouse.down()
    page.mouse.move(drag_to, y, steps=6)
    page.mouse.up()
    page.wait_for_timeout(250)
    assert not page.evaluate("() => window.__kbterm.getSelection()"), "plain drag must not steal the app's mouse"

    # Shift+drag: forces a selection, which auto-copies
    page.evaluate("() => navigator.clipboard.writeText('__none__')")
    page.keyboard.down("Shift")
    page.mouse.move(box["x"] + 4, y)
    page.mouse.down()
    page.mouse.move(drag_to, y, steps=6)
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


# ---- the tab strip behaves like Chrome's ------------------------------------
# Content-sized tabs put every × at a different offset, so closing several meant
# re-aiming for each one. Equal widths put the ×s on a regular pitch; the
# close-streak lock is what makes that pitch hold still while you use it.

def _tab_fixtures(n):
    """n scratch documents, created before login so the first tree render has
    them — no waiting on the tree poll."""
    import httpx
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    stamp = int(time.time())
    paths = [doc(f"tabw_{stamp}_{i}.md") for i in range(n)]
    for p in paths:
        assert c.post("/api/file", json={"path": p}).status_code in (200, 409)
        # open_doc waits for the CRDT text to arrive, so they cannot be empty
        assert c.post("/api/artifact/write",
                      json={"path": p, "content": f"# {p}\n"}).status_code == 200
    return c, paths


def _widths(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('#tabbar .tab')]"
        ".map(e => Math.round(e.getBoundingClientRect().width))")


def test_every_tab_is_the_same_width_and_they_shrink_together(browser):
    c, paths = _tab_fixtures(6)
    # a fixed viewport, or "do they shrink?" depends on the runner's screen
    ctx = browser.new_context(viewport={"width": 1100, "height": 800})
    try:
        page = login(ctx, "alice")
        open_doc(page, paths[0])
        open_doc(page, paths[1])
        two = _widths(page)
        assert len(set(two)) == 1, f"two tabs, two widths: {two}"

        for p in paths[2:]:
            open_doc(page, p)
        six = _widths(page)
        assert len(six) == 6
        assert len(set(six)) == 1, f"tabs are not equal width: {six}"
        assert six[0] < two[0], \
            f"six tabs did not shrink (six={six[0]} two={two[0]})"
    finally:
        for p in paths:
            c.post("/api/fs/delete", json={"path": p})
        ctx.close()


def test_closing_with_the_mouse_keeps_the_next_x_under_the_cursor(browser):
    """The reported UX, exactly: close one, the strip does NOT re-flow, so the
    next × is already where your finger is. It springs back on hover away."""
    c, paths = _tab_fixtures(6)
    ctx = browser.new_context(viewport={"width": 1100, "height": 800})
    try:
        page = login(ctx, "alice")
        for p in paths:
            open_doc(page, p)
        before = _widths(page)
        assert len(set(before)) == 1, before

        # aim at the × of the third tab and never move again
        target = '#tabbar .tab:nth-child(3)'
        page.hover(target)
        spot = page.evaluate(
            "(s) => {const r = document.querySelector(s + ' .tab-x')"
            ".getBoundingClientRect();"
            " return {x: r.x + r.width / 2, y: r.y + r.height / 2};}", target)
        doomed = page.get_attribute(target, "data-path")
        next_up = page.get_attribute('#tabbar .tab:nth-child(4)', "data-path")

        page.mouse.move(spot["x"], spot["y"])
        page.mouse.click(spot["x"], spot["y"])
        page.wait_for_function(
            "(p) => !document.querySelector(`#tabbar .tab[data-path=\"${p}\"]`)",
            arg=doomed, timeout=5000)

        after = _widths(page)
        assert len(after) == 5
        assert after[0] == before[0], \
            f"the strip re-flowed under the cursor: {before[0]} -> {after[0]}"
        # the tab that was next along has moved INTO the spot, × and all
        hit = page.evaluate(
            "(p) => {const e = document.elementFromPoint(p.x, p.y);"
            " return e && {cls: e.className,"
            "   path: e.closest('.tab') && e.closest('.tab').dataset.path};}", spot)
        assert hit and "tab-x" in (hit["cls"] or ""), f"no close button at the cursor: {hit}"
        assert hit["path"] == next_up, f"wrong tab under the cursor: {hit}"

        # so a second click, without moving, closes the next one too
        page.mouse.click(spot["x"], spot["y"])
        page.wait_for_function(
            "(p) => !document.querySelector(`#tabbar .tab[data-path=\"${p}\"]`)",
            arg=next_up, timeout=5000)
        assert _widths(page)[0] == before[0], "the strip re-flowed mid-streak"

        # leaving the strip ends the streak and the tabs take the space back
        page.mouse.move(spot["x"], spot["y"] + 400)
        page.wait_for_function(
            "(w) => {const t = document.querySelector('#tabbar .tab');"
            "        return t && t.getBoundingClientRect().width > w + 1;}",
            arg=before[0], timeout=5000)
        grown = _widths(page)
        assert len(set(grown)) == 1, f"tabs grew back unevenly: {grown}"
    finally:
        for p in paths:
            c.post("/api/fs/delete", json={"path": p})
        ctx.close()


def test_a_keyboard_close_does_not_freeze_the_strip(browser):
    """There is no cursor to keep a × under, so the strip must re-flow at once
    — a lock left behind here would pin the tabs narrow for no reason."""
    c, paths = _tab_fixtures(5)
    ctx = browser.new_context(viewport={"width": 1100, "height": 800})
    try:
        page = login(ctx, "alice")
        for p in paths:
            open_doc(page, p)
        before = _widths(page)
        page.keyboard.press("Alt+w")
        page.wait_for_function("() => document.querySelectorAll('#tabbar .tab').length === 4",
                               timeout=5000)
        page.wait_for_function(
            "(w) => document.querySelector('#tabbar .tab').getBoundingClientRect().width > w + 1",
            arg=before[0], timeout=5000)
    finally:
        for p in paths:
            c.post("/api/fs/delete", json={"path": p})
        ctx.close()


def _scrolled_strip(browser, n=10):
    """A strip with more tabs than fit, so it actually scrolls. 900px wide: the
    app keeps its desktop layout (the mobile breakpoint is 880px) and the bar
    is narrow enough that ten tabs overflow it."""
    c, paths = _tab_fixtures(n)
    ctx = browser.new_context(viewport={"width": 900, "height": 800})
    page = login(ctx, "alice")
    for p in paths:
        open_doc(page, p)
    page.wait_for_function(
        "() => {const b = document.querySelector('#tabbar');"
        "       return b.scrollWidth > b.clientWidth + 8;}", timeout=8000)
    return c, paths, ctx, page


def test_a_scrolled_strip_stays_where_you_left_it(browser):
    """Re-rendering the bar rebuilds its children, which zeroes scrollLeft —
    so closing a tab used to fling a scrolled strip back to its first tab."""
    c, paths, ctx, page = _scrolled_strip(browser)
    try:
        # A modest offset on purpose. Closing a tab removes its width from
        # scrollWidth, so the maximum scroll drops too — park near the old end
        # and the browser legitimately clamps you back, which is physics, not a
        # jump, and would make the assertion below measure the wrong thing.
        page.evaluate("() => {document.querySelector('#tabbar').scrollLeft = 60;}")
        page.wait_for_timeout(100)
        # Two tabs that are WHOLLY inside the strip. The current one has to be
        # fully visible or revealCurrentTab legitimately nudges the scroll to
        # finish showing it, and this test would be measuring that instead of
        # the reset it is here for.
        whole = page.evaluate(
            "() => {const b = document.querySelector('#tabbar').getBoundingClientRect();"
            " return [...document.querySelectorAll('#tabbar .tab')]"
            "   .filter(e => {const r = e.getBoundingClientRect();"
            "                 return r.left >= b.left - .5 && r.right <= b.right + .5;})"
            "   .map(e => e.dataset.path);}")
        assert len(whole) >= 2, f"not enough fully-visible tabs: {whole}"
        page.click(f'#tabbar .tab[data-path="{whole[0]}"]')
        page.wait_for_timeout(150)
        before = page.evaluate("() => document.querySelector('#tabbar').scrollLeft")
        assert before >= 40, f"could not scroll the strip: {before}"

        vis = whole[1]
        page.hover(f'#tabbar .tab[data-path="{vis}"]')
        page.click(f'#tabbar .tab[data-path="{vis}"] .tab-x')
        page.wait_for_function(
            "(p) => !document.querySelector(`#tabbar .tab[data-path=\"${p}\"]`)",
            arg=vis, timeout=5000)
        after, room = page.evaluate(
            "() => {const b = document.querySelector('#tabbar');"
            " return [b.scrollLeft, b.scrollWidth - b.clientWidth];}")
        assert room >= before, f"the close clamped the scroll after all (room={room})"
        assert abs(after - before) <= 2, f"the strip jumped: {before} -> {after}"
    finally:
        for p in paths:
            c.post("/api/fs/delete", json={"path": p})
        ctx.close()


def test_activating_an_offscreen_tab_scrolls_it_into_view(browser):
    """Chrome keeps the tab you are looking at on screen."""
    c, paths, ctx, page = _scrolled_strip(browser)
    try:
        page.evaluate("() => {document.querySelector('#tabbar').scrollLeft = 1e6;}")
        page.wait_for_timeout(100)
        # the first tab is now scrolled off to the left
        assert page.evaluate(
            "() => {const b = document.querySelector('#tabbar').getBoundingClientRect();"
            " return document.querySelector('#tabbar .tab').getBoundingClientRect().left"
            "        < b.left - 1;}"), "the first tab is still on screen — no test here"
        page.keyboard.press("Alt+1")            # go to tab 1
        page.wait_for_function(
            "() => {const b = document.querySelector('#tabbar').getBoundingClientRect();"
            " const t = document.querySelector('#tabbar .tab').getBoundingClientRect();"
            " return t.left >= b.left - 1 && t.right <= b.right + 1;}", timeout=5000)
    finally:
        for p in paths:
            c.post("/api/fs/delete", json={"path": p})
        ctx.close()
