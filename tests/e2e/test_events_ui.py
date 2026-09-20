"""The browser gets the tree, presence and config over one event stream and
never polls: a file created behind its back appears on its own, a content
change patches the row's timestamp without refetching the tree, and a stream
that is torn down comes back with a catch-up."""
import time

import httpx
from conftest import BASE, CREDS, login, wait_path
from kbenv import U, doc

TAG = str(int(time.time()))


def api(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def test_tree_follows_without_polling(browser):
    ctx = browser.new_context()
    path = doc(f"evui_{TAG}.md")
    b = api("bob")
    try:
        page = login(ctx, "bob")
        page.wait_for_function("() => window.__kbevents && window.__kbevents.mode === 'sse'", timeout=15000)
        fetches = page.evaluate("() => window.__kbtreefetches || 0")
        # created behind the browser's back: the stream says `full`, the tree refetches once
        assert b.post("/fs/newfile", json={"path": path}).status_code in (200, 409)
        page.wait_for_selector(f'.tree-item[data-path="{path}"]', timeout=10000)
        assert page.evaluate("() => window.__kbtreefetches") == fetches + 1
        assert page.evaluate("() => window.__kbevents.polls") == 0
        # a content change: a delta patches the row, no tree refetch at all
        time.sleep(1.2)
        events = page.evaluate("() => window.__kbevents.events")
        assert b.post("/api/artifact/write", json={"path": path, "content": "typed\n"}).status_code == 200
        page.wait_for_function("(n) => window.__kbevents.events > n", arg=events, timeout=10000)
        page.wait_for_timeout(500)
        assert page.evaluate("() => window.__kbtreefetches") == fetches + 1
        # torn down and reopened: a fresh hello, still no polling
        opens = page.evaluate("() => window.__kbevents.opens")
        page.evaluate("() => window.__kbevents.reopen()")
        page.wait_for_function("(n) => window.__kbevents.opens > n && window.__kbevents.mode === 'sse'",
                               arg=opens, timeout=15000)
        assert page.evaluate("() => window.__kbevents.polls") == 0
    finally:
        b.post("/api/fs/delete", json={"path": path})
        ctx.close()
