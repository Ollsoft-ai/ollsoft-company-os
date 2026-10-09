"""Text an agent (or vim, or a script) writes into an open document must not
just silently appear: it blooms in, keeps a glow while you are elsewhere, and
fades within ~15 s of you clicking into the document. A colleague's typing is
NOT marked — their caret is already on screen — and neither is the document's
own first load."""
import time

import httpx
from conftest import BASE, CREDS, login
from kbenv import U, doc as kbdoc

BASE_TEXT = (
    "# Glow notes\n\n"
    "line one about the alpha project\n"
    "line two about the beta project\n"
    "line three about the gamma project\n"
)


def api(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def fresh_doc():
    path = kbdoc(f"extglow_{int(time.time() * 1000)}.md")
    k = api("alice")
    assert k.post("/api/file", json={"path": path}).status_code in (200, 409)
    assert k.post("/api/artifact/write", json={"path": path, "content": BASE_TEXT}).status_code == 200
    time.sleep(1.0)   # let the watcher settle on the seeded content
    return path


def open_doc(context, user, path):
    page = login(context, user)
    page.click(f'.tree-item[data-path="{path}"]')
    page.wait_for_function("() => window.__kbview && window.__kbview.state.doc.length > 0")
    return page


def glowing(page):
    return page.evaluate("() => [...document.querySelectorAll('.cm-ext')].map((e) => e.textContent).join('')")


def test_agent_write_glows_until_you_look(browser):
    path = fresh_doc()
    ctx = browser.new_context()
    try:
        page = open_doc(ctx, "alice", path)
        page.wait_for_timeout(1500)
        assert page.locator(".cm-ext, .cm-ext-cut").count() == 0, "the first load is not news"
        page.evaluate("() => document.activeElement && document.activeElement.blur()")

        # the agent drops line two and appends a line, straight to disk
        with open(f"/srv/kb/{path}", "w") as f:
            f.write(BASE_TEXT.replace("line two about the beta project\n", "") + "AGENT WAS HERE\n")
        page.wait_for_function("() => [...document.querySelectorAll('.cm-ext')]"
                               ".map((e) => e.textContent).join('').includes('AGENT WAS HERE')",
                               timeout=15000)
        assert page.locator(".cm-ext-cut").count() == 1, "the removed line leaves a tick"
        assert "alpha" not in glowing(page), "only the new text glows"

        # nobody has looked yet: it holds
        page.wait_for_timeout(3000)
        assert "AGENT WAS HERE" in glowing(page)

        # click in: it starts fading and is gone after the fade
        page.click(".cm-content")
        page.wait_for_function(
            "() => [...window.__kbview.dom.style].some((k) =>"
            " k.startsWith('--kbx-a') && parseFloat(window.__kbview.dom"
            ".style.getPropertyValue(k)) < 1)", timeout=8000)
        page.wait_for_function("() => !document.querySelector('.cm-ext, .cm-ext-cut')", timeout=25000)
    finally:
        ctx.close()
        api("alice").post("/api/fs/delete", json={"path": path})


def test_colleague_typing_does_not_glow(browser):
    path = fresh_doc()
    ca, cb = browser.new_context(), browser.new_context()
    try:
        a = open_doc(ca, "alice", path)
        b = open_doc(cb, "bob", path)
        a.evaluate("() => document.activeElement && document.activeElement.blur()")
        b.evaluate("""() => { const v = window.__kbview;
                              v.dispatch({ changes: { from: v.state.doc.length, insert: 'bob typed this\\n' } }); }""")
        a.wait_for_function("() => window.__kbview.state.doc.toString().includes('bob typed this')", timeout=15000)
        a.wait_for_timeout(800)
        assert a.locator(".cm-ext, .cm-ext-cut").count() == 0, "a colleague's typing is not marked"

        # …while an agent's write, with bob still there, is — as a whole word,
        # though syncd sends only the three letters that changed
        with open(f"/srv/kb/{path}") as f:
            disk = f.read()
        with open(f"/srv/kb/{path}", "w") as f:
            f.write(disk.replace("alpha project", "alphabet project"))
        a.wait_for_selector(".cm-ext", timeout=15000)
        assert glowing(a) == "alphabet"
    finally:
        ca.close()
        cb.close()
        api("alice").post("/api/fs/delete", json={"path": path})
