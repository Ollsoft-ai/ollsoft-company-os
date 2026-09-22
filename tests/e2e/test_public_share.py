"""Public links: a folder or a file handed to someone with no account here.

The platform bind-mounts the thing in front of a separate container and hands
back one URL. These tests drive the platform half — who may publish, what the
link record says, and that revoking takes the mount away. Whether the
container then serves it is checked on the box (docs/public-sharing.md); CI
has no Docker.
"""
import os
import time

import httpx
import pytest
from conftest import BASE, CREDS, login
from kbenv import U, doc, full

PUB = "/srv/kb-public"
pytestmark = pytest.mark.skipif(not os.path.isdir(PUB),
                                reason="public sharing is not installed on this box")


def api(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def write(c, path, text="# hello\n"):
    assert c.post("/api/artifact/write", json={"path": path, "content": text}).status_code == 200


def mine(c, path):
    return [s for s in c.get("/fs/public").json()["shares"] if s["path"] == path]


def test_a_link_mounts_the_thing_and_revoking_takes_it_away():
    a = api("alice")
    path = doc(f"pub-{int(time.time())}.md")
    write(a, path, "# for the client\n")
    r = a.post("/fs/public", json={"path": path, "mode": "view", "days": 3})
    assert r.status_code == 200, r.text
    sh = r.json()["share"]
    try:
        assert sh["mode"] == "view" and sh["by"] == U("alice") and not sh["password"]
        assert 2 * 86400 < sh["expires"] - time.time() <= 3 * 86400
        # whole when the box knows its public address, a bare path when not —
        # and never both glued together
        base = a.get("/fs/public").json().get("base") or ""
        assert sh["url"] == f"{base}/s/{sh['id']}/" + sh["url"].split("/")[-1], sh["url"]
        assert len(sh["url"].split("/")[-1]) >= 20 and sh["url"].count("/s/") == 1
        # the container sees exactly one file, under the share's id, and the
        # content is the real thing (a bind mount, not a copy)
        served = f"{PUB}/data/{sh['id']}/{os.path.basename(path)}"
        assert os.path.isfile(served)
        with open(served) as f:
            assert f.read() == "# for the client\n"
        # (the conf beside it is 0640 root:kbshare — deliberately unreadable
        # to everyone but the container, so a test cannot look at it either)
        # …and the URL is shown once: the listing never repeats the token
        assert all("…" in s["url"] for s in mine(a, path))
    finally:
        assert a.post("/fs/public/revoke", json={"id": sh["id"]}).status_code == 200
        a.post("/api/fs/delete", json={"path": path, "permanent": True})
    assert not os.path.exists(f"{PUB}/data/{sh['id']}"), "the mount goes with the link"
    assert mine(a, path) == []


def test_only_the_owner_publishes_and_only_they_can_stop_it():
    a, b = api("alice"), api("bob")
    path = doc(f"pub-own-{int(time.time())}.md")
    write(a, path)
    r = b.post("/fs/public", json={"path": path})
    assert r.status_code == 403, r.text
    sh = a.post("/fs/public", json={"path": path}).json()["share"]
    try:
        assert b.post("/fs/public/revoke", json={"id": sh["id"]}).status_code == 403
        assert mine(b, path) == [], "bob does not even see alice's links"
    finally:
        a.post("/fs/public/revoke", json={"id": sh["id"]})
        a.post("/api/fs/delete", json={"path": path, "permanent": True})


def test_a_secret_is_never_publishable():
    a = api("alice")
    path = doc(f"_secrets/pub-{int(time.time())}.md")
    write(a, path, "token\n")
    r = a.post("/fs/public", json={"path": path})
    assert r.status_code == 400 and "secret" in r.text.lower(), r.text


def test_the_container_gets_a_read_only_mount_unless_the_link_may_edit():
    a = api("alice")
    stamp = int(time.time())
    for mode, writable in (("view", False), ("edit", True)):
        folder = doc(f"pub-{mode}-{stamp}")
        assert a.post("/api/fs/mkdir", json={"path": folder}).status_code == 200
        write(a, f"{folder}/note.md")
        sh = a.post("/fs/public", json={"path": folder, "mode": mode}).json()["share"]
        try:
            served = f"{PUB}/data/{sh['id']}"
            assert os.path.isdir(served)
            # the kernel's word, not ours: try to write into the mount as root
            probe = os.path.join(served, ".probe")
            try:
                with open(probe, "w") as f:
                    f.write("x")
                os.unlink(probe)
                could = True
            except OSError:
                could = False
            assert could is writable, f"{mode} mount writable={could}"
        finally:
            a.post("/fs/public/revoke", json={"id": sh["id"]})
            a.post("/api/fs/delete", json={"path": folder, "permanent": True})


def test_the_panel_offers_a_link_and_shows_it_once(browser):
    a = api("alice")
    path = doc(f"pub-ui-{int(time.time())}.md")
    write(a, path)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    sid = None
    try:
        page = login(ctx, "alice")
        page.reload()
        page.wait_for_selector(f'.tree-item[data-path="{path}"]', timeout=15000)
        page.locator(f'.tree-item[data-path="{path}"]').first.click(button="right")
        page.wait_for_selector('[data-testid="ctx-menu"]')
        page.click('.ctx-item:has-text("Share")')
        page.wait_for_selector('[data-testid="sh-public"]')
        page.wait_for_selector('[data-testid="sh-public-create"]')
        page.fill('[data-testid="sh-public-days"]', "7")
        page.click('[data-testid="sh-public-create"]')
        url = page.input_value('[data-testid="sh-public-url"]')
        assert "/s/" in url and len(url.split("/")[-1]) >= 20, url
        # …and exactly once: the server's url is whole, so prefixing the base
        # again produced "https://hosthttps://host/s/…" (2026-09-22)
        assert url.count("/s/") == 1 and url.count("://") <= 1, url
        base = a.get("/fs/public").json().get("base") or ""
        assert url.startswith(base) and url[len(base):].startswith("/s/"), (base, url)
        sid = url.split("/s/")[1].split("/")[0]
        page.click('.sh-public-acts button:has-text("Done")')
        row = page.locator('[data-testid="sh-public-row"]')
        row.wait_for(timeout=8000)
        assert "read" in row.text_content()
        row.locator('[data-testid="sh-public-off"]').click()
        page.wait_for_selector('[data-testid="sh-public-create"]', timeout=8000)
        assert mine(a, path) == []
    finally:
        if sid:
            a.post("/fs/public/revoke", json={"id": sid})
        a.post("/api/fs/delete", json={"path": path, "permanent": True})
        ctx.close()


# ---- the page a stranger actually sees --------------------------------------
# "why does the sharing service not use the actual editor code we've spent so
#  much time tuning?" — krystof, 2026-09-22. It does now: the container mounts
# the platform's built bundle read-only and serves the same rich editor.

SHARE_HTTP = os.environ.get("KB_SHARE_LOCAL", "http://127.0.0.1:8402")


def _container_up() -> bool:
    try:                                  # any answer at all means it is there
        httpx.get(SHARE_HTTP + "/s/aaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbbbbbbbb", timeout=2)
        return True
    except httpx.HTTPError:
        return False


container = pytest.mark.skipif(not _container_up(),
                               reason="the kb-share container is not running on this box")


def wait_for_body(c, path, needle, timeout=15):
    """The page saves a moment after you stop typing — wait for the file, not
    for a status chip that may still be showing the last save."""
    end = time.time() + timeout
    body = ""
    while time.time() < end:
        body = c.get("/api/file", params={"path": path}).text
        if needle in body:
            return body
        time.sleep(0.4)
    raise AssertionError(f"{needle!r} never reached {path}: {body[:400]}")


@pytest.fixture
def shared_doc():
    """A real, live public link to a markdown document — revoked afterwards."""
    a = api("alice")
    path = doc(f"pubdoc-{int(time.time() * 1000)}.md")
    write(a, path, "# The brief\n\n- [ ] one thing\n\n| What | Who |\n| --- | --- |\n| **ship** | you |\n")
    made = {}

    def make(mode="view"):
        r = a.post("/fs/public", json={"path": path, "mode": mode, "days": 2})
        assert r.status_code == 200, r.text
        sh = r.json()["share"]
        made["id"] = sh["id"]
        tok = sh["url"].rsplit("/", 1)[-1]
        return f"{SHARE_HTTP}/s/{sh['id']}/{tok}", path
    yield make
    if made.get("id"):
        a.post("/fs/public/revoke", json={"id": made["id"]})
    a.post("/api/fs/delete", json={"path": path, "permanent": True})


@container
def test_a_shared_document_opens_in_the_real_editor(browser, shared_doc):
    url, path = shared_doc("view")
    ctx = browser.new_context()
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector(".cm-content", timeout=15000)
        # the SAME rendered view the app gives: a heading line, a real
        # checkbox, and a table you could type into if you were allowed
        assert page.locator(".cm-h1").count() >= 1
        assert page.locator(".cm-task input[type=checkbox]").count() == 1
        assert page.locator("table.cm-table").count() == 1
        assert page.locator('table.cm-table [data-cell="1,0"] .cm-cell-md strong').count() == 1
        # …and read-only means read-only: no cell inputs, no save
        page.locator('table.cm-table [data-cell="1,0"]').click()
        page.wait_for_timeout(300)
        assert page.locator("table.cm-table input").count() == 0
        assert page.locator(".cm-task input[type=checkbox]").first.is_disabled()
        assert not errors, errors
    finally:
        ctx.close()


@container
def test_an_edit_link_saves_through_the_container(browser, shared_doc):
    url, path = shared_doc("edit")
    a = api("alice")
    ctx = browser.new_context()
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector(".cm-content", timeout=15000)
        page.locator(".cm-content").click()
        page.keyboard.press("Control+End")
        page.keyboard.type("\nfrom the outside")
        page.wait_for_selector("#status:has-text('Saved')", timeout=10000)
        assert not errors, errors
        # the platform sees the stranger's edit in the real file
        wait_for_body(a, path, "from the outside")
        # a cell in the table is editable through the link too
        page.locator('table.cm-table [data-cell="1,1"]').click()
        inp = page.locator('table.cm-table input[data-cell="1,1"]')
        inp.wait_for(state="visible", timeout=4000)
        inp.fill("them")
        page.keyboard.press("Escape")
        wait_for_body(a, path, "| **ship** | them |")
    finally:
        ctx.close()


@container
def test_the_page_picks_up_an_edit_made_in_the_app(browser, shared_doc):
    """No CRDT out here — the page asks the container for the file's timestamp
    every few seconds, which is enough to see a colleague's edit land."""
    url, path = shared_doc("view")
    a = api("alice")
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector(".cm-content", timeout=15000)
        write(a, path, "# The brief\n\nrewritten in the app\n")
        page.wait_for_function(
            "() => document.querySelector('.cm-content').textContent.includes('rewritten in the app')",
            timeout=20000)
    finally:
        ctx.close()
