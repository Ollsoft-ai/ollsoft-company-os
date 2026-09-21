"""The inbox: what happened while you were elsewhere.

Events are written by whoever saw them — syncd when a document you were
`@named` in is committed, the hub when something is shared with you — into
your own `users/<you>/.os/inbox.jsonl`. The app reads that file and marks
lines read; nothing else writes it.
"""
import time

import httpx
from conftest import BASE, CREDS, login, user_menu
from kbenv import U, doc, people


def api(user):
    c = httpx.Client(base_url=BASE, timeout=20)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def inbox(c):
    r = c.get("/api/inbox")
    assert r.status_code == 200, r.text
    return r.json()


def wait_for(c, pred, what, secs=40):
    for _ in range(secs * 2):
        j = inbox(c)
        hit = next((e for e in j["events"] if pred(e)), None)
        if hit:
            return hit
        time.sleep(0.5)
    raise AssertionError(f"{what} never arrived: {inbox(c)['events'][:3]}")


def test_being_mentioned_in_a_document_lands_in_your_inbox():
    """alice writes @bob into a shared document; syncd commits it and tells
    bob. The line comes with it, so the inbox says what was said."""
    a, b = api("alice"), api("bob")
    b.post("/api/inbox/read", json={"clear": True})
    path = doc(f"mention-{int(time.time())}.md")
    a.post("/api/artifact/write",
           json={"path": path, "content": f"# plan\n\nhey @{U('bob')} can you look at this?\n"})
    try:
        ev = wait_for(b, lambda e: e["kind"] == "mention" and e["path"] == path, "the mention")
        assert ev["line"] == 3 and U("bob") in ev["text"]
        assert ev["actor"] == U("alice"), ev
        assert ev["read"] is False
        # …and it is bob's file to mark read
        assert b.post("/api/inbox/read", json={"ids": [ev["id"]]}).json()["unread"] == 0
        assert all(e["read"] for e in inbox(b)["events"])
        # alice, who wrote it, is told nothing
        assert all(e["path"] != path for e in inbox(a)["events"])
    finally:
        a.post("/api/fs/delete", json={"path": path, "permanent": True})
        b.post("/api/inbox/read", json={"clear": True})


def test_a_mention_that_is_already_there_is_not_news():
    """Editing a document that already names you must not tell you again."""
    a, b = api("alice"), api("bob")
    path = doc(f"mention2-{int(time.time())}.md")
    a.post("/api/artifact/write", json={"path": path, "content": f"@{U('bob')} first\n"})
    try:
        wait_for(b, lambda e: e["path"] == path, "the first mention")
        b.post("/api/inbox/read", json={"clear": True})
        a.post("/api/artifact/write", json={"path": path, "content": f"@{U('bob')} first\nand more text\n"})
        time.sleep(12)          # a commit pass or two
        assert all(e["path"] != path for e in inbox(b)["events"]), "told twice about one mention"
    finally:
        a.post("/api/fs/delete", json={"path": path, "permanent": True})
        b.post("/api/inbox/read", json={"clear": True})


def test_sharing_something_tells_the_person_you_shared_it_with():
    a, b = api("alice"), api("bob")
    b.post("/api/inbox/read", json={"clear": True})
    path = doc(f"shared-{int(time.time())}.md")
    a.post("/api/artifact/write", json={"path": path, "content": "# for you\n"})
    try:
        r = a.post("/fs/share", json={"path": path, "scope": "people",
                                      "people": people([("bob", "view")])})
        assert r.status_code == 200, r.text
        ev = wait_for(b, lambda e: e["kind"] == "share" and e["path"] == path, "the share")
        assert ev["actor"] == U("alice")
        assert all(e["kind"] != "share" or e["path"] != path for e in inbox(a)["events"])
    finally:
        a.post("/api/fs/delete", json={"path": path, "permanent": True})
        b.post("/api/inbox/read", json={"clear": True})


def test_the_inbox_is_a_dot_on_the_person_and_a_list_behind_it(browser):
    a, b = api("alice"), api("bob")
    b.post("/api/inbox/read", json={"clear": True})
    path = doc(f"mention-ui-{int(time.time())}.md")
    a.post("/api/artifact/write",
           json={"path": path, "content": f"# ui\n\n@{U('bob')} have a look\n"})
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        wait_for(b, lambda e: e["path"] == path, "the mention")
        page = login(ctx, "bob")
        page.wait_for_function("() => document.querySelector('#user-btn.has-news')", timeout=20000)
        user_menu(page)
        assert page.text_content('[data-testid="inbox-n"]').strip() == "1"
        page.click('[data-testid="inbox-btn"]')
        row = page.locator('[data-testid="inbox-item"]', has_text="mention-ui")
        row.wait_for(timeout=8000)
        assert "have a look" in row.text_content()
        row.click()                                   # opens the document at the line
        page.wait_for_function("(p) => window.__kbpath === p", arg=path, timeout=10000)
        page.wait_for_function("() => !document.querySelector('#user-btn.has-news')", timeout=8000)
    finally:
        a.post("/api/fs/delete", json={"path": path, "permanent": True})
        b.post("/api/inbox/read", json={"clear": True})
        ctx.close()
