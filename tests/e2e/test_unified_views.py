"""One layout for everything: a terminal is a tab in a group, the terminal
panel is the dock, groups stack in columns. What the drag zones, the palette
and the keys do, what a reload brings back, and what an old session record
turns into."""
import json

from conftest import login, open_doc, wait_path, user_menu
from kbenv import doc

DRAG_JS = """([path, targetSel, fx, fy]) => {
  const tab = document.querySelector('.tab[data-path="' + path + '"]') || document.querySelector('.tab[data-sid="' + path + '"]');
  const target = document.querySelector(targetSel);
  if (!tab || !target) return {ok: false, why: 'missing ' + (!tab ? 'tab' : 'target')};
  const dt = new DataTransfer();
  tab.dispatchEvent(new DragEvent('dragstart', {bubbles: true, dataTransfer: dt}));
  const r = target.getBoundingClientRect();
  const x = r.left + r.width * fx, y = r.top + r.height * fy;
  target.dispatchEvent(new DragEvent('dragover', {bubbles: true, dataTransfer: dt, clientX: x, clientY: y}));
  target.dispatchEvent(new DragEvent('drop', {bubbles: true, dataTransfer: dt, clientX: x, clientY: y}));
  tab.dispatchEvent(new DragEvent('dragend', {bubbles: true, dataTransfer: dt}));
  return {ok: true};
}"""

LAYOUT_JS = """() => ({
  columns: [...document.querySelectorAll('#panes > .col')].map(c => [...c.querySelectorAll(':scope > .pane')].map(p =>
    [...p.querySelectorAll('.tab')].map(t => t.dataset.path || ('term:' + t.dataset.sid)))),
  dockTabs: [...document.querySelectorAll('#term-tabs .tab')].map(t => t.dataset.path || ('term:' + t.dataset.sid)),
  dockHidden: document.querySelector('#terminal-panel').hidden,
  dockSide: document.querySelector('.main-col').dataset.dock,
  maximized: document.body.classList.contains('maximized'),
  active: window.__kbpath,
})"""


def show_terminal(page):
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    page.wait_for_function("() => window.__kbterm && window.__kbterm.buffer.active.length > 0")


def test_a_terminal_dragged_beside_a_document_keeps_working(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    show_terminal(page)
    sid = page.evaluate("() => window.__kbterms[0].sid")
    lay = page.evaluate(LAYOUT_JS)
    assert lay["dockTabs"] == ["term:" + sid] and not lay["dockHidden"]
    # onto the right edge of the document's group: a new column beside it
    assert page.evaluate(DRAG_JS, [sid, "#panes > .col > .pane .pane-drop", 0.95, 0.5])["ok"]
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 2")
    lay = page.evaluate(LAYOUT_JS)
    assert lay["columns"] == [[[doc("overview.md")]], [["term:" + sid]]]
    assert lay["dockHidden"], "the emptied dock collapses"
    assert lay["active"] == doc("overview.md"), "the header still describes the document"
    # the terminal is alive where it landed: type, and see the echo
    page.click("#panes > .col:last-of-type .tab-content.term")
    page.keyboard.type("echo moved-ok")
    page.keyboard.press("Enter")
    page.wait_for_function("""() => { const b = window.__kbterm.buffer.active; let s = '';
      for (let i = 0; i < b.length; i++) s += b.getLine(i).translateToString(true) + '\\n';
      return s.split('\\n').some(l => l.trim() === 'moved-ok'); }""", timeout=15000)
    # Ctrl+` with an empty dock and a terminal elsewhere goes to that terminal, spawning nothing
    page.click(f'.tab[data-path="{doc("overview.md")}"]')
    page.keyboard.press("Control+`")
    page.wait_for_timeout(500)
    assert page.evaluate("() => window.__kbterms.length") == 1
    assert page.evaluate("() => document.activeElement.closest('.tab-content.term') !== null")
    ctx.close()


def test_a_document_can_live_in_the_dock_and_the_dock_stays(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_doc(page, doc("onboarding.md")); wait_path(page, doc("onboarding.md"))
    show_terminal(page)
    assert page.evaluate(DRAG_JS, [doc("onboarding.md"), "#terminal-panel .pane-drop", 0.5, 0.5])["ok"]
    page.wait_for_function(f"() => document.querySelector('#term-tabs .tab[data-path=\"{doc('onboarding.md')}\"]')")
    lay = page.evaluate(LAYOUT_JS)
    assert lay["dockTabs"][-1] == doc("onboarding.md")
    assert lay["active"] == doc("onboarding.md"), "a document activated in the dock is the active document"
    # kill the dock's terminal: the document keeps the dock open
    page.hover("#term-tabs .tab.term-tab"); page.click("#term-tabs .tab.term-tab .tab-x")
    page.wait_for_function("() => window.__kbterms.length === 0")
    assert not page.evaluate(LAYOUT_JS)["dockHidden"]
    # hide the dock: the active document moves back to a visible one
    page.keyboard.press("Control+`")
    page.wait_for_function("() => document.querySelector('#terminal-panel').hidden")
    assert page.evaluate("() => window.__kbpath") == doc("overview.md")
    ctx.close()


def test_split_below_maximize_and_dock_side(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_doc(page, doc("onboarding.md")); wait_path(page, doc("onboarding.md"))
    page.keyboard.press("Alt+Shift+\\")
    page.wait_for_function("() => document.querySelectorAll('#panes > .col > .pane').length === 2")
    lay = page.evaluate(LAYOUT_JS)
    assert lay["columns"] == [[[doc("overview.md")], [doc("onboarding.md")]]], "one column, two groups"
    assert page.locator("#panes .row-split").count() == 1
    # maximize the focused group and restore it
    page.keyboard.press("Alt+Z")
    page.wait_for_function("() => document.body.classList.contains('maximized')")
    assert page.evaluate("() => [...document.querySelectorAll('#panes .pane')].filter(p => p.offsetParent !== null).length") == 1
    page.keyboard.press("Alt+Z")
    page.wait_for_function("() => !document.body.classList.contains('maximized')")
    # the dock on the right, through the palette
    show_terminal(page)
    page.keyboard.press("Control+Shift+P")
    page.fill("[data-testid='palette-input']", ">Terminal panel right")   # ">" = commands mode
    page.wait_for_function("() => [...document.querySelectorAll('.palette-item')].some(e => e.textContent.includes('Terminal panel: right'))")
    page.click(".palette-item:has-text('Terminal panel: right')")
    page.wait_for_function("() => document.querySelector('.main-col').dataset.dock === 'right'")
    box = page.locator("#terminal-panel").bounding_box()
    editor = page.locator("#panes").bounding_box()
    assert box["x"] > editor["x"] + editor["width"] - 5, "the dock is a column on the right"
    assert page.evaluate("() => window.__kbterm.cols") > 10
    # and it all comes back after a reload
    page.reload()
    page.wait_for_selector("#terminal .xterm-rows", timeout=30000)
    page.wait_for_function("() => document.querySelectorAll('#panes > .col > .pane').length === 2")
    lay = page.evaluate(LAYOUT_JS)
    assert lay["dockSide"] == "right" and lay["columns"] == [[[doc("overview.md")], [doc("onboarding.md")]]]
    ctx.close()


def test_a_pre_grid_session_record_is_migrated(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    # (the URL is overview.md, and a deep link wins over the restored active tab —
    # so the record's `active` is that one; the second column's own selection
    # is what the migration must bring back)
    v1 = {"tabs": [{"path": doc("overview.md"), "kind": "doc", "pane": 0}, {"path": doc("onboarding.md"), "kind": "doc", "pane": 1},
                   {"path": doc("todos.html"), "kind": "artifact", "pane": 1}],
          "panes": [1, 1], "paneActive": [doc("overview.md"), doc("onboarding.md")], "active": doc("overview.md"),
          "terms": [], "activeTerm": None, "termOpen": False}
    page.evaluate("s => localStorage.setItem('kbOpen', JSON.stringify(s))", v1)
    assert json.loads(page.evaluate("() => localStorage.getItem('kbOpen')")).get("v") is None, "the v1 record is what the app will read"
    page.reload()
    try:
        page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 2", timeout=30000)
    except Exception:
        print("DEBUG", page.evaluate(LAYOUT_JS), page.evaluate("() => localStorage.getItem('kbOpen').slice(0, 400)"))
        raise
    wait_path(page, doc("overview.md"))
    lay = page.evaluate(LAYOUT_JS)
    assert lay["columns"] == [[[doc("overview.md")]], [[doc("onboarding.md"), doc("todos.html")]]]
    page.wait_for_function(f"() => document.querySelector('#panes > .col:last-of-type .tab.current').dataset.path === '{doc('onboarding.md')}'")
    saved = json.loads(page.evaluate("() => localStorage.getItem('kbOpen')"))
    assert saved["v"] == 2 and saved["active"] == doc("overview.md")
    assert [t["path"] for t in saved["tabs"]] == [doc("overview.md"), doc("onboarding.md"), doc("todos.html")], "the v1 shadow is written beside the layout"
    assert saved["paneActive"] == [doc("overview.md"), doc("onboarding.md")]
    ctx.close()


def open_artifact(page, path):
    page.click(f'.tree-item[data-path="{path}"]')
    page.wait_for_selector(f'.tab.active[data-path="{path}"]', timeout=10000)


def test_folding_a_lone_group_gives_its_width_away_and_survives_a_reload(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_doc(page, doc("onboarding.md")); wait_path(page, doc("onboarding.md"))
    open_artifact(page, doc("todos.html"))
    # three columns
    assert page.evaluate(DRAG_JS, [doc("onboarding.md"), "#panes > .col > .pane .pane-drop", 0.95, 0.5])["ok"]
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 2")
    assert page.evaluate(DRAG_JS, [doc("todos.html"), "#panes > .col:last-of-type > .pane .pane-drop", 0.95, 0.5])["ok"]
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 3")
    widths = lambda: page.evaluate("() => [...document.querySelectorAll('#panes > .col')].map(c => Math.round(c.getBoundingClientRect().width))")
    before = widths()
    assert min(before) > 300, before
    # fold the middle group: its column becomes a thin rail, the neighbours take the width
    page.locator("#panes > .col").nth(1).locator('.pane [data-act="fold"]').click()
    page.wait_for_function("() => document.querySelectorAll('#panes > .col')[1].classList.contains('folded')")
    after = widths()
    assert after[1] <= 40, after
    assert after[0] > before[0] + 100 and after[2] > before[2] + 100, (before, after)
    handle = page.locator("#panes > .col").nth(1).locator('.pane-handle.on')
    assert handle.count() == 1 and "onboarding.md" in handle.text_content()
    # it comes back the same way after a reload…
    page.reload()
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 3", timeout=30000)
    page.wait_for_function("() => document.querySelectorAll('#panes > .col')[1].classList.contains('folded')", timeout=10000)
    # …and the handle opens it again, with the width restored
    page.locator("#panes > .col").nth(1).locator('.pane-handle.on').click()
    page.wait_for_function("() => !document.querySelectorAll('#panes > .col')[1].classList.contains('folded')")
    restored = widths()
    assert min(restored) > 300, restored
    ctx.close()


def test_a_split_handle_keeps_working_over_an_artifact(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_artifact(page, doc("todos.html"))
    assert page.evaluate(DRAG_JS, [doc("todos.html"), "#panes > .col > .pane .pane-drop", 0.95, 0.5])["ok"]
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 2")
    page.wait_for_selector("iframe.artifact-frame")
    box = page.locator("#panes > .pane-split").bounding_box()
    w0 = page.evaluate("() => Math.round(document.querySelector('#panes > .col').getBoundingClientRect().width)")
    # a real pointer drag that ends over the artifact's iframe
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y); page.mouse.down()
    for i in range(1, 11):
        page.mouse.move(x + 20 * i, y)
    page.mouse.up()
    page.wait_for_timeout(200)
    w1 = page.evaluate("() => Math.round(document.querySelector('#panes > .col').getBoundingClientRect().width)")
    assert w1 > w0 + 150, (w0, w1)
    assert not page.evaluate("() => document.body.classList.contains('pane-resizing')"), "the drag ended"
    ctx.close()


def two_columns(page):
    """overview.md in the first column, onboarding.md alone in a second."""
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_doc(page, doc("onboarding.md")); wait_path(page, doc("onboarding.md"))
    assert page.evaluate(DRAG_JS, [doc("onboarding.md"), "#panes > .col > .pane .pane-drop", 0.95, 0.5])["ok"]
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 2")


def folded(page, i):
    return page.evaluate(f"() => document.querySelectorAll('#panes > .col')[{i}].classList.contains('folded')")


def test_the_last_open_group_never_folds_and_an_emptied_neighbour_opens_a_folded_one(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    two_columns(page)
    cols = page.locator("#panes > .col")
    cols.nth(1).locator('.pane [data-act="fold"]').click()
    page.wait_for_function("() => document.querySelectorAll('#panes > .col')[1].classList.contains('folded')")
    # the other one is the last open group: its strip offers no fold, and the
    # palette's fold refuses with a toast
    page.wait_for_function("() => !document.querySelectorAll('#panes > .col')[0].querySelector('.pane [data-act=\"fold\"]')")
    page.keyboard.press("Control+Shift+P")
    page.keyboard.type("fold")
    page.keyboard.press("Enter")
    page.wait_for_function("() => /stays open/.test(document.querySelector('#toasts').textContent)")
    assert not folded(page, 0)
    assert not page.evaluate("() => document.querySelector('#panes > .col > .pane').hidden")
    # close the open group's only tab: its column goes, and the folded
    # neighbour opens instead of leaving a blank workspace behind a rail
    page.click(f'.tab[data-path="{doc("overview.md")}"] .tab-x')
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 1")
    page.wait_for_function("() => !document.querySelector('#panes > .col').classList.contains('folded')"
                           " && !document.querySelector('#panes > .col > .pane').hidden")
    wait_path(page, doc("onboarding.md"))
    page.reload()
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 1"
                           " && !document.querySelector('#panes > .col > .pane').hidden", timeout=30000)
    wait_path(page, doc("onboarding.md"))
    ctx.close()


def test_what_you_open_while_a_group_is_maximized_is_what_you_see(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    two_columns(page)
    page.locator("#panes > .col").nth(1).locator('.pane [data-act="max"]').click()
    page.wait_for_function("() => document.body.classList.contains('maximized')")
    # a new document opens into the maximized group — the only one in view
    open_artifact(page, doc("todos.html"))
    lay = page.evaluate(LAYOUT_JS)
    assert lay["maximized"], lay
    assert lay["columns"] == [[[doc("overview.md")]], [[doc("onboarding.md"), doc("todos.html")]]], lay
    # a tab that lives in a hidden group brings the layout back
    page.click(f'.tree-item[data-path="{doc("overview.md")}"]')
    page.wait_for_function("() => !document.body.classList.contains('maximized')")
    wait_path(page, doc("overview.md"))
    assert page.locator("#panes > .col").nth(0).locator(".pane").first.is_visible()
    assert page.locator("#panes > .col").nth(1).locator(".pane").first.is_visible()
    ctx.close()


def test_a_tab_dropped_on_a_folded_handle_goes_in_and_opens_the_group(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    two_columns(page)
    open_artifact(page, doc("todos.html"))   # into the active document's group: the first column
    page.locator("#panes > .col").nth(1).locator('.pane [data-act="fold"]').click()
    page.wait_for_function("() => document.querySelectorAll('#panes > .col')[1].classList.contains('folded')")
    assert page.evaluate(DRAG_JS, [doc("todos.html"), "#panes > .col:last-of-type > .pane-handle.on", 0.5, 0.5])["ok"]
    page.wait_for_function("() => !document.querySelectorAll('#panes > .col')[1].classList.contains('folded')")
    lay = page.evaluate(LAYOUT_JS)
    assert lay["columns"] == [[[doc("overview.md")]], [[doc("onboarding.md"), doc("todos.html")]]], lay
    assert lay["active"] == doc("todos.html")
    ctx.close()


W = "() => [...document.querySelectorAll('#panes > .col')].map(c => Math.round(c.getBoundingClientRect().width))"
PW = "() => Math.round(document.querySelector('#panes').getBoundingClientRect().width)"
TP = "() => Math.round(document.querySelector('#terminal-panel').getBoundingClientRect().%s)"


def test_fold_and_maximize_fill_after_a_reload(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    two_columns(page)
    open_artifact(page, doc("todos.html"))
    assert page.evaluate(DRAG_JS, [doc("todos.html"), "#panes > .col:last-of-type > .pane .pane-drop", 0.95, 0.5])["ok"]
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 3")
    page.reload()
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 3", timeout=30000)
    page.wait_for_selector("iframe.artifact-frame")
    total = page.evaluate(PW)
    page.locator("#panes > .col").nth(0).locator('.pane [data-act="fold"]').click()
    page.wait_for_function("() => document.querySelectorAll('#panes > .col')[0].classList.contains('folded')")
    w = page.evaluate(W); print("fold col0:", w, total)
    assert abs(sum(w) - total) <= 12 and w[0] <= 40 and min(w[1:]) > 400, (w, total)
    page.locator("#panes > .col").nth(1).locator('.pane [data-act="fold"]').click()
    page.wait_for_function("() => document.querySelectorAll('#panes > .col')[1].classList.contains('folded')")
    w = page.evaluate(W); print("fold col1:", w, total)
    assert w[2] > total - 90, (w, total)
    page.locator("#panes > .col").nth(0).locator(".pane-handle.on").click()
    page.locator("#panes > .col").nth(1).locator(".pane-handle.on").click()
    page.wait_for_function("() => ![...document.querySelectorAll('#panes > .col')].some(c => c.classList.contains('folded'))")
    w = page.evaluate(W); print("unfolded:", w, total)
    assert abs(sum(w) - total) <= 12, (w, total)
    page.locator("#panes > .col").nth(1).locator('.pane [data-act="max"]').click()
    page.wait_for_function("() => document.body.classList.contains('maximized')")
    mw = page.evaluate("() => Math.round(document.querySelector('.pane.maximized').getBoundingClientRect().width)")
    print("maximized:", mw, total)
    assert mw > total - 10, (mw, total)
    page.locator(".pane.maximized").locator('[data-act="max"]').click()
    page.wait_for_function("() => !document.body.classList.contains('maximized')")
    page.click(f'.tab[data-path="{doc("todos.html")}"] .tab-x')
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 2")
    w = page.evaluate(W); print("after close:", w, total)
    assert abs(sum(w) - total) <= 8, (w, total)
    ctx.close()


def test_maximize_with_the_dock_open_and_the_left_dock_handle(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    show_terminal(page)
    H = "() => Math.round(document.querySelector('.main-col').getBoundingClientRect().height)"
    total = page.evaluate(H)
    page.locator("#panes > .col").nth(0).locator('.pane [data-act="max"]').click()
    page.wait_for_function("() => document.body.classList.contains('maximized')")
    h = page.evaluate("() => Math.round(document.querySelector('.pane.maximized').getBoundingClientRect().height)")
    print("doc maximized with dock open:", h, total)
    assert h > total - 10, (h, total)
    page.keyboard.press("Control+Backquote")
    page.wait_for_function("() => !document.body.classList.contains('maximized')")
    assert not page.evaluate("() => document.querySelector('#terminal-panel').hidden")
    assert page.evaluate("() => document.querySelector('#terminal-panel').classList.contains('focused')")
    page.click("#term-max")
    page.wait_for_function("() => document.body.classList.contains('maximized')")
    h = page.evaluate(TP % "height"); print("dock maximized:", h, total)
    assert h > total - 10, (h, total)
    page.click("#term-max")
    page.wait_for_function("() => !document.body.classList.contains('maximized')")
    page.keyboard.press("Control+Shift+P"); page.keyboard.type("Terminal panel: left"); page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelector('.main-col').dataset.dock === 'left'")
    w0 = page.evaluate(TP % "width")
    box = page.locator("#term-resizer").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y); page.mouse.down()
    for i in range(1, 11):
        page.mouse.move(x + 20 * i, y)
    page.mouse.up()
    page.wait_for_timeout(200)
    w1 = page.evaluate(TP % "width"); print("left dock drag right:", w0, w1)
    assert w1 > w0 + 150, (w0, w1)
    page.click("#term-max")
    page.wait_for_function("() => document.body.classList.contains('maximized')")
    mw = page.evaluate(TP % "width")
    tw = page.evaluate("() => Math.round(document.querySelector('.main-col').getBoundingClientRect().width)")
    print("left dock maximized:", mw, tw)
    assert mw > tw - 10, (mw, tw)
    page.click("#term-max")
    page.keyboard.press("Control+Shift+P"); page.keyboard.type("Terminal panel: bottom"); page.keyboard.press("Enter")
    page.wait_for_function("() => document.querySelector('.main-col').dataset.dock === 'bottom'")
    ctx.close()


def test_restore_keeps_the_terminal_a_group_showed(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_doc(page, doc("onboarding.md")); wait_path(page, doc("onboarding.md"))
    # a terminal in the document's own group (the palette's way; there is no ＋)
    page.click("#panes > .col:first-child .cm-content >> visible=true")
    page.keyboard.press("Control+Shift+Backquote")
    page.wait_for_selector("#terminal-panel:not([hidden])")
    page.keyboard.press("Control+Shift+P"); page.keyboard.type("Move tab to the column on the left"); page.keyboard.press("Enter")
    page.wait_for_selector("#panes .tab-content.term .xterm-rows")
    page.wait_for_function("() => document.querySelector('#panes .tab.current.term-tab')")
    page.reload()
    page.wait_for_selector("#panes .tab-content.term .xterm-rows", timeout=30000)
    page.wait_for_function("() => document.querySelector('#panes .tab.current.term-tab')", timeout=10000)
    assert page.evaluate("() => window.__kbpath") == doc("onboarding.md")
    ctx.close()


def test_a_tall_window_clamps_the_dock_while_dragging_and_a_narrow_mouse_window_keeps_its_record(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 1400})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    show_terminal(page)
    # the dock's drag clamps to a tenth of the pair WHILE dragging: no jump on release
    box = page.locator("#term-resizer").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y); page.mouse.down()
    for i in range(1, 41):
        page.mouse.move(x, y + 30 * i)
    during = page.evaluate(TP % "height")
    page.mouse.up()
    page.wait_for_timeout(150)
    after = page.evaluate(TP % "height")
    total = page.evaluate("() => Math.round(document.querySelector('.main-col').getBoundingClientRect().height)")
    print("dock at the bottom stop:", during, after, total)
    assert abs(during - after) <= 2, (during, after)
    assert abs(after - total * 0.1) <= 8, (after, total)
    # a desktop window dragged under the phone breakpoint keeps its record:
    # no automatic full-screen sheet, nothing rewritten, and the handle is
    # back — hidden only while something is maximized — when it widens
    page.set_viewport_size({"width": 700, "height": 900})
    page.wait_for_timeout(200)
    page.reload()
    page.wait_for_selector("#terminal-panel:not([hidden])", timeout=30000)
    page.wait_for_timeout(500)
    assert page.evaluate("() => JSON.parse(localStorage.kbOpen).maximized") is None
    assert not page.evaluate("() => document.body.classList.contains('maximized')")
    page.set_viewport_size({"width": 1400, "height": 900})
    page.wait_for_timeout(200)
    assert not page.evaluate("() => document.body.classList.contains('maximized')")
    assert page.locator("#term-resizer").is_visible()
    page.click("#term-max")
    page.wait_for_function("() => document.body.classList.contains('maximized')")
    assert not page.locator("#term-resizer").is_visible()
    ctx.close()


def test_the_keyboard_never_lands_on_body(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    two_columns(page)
    page.click("#panes > .col:last-of-type .cm-content")
    page.keyboard.press("Alt+KeyW")   # closes the column's only tab, and the column
    page.wait_for_function("() => document.querySelectorAll('#panes > .col').length === 1")
    assert page.evaluate("() => !!document.activeElement.closest('.cm-content')"), "the survivor's document has the caret"
    page.keyboard.press("Control+Shift+P"); page.keyboard.type("Maximize"); page.keyboard.press("Enter")
    page.wait_for_function("() => document.body.classList.contains('maximized')")
    page.wait_for_timeout(100)
    assert page.evaluate("() => !!document.activeElement.closest('.cm-content')"), "the palette's maximize keeps the caret"
    ctx.close()


def test_a_tiny_mouse_window_hides_the_toolbar_and_a_narrow_strip_keeps_its_name(browser):
    ctx = browser.new_context(viewport={"width": 600, "height": 500})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    page.click(".cm-content")
    page.keyboard.press("Control+Backquote")   # a half-height dock leaves the document ~200px tall
    page.wait_for_selector("#terminal-panel:not([hidden])")
    page.click(".cm-content")
    page.wait_for_function("() => document.querySelector('#panes .pane').classList.contains('tight')")
    assert page.evaluate("() => getComputedStyle(document.querySelector('#mdbar')).display") == "none"
    ctx.close()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    two_columns(page)
    box = page.locator("#panes > .pane-split").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y); page.mouse.down()
    for i in range(1, 41):
        page.mouse.move(x + 25 * i, y)
    page.mouse.up(); page.wait_for_timeout(300)
    col = page.locator("#panes > .col").nth(1)
    w = page.evaluate("() => Math.round(document.querySelectorAll('#panes > .col')[1].getBoundingClientRect().width)")
    assert w <= 240, w
    assert col.locator('.pane [data-act="max"]').is_visible()
    assert not col.locator('.pane [data-act="fold"]').is_visible()
    name_w = col.locator(".tab-name").first.evaluate("e => e.clientWidth")
    print("narrow column:", w, "name width:", name_w)
    assert name_w >= 60, name_w
    ctx.close()


def test_one_group_offers_no_maximize(browser):
    """Maximize is only there when something else is on screen to step aside."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    assert page.locator('#panes [data-act="max"]').count() == 0
    assert page.locator('#panes [data-act="term"]').count() == 0, "the ＋ is gone for good"
    # a terminal panel is a second group: now there is something to maximize
    page.keyboard.press("Control+Backquote")
    page.wait_for_selector("#terminal-panel:not([hidden])")
    page.wait_for_function("() => document.querySelectorAll('[data-act=\"max\"]').length === 2")
    # …and when it goes, so does the button
    page.click("#term-hide")
    page.wait_for_selector("#dock-handle.on")
    page.wait_for_function("() => document.querySelectorAll('#panes [data-act=\"max\"]').length === 1", timeout=5000)
    ctx.close()
