"""Editable keyboard shortcuts: change a key in the shortcut sheet, and it is
the person's own setting (keys.bindings), used at once and nowhere else."""
import os

import httpx
import pytest

from conftest import BASE, CREDS, login, open_doc, doc_text
from kbenv import U, doc

SCRATCH = doc(f"kbtest_shortcuts_{os.getpid()}.md")
SHOTS = os.environ.get("KB_SHOTS")


def api(user):
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


@pytest.fixture(scope="module")
def page(browser):
    a = api("alice")
    a.post("/api/file", json={"path": SCRATCH})
    a.post("/api/artifact/write", json={"path": SCRATCH, "content": "# keys\n\nplain words\n"})
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    p = login(ctx, "alice")
    try:
        yield p
    finally:
        ctx.close()
        a.post("/api/settings", json={"unset": ["keys.bindings", "keys.terminal"]})
        a.post("/api/fs/delete", json={"path": SCRATCH})


def mine(user="alice"):
    j = api(user).get("/api/settings").json()
    return j["user"]["values"]


def sheet(page):
    page.keyboard.press("F1")
    page.wait_for_selector('[data-testid="shortcuts"]')


def close_sheet(page):
    page.keyboard.press("Escape")
    page.wait_for_selector('[data-testid="shortcuts"]', state="detached")


def record(page, bid, combo, add=False):
    page.click(f'[data-testid="keys-{"add" if add else "edit"}-{bid}"]')
    page.wait_for_selector(".keys-card .capturing")
    page.keyboard.press(combo)


def test_rebinding_a_key_saves_it_as_mine_and_takes_effect_at_once(page):
    sheet(page)
    record(page, "palette", "Alt+G")
    page.wait_for_function("() => (window.__kbsettings.get('keys.bindings') || {}).palette === 'Alt+G'")
    assert mine()["keys.bindings"] == {"palette": "Alt+G"}
    row = page.locator('[data-testid="keys-row-palette"]')
    assert "Alt+G" in row.inner_text()
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, "shortcuts-1-changed.png"))
    close_sheet(page)
    page.keyboard.press("Alt+G")
    page.wait_for_selector('[data-testid="palette"]')
    page.keyboard.press("Escape")
    page.keyboard.press("Control+P")
    page.wait_for_timeout(300)
    assert page.locator('[data-testid="palette"]').count() == 0, "the old key must be gone"


def test_it_survives_a_reload_and_is_nobody_elses(page):
    page.reload()
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    page.keyboard.press("Alt+G")
    page.wait_for_selector('[data-testid="palette"]')
    page.keyboard.press("Escape")
    assert "keys.bindings" not in mine("bob")


def test_a_key_in_use_moves_only_after_asking(page):
    sheet(page)
    record(page, "sidebar", "Alt+N")              # Alt+N is "New document"
    page.wait_for_selector('[data-testid="dlg-ok"]')
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, "shortcuts-2-conflict.png"))
    page.click('[data-testid="dlg-ok"]')
    page.wait_for_function("() => (window.__kbsettings.get('keys.bindings') || {}).sidebar === 'Alt+N'")
    b = mine()["keys.bindings"]
    assert b["sidebar"] == "Alt+N" and b["newdoc"] == "", "New document must lose the key"
    page.click('[data-testid="keys-reset-sidebar"]')
    page.click('[data-testid="keys-reset-newdoc"]')
    page.wait_for_function("() => !('sidebar' in (window.__kbsettings.get('keys.bindings') || {}))")
    close_sheet(page)


def test_browser_keys_and_bare_letters_are_refused(page):
    sheet(page)
    record(page, "chat", "Control+W")
    page.wait_for_selector(".toast", timeout=4000)
    record(page, "chat", "Q")
    page.wait_for_timeout(300)
    assert "chat" not in (mine().get("keys.bindings") or {})
    close_sheet(page)


def test_formatting_keys_are_editable_and_work_in_the_editor(page):
    sheet(page)
    record(page, "strike", "Alt+Shift+X")
    page.wait_for_function("() => (window.__kbsettings.get('keys.bindings') || {}).strike === 'Alt+Shift+X'")
    close_sheet(page)
    open_doc(page, SCRATCH)
    page.evaluate("""() => { const v = window.__kbview; const t = v.state.doc.toString();
      const i = t.indexOf('plain'); v.dispatch({ selection: { anchor: i, head: i + 5 } }); v.focus(); }""")
    page.keyboard.press("Alt+Shift+X")
    page.wait_for_function("() => window.__kbview.state.doc.toString().includes('~~plain~~')")
    assert "~~plain~~" in doc_text(page)


def test_terminal_toggle_and_reset_all(page):
    sheet(page)
    page.click('[data-testid="keys-term-closetab"]')    # ships as working in a terminal
    page.wait_for_function("() => (window.__kbsettings.get('keys.terminal') || {}).closetab === 'off'")
    page.click('[data-testid="keys-reset-all"]')
    page.click('[data-testid="dlg-ok"]')
    page.wait_for_function("""() => !Object.keys(window.__kbsettings.get('keys.bindings') || {}).length
                               && !Object.keys(window.__kbsettings.get('keys.terminal') || {}).length""")
    v = mine()
    assert "keys.bindings" not in v and "keys.terminal" not in v
    close_sheet(page)
