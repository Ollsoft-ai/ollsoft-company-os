"""Keyboard navigation: the command palette, the shortcut layer, the shortcut
sheet and arrow-key movement in the file tree.

Every binding in the app comes from one table (BINDINGS in app.js), which also
renders the help sheet — so a test that reads the sheet is testing the same
source of truth the dispatcher uses.
"""
import json
import os

import httpx
import pytest

from conftest import BASE, CREDS, login

SCRATCH = f"company/kbtest_keys_{os.getpid()}.md"
SCRATCH_BODY = "# scratch\n\nkbtest scratch document\n"


@pytest.fixture(scope="module")
def page(browser):
    """One browser context for the whole module, plus a throwaway document —
    the typing test must never touch a shared fixture file."""
    api = httpx.Client(base_url=BASE, timeout=30)
    api.post("/login", data={"username": "alice", "password": CREDS["alice"]})
    api.post("/api/file", json={"path": SCRATCH})
    api.post("/api/artifact/write", json={"path": SCRATCH, "content": SCRATCH_BODY})
    ctx = browser.new_context()
    p = login(ctx, "alice")
    try:
        yield p
    finally:
        ctx.close()
        api.post("/api/fs/delete", json={"path": SCRATCH})


def palette(page, query=None, key="Control+p"):
    page.keyboard.press(key)
    page.wait_for_selector('[data-testid="palette-input"]', timeout=5000)
    if query:
        page.keyboard.type(query)
        page.wait_for_timeout(350)
    return page.locator('[data-testid="palette-item"]')


def paths(items):
    return [i.get_attribute("data-path") for i in items.all()]


def cursor(page):
    return page.evaluate(
        "() => { const e = document.querySelector('#tree .tree-item.cursor');"
        "        return e && e.dataset.path; }")


# --- the reported bug, through the UI ---------------------------------------

def test_quick_open_finds_a_file_by_name(page):
    items = palette(page, "todos")
    # content hits (path:line) may already sit above the file section — the
    # best FILE match is what quick-open is about
    files = [p for p in paths(items) if p and ":" not in p]
    assert files[0] == "company/todos.html"
    page.keyboard.press("Escape")


def test_quick_open_opens_it_with_the_right_viewer(page):
    palette(page, "todos")
    page.keyboard.press("Enter")
    page.wait_for_function("() => window.__kbpath === 'company/todos.html'", timeout=8000)
    assert page.evaluate("() => window.__kbkind") == "artifact"


def test_partial_and_out_of_order_names_match(page):
    assert "company/onboarding.md" in paths(palette(page, "onbo"))
    page.keyboard.press("Escape")
    assert "projects/acme/plan.md" in paths(palette(page, "acme plan"))
    page.keyboard.press("Escape")


def test_secrets_never_appear_in_the_palette(page):
    assert not [p for p in paths(palette(page, "_secrets")) if p and "_secrets" in p]
    page.keyboard.press("Escape")


def test_content_search_streams_in_on_top(page):
    palette(page, "zebrafish")
    page.wait_for_timeout(900)
    groups = [g.lower() for g in page.locator(".palette-group").all_inner_texts()]
    assert any("document" in g for g in groups), groups
    # the documents section renders ABOVE the files section
    assert "document" in groups[0], groups
    assert [p for p in paths(page.locator('[data-testid="palette-item"]'))
            if p and p.startswith("projects/acme/plan.md:")]
    page.keyboard.press("Escape")


def test_enter_still_opens_the_best_file_after_content_lands(page):
    """The document section inserts above the files, but the selection must
    stay anchored on the best file match — Enter cannot change meaning
    depending on whether the server had answered by then."""
    palette(page, "todos")
    page.wait_for_timeout(900)                    # let the content search land
    sel = page.locator(".palette-item.sel")
    assert sel.get_attribute("data-path") == "company/todos.html"
    page.keyboard.press("Escape")


def test_files_section_caps_at_five_until_expanded(page):
    # a one-letter query matches lots of files locally and (being under two
    # characters) never triggers the content search, so every row is a file
    palette(page, "e")
    files = lambda: [p for p in paths(page.locator('[data-testid="palette-item"]'))
                     if p and ":" not in p]
    n = len(files())
    more = page.locator('[data-testid="palette-more"]')
    if more.count():
        assert n == 5, files()
        more.click()
        assert len(files()) > 5
        assert page.locator('[data-testid="palette-input"]').count() == 1  # stayed open
    else:
        assert n <= 5, files()          # nothing was hidden
    page.keyboard.press("Escape")


def test_rows_keep_their_labels_across_re_renders(page):
    """render() runs again on every arrow key and when the content results land.
    Assert on the TEXT, not just data-path: a row can keep its path attribute
    and still paint empty (a DocumentFragment is emptied by the first append)."""
    palette(page, "plan")
    labels = lambda: [t.strip() for t in page.locator(".pi-main").all_inner_texts()]
    assert all(labels()), labels()
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(150)
    assert all(labels()), f"labels blanked after ArrowDown: {labels()}"
    page.wait_for_timeout(1200)          # let the content search land and re-render
    groups = [g.lower() for g in page.locator(".palette-group").all_inner_texts()]
    assert any("document" in g for g in groups), \
        f"no content results, so the re-render never happened — test is vacuous: {groups}"
    assert all(labels()), f"labels blanked when content results arrived: {labels()}"
    subs = [t.strip() for t in page.locator(".pi-sub").all_inner_texts()]
    assert all(subs), subs
    page.keyboard.press("Escape")


def test_the_close_button_dismisses_the_palette(page):
    """A phone has no reachable backdrop — the card fills the screen."""
    palette(page, "over")
    page.click(".palette-x")
    page.wait_for_selector('[data-testid="palette"]', state="detached", timeout=4000)


def test_the_search_button_can_be_tabbed_past(page):
    """Opening on focus would make the topbar untraversable by keyboard."""
    page.evaluate("() => document.querySelector('[data-testid=\"search\"]').focus()")
    page.wait_for_timeout(250)
    assert page.locator('[data-testid="palette"]').count() == 0
    page.keyboard.press("Enter")
    page.wait_for_selector('[data-testid="palette-input"]', timeout=4000)
    page.keyboard.press("Escape")


def test_escape_closes_the_palette(page):
    palette(page, "over")
    page.keyboard.press("Escape")
    page.wait_for_selector('[data-testid="palette"]', state="detached", timeout=4000)


def test_the_topbar_field_opens_the_palette(page):
    page.click('[data-testid="search"]')
    page.wait_for_selector('[data-testid="palette-input"]', timeout=4000)
    page.keyboard.press("Escape")


# --- commands ----------------------------------------------------------------

def test_command_palette(page):
    palette(page, key="Control+Shift+p")
    assert page.locator('[data-testid="palette-kind"]').inner_text().lower() == "run"
    assert page.evaluate("() => document.querySelector('.palette-input').value") == ">"
    page.keyboard.type("terminal")
    page.wait_for_timeout(250)
    assert page.locator('[data-testid="palette-item"]').count() > 0
    page.keyboard.press("Escape")


def test_a_typed_angle_bracket_switches_to_commands(page):
    palette(page, ">shortcut")
    assert page.locator('[data-testid="palette-kind"]').inner_text().lower() == "run"
    assert page.locator('[data-testid="palette-item"]').count() > 0
    page.keyboard.press("Escape")


# --- the shortcut sheet ------------------------------------------------------

def test_question_mark_opens_the_shortcut_sheet(page):
    page.keyboard.press("?")
    page.wait_for_selector('[data-testid="shortcuts"]', timeout=4000)
    txt = page.locator('[data-testid="shortcuts"]').inner_text()
    for expected in ["Go to file", "Next tab", "Close tab", "Version history",
                     "Rename", "Open the file"]:
        assert expected in txt, expected
    page.keyboard.press("Escape")
    page.wait_for_selector('[data-testid="shortcuts"]', state="detached", timeout=4000)


def test_the_menu_button_opens_it_too(page):
    # on a wide screen .topbar-actions is display:contents, so the button is
    # right there in the topbar; below 880px it lives inside the ⋯ menu
    page.click('[data-testid="keys-btn"]')
    page.wait_for_selector('[data-testid="shortcuts"]', timeout=4000)
    page.keyboard.press("Escape")
    page.wait_for_selector('[data-testid="shortcuts"]', state="detached", timeout=4000)


# --- tab navigation ----------------------------------------------------------

def test_tab_shortcuts(page):
    palette(page, "overview.md")
    page.keyboard.press("Enter")
    page.wait_for_function("() => window.__kbpath === 'company/overview.md'", timeout=8000)
    n = page.evaluate("() => document.querySelectorAll('#tabbar .tab').length")
    assert n >= 2, n

    page.keyboard.press("Alt+1")
    page.wait_for_timeout(250)
    first = page.evaluate("() => document.querySelector('#tabbar .tab').dataset.path")
    assert page.evaluate("() => window.__kbpath") == first

    page.keyboard.press("Alt+BracketRight")
    page.wait_for_timeout(250)
    assert page.evaluate("() => window.__kbpath") != first
    page.keyboard.press("Alt+BracketLeft")
    page.wait_for_timeout(250)
    assert page.evaluate("() => window.__kbpath") == first

    # Firefox reserves Alt+digit for its own tabs; Ctrl+Shift+digit is the twin
    page.keyboard.press("Alt+BracketRight")
    page.wait_for_timeout(250)
    page.keyboard.press("Control+Shift+Digit1")
    page.wait_for_timeout(250)
    assert page.evaluate("() => window.__kbpath") == first

    page.keyboard.press("Alt+w")
    page.wait_for_timeout(400)
    assert page.evaluate("() => document.querySelectorAll('#tabbar .tab').length") == n - 1


def test_sidebar_toggle(page):
    page.keyboard.press("Alt+b")
    page.wait_for_timeout(200)
    assert page.evaluate("() => document.body.classList.contains('nav-hidden')")
    page.keyboard.press("Alt+b")
    page.wait_for_timeout(200)
    assert not page.evaluate("() => document.body.classList.contains('nav-hidden')")


# --- the file tree from the keyboard ----------------------------------------

def test_tree_keyboard_navigation(page):
    page.keyboard.press("Control+Shift+e")
    page.wait_for_timeout(250)
    start = cursor(page)
    assert start, "focusing the tree must place a cursor"

    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(150)
    assert cursor(page) != start

    page.keyboard.type("us")            # type-ahead
    page.wait_for_timeout(250)
    assert cursor(page) == "users"

    def is_open():
        return page.evaluate(
            "() => document.querySelector('#tree .tree-item[data-path=\"users\"] .caret')"
            "        .classList.contains('open')")

    assert is_open(), "folders start expanded"
    page.keyboard.press("ArrowLeft")    # collapse
    page.wait_for_timeout(250)
    assert not is_open()
    assert cursor(page) == "users", "collapsing must not move the cursor"
    page.keyboard.press("ArrowRight")   # expand again
    page.wait_for_timeout(250)
    assert is_open()
    page.keyboard.press("Escape")


def test_the_tree_cursor_survives_a_repaint(page):
    """The tree re-renders itself from the server every few seconds, so the
    cursor lives in a variable and is re-applied after each render."""
    page.keyboard.press("Control+Shift+e")
    page.wait_for_timeout(250)
    here = cursor(page)
    page.evaluate("() => window.__kbrerender()")
    page.wait_for_timeout(200)
    assert cursor(page) == here
    page.keyboard.press("Escape")


# --- what must NOT be hijacked ----------------------------------------------

def open_scratch(page):
    palette(page, SCRATCH.split("/")[-1])
    page.keyboard.press("Enter")
    page.wait_for_function(f"() => window.__kbpath === {json.dumps(SCRATCH)}", timeout=8000)
    page.wait_for_function("() => window.__kbview && window.__kbview.state.doc.length > 0",
                           timeout=10000)


def test_typing_in_the_editor_is_never_intercepted(page):
    open_scratch(page)
    before = page.evaluate("() => window.__kbview.state.doc.length")
    page.locator(".cm-content:visible").click()
    page.keyboard.type("?")
    page.wait_for_timeout(300)
    assert page.locator('[data-testid="shortcuts"]').count() == 0, "`?` must type, not open help"
    assert page.evaluate("() => window.__kbview.state.doc.length") == before + 1
    page.keyboard.press("Control+z")
    page.wait_for_timeout(400)
    assert page.evaluate("() => window.__kbview.state.doc.length") == before


def test_find_in_document(page):
    open_scratch(page)
    page.locator(".cm-content:visible").click()
    page.keyboard.press("Control+f")
    page.wait_for_selector(".cm-panel.cm-search", timeout=4000)
    page.keyboard.press("Escape")
    page.wait_for_selector(".cm-panel.cm-search", state="detached", timeout=4000)
