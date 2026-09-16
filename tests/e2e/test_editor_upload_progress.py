"""An upload started from INSIDE a document has to say so.

Dropping a big file into the editor used to be a silent wait: no row, no bar,
nothing until the link appeared minutes later. It reports in the upload tray
now — one live bar per file, with a percentage — and the link still lands in the
document when it finishes. Upload bandwidth is throttled via CDP so the
in-flight state is actually observable.
"""
import os
import time

import httpx

from conftest import BASE, CREDS, doc_text, login, open_doc
from kbenv import AREA, U, doc


def _throttle(ctx, page, kbps=700):
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {
        "offline": False, "latency": 0,
        "downloadThroughput": -1, "uploadThroughput": kbps * 1024})


def test_editor_upload_shows_live_percentage_then_inserts_the_link(browser, tmp_path):
    stamp = int(time.time())
    docname = f"upedit_{stamp}.md"
    name = f"upedit_{stamp}.bin"
    big = tmp_path / name
    big.write_bytes(os.urandom(3 * 1024 * 1024))     # 3 MB, incompressible

    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    assert c.post("/api/file", json={"path": doc(docname)}).status_code in (200, 409)
    # /api/file makes an EMPTY document; open_doc waits for text to arrive, so
    # seed some through the same write path the agent tools use.
    assert c.post("/api/artifact/write", json={"path": doc(docname),
                                               "content": "# upload\n\n"}).status_code == 200
    time.sleep(1.0)     # let the sync daemon settle on the seeded content

    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        _throttle(ctx, page)
        open_doc(page, doc(docname))
        with page.expect_file_chooser() as fc:        # the "Attach file" button
            page.click('#mdbar button[data-md="file"]')
        fc.value.set_files(str(big))

        tray = page.locator('[data-testid="upload-tray"] .uprow')
        tray.wait_for(state="visible", timeout=10000)
        assert name in tray.inner_text(), "the tray row must name the file"
        # a real percentage, not just a spinner — that is the whole point
        page.wait_for_function(
            "() => { const el = document.querySelector('#uptray .uprow .upct');"
            "        return el && /\\d+%/.test(el.textContent); }", timeout=20000)

        # …and when it lands, the link is in the document and the tray is empty
        page.wait_for_function(
            "(n) => window.__kbview && window.__kbview.state.doc.toString().includes(n)",
            arg=f"_files/{name}", timeout=60000)
        assert f"](_files/{name})" in doc_text(page)
        page.wait_for_selector("#uptray .uprow", state="hidden", timeout=15000)
    finally:
        ctx.close()
        c.post("/api/fs/delete", json={"path": doc(f"_files/{name}")})
        c.post("/api/fs/delete", json={"path": doc(docname)})


def test_a_big_upload_is_not_refused_for_being_big(browser, tmp_path):
    """The regression this feature exists for: a file past any single-request
    body limit used to come back as "larger than the server's upload limit".
    Chunked, it simply uploads — asserted through the browser, so the whole
    client protocol (begin/chunk/finish) is what is being exercised."""
    stamp = int(time.time())
    name = f"upbig_{stamp}.bin"
    big = tmp_path / name
    big.write_bytes(os.urandom(24 * 1024 * 1024))    # 3 chunks at 8 MiB
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        row = page.locator(f'.tree-item[data-path="{AREA}"]')
        row.hover()
        with page.expect_file_chooser() as fc:
            row.locator('button[title="Upload files here"]').click()
        fc.value.set_files(str(big))
        page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=180000)
        toasts = page.locator('[data-testid="toast"]')
        assert "upload limit" not in toasts.all_inner_texts().__str__()
    finally:
        ctx.close()
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    r = c.get("/api/attachment", params={"path": doc(name)})
    assert r.status_code == 200 and len(r.content) == 24 * 1024 * 1024
    c.post("/api/fs/delete", json={"path": doc(name)})
