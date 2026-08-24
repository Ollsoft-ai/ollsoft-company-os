"""Uploading into the tree must be VISIBLE while it runs: the target folder
shows a ghost row (dimmed, spinner, live percentage) until the real row lands.
Upload bandwidth is throttled via CDP so the in-flight state is observable."""
import os
import time

import httpx

from conftest import BASE, CREDS, login
from kbenv import U, doc


def test_tree_upload_shows_ghost_row_with_progress(browser, tmp_path):
    name = f"upv_{int(time.time())}.bin"
    big = tmp_path / name
    big.write_bytes(os.urandom(3 * 1024 * 1024))   # 3 MB, incompressible

    ctx = browser.new_context()
    page = login(ctx, "alice")
    # throttle ONLY the upload direction — ~700 KB/s makes a 3 MB file take
    # a few seconds, long enough to watch the ghost row live
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {
        "offline": False, "latency": 0,
        "downloadThroughput": -1, "uploadThroughput": 700 * 1024})

    row = page.locator('.tree-item[data-path="company"]')
    row.hover()
    with page.expect_file_chooser() as fc:
        row.locator('button[title="Upload files here"]').click()
    fc.value.set_files(str(big))

    ghost = page.locator(".tree-item.uploading")
    ghost.wait_for(state="visible", timeout=5000)
    assert name in ghost.inner_text(), "ghost row must carry the file name"
    # a live percentage appears (not just the static 'waiting…')
    page.wait_for_function(
        "() => { const el = document.querySelector('.tree-item.uploading .upct');"
        "        return el && /\\d+%/.test(el.textContent); }", timeout=10000)

    # the ghost resolves into the real tree row, and no ghost remains
    page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=30000)
    page.wait_for_selector(".tree-item.uploading", state="hidden", timeout=5000)
    ctx.close()

    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    c.post("/api/fs/delete", json={"path": doc(f"{name}")})
