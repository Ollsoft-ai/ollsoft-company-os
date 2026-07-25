"""HIGH: an artifact's kb-read/kb-write is scoped to its own folder, so a hostile
artifact can't read the viewer's private files elsewhere (confused-deputy fix)."""
from conftest import login


def test_artifact_cannot_read_outside_its_folder(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    # scopetest.html lives in company/dashboards/ and tries to read
    # users/alice/private.md (a different folder, and alice's own secret).
    page.click('.tree-item[data-path="company/dashboards/scopetest.html"]')
    frame = page.frame_locator("iframe.artifact-frame")
    out = frame.locator("#o")
    out.wait_for(timeout=10000)
    text = ""
    for _ in range(20):
        text = out.inner_text()
        if "err=" in text and "run" not in text:
            break
        page.wait_for_timeout(200)
    assert "leaked=no" in text, f"artifact leaked a file outside its folder: {text!r}"
    assert "outside this artifact" in text, f"expected scope rejection, got {text!r}"
    ctx.close()
