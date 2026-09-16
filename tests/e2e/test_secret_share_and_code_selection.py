"""Two things the user could see were wrong, pinned in a browser.

1. Selecting text inside a fenced code block showed no highlight at all.
   CodeMirror's drawSelection() paints the selection into a layer BEHIND the
   text and hides the browser's own; a code block's solid background covered
   every rectangle drawn under it.
2. A `_secrets/` folder could not be shared with anyone from the UI — the
   panel disabled itself, so a team with a shared project had no way to hand
   each other a credential.
"""
import time

import httpx

from conftest import BASE, CREDS, dlg_ok, expand_folder, login, open_doc
from kbenv import AREA, U
from kbenv import doc as kbdoc

TAG = str(int(time.time()))


def api(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    assert c.post("/login", data={"username": U(user),
                                  "password": CREDS[user]}).status_code == 200
    return c


# --- 1. the selection inside ``` -------------------------------------------
def test_selection_inside_a_code_block_is_painted(browser):
    """Asserted on the mechanism, because that is what broke: the drawn layer
    is unusable under an opaque line, so those lines must carry a real
    ::selection colour of their own. (If a future change makes code blocks
    transparent, this test is the place to reconsider the rule — not a silent
    regression back to an invisible selection.)"""
    name = f"codesel_{TAG}.md"
    path = kbdoc(name)
    c = api("alice")
    c.post("/api/file", json={"path": path})
    c.post("/api/artifact/write", json={
        "path": path,
        "content": "# code\n\ntext before\n\n```python\nsecret_value = 42\nprint(secret_value)\n```\n"})
    time.sleep(1.0)

    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        open_doc(page, path)
        line = page.locator(".cm-line.cm-codeblock").nth(1)   # a line of code
        line.wait_for(state="visible", timeout=8000)

        bg = line.evaluate("el => getComputedStyle(el).backgroundColor")
        assert bg not in ("rgba(0, 0, 0, 0)", "transparent"), \
            f"precondition: a code line paints its own background, got {bg}"

        sel = line.evaluate("el => getComputedStyle(el, '::selection').backgroundColor")
        assert sel not in ("rgba(0, 0, 0, 0)", "transparent"), \
            "a code line must paint the native selection — the drawn one is hidden under it"

        # and an ordinary prose line still relies on the drawn layer
        prose = page.locator(".cm-line:not(.cm-codeblock)").first
        pbg = prose.evaluate("el => getComputedStyle(el).backgroundColor")
        assert pbg in ("rgba(0, 0, 0, 0)", "transparent"), \
            f"prose lines must stay transparent for the drawn selection, got {pbg}"

        # a real selection over the code really does exist in the DOM
        page.evaluate("""() => {
            const v = window.__kbview;
            const i = v.state.doc.toString().indexOf('secret_value');
            v.dispatch({selection: {anchor: i, head: i + 12}});
            v.focus();
        }""")
        assert page.evaluate("() => window.getSelection().toString()") == "secret_value"
    finally:
        ctx.close()
        c.post("/api/fs/delete", json={"path": path})


# --- 2. sharing a _secrets folder from the UI ------------------------------
def test_a_secrets_folder_can_be_shared_from_the_panel(browser):
    proj = kbdoc(f"secshare_{TAG}")
    sec = f"{proj}/_secrets"
    key = f"{sec}/apikey_{TAG}.env"
    marker = f"uisecret_{TAG}"
    c = api("alice")
    assert c.post("/api/fs/mkdir", json={"path": proj}).status_code == 200
    assert c.post("/api/fs/mkdir", json={"path": sec}).status_code == 200
    assert c.post("/fs/newfile", json={"path": key}).status_code == 200
    c.post("/api/artifact/write", json={"path": key, "content": f"KEY={marker}\n"})
    assert api("bob").get("/api/file", params={"path": key}).status_code == 403

    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        expand_folder(page, proj)
        page.hover(f'.tree-item[data-path="{sec}"]')
        page.click(f'.tree-item[data-path="{sec}"] .tbtn[title="Who can open this"]')
        scope = page.locator('[data-testid="sh-scope"]')
        scope.wait_for(state="visible", timeout=8000)
        assert not scope.is_disabled(), "the owner of a secret must be able to share it"

        scope.select_option("people")
        page.select_option('[data-testid="sh-who"]', U("bob"))
        page.select_option('[data-testid="sh-role"]', "view")
        page.click('[data-testid="sh-addbtn"]')
        page.click('[data-testid="sh-save"]')
        try:                       # ancestor-traverse notice, when it appears
            dlg_ok(page)
        except Exception:
            pass
        page.wait_for_selector('.modal-overlay', state='detached', timeout=15000)

        r = api("bob").get("/api/file", params={"path": key})
        assert r.status_code == 200 and marker in r.json()["content"], \
            "bob was added in the panel and still cannot read the key"
    finally:
        ctx.close()
        c.post("/api/fs/delete", json={"path": proj})
