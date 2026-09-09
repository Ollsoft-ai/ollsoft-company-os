"""End-to-end: an artifact makes a subfolder, lists it, and deletes it again —
through the bridge, as the viewer, and only inside its own folder."""
from conftest import login
from kbenv import doc, full


def test_artifact_lists_creates_and_deletes(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click(f'.tree-item[data-path="{doc("dashboards/fstest.html")}"]')
    frame = page.frame_locator("iframe.artifact-frame")
    out = frame.locator("#out")
    out.wait_for(timeout=10000)
    text = ""
    for _ in range(30):
        text = out.inner_text()
        if "gone=" in text:
            break
        page.wait_for_timeout(200)
    assert "mkdir_ok=true" in text, f"artifact mkdir failed: {text!r}"
    assert "listed=true" in text, f"artifact list did not see the written file: {text!r}"
    assert "guard=refused" in text, f"artifact was allowed to delete its own folder: {text!r}"
    assert "delete_ok=true gone=true" in text, f"artifact delete failed: {text!r}"
    # and the subtree really is off disk
    assert not full(doc("dashboards/fs_probe")).exists()
    ctx.close()
