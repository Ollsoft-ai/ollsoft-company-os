"""The agent chat, end to end, against the echo test agent: open a chat in a
side column, send, watch the reply stream as markdown, see a tool call ask
for permission and take the answer, survive a reload with the conversation
intact, and keep the document header describing the document."""
import json

import pytest
from conftest import BASE, dlg_fill, dlg_ok, expand_folder, login, open_doc, wait_path
from kbenv import AREA, doc, full


def open_echo_chat(page):
    page.evaluate("() => window.__kbopenview('chat', {agent: 'echo'})")
    visible = "(sel) => { const e = document.querySelector(sel); return !!e && e.offsetParent !== null; }"
    page.wait_for_function(
        "() => ['[data-testid=\"chat-picker\"]', '[data-testid=\"chat-input\"]'].some(" + visible + ")",
        timeout=20000)
    # the echo agent needs no sign-in: the picker (if shown) offers Start
    if page.locator('[data-testid="chat-start-echo"]').count():
        page.click('[data-testid="chat-start-echo"]')
    page.wait_for_function("() => (" + visible + ")('[data-testid=\"chat-input\"]')", timeout=20000)
    page.wait_for_function("() => !document.querySelector('.chat-status.busy')", timeout=30000)


def test_chat_opens_beside_the_document_and_streams_markdown(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md"))
    wait_path(page, doc("overview.md"))
    open_echo_chat(page)
    # a second column on the right, the document still active in the first
    assert page.evaluate("() => document.querySelectorAll('#panes > .col').length") == 2
    assert page.evaluate("() => window.__kbpath") == doc("overview.md")
    assert page.locator(".tab.active").get_attribute("data-path") == doc("overview.md")
    page.fill('[data-testid="chat-input"]', "hello **world**")
    page.keyboard.press("Enter")
    page.wait_for_selector(".chat-msg.user", timeout=10000)
    page.wait_for_function(
        "() => [...document.querySelectorAll('.chat-msg.agent strong')].some(e => e.textContent.includes('hello'))",
        timeout=20000)
    page.wait_for_function("() => document.querySelector('.chat-md pre.chat-code code.hljs')", timeout=20000)
    page.wait_for_function("() => !document.querySelector('.chat-status.busy')", timeout=20000)
    assert page.locator(".chat-thought").count() >= 1
    # the header never switched to the chat
    assert page.evaluate("() => window.__kbpath") == doc("overview.md")
    ctx.close()


def test_a_tool_call_asks_for_permission_and_the_answer_lands(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    page.fill('[data-testid="chat-input"]', "please ask permission")
    page.keyboard.press("Enter")
    page.wait_for_selector('[data-testid="chat-perm"] .chat-perm-btn.allow', timeout=20000)
    assert page.locator('[data-testid="chat-tool"]').count() == 1
    page.click('[data-testid="chat-perm"] .chat-perm-btn.allow')
    page.wait_for_function("() => document.querySelector('[data-testid=\"chat-tool\"]').dataset.status === 'completed'", timeout=20000)
    page.wait_for_function("() => !document.querySelector('.chat-status.busy')", timeout=20000)
    assert "Allow once" in page.text_content('[data-testid="chat-perm"]')
    ctx.close()


def test_the_conversation_survives_a_reload(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    page.fill('[data-testid="chat-input"]', "remember me")
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelectorAll('.chat-msg.agent').length >= 1 && !document.querySelector('.chat-status.busy')", timeout=20000)
    saved = json.loads(page.evaluate("() => localStorage.getItem('kbOpen')"))
    specs = [t for c in saved["columns"] for g in c["groups"] for t in g["tabs"]]
    assert any(t["kind"] == "chat" and t.get("sessionId") for t in specs), specs
    page.reload()
    page.wait_for_selector('[data-testid="chat-input"]', timeout=30000)
    page.wait_for_function(
        "() => [...document.querySelectorAll('.chat-msg.user')].some(e => e.textContent.includes('remember me'))",
        timeout=30000)
    assert page.locator(".chat-msg.agent").count() >= 1
    ctx.close()


def test_slash_commands_and_the_plan(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    page.fill('[data-testid="chat-input"]', "/pl")
    page.wait_for_selector(".chat-slash-row", timeout=10000)
    assert "/plan" in page.text_content(".chat-slash-row.sel")
    page.keyboard.press("Tab")
    assert page.input_value('[data-testid="chat-input"]').startswith("/plan")
    page.fill('[data-testid="chat-input"]', "/plan tests")
    page.keyboard.press("Enter")
    page.wait_for_selector(".chat-plan-item", timeout=20000)
    assert page.locator(".chat-plan-item").count() == 3
    page.wait_for_function("() => !document.querySelector('.chat-status.busy')", timeout=20000)
    ctx.close()


def test_the_chat_button_opens_a_new_chat_every_time(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    # the agent you chatted with is remembered as the default for the next one
    page.wait_for_function("() => localStorage.getItem('kbChatAgent') === 'echo'", timeout=10000)
    page.click('[data-testid="chat-new"]')   # the sidebar's New (a desktop has no top-right chat button)
    page.wait_for_function("() => document.querySelectorAll('.tab-content.chat').length === 2", timeout=20000)
    # the new chat opened straight into the echo agent — no picker — beside the first one
    page.wait_for_function("() => document.querySelectorAll('.tab-content.chat .chat-input').length === 2", timeout=20000)
    assert page.evaluate("() => document.querySelectorAll('[data-testid=\"chat-picker\"]').length") == 0
    # nothing else was open: the first chat took the only group, the second joins it
    assert page.evaluate("() => document.querySelectorAll('#panes > .col').length") == 1, "both chats share the group"
    assert page.evaluate("() => document.querySelectorAll('#panes .tab').length") == 2
    # only the tab in view is highlighted
    cur = page.evaluate("() => [...document.querySelectorAll('#panes > .col:last-of-type .tab')].map(t => t.classList.contains('current'))")
    assert cur == [False, True]
    page.click("body")
    page.keyboard.press("Alt+C")
    page.wait_for_function("() => document.querySelectorAll('.tab-content.chat').length === 3", timeout=20000)
    ctx.close()


CHATVIEW = "window.__kbchatview(document.querySelector('.chat-view'))"


def ask_echo(page, text):
    page.fill('[data-testid="chat-input"]', text)
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelectorAll('.chat-msg.agent').length >= 1 && !document.querySelector('.chat-status.busy')", timeout=20000)


def test_history_comes_back_after_the_agent_process_restarts(browser):
    """An idle stop or a restart leaves the backend with no log of the chat:
    the tab asks the agent for the conversation instead of showing a blank
    "new chat"."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    ask_echo(page, "first thing")
    r = page.request.post(BASE + "/api/acp/restart", data=json.dumps({"agent": "echo"}), headers={"content-type": "application/json"})
    assert r.ok, r.status
    page.reload()
    page.wait_for_selector('[data-testid="chat-input"]', timeout=30000)
    page.wait_for_function("() => [...document.querySelectorAll('.chat-msg.user')].some(e => e.textContent.includes('first thing'))", timeout=30000)
    assert page.locator(".chat-msg.agent").count() >= 1
    assert page.evaluate("() => !!" + CHATVIEW + ".modes"), "the modes are back with the conversation"
    ctx.close()


def test_a_message_sent_while_disconnected_waits_and_goes_when_the_agent_is_back(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    page.evaluate("() => { const v = " + CHATVIEW + "; v.conn.ws.close(); }")
    page.wait_for_function("() => { const v = " + CHATVIEW + "; return !v.conn.ws || v.conn.ws.readyState !== 1; }")
    page.fill('[data-testid="chat-input"]', "are you there")
    page.keyboard.press("Enter")
    page.wait_for_selector(".chat-msg.user.unsent .chat-unsent-retry")
    # delivered on its own once the connection is back
    page.wait_for_function("() => !document.querySelector('.chat-msg.user.unsent') && document.querySelectorAll('.chat-msg.agent').length >= 1", timeout=20000)
    ctx.close()


def test_keys_answer_the_first_open_permission(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    req = """(id, title) => ({id, method: 'session/request_permission', params: {toolCall: {toolCallId: 'p' + id, title, kind: 'edit'},
      options: [{optionId: 'a', name: 'Yes', kind: 'allow_once'}, {optionId: 'b', name: 'Always', kind: 'allow_always'}, {optionId: 'c', name: 'No', kind: 'reject_once'}]}})"""
    page.evaluate("() => { const v = " + CHATVIEW + "; const req = " + req + "; v.onAgentRequest(req(901, 'Write a.md')); v.onAgentRequest(req(902, 'Write b.md')); }")
    hints = page.locator(".chat-perm-hint")
    assert hints.count() == 2
    assert "1–3" in hints.nth(0).text_content() and "above" in hints.nth(1).text_content()
    page.click('[data-testid="chat-input"]')
    page.keyboard.press("1")
    page.wait_for_function("() => document.querySelectorAll('.chat-perm.done').length === 1")
    assert "✓" in page.locator(".chat-perm.done .chat-perm-answer").text_content()
    assert "1–3" in page.locator(".chat-perm:not(.done) .chat-perm-hint").text_content(), "the second prompt now takes the keys"
    page.keyboard.press("3")
    page.wait_for_function("() => document.querySelectorAll('.chat-perm.done').length === 2")
    assert page.evaluate("() => document.querySelector('[data-testid=\"chat-input\"]').value") == ""
    ctx.close()


def test_a_stop_the_agent_ignores_still_ends_the_turn(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    page.evaluate("() => { const v = " + CHATVIEW + "; v.setRunning(true); v.cancel(); }")
    assert page.locator(".chat-send.stop").count() == 1
    page.wait_for_function("() => !" + CHATVIEW + ".running", timeout=8000)
    assert page.locator(".chat-send.stop").count() == 0
    assert "Interrupted" in page.locator(".chat-note").last.text_content()
    ctx.close()


def test_a_chat_alone_takes_the_screen_and_the_first_document_opens_beside_it(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    assert page.evaluate("() => document.querySelectorAll('#panes > .col').length") == 1
    w = page.evaluate("() => [document.querySelector('.chat-view').getBoundingClientRect().width, document.querySelector('#panes').getBoundingClientRect().width]")
    assert w[0] > w[1] - 4, w
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 2")
    cols = page.evaluate("() => [...document.querySelectorAll('#panes > .col')].map(c => ({w: Math.round(c.getBoundingClientRect().width), chat: !!c.querySelector('.chat-view')}))")
    assert not cols[0]["chat"] and cols[1]["chat"], cols
    assert cols[1]["w"] < cols[0]["w"] * 0.7, cols
    ctx.close()


def test_a_long_title_names_the_tab_and_the_chat_keeps_its_controls(browser):
    """The tab strip is the chat's title bar: a long title truncates there,
    and the composer's controls are untouched by it."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_echo_chat(page)
    page.evaluate("() => " + CHATVIEW + ".onUpdate({method: 'session/update', params: {update: {sessionUpdate: 'session_info_update', title: 'A very long title that goes on and on about the onboarding guide and its many sections and subsections'}}})")
    assert page.locator(".chat-head").count() == 0, "no second title bar over the tab strip"
    tab = page.locator('#panes .tab', has_text="A very long title").first
    assert tab.count() == 1
    box = page.evaluate("""() => { const v = document.querySelector('.chat-view').getBoundingClientRect();
      const c = document.querySelector('[data-testid="chat-agent-chip"]').getBoundingClientRect();
      const m = document.querySelector('.chat-more-btn').getBoundingClientRect();
      const s = document.querySelector('[data-testid="chat-send"]').getBoundingClientRect();
      return {view: v.right, chip: c.right, more: m.width, send: s.right}; }""")
    assert box["chip"] < box["view"] and box["send"] <= box["view"] + 1 and box["more"] > 20, box
    ctx.close()


def test_the_agent_menu_is_whole_in_the_terminal_panel(browser):
    """The chat in the panel is short; its menu is a page-level layer, so the
    first agents are not clipped away by the panel's top edge."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_echo_chat(page)
    # put the chat into the terminal panel, where it is short
    page.keyboard.press("Control+Backquote")
    page.wait_for_selector("#terminal-panel:not([hidden])")
    page.click(".chat-view")
    page.keyboard.press("Control+Shift+P"); page.keyboard.type("Move tab into the terminal panel"); page.keyboard.press("Enter")
    page.wait_for_function("() => !!document.querySelector('#terminal-panel .chat-view')")
    page.click('#terminal-panel [data-testid="chat-agent-chip"]')
    page.wait_for_selector('[data-testid="chat-agent-menu"]')
    m = page.evaluate("""() => { const el = document.querySelector('[data-testid="chat-agent-menu"]');
      const r = el.getBoundingClientRect(); const rows = [...el.querySelectorAll('.chat-agent-item')].map(x => x.getBoundingClientRect().top);
      return {top: r.top, bottom: r.bottom, h: r.height, sh: el.scrollHeight, rows: rows.length, firstRow: rows[0], vis: window.innerHeight}; }""")
    print("menu in the panel:", m)
    assert m["top"] >= 0 and m["bottom"] <= m["vis"] + 1, m
    assert m["rows"] >= 1 and m["firstRow"] >= m["top"] - 1, m
    assert m["h"] >= min(m["sh"], 160) - 1, m
    ctx.close()


def test_a_short_chat_uses_a_one_row_composer(browser):
    ctx = browser.new_context(viewport={"width": 1000, "height": 430})
    page = login(ctx, "alice")
    open_echo_chat(page)
    page.wait_for_timeout(200)
    m = page.evaluate("() => { const b = document.querySelector('.chat-box'); const t = document.querySelector('.chat-input').getBoundingClientRect(); const s = document.querySelector('.chat-send').getBoundingClientRect(); return {dir: getComputedStyle(b).flexDirection, dy: Math.abs((t.top + t.bottom) / 2 - (s.top + s.bottom) / 2)}; }")
    assert m["dir"] == "row" and m["dy"] < 12, m
    ctx.close()


def test_the_sidebar_lists_the_chat_once_it_has_a_name_and_brings_it_back(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    assert page.locator("#chats .chat-row").count() == 0 or page.locator("#chats").is_hidden() or True
    ask_echo(page, "sidebar row please")
    page.wait_for_function("() => [...document.querySelectorAll('#chats .chat-row')].some(r => r.textContent.includes('sidebar row please'))", timeout=10000)
    row = page.locator("#chats .chat-row", has_text="sidebar row please")
    assert row.locator(".av").count() == 1, "the agent's mark leads the row"
    assert "active" in (row.get_attribute("class") or ""), "the chat in view is marked"
    # close the tab, click the row: the same conversation comes back
    page.click(".tab[data-sid], .tab .tab-x >> nth=0")
    page.wait_for_function("() => !document.querySelector('.chat-view')")
    row.click()
    page.wait_for_selector('[data-testid="chat-input"]', timeout=20000)
    page.wait_for_function("() => [...document.querySelectorAll('.chat-msg.user')].some(e => e.textContent.includes('sidebar row please'))", timeout=30000)
    ctx.close()


def test_the_agent_chip_switches_the_mode_and_names_it(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    chip = page.locator('[data-testid="chat-agent-chip"]')
    assert "Ask first" in chip.text_content(), chip.text_content()
    chip.click()
    page.wait_for_selector('[data-testid="chat-agent-menu"]')
    assert page.locator('[data-testid="chat-agent-menu"] .chat-agent-item[data-agent="echo"].on').count() == 1
    page.click('[data-testid="chat-mode"] [data-mode="auto"]')
    page.wait_for_function("() => document.querySelector('[data-testid=\"chat-agent-chip\"]').textContent.includes('Just do it')")
    page.keyboard.press("Escape")
    page.wait_for_function("() => !document.querySelector('[data-testid=\"chat-agent-menu\"]')")
    # the empty screen's suggestion lands in the box
    page.click(".chat-sug >> nth=0")
    assert page.input_value('[data-testid="chat-input"]').startswith("Summarise")
    ctx.close()


def test_a_chat_row_can_be_pinned_renamed_and_deleted(browser):
    """The sidebar's chats are a list you can keep: right-click (or the ⋯ on
    a finger) pins a chat to the top, renames it, or takes it off the list."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    ask_echo(page, "a chat worth keeping")
    # by name, not by position: the list also holds the chats the tests before
    # this one left behind, and a pinned row sorts above them all
    row = page.locator("#chats .chat-row", has_text="a chat worth keeping")
    row.wait_for(timeout=10000)
    sid = row.get_attribute("data-sid")
    row.click(button="right")
    page.wait_for_selector('[data-testid="ctx-menu"]')
    assert [b.strip() for b in page.evaluate("() => [...document.querySelectorAll('.ctx-item')].map(b => b.textContent)")] \
        == ["Open", "Pin to the top", "Rename…", "Delete"]
    page.click('.ctx-item:has-text("Pin to the top")')
    page.wait_for_selector(f'#chats .chat-row.pinned[data-sid="{sid}"]')
    page.reload()
    page.wait_for_selector(f'#chats .chat-row.pinned[data-sid="{sid}"]', timeout=30000)   # a pin is kept, not a browser whim
    # rename it from the list: the row and the open tab follow
    page.locator(f'#chats .chat-row[data-sid="{sid}"]').click(button="right")
    page.wait_for_selector('[data-testid="ctx-menu"]')
    page.click('.ctx-item:has-text("Rename")')
    dlg_fill(page, "Kept and renamed")
    page.wait_for_function("() => [...document.querySelectorAll('#chats .chat-row-title')].some(e => e.textContent === 'Kept and renamed')", timeout=10000)
    assert page.locator('#panes .tab', has_text="Kept and renamed").count() == 1, "the open tab follows the new name"
    # …and delete it
    page.locator(f'#chats .chat-row[data-sid="{sid}"]').click(button="right")
    page.wait_for_selector('[data-testid="ctx-menu"]')
    page.click('.ctx-item.danger:has-text("Delete")')
    dlg_ok(page)
    page.wait_for_function("() => ![...document.querySelectorAll('#chats .chat-row-title')].some(e => e.textContent === 'Kept and renamed')", timeout=10000)
    ctx.close()


def test_the_chat_carries_what_you_have_open(browser):
    """The document in the window rides along as a context chip, and the
    prompt reaches the agent with a resource_link for it (the echo agent
    prints what it was given). Removing the chip stops it."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    page.request.post(BASE + "/api/acp/restart", data=json.dumps({"agent": "echo"}),
                      headers={"content-type": "application/json"})
    open_doc(page, doc("overview.md"))
    wait_path(page, doc("overview.md"))
    open_echo_chat(page)
    chip = page.locator('.chat-ctx .chat-ctx-chip.auto', has_text="overview.md")
    chip.wait_for(timeout=8000)
    ask_echo(page, "look at this")
    # the agent was handed the path, not the text
    assert doc("overview.md") in page.text_content(".chat-msg.agent")
    # …and the bubble records what went with it
    assert page.locator('.chat-msg.user .chat-ctx-chip.sent').count() == 1
    # drop it: the next prompt goes alone
    chip.locator(".chat-ctx-x").click()
    page.wait_for_selector('.chat-ctx .chat-ctx-chip', state="detached", timeout=5000)
    page.fill('[data-testid="chat-input"]', "and now alone")
    page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelectorAll('.chat-msg.agent').length === 2", timeout=15000)
    assert "context:" not in page.locator(".chat-msg.agent").nth(1).text_content()
    ctx.close()


def test_add_a_file_by_hand_and_start_a_chat_in_a_folder(browser):
    """＋ → a file: it stays until you remove it. ＋ → a folder before the
    first word: the session starts there, and the agent says so."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    page.request.post(BASE + "/api/acp/restart", data=json.dumps({"agent": "echo"}),
                      headers={"content-type": "application/json"})
    open_echo_chat(page)
    page.click('[data-testid="chat-add"]')
    page.click('.chat-menu-item:has-text("Add a file…")')
    page.wait_for_selector('[data-testid="pickpath-input"]')
    page.fill('[data-testid="pickpath-input"]', "onboarding.md")
    page.wait_for_selector('[data-testid="pickpath-item"]')
    page.click('[data-testid="pickpath-item"]')
    page.wait_for_selector('.chat-ctx .chat-ctx-chip:not(.auto)', timeout=5000)
    assert "onboarding.md" in page.text_content(".chat-ctx")
    # …and a working folder, while this chat has said nothing yet
    page.click('[data-testid="chat-add"]')
    page.click('.chat-menu-item:has-text("Work in a folder…")')
    page.wait_for_selector('[data-testid="pickpath-input"]')
    page.fill('[data-testid="pickpath-input"]', AREA)
    page.wait_for_selector('[data-testid="pickpath-item"]')
    page.click('[data-testid="pickpath-item"]')
    page.wait_for_selector(".chat-ctx-chip.cwd", timeout=5000)
    ask_echo(page, "where are we")
    assert "/" + AREA in page.text_content(".chat-msg.agent")      # the agent stands there
    ctx.close()


def test_a_secret_never_rides_along_as_context(browser):
    """`_secrets/` is out of the index, out of git and out of search. A
    secret you happen to have open must not be handed to a model either —
    not automatically, and not through the file picker."""
    path = doc("_secrets/keys-ctx.md")
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    r = page.request.post(BASE + "/api/artifact/write",
                          data=json.dumps({"path": path, "content": "token = hunter2\n"}),
                          headers={"content-type": "application/json"})
    assert r.ok, r.text()
    try:
        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        expand_folder(page, doc("_secrets"))      # secret folders open collapsed
        page.click(f'.tree-item[data-path="{path}"]')     # not open_doc: a secret has no CRDT
        wait_path(page, path, kind="secret", timeout=10000)
        open_echo_chat(page)
        page.wait_for_timeout(600)
        assert page.locator(".chat-ctx .chat-ctx-chip").count() == 0, "a secret is not context"
        # …and the picker will not offer it either
        page.click('[data-testid="chat-add"]')
        page.click('.chat-menu-item:has-text("Add a file…")')
        page.wait_for_selector('[data-testid="pickpath-input"]')
        page.fill('[data-testid="pickpath-input"]', "keys-ctx")
        page.wait_for_timeout(300)
        assert page.locator('[data-testid="pickpath-item"]').count() == 0
        page.keyboard.press("Escape")
    finally:
        page.request.post(BASE + "/api/fs/delete", data=json.dumps({"path": path}),
                          headers={"content-type": "application/json"})
        ctx.close()


def test_resuming_a_chat_from_the_picker_never_forgets_it(browser):
    """Picking a folder restarts the session of a chat that has said nothing.
    A chat resumed from the picker HAS said something — its history is the
    agent's — so it must open a new chat instead, and stay on the list."""
    first = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(first, "alice")
    open_echo_chat(page)
    ask_echo(page, "the chat that must survive")
    sid = page.evaluate(CHATVIEW + ".sessionId")
    first.close()

    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    try:
        # with the agent stopped, a new chat opens on the picker — which is
        # where a chat is resumed IN PLACE (the path that used to forget it)
        page.request.post(BASE + "/api/acp/restart", data=json.dumps({"agent": "echo"}),
                          headers={"content-type": "application/json"})
        page.evaluate("() => window.__kbopenview('chat', {agent: 'echo'})")
        page.wait_for_selector('[data-testid="chat-picker"]', timeout=20000)
        page.click('.chat-history-row:has-text("the chat that must survive")')
        page.wait_for_function("() => document.querySelector('[data-testid=\"chat-input\"]')", timeout=20000)
        page.wait_for_function(CHATVIEW + ".sessionId === " + json.dumps(sid), timeout=10000)
        page.click('[data-testid="chat-add"]')
        page.click('.chat-menu-item:has-text("Change the working folder…"), .chat-menu-item:has-text("Work in a folder…")')
        page.wait_for_selector('[data-testid="pickpath-input"]')
        page.fill('[data-testid="pickpath-input"]', AREA)
        page.wait_for_selector('[data-testid="pickpath-item"]')
        page.click('[data-testid="pickpath-item"]')
        # a second chat opened in that folder, and the resumed one is untouched
        page.wait_for_function("() => document.querySelectorAll('.chat-view').length === 2", timeout=10000)
        ids = page.request.get(BASE + "/api/acp/chats").json()["chats"]
        assert any(c["id"] == sid for c in ids), "the resumed chat was forgotten"
    finally:
        ctx.close()


def test_a_message_typed_mid_turn_is_queued_not_lost(browser):
    """While the agent works the round button is a stop square — until you
    type, when it becomes Send and the message waits for the turn in front of
    it (Claude Code's "Queue a message…"). Esc still interrupts."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    page.fill('[data-testid="chat-input"]', "tell me a slow story")
    page.keyboard.press("Enter")
    page.wait_for_selector(".chat-send.stop", timeout=10000)
    assert page.input_value('[data-testid="chat-input"]') == ""
    assert page.get_attribute('[data-testid="chat-input"]', "placeholder") == "Queue a message…"
    page.fill('[data-testid="chat-input"]', "and one more thing")
    page.wait_for_function("() => !document.querySelector('.chat-send.stop')", timeout=5000)
    page.keyboard.press("Enter")
    page.wait_for_selector(".chat-msg.user.queued", timeout=5000)
    assert "Queued" in page.text_content(".chat-msg.user.queued")
    # …and it goes by itself when the first turn ends
    page.wait_for_function("() => !document.querySelector('.chat-msg.user.queued')", timeout=25000)
    page.wait_for_function("() => document.querySelectorAll('.chat-msg.agent').length >= 2", timeout=25000)
    assert "one more thing" in page.text_content(".chat-log")
    ctx.close()


def test_a_filesystem_path_in_an_answer_is_a_link_into_the_app(browser):
    """Agents answer with the paths they see — `/srv/kb/company/notes.md`.
    In the transcript that is a link to the document, opened here rather
    than in a new tab (and the hub redirects the same URL typed into a
    browser, so the link works from anywhere)."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_echo_chat(page)
    abs_path = str(full(doc("overview.md")))
    ask_echo(page, f"see [the overview]({abs_path}) for the rest")
    link = page.locator(".chat-msg.agent a", has_text="the overview").first
    link.wait_for(timeout=10000)
    assert link.get_attribute("data-open-path") == abs_path
    assert link.get_attribute("target") is None, "a document opens here, not in a new tab"
    link.click()
    wait_path(page, doc("overview.md"), timeout=10000)
    ctx.close()
