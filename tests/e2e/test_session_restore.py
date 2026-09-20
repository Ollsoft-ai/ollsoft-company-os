"""Refresh must not cost you your workspace: open tabs come back, and
terminals REATTACH to their still-running shells (same processes, same
environment, recent output replayed). Plus desktop-style terminal copy."""
import re
import time

from conftest import BASE, CREDS, login, wait_path, user_menu
from kbenv import doc


def term_text(page):
    return page.inner_text("#terminal").replace("\n", "")


def wait_prompt(page, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline and "$" not in term_text(page):
        page.wait_for_timeout(150)


def test_tabs_and_terminal_survive_reload(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")

    # a doc tab + a live shell with state in it
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    wait_path(page, doc("overview.md"))
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    wait_prompt(page)
    page.keyboard.type("MARK=persist_$$; echo set_$MARK")
    page.keyboard.press("Enter")
    deadline = time.time() + 6
    while time.time() < deadline and not re.search(r"set_persist_\d+", term_text(page)):
        page.wait_for_timeout(150)
    marker = re.search(r"set_(persist_\d+)", term_text(page)).group(1)

    # the moment of truth
    page.reload()
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    # the doc tab is back and active
    wait_path(page, doc("overview.md"), timeout=10000)
    # the terminal is back — with the SAME shell: replayed output AND live env
    page.wait_for_selector("#terminal-panel:not([hidden])", timeout=10000)
    page.wait_for_selector("#terminal .xterm-rows")
    deadline = time.time() + 8
    while time.time() < deadline and f"set_{marker}" not in term_text(page):
        page.wait_for_timeout(200)
    assert f"set_{marker}" in term_text(page), "replayed scrollback missing"
    page.click("#terminal")
    page.keyboard.type("echo again_$MARK")
    page.keyboard.press("Enter")
    deadline = time.time() + 6
    ok = False
    while time.time() < deadline:
        if f"again_{marker}" in term_text(page):
            ok = True
            break
        page.wait_for_timeout(150)
    assert ok, "the shell after reload must be the SAME process (env survived)"

    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
    ctx.close()


def test_terminal_survives_connection_drop(browser):
    """A dropped websocket is NOT a dead shell: the tab must stay put, reattach
    on its own, come back to the SAME process, and never duplicate output."""
    ctx = browser.new_context()
    page = login(ctx, "alice")
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    wait_prompt(page)
    page.keyboard.type("DROPMARK=drop_$$; echo set_$DROPMARK")
    page.keyboard.press("Enter")
    deadline = time.time() + 6
    while time.time() < deadline and not re.search(r"set_drop_\d+", term_text(page)):
        page.wait_for_timeout(150)
    marker = re.search(r"set_(drop_\d+)", term_text(page)).group(1)

    # sever the socket out from under the terminal — what a sleeping laptop,
    # a proxy timeout or a network blip does
    page.evaluate("() => window.__kbterms[0].ws.close()")
    # the tab must NOT retire — it reattaches by itself (backoff starts at 1s)
    page.wait_for_function(
        "() => window.__kbterms.length === 1 && window.__kbterms[0].ws.readyState === 1",
        timeout=15000)
    page.wait_for_selector("#terminal-panel:not([hidden])")
    page.click("#terminal")
    page.keyboard.type("echo again_$DROPMARK")
    page.keyboard.press("Enter")
    deadline = time.time() + 6
    ok = False
    while time.time() < deadline:
        if f"again_{marker}" in term_text(page):
            ok = True
            break
        page.wait_for_timeout(150)
    assert ok, "after a reconnect it must be the SAME shell (env survived)"
    # offset replay: this client missed nothing, so nothing may appear twice
    assert term_text(page).count(f"set_{marker}") == 1, "replay duplicated output"

    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
    ctx.close()


def test_doc_badge_shows_live(browser):
    """An editor that is actually syncing wears the primary colour and says
    'live' on hover; a silently dead connection was how a whole pairing session
    looked broken."""
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    wait_path(page, doc("overview.md"))
    page.wait_for_selector("#access-badge.live", timeout=8000)
    assert "live" in page.get_attribute('[data-testid="access-badge"]', "title").lower()
    ctx.close()


def test_ctrl_c_copies_selection_but_still_interrupts(browser):
    tag = f"cptest_{int(time.time())}"
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = login(ctx, "alice")
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    wait_prompt(page)
    page.keyboard.type(f"echo {tag}")
    page.keyboard.press("Enter")
    deadline = time.time() + 6
    while time.time() < deadline and term_text(page).count(tag) < 2:
        page.wait_for_timeout(150)
    # WITH a selection: Ctrl+C copies (and does NOT reach the shell)
    page.evaluate("() => window.__kbterm.selectAll()")
    page.keyboard.press("Control+c")
    page.wait_for_timeout(300)
    clip = page.evaluate("() => navigator.clipboard.readText()")
    assert tag in clip, f"selection should be on the clipboard, got: {clip[:80]!r}"
    # WITHOUT a selection: Ctrl+C interrupts, as always
    page.keyboard.type("sleep 30")
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    page.keyboard.press("Control+c")
    deadline = time.time() + 5
    ok = False
    while time.time() < deadline:
        if "^C" in page.inner_text("#terminal"):
            ok = True
            break
        page.wait_for_timeout(150)
    assert ok, "plain Ctrl+C must still interrupt"
    page.keyboard.type("exit")
    page.keyboard.press("Enter")
    page.wait_for_selector("#terminal-panel", state="hidden", timeout=10000)
    ctx.close()
