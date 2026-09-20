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

from conftest import BASE, CREDS, login, wait_path, user_menu
from kbenv import NS, U, doc, proj

SCRATCH = doc(f"kbtest_keys_{os.getpid()}.md")
SCRATCH_BODY = "# scratch\n\nkbtest scratch document\n"


@pytest.fixture(scope="module")
def page(browser):
    """One browser context for the whole module, plus a throwaway document —
    the typing test must never touch a shared fixture file."""
    api = httpx.Client(base_url=BASE, timeout=30)
    api.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    api.post("/api/file", json={"path": SCRATCH})
    api.post("/api/artifact/write", json={"path": SCRATCH, "content": SCRATCH_BODY})
    ctx = browser.new_context()
    p = login(ctx, "alice")
    try:
        yield p
    finally:
        ctx.close()
        api.post("/api/fs/delete", json={"path": SCRATCH})


def mine(query):
    """A palette query that can only match THIS run's files.

    The palette ranks a shorter path higher, so on a box with real content
    "todos" finds company/todos.html before company/kbtest-<ns>/todos.html.
    That is quick-open behaving correctly; the assertions below were written
    against a box where the only documents were the fixture's. Including the
    namespace makes the intent — "find MY file by name" — actually testable.
    Out-of-order matching is a feature the palette already has (see
    test_partial_and_out_of_order_names_match).
    """
    return f"{NS} {query}" if NS else query


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
    items = palette(page, mine("todos"))
    # content hits (path:line) may already sit above the file section — the
    # best FILE match is what quick-open is about
    files = [p for p in paths(items) if p and ":" not in p]
    assert files[0] == doc("todos.html")
    page.keyboard.press("Escape")


def test_quick_open_opens_it_with_the_right_viewer(page):
    palette(page, mine("todos"))
    page.keyboard.press("Enter")
    wait_path(page, doc("todos.html"), timeout=8000)
    assert page.evaluate("() => window.__kbkind") == "artifact"


def test_partial_and_out_of_order_names_match(page):
    assert doc("onboarding.md") in paths(palette(page, mine("onbo")))
    page.keyboard.press("Escape")
    assert proj("plan.md") in paths(palette(page, mine("plan")))
    page.keyboard.press("Escape")


def test_secrets_never_appear_in_the_palette(page):
    assert not [p for p in paths(palette(page, "_secrets")) if p and "_secrets" in p]
    page.keyboard.press("Escape")


def test_content_search_streams_in_underneath(page):
    """Document matches land last, so they render LAST — under the files.

    They used to insert above, which meant every late result shoved the file
    rows down while the pointer was already travelling towards one. Whichever
    section arrives last has to be the bottom one.
    """
    palette(page, "zebrafish")
    page.wait_for_timeout(900)
    groups = [g.lower() for g in page.locator(".palette-group").all_inner_texts()]
    assert any("document" in g for g in groups), groups
    assert "document" in groups[-1], groups
    assert [p for p in paths(page.locator('[data-testid="palette-item"]'))
            if p and p.startswith(proj("plan.md:"))]
    page.keyboard.press("Escape")


def test_the_palette_never_moves_under_the_pointer(page):
    """The reported bug, in two halves.

    The card was sized by its content (`max-height`), so it was small while the
    local filename matches were all it had and grew when the document results
    landed — resizing under a pointer already on its way to a row. And the rows
    themselves shifted, because the documents section inserted above them.

    Measured, not asserted about the CSS: the card's box and every file row's
    box have to be identical before and after the content search resolves.
    """
    boxes = """() => {
      const card = document.querySelector('.palette-card').getBoundingClientRect();
      const rows = [...document.querySelectorAll('[data-testid=palette-item]')]
        .filter(r => r.dataset.path && !r.dataset.path.includes(':'))
        .map(r => [r.dataset.path, Math.round(r.getBoundingClientRect().top)]);
      return {card: [Math.round(card.width), Math.round(card.height)], rows};
    }"""
    # The card scales in (`animation: rise`, .985 -> 1), so a box read in the
    # first 140 ms is 1% short of the real one and every later comparison is
    # against a number that never existed. Measure once it has settled.
    settled = """() => {const c = document.querySelector('.palette-card');
      return !!c && c.getAnimations().every(a => a.playState === 'finished');}"""
    page.keyboard.press("Control+p")
    page.wait_for_selector('[data-testid="palette-input"]', timeout=5000)
    page.wait_for_function(settled, timeout=5000)
    opened = page.evaluate(boxes)["card"]

    # "onboarding" is the one seeded token that matches BOTH a file name and
    # document contents. A content-only word (zebrafish) would leave the row
    # comparison below comparing [] to [] and proving nothing.
    page.keyboard.type("onboarding")
    page.wait_for_selector('[data-testid="palette-searching"]', timeout=10000)
    during = page.evaluate(boxes)

    page.wait_for_selector('[data-testid="palette-searching"]', state="detached",
                           timeout=30000)
    after = page.evaluate(boxes)

    assert during["card"] == opened == after["card"], \
        f"the palette resized: opened={opened} during={during['card']} after={after['card']}"
    assert during["rows"], "no file rows were on screen — nothing could have jumped"
    assert during["rows"] == after["rows"], \
        f"file rows moved when the documents landed:\n{during['rows']}\n{after['rows']}"
    # and the documents really did arrive, or the race was never run
    assert [p for p in paths(page.locator('[data-testid="palette-item"]'))
            if p and ":" in p], "no document matches — the test never exercised the race"
    page.keyboard.press("Escape")


def test_enter_still_opens_the_best_file_after_content_lands(page):
    """The document section inserts above the files, but the selection must
    stay anchored on the best file match — Enter cannot change meaning
    depending on whether the server had answered by then."""
    palette(page, mine("todos"))
    page.wait_for_timeout(900)                    # let the content search land
    sel = page.locator(".palette-item.sel")
    assert sel.get_attribute("data-path") == doc("todos.html")
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
    # "onboarding" is both a fixture FILE name and fixture document TEXT
    # ("Write the onboarding guide"), so both palette sections are populated on
    # any box — a query that only matches files leaves nothing to re-render.
    palette(page, "onboarding")
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
    # the button lives in the user menu (bottom left of the sidebar) at every width
    user_menu(page); page.click('[data-testid="keys-btn"]')
    page.wait_for_selector('[data-testid="shortcuts"]', timeout=4000)
    page.keyboard.press("Escape")
    page.wait_for_selector('[data-testid="shortcuts"]', state="detached", timeout=4000)


# --- tab navigation ----------------------------------------------------------

def test_tab_shortcuts(page):
    palette(page, mine("overview.md"))
    page.keyboard.press("Enter")
    wait_path(page, doc("overview.md"), timeout=8000)
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
