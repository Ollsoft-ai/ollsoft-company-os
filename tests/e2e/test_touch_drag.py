"""A finger moves tabs too: hold a tab to lift it, carry it, let go — into
another group, below one, into the panel — and a plain swipe on the strip is
still a scroll."""
from conftest import login, wait_path, user_menu
from kbenv import doc


def open_doc(page, path):
    """On a phone the drawer closes when a file opens; open it again first."""
    if not page.evaluate("() => document.body.classList.contains('nav-open')"):
        page.click("#nav-btn")
        page.wait_for_function("() => document.body.classList.contains('nav-open')")
    page.click(f'.tree-item[data-path="{path}"]')
    wait_path(page, path)

PHONE = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}

TOUCH = """async ([sel, moves, hold]) => {
  const el = document.querySelector(sel);
  if (!el) return 'no ' + sel;
  const r = el.getBoundingClientRect();
  const x0 = r.left + r.width / 2, y0 = r.top + r.height / 2;
  const ev = (type, target, x, y) => target.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true,
    pointerType: 'touch', pointerId: 7, isPrimary: true, clientX: x, clientY: y}));
  ev('pointerdown', el, x0, y0);
  await new Promise(r => setTimeout(r, hold));
  let last = [x0, y0];
  for (const [fx, fy, tsel] of moves) {
    const tr = (tsel ? document.querySelector(tsel) : document.body).getBoundingClientRect();
    const x = tr.left + tr.width * fx, y = tr.top + tr.height * fy;
    const under = document.elementFromPoint(x, y) || document.body;
    ev('pointermove', under, x, y);
    await new Promise(r => requestAnimationFrame(r));
    last = [x, y];
  }
  const under = document.elementFromPoint(last[0], last[1]) || document.body;
  ev('pointerup', under, last[0], last[1]);
  return 'ok';
}"""

LAYOUT = """() => ({
  groups: [...document.querySelectorAll('#panes .pane')].map(p => [...p.querySelectorAll('.tab')].map(t => t.dataset.path || ('term:' + t.dataset.sid))),
  dock: [...document.querySelectorAll('#term-tabs .tab')].map(t => t.dataset.path || ('term:' + t.dataset.sid)),
  ghost: !!document.querySelector('.tab-ghost'),
  dragging: document.body.classList.contains('dragging-tab'),
})"""


def test_hold_and_carry_a_tab_below_its_group_on_a_phone(browser):
    ctx = browser.new_context(**PHONE)
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_doc(page, doc("onboarding.md")); wait_path(page, doc("onboarding.md"))
    assert page.evaluate(LAYOUT)["groups"] == [[doc("overview.md"), doc("onboarding.md")]]
    # a quick swipe on the tab is not a drag: nothing lifts
    r = page.evaluate(TOUCH, [f'.tab[data-path="{doc("onboarding.md")}"]', [[0.5, 0.9, "#panes .pane"]], 60])
    assert r == "ok"
    lay = page.evaluate(LAYOUT)
    assert lay["groups"] == [[doc("overview.md"), doc("onboarding.md")]] and not lay["ghost"]
    # held, then carried to the bottom of the group: a new group below
    r = page.evaluate(TOUCH, [f'.tab[data-path="{doc("onboarding.md")}"]',
                              [[0.5, 0.5, "#panes .pane"], [0.5, 0.9, "#panes .pane"]], 450])
    assert r == "ok"
    page.wait_for_function("() => document.querySelectorAll('#panes .pane').length === 2", timeout=5000)
    page.wait_for_function("() => !document.querySelector('.tab-ghost')", timeout=3000)   # it fades out
    lay = page.evaluate(LAYOUT)
    assert lay["groups"] == [[doc("overview.md")], [doc("onboarding.md")]]
    assert not lay["dragging"], "the overlays are gone after the drop"
    # both are on screen, stacked
    boxes = page.evaluate("() => [...document.querySelectorAll('#panes .pane')].map(p => p.getBoundingClientRect().top)")
    assert boxes[0] < boxes[1]
    ctx.close()


def test_a_tab_carried_into_a_group_of_its_own(browser):
    """A phone has no terminal panel — a terminal is a tab. Carrying one down
    puts it in a group of its own under the documents, and carrying it back
    onto them returns it to their strip."""
    ctx = browser.new_context(**PHONE)
    page = login(ctx, "alice")
    open_doc(page, doc("overview.md")); wait_path(page, doc("overview.md"))
    open_doc(page, doc("onboarding.md")); wait_path(page, doc("onboarding.md"))
    page.click("#nav-btn")   # the person's menu lives in the drawer on a phone
    page.wait_for_function("() => document.body.classList.contains('nav-open')")
    user_menu(page); page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#panes .tab-content.term .xterm-rows")
    sid = page.evaluate("() => window.__kbterms[0].sid")
    lay = page.evaluate(LAYOUT)
    assert lay["dock"] == [] and lay["groups"] == [[doc("overview.md"), doc("onboarding.md"), "term:" + sid]], lay
    # carry it to the bottom of the group: a group of its own, below
    r = page.evaluate(TOUCH, [f'.tab[data-sid="{sid}"]',
                              [[0.5, 0.5, "#panes .pane"], [0.5, 0.92, "#panes .pane"]], 450])
    assert r == "ok"
    page.wait_for_function("() => document.querySelectorAll('#panes .pane').length === 2", timeout=5000)
    lay = page.evaluate(LAYOUT)
    assert lay["groups"] == [[doc("overview.md"), doc("onboarding.md")], ["term:" + sid]], lay
    page.wait_for_function("() => window.__kbterm && window.__kbterm.cols > 10")
    # …and back onto the documents' strip
    r = page.evaluate(TOUCH, [f'.tab[data-sid="{sid}"]',
                              [[0.5, 0.5, "#panes .pane"], [0.5, 0.5, "#panes .pane"]], 450])
    assert r == "ok"
    page.wait_for_function("() => document.querySelectorAll('#panes .pane').length === 1", timeout=5000)
    assert page.evaluate("() => document.querySelector('#terminal-panel').hidden"), "no panel on a phone"
    ctx.close()
