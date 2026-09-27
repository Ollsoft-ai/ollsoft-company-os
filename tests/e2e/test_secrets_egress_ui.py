"""Browser-level: the masked secret viewer (reveal / denied), and a REAL
sandboxed artifact calling the network through kb-fetch — allowlisted domain,
secret injected server-side, response rendered in the iframe."""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from conftest import BASE, CREDS, dlg_fill, expand_folder, login
from kbenv import U, doc as kbdoc

TAG = str(int(time.time()))
SECRET = kbdoc(f"_secrets/uikey_{TAG}.env")
KEY = f"uisecret_{TAG}"
ART = kbdoc(f"dashboards/egressui_{TAG}.html")


def api(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


class Echo(BaseHTTPRequestHandler):
    def _do(self):
        out = json.dumps({"got_key": self.headers.get("X-Api-Key", "")}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    do_GET = do_POST = _do

    def log_message(self, *a):
        pass


ARTIFACT_HTML = """<!doctype html><meta charset="utf-8">
<button id="go">transcribe</button><pre id="out">idle</pre>
<script>
let seq = 0; const pend = {};
window.addEventListener("message", (e) => {
  const d = e.data || {};
  if (d.type === "kb-result" && pend[d.id]) { pend[d.id](d); delete pend[d.id]; }
});
function kb(msg) { return new Promise((res) => { msg.id = ++seq; pend[msg.id] = res; parent.postMessage(msg, "*"); }); }
document.getElementById("go").onclick = async () => {
  const r = await kb({ type: "kb-fetch", url: "__URL__", method: "POST",
                       headers: { "X-Api-Key": "secret:__SECRET__" }, body: "ping" });
  document.getElementById("out").textContent =
    r.ok ? atob(r.body_b64) : "ERR " + (r.error || r.status);
};
</script>"""


@pytest.fixture(scope="module")
def setup():
    srv = HTTPServer(("127.0.0.1", 0), Echo)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    k = api("alice")
    k.post("/api/fs/mkdir", json={"path": kbdoc("_secrets")})
    assert k.post("/fs/newfile", json={"path": SECRET}).status_code == 200
    k.post("/api/artifact/write", json={"path": SECRET, "content": KEY + "\n"})
    html = ARTIFACT_HTML.replace("__URL__", f"http://127.0.0.1:{port}/v1/x").replace("__SECRET__", SECRET)
    assert k.post("/fs/newfile", json={"path": ART}).status_code == 200
    k.post("/api/artifact/write", json={"path": ART, "content": html})
    assert k.post("/admin/egress", json={"artifact": ART,
                                         "domains": [f"127.0.0.1:{port}"]}).status_code == 200
    yield
    k.post("/admin/egress", json={"artifact": ART, "domains": []})
    k.post("/api/fs/delete", json={"path": SECRET})
    k.post("/api/fs/delete", json={"path": ART})
    srv.shutdown()


def test_secret_viewer_masked_and_reveal(browser, setup):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    # _secrets auto-collapses, so open it before the secret row becomes visible
    page.wait_for_selector(f'.tree-item[data-path="{kbdoc("_secrets")}"]', timeout=8000)
    expand_folder(page, kbdoc("_secrets"))
    page.wait_for_selector(f'.tree-item[data-path="{SECRET}"]', timeout=8000)
    page.click(f'.tree-item[data-path="{SECRET}"]')
    body = page.locator('[data-testid="secret-body"]')
    body.wait_for(timeout=8000)
    assert KEY not in body.inner_text(), "content must be masked by default"
    assert "•" in body.inner_text()
    page.click('[data-testid="secret-reveal"]')
    assert KEY in body.inner_text()
    page.click('[data-testid="secret-reveal"]')       # Hide again
    assert KEY not in body.inner_text()
    ctx.close()


def test_a_new_file_in_secrets_opens_as_a_secret(browser, setup):
    """"New file here" on _secrets/ opens the fresh file in the secret viewer.
    It used to open as a collaborative document, which the live-doc relay
    refuses for a secret: an empty note loading forever, until a reopen."""
    name = f"uinew_{TAG}.env"
    path = kbdoc(f"_secrets/{name}")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        page.wait_for_selector(f'.tree-item[data-path="{kbdoc("_secrets")}"]', timeout=8000)
        page.hover(f'.tree-item[data-path="{kbdoc("_secrets")}"]')
        page.click(f'.tree-item[data-path="{kbdoc("_secrets")}"] .tbtn[title="New file here"]')
        dlg_fill(page, name)
        page.wait_for_selector(".secret-view [data-testid='secret-body']", timeout=8000)
        assert page.locator(".tab-content.secret-view .cm-editor").count() == 0
        assert page.locator(".tab-content.secret-view [data-testid='tab-loading']").count() == 0
    finally:
        api("alice").post("/api/fs/delete", json={"path": path})
        ctx.close()


def test_secret_hidden_from_tree_and_denied_via_link(browser, setup):
    """Unreadable secrets don't even show their NAME in the tree; following a
    doc link to one lands on the denied card, never content."""
    k = api("alice")
    doc = kbdoc(f"seclink_{TAG}.md")
    k.post("/api/file", json={"path": doc})
    k.post("/api/artifact/write", json={"path": doc,
                                        "content": f"See [the key](/{SECRET}) for prod.\n"})
    ctx = browser.new_context()
    try:
        page = login(ctx, "bob")
        assert page.locator(f'.tree-item[data-path="{SECRET}"]').count() == 0
        page.wait_for_selector(f'.tree-item[data-path="{doc}"]', timeout=8000)
        page.click(f'.tree-item[data-path="{doc}"]')
        page.wait_for_function("() => window.__kbview && window.__kbview.state.doc.length > 0")
        page.wait_for_timeout(400)
        page.dblclick(".cm-md-link")
        page.wait_for_selector(".secret-denied", timeout=8000)
        assert KEY not in page.inner_text(".secret-view")
    finally:
        k.post("/api/fs/delete", json={"path": doc})
        ctx.close()


def test_artifact_kb_fetch_roundtrip(browser, setup):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.wait_for_selector(f'.tree-item[data-path="{ART}"]', timeout=8000)
    page.click(f'.tree-item[data-path="{ART}"]')
    frame = page.frame_locator("iframe.artifact-frame")
    frame.locator("#go").click()
    deadline = time.time() + 10
    out = ""
    while time.time() < deadline:
        out = frame.locator("#out").inner_text()
        if out != "idle":
            break
        page.wait_for_timeout(200)
    assert f'"got_key": "{KEY}"' in out, f"expected injected secret in echo, got: {out}"
    ctx.close()
