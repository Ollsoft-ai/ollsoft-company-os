"""An artifact must be able to SHOW binary that lives next to it — play a video,
render a photo — without a base64 sidecar. The bytes cross into the sandbox as a
Blob (structured clone) and the artifact makes its own blob: URL; the CSP allows
blob: for media/img but still forbids every network channel, so bytes can be
displayed and never sent anywhere."""
import json
import os
import time

import httpx
import pytest

from conftest import BASE, CREDS, login
from kbenv import U, doc

# A tiny REAL MP4 generated at test time with ffmpeg — one second of test
# pattern. It must be genuinely playable: test_artifact_plays_a_video waits for
# the browser to fire `loadedmetadata`, and the old 133-byte ftyp+empty-moov
# stub has zero tracks, so chromium fires `error` instead and the test can
# never pass anywhere. The stub remains as the fallback for boxes without
# ffmpeg (the byte-path tests still work there); PLAYABLE records which one
# this run got, and the playback test skips honestly instead of failing.
def _fixture_mp4(tmp_path_factory=None):
    import base64, shutil, subprocess, tempfile, os
    fd, path = tempfile.mkstemp(suffix=".mp4", prefix="kb-test-")
    os.close(fd)
    if shutil.which("ffmpeg"):
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=duration=1:size=64x64:rate=10",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", path],
            capture_output=True)
        if r.returncode == 0 and os.path.getsize(path) > 1000:
            return path, True
    data = base64.b64decode(
        "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAABIbW9vdgAAAGxtdmhkAAAAAAAA"
        "AAAAAAAAAAAAAAAAA+gAAAAAAAEAAAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAABAAAA"
        "AAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC"
    )
    with open(path, "wb") as fh:
        fh.write(data)
    return path, False


SRC_MP4, PLAYABLE = _fixture_mp4()

ARTIFACT = """<!doctype html><meta charset="utf-8">
<body><video id="v" muted playsinline></video>
<div id="status">start</div><div id="scope">start</div><div id="net">start</div>
<script>
  let _id = 0; const _pending = {};
  window.addEventListener("message", (ev) => {
    const m = ev.data || {};
    if (m.type === "kb-result" && _pending[m.id]) { _pending[m.id](m); delete _pending[m.id]; }
  });
  const _send = (msg) => new Promise((res) => {
    const id = ++_id; _pending[id] = res; parent.postMessage({ id, ...msg }, "*"); });
  const kbReadBytes = (path) => _send({ type: "kb-read-bytes", path });
  const st = document.getElementById("status");
  (async () => {
    const r = await kbReadBytes("__CLIP__");
    if (r.error) { st.textContent = "ERROR:" + r.error; return; }
    if (!(r.blob instanceof Blob)) { st.textContent = "NOTBLOB:" + typeof r.blob; return; }
    const v = document.getElementById("v");
    v.onloadedmetadata = () => { st.textContent = "PLAYABLE:" + r.size; };
    v.onerror = () => { st.textContent = "MEDIAERR"; };
    v.src = URL.createObjectURL(r.blob);
  })();
  // reaching OUTSIDE this artifact's folder must be refused by the host
  (async () => {
    const r = await kbReadBytes("__OUTSIDE__");
    document.getElementById("scope").textContent =
      r.error ? "REFUSED:" + r.error : "LEAKED:" + (r.size || 0);
  })();
  // and the network must stay shut — bytes can be shown, never sent
  (async () => {
    try {
      await fetch("/api/tree");
      document.getElementById("net").textContent = "EXFIL_WORKED";
    } catch (e) { document.getElementById("net").textContent = "EXFIL_BLOCKED"; }
  })();
</script></body>"""


@pytest.fixture(scope="module")
def folder():
    """Build the artifact + its video AS BOB, through the product's own API."""
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/login", data={"username": U("bob"), "password": CREDS["bob"]})
    assert r.status_code == 200, r.text
    if c.get("/api/cron").json().get("v", 0) < 12:
        pytest.skip("backend predates artifact binary reads (v12)")
    rel = doc(f"vidtest_{int(time.time())}")
    assert c.post("/api/fs/mkdir", json={"path": rel}).status_code == 200
    with open(SRC_MP4, "rb") as fh:
        up = c.post(f"/api/upload?dir={rel}", files={"file": ("clip.mp4", fh, "video/mp4")})
    assert up.status_code == 200, up.text
    clip = f"{rel}/_files/clip.mp4"
    # both placeholders are substituted in PYTHON: ARTIFACT is JS, so a bare
    # doc("overview.md") in there is an undefined function, not this run's path
    html = ARTIFACT.replace("__CLIP__", clip).replace("__OUTSIDE__", doc("overview.md"))
    # .html is born via the hub's newfile (inherits the folder's owner/group)
    assert c.post("/fs/newfile", json={"path": f"{rel}/player.html"}).status_code in (200, 409)
    w = c.post("/api/artifact/write", json={"path": f"{rel}/player.html", "content": html})
    assert w.status_code == 200, w.text
    yield rel
    c.post("/api/fs/delete", json={"path": rel})


def test_artifact_plays_a_video_from_its_folder(browser, folder):
    if not PLAYABLE:
        pytest.skip("no ffmpeg on this box — the fallback fixture cannot decode")
    ctx = browser.new_context()
    page = login(ctx, "bob")
    page.goto(f"{BASE}/{folder}/player.html")
    page.wait_for_selector("iframe.artifact-frame", timeout=15000)
    frame = page.frame_locator("iframe.artifact-frame")
    status = frame.locator("#status")
    status.wait_for(timeout=15000)
    deadline = time.time() + 25
    txt = ""
    while time.time() < deadline:
        txt = status.inner_text()
        if txt != "start":
            break
        page.wait_for_timeout(250)
    assert txt.startswith("PLAYABLE:"), f"video did not play in the sandbox: {txt!r}"
    # the sandbox channel delivered the file COMPLETELY — byte-for-byte size,
    # not a magic threshold (the fixture is a deliberately tiny real clip)
    assert int(txt.split(":")[1]) == os.path.getsize(SRC_MP4), txt

    # ...and the walls the binary channel must NOT have loosened:
    scope = _settled(page, frame.locator("#scope"))
    assert scope.startswith("REFUSED:"), f"out-of-folder binary read allowed: {scope!r}"
    assert "outside" in scope, scope
    net = _settled(page, frame.locator("#net"))
    assert net == "EXFIL_BLOCKED", f"artifact reached the network: {net!r}"
    ctx.close()


def _settled(page, locator, timeout=15.0):
    deadline = time.time() + timeout
    txt = locator.inner_text()
    while time.time() < deadline and txt == "start":
        page.wait_for_timeout(200)
        txt = locator.inner_text()
    return txt
