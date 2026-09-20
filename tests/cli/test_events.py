"""/api/events — the tree, presence and config PUSHED over one server-sent
event stream, instead of every tab polling the tree every 4 s. The first event
is always `hello` (the catch-up); a file's content change is a small `tree`
delta (mtime only, patched in place by the client); a new file is `full`
(refetch with the ETag); a config file changing on disk is also `config`.

Reading the stream at all proves the hub pipes it instead of buffering it:
a buffered body would arrive only when the stream ended, i.e. never."""
import json
import time

import httpx
from kbenv import BASE, CREDS, U, doc, home

TAG = str(int(time.time()))


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


class Stream:
    """One SSE connection, parsed event by event."""

    def __init__(self, c, timeout=20):
        self.cm = c.stream("GET", "/api/events", timeout=httpx.Timeout(timeout, read=timeout))
        self.r = self.cm.__enter__()
        self.lines = self.r.iter_lines()

    def next(self, want=None, deadline=15):
        """The next event, or the next `want` event, skipping others."""
        end = time.time() + deadline
        ev, data = None, ""
        while time.time() < end:
            line = next(self.lines)
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
            elif line == "" and ev:
                out = (ev, json.loads(data) if data else {})
                ev, data = None, ""
                if want is None or out[0] == want:
                    return out
        raise AssertionError(f"no {want or 'event'} within {deadline}s")

    def close(self):
        try:
            self.cm.__exit__(None, None, None)
        except Exception:
            pass


def test_stream_hello_delta_full_and_config():
    a = cl("alice")
    s = Stream(a)
    path = doc(f"events_{TAG}.md")
    try:
        assert s.r.status_code == 200, s.r.status_code
        assert s.r.headers["content-type"].startswith("text/event-stream")
        ev, hello = s.next("hello")
        assert hello["etag"].startswith('"') and hello["v"] >= 32
        assert "presence" in hello
        etag0 = hello["etag"]
        # a NEW file: structural, so the client is told to refetch (full)
        w = cl("alice")
        assert w.post("/fs/newfile", json={"path": path}).status_code in (200, 409)
        ev, d = s.next("tree", deadline=12)
        assert d["full"] is True and d["etag"] != etag0, d
        assert path in d["paths"]
        # a CONTENT change: only the mtime moved — a delta, patched in place
        time.sleep(1.2)
        r = w.post("/api/artifact/write", json={"path": path, "content": f"# events {TAG}\n"})
        assert r.status_code == 200, r.text
        ev, d = s.next("tree", deadline=12)
        assert d["full"] is False, d
        assert any(c["path"] == path and isinstance(c["mtime"], (int, float)) for c in d["changed"]), d
        assert d["etag"] != etag0
        # the ETag the stream announced is the one GET /api/tree answers 304 to
        assert w.get("/api/tree", headers={"If-None-Match": d["etag"]}).status_code == 304
        # my own launcher list changing on disk is a `config` event too
        r = w.post("/api/launchers", json={"buttons": [{"label": "ev", "kind": "file", "target": path}]})
        assert r.status_code == 200, r.text
        ev, d = s.next("config", deadline=12)
        assert any(p.startswith(home("alice", ".os/")) for p in d["paths"]), d
    finally:
        s.close()
        w = cl("alice")
        w.post("/api/launchers", json={"buttons": []})
        w.post("/api/fs/delete", json={"path": path})


def test_stream_is_private_and_needs_a_session():
    c = httpx.Client(base_url=BASE, timeout=10)
    assert c.get("/api/events").status_code == 401
    # bob's stream never carries alice's private file
    b = cl("bob")
    s = Stream(b)
    path = home("alice", f"private_{TAG}.md")
    try:
        s.next("hello")
        a = cl("alice")
        assert a.post("/api/artifact/write", json={"path": path, "content": "secret"}).status_code == 200
        end = time.time() + 6
        while time.time() < end:
            try:
                ev, d = s.next(deadline=3)
            except (AssertionError, StopIteration):
                break
            if ev == "tree":
                assert path not in d.get("paths", []), d
                assert all(c["path"] != path for c in d.get("changed", []))
    finally:
        s.close()
        cl("alice").post("/api/fs/delete", json={"path": path})


def test_heartbeat_keeps_the_stream_alive():
    """A quiet stream still speaks: `ping` within 25 s (Cloudflare drops idle
    connections at 100 s; the client's watchdog reopens after 60 s of silence)."""
    s = Stream(cl("carol"), timeout=40)
    try:
        s.next("hello")
        t0 = time.time()
        ev, _ = s.next("ping", deadline=30)
        assert 5 < time.time() - t0 < 30
    finally:
        s.close()
