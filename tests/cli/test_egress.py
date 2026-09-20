"""The egress proxy: sandboxed artifacts reach the network ONLY through the
hub — per-artifact admin allowlist, server-side secret injection (resolved by
reading the `_secrets/` file AS the caller, kernel-checked), multipart file
forwarding, no redirects, audit-logged."""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from kbenv import BASE, CREDS, U, doc, full

EGRESS = ".os/egress.json"        # the allowlist: platform config, in the repo

TAG = str(int(time.time()))
ART = doc("dashboards/egresstest.html")
SECRET = doc(f"_secrets/egkey_{TAG}.env")
KEY = f"topsecret_{TAG}"


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


BIG = bytes((i * 7 + 11) % 251 for i in range(300_000))


class Echo(BaseHTTPRequestHandler):
    def _big(self):
        """A reply too big for one chunk, written in pieces with a pause between
        them so the hub genuinely reads it across several reads."""
        self.send_response(200)
        self.send_header("content-type", "application/octet-stream")
        self.send_header("content-length", str(len(BIG)))
        self.end_headers()
        for i in range(0, len(BIG), 8192):
            self.wfile.write(BIG[i:i + 8192])
            self.wfile.flush()
            time.sleep(0.002)

    def _do(self):
        if self.path == "/big":
            return self._big()
        ln = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(ln)
        out = json.dumps({"path": self.path, "key": self.headers.get("X-Api-Key", ""),
                          "clen": len(body), "ct": self.headers.get("content-type", "")}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    do_GET = do_POST = _do

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def setup():
    srv = HTTPServer(("127.0.0.1", 0), Echo)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    k = cl("alice")
    k.post("/api/fs/mkdir", json={"path": doc("_secrets")})
    assert k.post("/fs/newfile", json={"path": SECRET}).status_code == 200
    k.post("/api/artifact/write", json={"path": SECRET, "content": KEY + "\n"})
    assert k.post("/fs/newfile", json={"path": ART}).status_code in (200, 409)
    r = k.post("/admin/egress", json={"artifact": ART, "domains": [f"127.0.0.1:{port}"]})
    assert r.status_code == 200, r.text
    yield port
    k.post("/admin/egress", json={"artifact": ART, "domains": []})
    k.post("/api/fs/delete", json={"path": SECRET})
    k.post("/api/fs/delete", json={"path": ART})
    srv.shutdown()


def echo_of(resp):
    assert resp.status_code == 200, resp.text
    return json.loads(base64.b64decode(resp.json()["body_b64"]))


def test_allowlisted_call_with_secret_injection(setup):
    port = setup
    r = cl("alice").post("/egress", json={
        "artifact": ART, "url": f"http://127.0.0.1:{port}/v1/transcribe", "method": "POST",
        "headers": {"X-Api-Key": f"secret:{SECRET}"}, "body": "hello"})
    e = echo_of(r)
    assert e["key"] == KEY, "the secret must be injected server-side"
    assert e["clen"] == 5 and e["path"] == "/v1/transcribe"


def test_multipart_file_forwarding(setup):
    port = setup
    k = cl("alice")
    up = k.post("/api/upload", params={"dir": doc("dashboards")},
                files={"file": (f"eg_{TAG}.bin", b"A" * 5000, "application/octet-stream")})
    assert up.status_code == 200, up.text
    fpath = doc(f"dashboards/_files/eg_{TAG}.bin")
    r = k.post("/egress", json={
        "artifact": ART, "url": f"http://127.0.0.1:{port}/stt", "method": "POST",
        "form": {"fields": {"model_id": "scribe_v1"},
                 "file": {"path": fpath, "field": "file", "filename": "audio.bin"}}})
    e = echo_of(r)
    assert e["clen"] > 5000 and "multipart" in e["ct"]
    k.post("/api/fs/delete", json={"path": fpath})


def test_denials(setup):
    port = setup
    k = cl("alice")
    # an artifact with no grant has no network
    assert k.post("/egress", json={"artifact": doc("todos.html"),
                                   "url": f"http://127.0.0.1:{port}/"}).status_code == 403
    # a domain outside the allowlist is refused
    assert k.post("/egress", json={"artifact": ART,
                                   "url": "https://evil.example.com/"}).status_code == 403
    # a secret the caller cannot read never leaves the box
    r = cl("bob").post("/egress", json={"artifact": ART, "url": f"http://127.0.0.1:{port}/",
                                         "headers": {"X-Api-Key": f"secret:{SECRET}"}})
    assert r.status_code == 403
    # only _secrets/ files are injectable at all
    r = k.post("/egress", json={"artifact": ART, "url": f"http://127.0.0.1:{port}/",
                                "headers": {"X-Api-Key": "secret:company/overview.md"}})
    assert r.status_code == 403
    # plain http to a non-local host is refused
    assert k.post("/egress", json={"artifact": ART,
                                   "url": "http://api.example.com/"}).status_code == 400
    # non-admins cannot touch the allowlist
    assert cl("bob").post("/admin/egress",
                           json={"artifact": ART, "domains": ["x.com"]}).status_code == 403


def test_delegated_non_admin_and_agent_can_manage_network(setup):
    """Delegation = write-ACL on .os/egress.json, granted with the normal
    permissions machinery. A delegated user passes the hub endpoints AND their
    agent can edit the file directly (kernel-checked); the validating loader
    ignores malformed hand-edits."""
    port = setup
    k = cl("alice")
    art2 = doc("dashboards/egressdelegate.html")
    assert k.post("/fs/newfile", json={"path": art2}).status_code in (200, 409)
    assert cl("bob").get("/admin/egress").json()["can_edit"] is False
    assert cl("bob").post("/admin/egress",
                           json={"artifact": art2, "domains": ["x.com"]}).status_code == 403
    r = k.post("/fs/props", json={"path": EGRESS,
                                  "acl_add": [{"type": "user", "name": U("bob"), "perms": "rw"}]})
    assert r.status_code == 200, r.text
    try:
        j = cl("bob")
        assert j.get("/admin/egress").json()["can_edit"] is True
        assert j.post("/admin/egress",
                      json={"artifact": art2, "domains": [f"127.0.0.1:{port}"]}).status_code == 200
        # the AGENT path: edit the file itself, as bob, kernel-enforced
        cfg = json.loads(full(EGRESS).read_text())
        cfg[art2] = {"domains": [f"127.0.0.1:{port}", "api.example.com"]}
        w = j.post("/api/artifact/write", json={"path": EGRESS,
                                                "content": json.dumps(cfg)})
        assert w.status_code == 200, w.text
        assert "api.example.com" in k.get("/admin/egress").json()["entries"][art2]["domains"]
        # hostile/malformed hand-edits never survive the validating loader
        cfg["../../etc/passwd"] = {"domains": ["evil.com"]}
        cfg["notes.txt"] = {"domains": ["evil.com"]}
        cfg[art2 + ".broken"] = "garbage"
        j.post("/api/artifact/write", json={"path": EGRESS,
                                            "content": json.dumps(cfg)})
        ents = k.get("/admin/egress").json()["entries"]
        assert "../../etc/passwd" not in ents and "notes.txt" not in ents
    finally:
        k.post("/fs/props", json={"path": EGRESS,
                                  "acl_remove": [{"type": "user", "name": U("bob")}]})
        k.post("/admin/egress", json={"artifact": art2, "domains": []})
        k.post("/api/fs/delete", json={"path": art2})
    assert cl("bob").get("/admin/egress").json()["can_edit"] is False


def test_large_reply_is_not_truncated(setup):
    """Regression. The hub read the upstream reply with one
    `resp.content.read(cap + 1)`, which returns only what happens to be buffered
    — so any reply arriving in more than one chunk reached the artifact as a
    silent prefix, status 200 and all. /stt at least noticed (json.loads raised,
    502); here there was nothing to notice it. Bytes in must equal bytes out."""
    port = setup
    r = cl("alice").post("/egress", json={
        "artifact": ART, "url": f"http://127.0.0.1:{port}/big"})
    assert r.status_code == 200, r.text
    body = base64.b64decode(r.json()["body_b64"])
    assert len(body) == len(BIG), f"got {len(body)} of {len(BIG)} bytes"
    assert body == BIG
