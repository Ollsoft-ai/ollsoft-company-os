"""Live collaboration UX: presence avatars (who has the doc open) and remote
cursors rendered with the collaborator's name — both in the rich rendered view.

Presence + cursors both ride on the Yjs awareness that flows through kb-syncd;
this proves the awareness relay works end-to-end AND that a late joiner learns
about people who were already there (the re-announce-on-join fix)."""
import time

from conftest import BASE, CREDS, dlg_fill
from kbenv import U, doc


def login(ctx, user):
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', CREDS[user])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    return page


def presence_titles(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('#presence .presence-avatar')].map(a => a.title)")


def caret_names(page):
    return page.evaluate(
        "() => [...document.querySelectorAll('.cm-ySelectionInfo')].map(e => e.textContent)")


def open_shared(browser, cleanup_paths, doc):
    """alice creates+seeds `doc`; bob opens it. Returns (ctxs, k_page, j_page)."""
    ck = browser.new_context(); k = login(ck, "alice")
    k.click('[data-testid="newdoc"]')
    dlg_fill(k, doc)
    k.wait_for_function(f"() => window.__kbview && window.__kbpath === '{doc}'", timeout=10000)
    k.evaluate("() => window.__kbview.dispatch({changes:{from:0,"
               "insert:'# Shared\\n\\nfirst line\\n\\nfourth line here\\n'}})")
    k.evaluate("() => { const v=window.__kbview; v.dispatch({selection:{anchor:3}}); v.focus(); }")
    cleanup_paths.append(doc)
    time.sleep(1)
    cj = browser.new_context(); j = login(cj, "bob")
    j.click(f'.tree-item[data-path="{doc}"]')
    j.wait_for_function("() => window.__kbview && window.__kbview.state.doc.length > 0", timeout=10000)
    time.sleep(2)   # let awareness converge (incl. the late-joiner re-announce)
    return (ck, cj), k, j


def _cleanup(paths):
    import httpx
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    for p in paths:
        c.post("/api/fs/delete", json={"path": p})


def test_presence_shows_all_viewers_both_sides(browser):
    paths = []
    (ck, cj), k, j = open_shared(browser, paths, doc(f"pres_{int(time.time())}.md"))
    try:
        kp, jp = presence_titles(k), presence_titles(j)
        assert any("alice (you)" in t for t in kp) and any(t.startswith("bob") for t in kp), kp
        assert any("bob (you)" in t for t in jp) and any(t.startswith("alice") for t in jp), jp
        # self avatar carries the accent ring class
        assert k.locator("#presence .presence-avatar.self").count() == 1
    finally:
        ck.close(); cj.close(); _cleanup(paths)


def test_remote_cursor_renders_with_name_in_rich_view(browser):
    paths = []
    (ck, cj), k, j = open_shared(browser, paths, doc(f"cur_{int(time.time())}.md"))
    try:
        # bob moves his cursor onto "fourth line here"
        j.evaluate("() => { const v=window.__kbview; const l=v.state.doc.line(5);"
                   "v.dispatch({selection:{anchor:l.from+3}}); v.focus(); }")
        time.sleep(1.5)
        assert k.locator(".cm-ySelectionCaret").count() >= 1, "alice must see bob's caret"
        assert "bob" in caret_names(k), "the caret must be labelled with the collaborator's name"
        # and the reverse: alice's caret is visible to bob with alice's name
        k.evaluate("() => { const v=window.__kbview; v.dispatch({selection:{anchor:2}}); v.focus(); }")
        time.sleep(1.5)
        assert "alice" in caret_names(j)
    finally:
        ck.close(); cj.close(); _cleanup(paths)


def test_distinct_colors_per_user(browser):
    paths = []
    (ck, cj), k, j = open_shared(browser, paths, doc(f"col_{int(time.time())}.md"))
    try:
        colors = k.evaluate(
            "() => [...document.querySelectorAll('#presence .presence-avatar')]"
            ".map(a => getComputedStyle(a).backgroundColor)")
        assert len(colors) == 2 and colors[0] != colors[1], colors
    finally:
        ck.close(); cj.close(); _cleanup(paths)


def test_presence_drops_when_a_viewer_leaves(browser):
    paths = []
    (ck, cj), k, j = open_shared(browser, paths, doc(f"leave_{int(time.time())}.md"))
    try:
        assert len(presence_titles(k)) == 2
        # bob closes the document tab -> his awareness state is removed
        j.locator(".tab.active .tab-x").click()
        time.sleep(1.5)
        left = presence_titles(k)
        assert len(left) == 1 and "alice (you)" in left[0], left
    finally:
        ck.close(); cj.close(); _cleanup(paths)
