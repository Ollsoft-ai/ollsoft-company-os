"""End-to-end: a real artifact writes then reads a file through the bridge."""
from conftest import login


def test_artifact_writes_and_reads_a_file(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click('.tree-item[data-path="company/dashboards/iotest.html"]')
    frame = page.frame_locator("iframe.artifact-frame")
    out = frame.locator("#out")
    out.wait_for(timeout=10000)
    # poll until the async write+read completes
    text = ""
    for _ in range(20):
        text = out.inner_text()
        if "write_ok=" in text and "read_proof=" in text:
            break
        page.wait_for_timeout(200)
    assert "write_ok=true" in text, f"artifact write failed: {text!r}"
    assert "read_proof=true" in text, f"artifact read-back failed: {text!r}"
    # and it really landed on disk (company files are world-readable)
    assert "IO_PROOF_42" in open("/srv/kb/company/dashboards/io_written.md").read()
    ctx.close()
