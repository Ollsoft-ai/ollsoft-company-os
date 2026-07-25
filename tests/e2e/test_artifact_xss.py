"""Artifact XSS / exfiltration containment.

Two independent walls must hold:
  1. sandbox (opaque origin) — the artifact can't touch the app's session/DOM.
  2. CSP (connect-src 'none', img/font data: only) — it can't reach the network,
     so even data the viewer is allowed to see can't be exfiltrated.
"""
from conftest import BASE, CREDS, login

XSS = "company/dashboards/xsstest.html"
GOOD = "company/dashboards/randoms.html"


def test_hostile_artifact_cannot_exfiltrate(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click(f'.tree-item[data-path="{XSS}"]')
    frame = page.frame_locator("iframe.artifact-frame")
    # The artifact attempts fetch + image + websocket to external hosts; CSP must
    # block them all and the page reports the violated directive.
    verdict = frame.locator("#v")
    verdict.wait_for(timeout=8000)
    # poll until the load handler has run and CSP has fired
    text = ""
    for _ in range(20):
        text = verdict.inner_text()
        if text.startswith("CSP_BLOCKED"):
            break
        page.wait_for_timeout(200)
    assert text.startswith("CSP_BLOCKED"), f"exfiltration was NOT blocked: {text!r}"
    assert "connect-src" in text or "img-src" in text
    ctx.close()


def test_artifact_iframe_is_opaque_origin(browser):
    # The frame must be sandboxed without allow-same-origin (opaque origin),
    # which is what stops it reading the app's cookies/DOM.
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click(f'.tree-item[data-path="{GOOD}"]')
    page.wait_for_selector("iframe.artifact-frame")
    sandbox = page.get_attribute("iframe.artifact-frame", "sandbox")
    assert "allow-scripts" in sandbox
    assert "allow-same-origin" not in sandbox, "opaque origin isolation would be lost"
    ctx.close()
