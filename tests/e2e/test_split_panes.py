"""The wide-screen half of the editor: a tree you can widen and that tells you
the names it had to cut, a document that uses the whole column, and editor
panes you build by dragging a tab side by side.

Drag-and-drop is driven the way this repo already drives it (test_rich_editor):
one real DataTransfer carried from dragstart to drop. Native drags hang headless
Chromium's input pipeline, and the browser's part (draggable=true) is not ours.
"""
import time

import httpx
import pytest
from conftest import BASE, CREDS, login
from kbenv import AREA, U, doc

TAG = str(int(time.time()))
FOLDER = doc(f"panes_{TAG}")
LONG_NAME = "a-document-whose-name-is-far-too-long-for-any-file-tree-column.md"


def api(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


@pytest.fixture()
def docs():
    """Three documents of this test's own, in a folder of its own."""
    c = api("alice")
    assert c.post("/api/fs/mkdir", json={"path": FOLDER}).status_code == 200
    paths = [f"{FOLDER}/one.md", f"{FOLDER}/two.md", f"{FOLDER}/{LONG_NAME}"]
    for p in paths:
        assert c.post("/api/file", json={
            "path": p, "content": f"# {p}\n\n" + "prose. " * 200 + "\n"}).status_code == 200
    try:
        yield paths
    finally:
        c.post("/api/fs/delete", json={"path": FOLDER})


# Dispatched by hand so the DataTransfer that dragstart fills is the exact
# object drop reads — the same contract the real browser provides.
DRAG_JS = """async ([path, targetSel, fx]) => {
  const tab = document.querySelector('#panes .tab[data-path="' + path + '"]');
  if (!tab) return {err: 'no tab for ' + path};
  if (!tab.draggable) return {err: 'tab is not draggable'};
  const dt = new DataTransfer();
  const ds = new DragEvent('dragstart', {bubbles: true, cancelable: true});
  Object.defineProperty(ds, 'dataTransfer', {value: dt});
  tab.dispatchEvent(ds);
  const target = document.querySelector(targetSel);
  if (!target) return {err: 'no drop target ' + targetSel};
  const r = target.getBoundingClientRect();
  if (!r.width) return {err: 'drop target is not laid out'};
  const x = r.left + r.width * fx, y = r.top + r.height * 0.5;
  for (const type of ['dragover', 'drop']) {
    const ev = new DragEvent(type, {bubbles: true, cancelable: true, clientX: x, clientY: y});
    Object.defineProperty(ev, 'dataTransfer', {value: dt});
    target.dispatchEvent(ev);
  }
  tab.dispatchEvent(new DragEvent('dragend', {bubbles: true}));
  await new Promise(res => setTimeout(res, 300));
  return {ok: true};
}"""

LAYOUT_JS = """() => ({
  panes: [...document.querySelectorAll('#panes > .pane')].map(p => ({
    tabs: [...p.querySelectorAll('.tab')].map(t => t.dataset.path),
    current: (p.querySelector('.tab.current') || {dataset: {}}).dataset.path || null,
    width: Math.round(p.getBoundingClientRect().width),
    showing: [...p.querySelectorAll('.tab-content')]
      .filter(c => c.style.display !== 'none').length,
  })),
  splits: document.querySelectorAll('#panes > .pane-split').length,
  active: window.__kbpath,
})"""


def layout(page):
    return page.evaluate(LAYOUT_JS)


def open_tab(page, path):
    page.click(f'.tree-item[data-path="{path}"]', timeout=15000)
    page.wait_for_selector(f'#panes .tab[data-path="{path}"]')
    page.wait_for_timeout(400)


def test_truncated_tree_name_gets_a_tooltip(browser, docs):
    """An ellipsis you cannot expand is information you do not have."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = login(ctx, "alice")
    try:
        long_path = f"{FOLDER}/{LONG_NAME}"
        page.wait_for_selector(f'.tree-item[data-path="{long_path}"]', timeout=15000)
        row = page.locator(f'.tree-item[data-path="{long_path}"]')
        assert row.get_attribute("title") is None, "no tooltip before you point at it"
        row.hover()
        page.wait_for_timeout(150)
        assert row.get_attribute("title") == LONG_NAME

        # A name that fits gets no tooltip — otherwise every row would nag.
        # The control is this fixture's own `one.md`, a SIBLING of the long
        # name: same folder, same indent, so the pair isolates the one thing
        # that differs. It used to be AREA, which was `company` when the suite
        # had no namespaces and is `kbtest-<ns>` one level deeper now — long
        # enough at this viewport that it really is clipped, so the app was
        # right to label it and the assertion was simply aimed at the wrong row.
        short = page.locator(f'.tree-item[data-path="{FOLDER}/one.md"]')
        short.hover()
        page.wait_for_timeout(150)
        assert short.get_attribute("title") is None
    finally:
        ctx.close()


def test_file_tree_resizes_and_remembers_its_width(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = login(ctx, "alice")
    try:
        width = "() => document.querySelector('.sidebar').getBoundingClientRect().width"
        start = page.evaluate(width)
        box = page.locator("#sb-resizer").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
        page.mouse.down()
        page.mouse.move(box["x"] + 150, box["y"] + 200, steps=8)
        page.mouse.up()
        page.wait_for_timeout(200)
        wider = page.evaluate(width)
        assert wider > start + 100, (start, wider)

        # the width is a preference, so it has to survive the reload
        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        page.wait_for_timeout(300)
        assert abs(page.evaluate(width) - wider) < 2

        # dragging it off the screen must still leave a usable tree
        box = page.locator("#sb-resizer").bounding_box()
        page.mouse.move(box["x"] + 2, box["y"] + 200)
        page.mouse.down()
        page.mouse.move(2, box["y"] + 200, steps=8)
        page.mouse.up()
        page.wait_for_timeout(200)
        assert page.evaluate(width) >= 140

        page.locator("#sb-resizer").dblclick()
        page.wait_for_timeout(200)
        assert page.evaluate(width) == 264, "double-click restores the default"
    finally:
        ctx.close()


def test_rich_document_uses_the_whole_editor_column(browser, docs):
    """A 60rem cap wrapped prose in the middle of a wide window."""
    ctx = browser.new_context(viewport={"width": 2200, "height": 900})
    page = login(ctx, "alice")
    try:
        page.evaluate("() => localStorage.setItem('kbEditMode', 'rich')")
        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        open_tab(page, docs[0])
        page.wait_for_selector(".cm-rich .cm-content")
        w = page.evaluate("""() => ({
          content: document.querySelector('.cm-rich .cm-content').getBoundingClientRect().width,
          column: document.querySelector('#panes > .pane .editor').getBoundingClientRect().width,
        })""")
        assert w["column"] > 1500, w        # the window really is wide
        assert w["content"] >= w["column"] - 20, w
    finally:
        ctx.close()


def test_dragging_a_tab_to_the_edge_splits_the_editor(browser, docs):
    ctx = browser.new_context(viewport={"width": 1800, "height": 950})
    page = login(ctx, "alice")
    try:
        page.evaluate("() => localStorage.removeItem('kbOpen')")
        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        one, two, three = docs
        for p in (one, two, three):
            open_tab(page, p)
        st = layout(page)
        assert len(st["panes"]) == 1 and st["splits"] == 0, st

        # right edge of the only pane -> a second column, carrying that tab
        r = page.evaluate(DRAG_JS, [one, "#panes > .pane .pane-drop", 0.9])
        assert r.get("ok"), r
        st = layout(page)
        assert len(st["panes"]) == 2 and st["splits"] == 1, st
        assert st["panes"][1]["tabs"] == [one], st
        assert st["active"] == one, st
        # both columns show a document at once — that is the whole point
        assert st["panes"][0]["showing"] == 1 and st["panes"][1]["showing"] == 1, st
        # the single-pane selectors the rest of the app (and these tests) use
        assert page.locator("#tabbar").count() == 1
        assert page.locator("#editor").count() == 1

        # middle of a pane -> move, not split
        r = page.evaluate(DRAG_JS, [three, "#panes > .pane:last-of-type .pane-drop", 0.5])
        assert r.get("ok"), r
        st = layout(page)
        assert len(st["panes"]) == 2, st
        assert st["panes"][1]["tabs"] == [one, three], st
        assert st["panes"][0]["tabs"] == [two], st

        # each pane keeps showing its OWN document
        page.click(f'#panes > .pane:first-of-type .tab[data-path="{two}"]')
        page.wait_for_timeout(300)
        st = layout(page)
        assert st["active"] == two, st
        assert st["panes"][1]["current"] == three, "the other pane must not blank"

        # emptying a pane retires it
        for p in (one, three):
            page.click(f'#panes .tab[data-path="{p}"] .tab-x')
            page.wait_for_timeout(300)
        st = layout(page)
        assert len(st["panes"]) == 1 and st["splits"] == 0, st
        assert st["panes"][0]["tabs"] == [two], st
    finally:
        ctx.close()


def test_split_layout_and_pane_widths_survive_a_reload(browser, docs):
    ctx = browser.new_context(viewport={"width": 1800, "height": 950})
    page = login(ctx, "alice")
    try:
        page.evaluate("() => localStorage.removeItem('kbOpen')")
        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        one, two, _ = docs
        open_tab(page, one)
        open_tab(page, two)
        assert page.evaluate(DRAG_JS, [two, "#panes > .pane .pane-drop", 0.9]).get("ok")

        # drag the handle so the two columns are visibly unequal
        box = page.locator("#panes > .pane-split").bounding_box()
        page.mouse.move(box["x"] + 2, box["y"] + 200)
        page.mouse.down()
        page.mouse.move(box["x"] - 300, box["y"] + 200, steps=8)
        page.mouse.up()
        page.wait_for_timeout(300)
        before = layout(page)
        assert before["panes"][0]["width"] < before["panes"][1]["width"] - 200, before

        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        page.wait_for_function("() => document.querySelectorAll('#panes > .pane').length === 2",
                               timeout=15000)
        page.wait_for_timeout(500)
        after = layout(page)
        assert [p["tabs"] for p in after["panes"]] == [p["tabs"] for p in before["panes"]], after
        assert abs(after["panes"][0]["width"] - before["panes"][0]["width"]) < 12, (before, after)
    finally:
        ctx.close()
