"""Dictation: the hub's /stt route.

The whole point of this endpoint is that everyone may SPEND the ElevenLabs key
and nobody may READ it — the exact inverse of the `_secrets/` model that
`/egress` implements. These tests are mostly about that boundary: that the route
is reachable by any logged-in user (not just admins), that it lives above the
`/api` catch-all so it can never be proxied into a per-user backend, and that the
key appears in no response, ever.

The upstream call is redirected at a local echo server via the KB_STT_URL
environment variable, which only root can set (systemd drop-in) — the same
technique test_egress.py uses. Without that drop-in the live-upstream tests are
skipped rather than billed.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from kbenv import BASE, CREDS, L, U


# A one-second 16 kHz mono WAV of silence: big enough to clear the server's
# "that was a stray tap" floor, small enough to be free if it ever escapes.
def _wav(seconds=1.0, rate=16000):
    n = int(rate * seconds)
    data = b"\x00\x00" * n
    hdr = (b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVEfmt "
           + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
           + (1).to_bytes(2, "little") + rate.to_bytes(4, "little")
           + (rate * 2).to_bytes(4, "little") + (2).to_bytes(2, "little")
           + (16).to_bytes(2, "little") + b"data" + len(data).to_bytes(4, "little"))
    return hdr + data


WAV = _wav()


def cl(user):
    """A logged-in client. It is already open — logging in issued a request — so
    callers must NOT wrap it in `with`; httpx raises "Cannot open a client
    instance more than once" on re-entry. Use closing() if you want it closed.
    """
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code in (200, 302), r.text
    return c


def post_audio(c, body=WAV, ctype="audio/wav", **kw):
    return c.post("/stt", content=body, headers={"content-type": ctype}, **kw)


@pytest.fixture(scope="module")
def k():
    c = cl("alice")
    try:
        yield c
    finally:
        c.close()


# ---- the auth boundary ------------------------------------------------------

def test_unauthenticated_is_401():
    with httpx.Client(base_url=BASE, timeout=30) as anon:
        assert post_audio(anon).status_code == 401


def test_not_admin_gated(k):
    """Dictation is for everyone who can log in. `carol` is deliberately not in
    the sudo group and not on any project team; they must get the SAME answer as
    the admin, never a 403."""
    i = cl("carol")
    try:
        a, b = post_audio(k), post_audio(i)
    finally:
        i.close()
    assert a.status_code == b.status_code, (a.status_code, b.status_code, a.text, b.text)
    assert b.status_code != 403


# ---- input validation, all of it before a single byte is billed -------------

def test_json_body_rejected(k):
    r = k.post("/stt", json={"hello": "world"})
    assert r.status_code == 400
    assert "audio" in r.json()["error"]


def test_no_content_type_rejected(k):
    r = k.post("/stt", content=WAV, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 400


def test_tiny_body_short_circuits(k):
    """Under 1 KiB is a stray tap. Answer 200 with an empty transcript and do NOT
    spend an API call — the audit log must not grow."""
    before = _audit_len()
    r = post_audio(k, body=b"\x00" * 200)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "text": ""}
    assert _audit_len() == before


def test_oversize_body_is_413(k):
    r = post_audio(k, body=b"\x00" * (12 * 1024 * 1024 + 64))
    assert r.status_code == 413


def test_get_is_405_and_not_proxied(k):
    """Proof the route sits above the `*` /api catch-all: a GET must be a plain
    405 from the hub, not something the per-user backend answered."""
    r = k.get("/stt")
    assert r.status_code == 405


# ---- the key never leaks ----------------------------------------------------

def _installed_key():
    """Readable only by root, which is the whole point — so this returns None for
    an unprivileged test run and the assertion below is skipped."""
    try:
        with open("/etc/kb/elevenlabs.key") as f:
            return f.read().strip() or None
    except OSError:
        return None


def test_key_never_appears_in_any_response(k):
    key = _installed_key()
    if not key:
        pytest.skip("/etc/kb/elevenlabs.key not readable here (expected unless run as root)")
    responses = [
        post_audio(k),
        post_audio(k, body=b"\x00" * 200),
        k.post("/stt", json={"x": 1}),
        k.get("/stt"),
    ]
    for r in responses:
        assert key not in r.text
        for v in r.headers.values():
            assert key not in v


def test_key_file_is_root_only():
    """0600 root:root, beside the session key. If this ever loosens, every OS
    user's shell can read the company's credential."""
    if not os.path.exists("/etc/kb/elevenlabs.key"):
        pytest.skip("dictation key not installed on this box")
    st = os.stat("/etc/kb/elevenlabs.key")
    assert st.st_uid == 0 and st.st_gid == 0
    assert st.st_mode & 0o777 == 0o600


# ---- the audit trail records the fact, never the words ---------------------

AUDIT = "/var/log/kb/stt.log"


def _audit_len():
    try:
        with open(AUDIT) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _audit_tail(n=1):
    try:
        with open(AUDIT) as f:
            return [json.loads(x) for x in f.read().strip().splitlines()[-n:]]
    except OSError:
        return []


@pytest.mark.skipif(not os.environ.get("KB_STT_LIVE"),
                    reason="set KB_STT_LIVE=1 to call the real ElevenLabs API (costs ~$0.0001)")
def test_live_roundtrip_and_audit(k):
    before = _audit_len()
    r = post_audio(k)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True
    assert isinstance(j["text"], str)          # silence transcribes to ""
    assert _audit_len() == before + 1
    line = _audit_tail(1)[0]
    assert L(line["user"]) == "alice"
    assert line["status"] == 200
    assert "text" not in line and "transcript" not in line   # never the words


@pytest.mark.skipif(not os.environ.get("KB_STT_LIVE"),
                    reason="set KB_STT_LIVE=1 to call the real ElevenLabs API")
def test_live_transcribes_real_speech(k):
    """The only test that proves the feature actually works. Needs a recording of
    someone saying the words in SPEECH_WORDS at tests/fixtures/speech.webm — see
    the file's own docstring for how to make one."""
    path = os.path.join(os.path.dirname(__file__), "..", "fixtures", "speech.webm")
    if not os.path.exists(path):
        pytest.skip("no tests/fixtures/speech.webm — record one to enable this")
    with open(path, "rb") as f:
        r = post_audio(k, body=f.read(), ctype="audio/webm")
    assert r.status_code == 200, r.text
    got = r.json()["text"].lower()
    assert got.strip(), "transcript was empty"
    for w in os.environ.get("KB_STT_WORDS", "architecture,document").split(","):
        assert w.strip().lower() in got, f"{w!r} missing from {got!r}"


# ---- the fake-upstream path: what we actually send ElevenLabs ---------------
# Requires a systemd drop-in setting KB_STT_URL at the local echo server, so it
# is opt-in. Run it as:
#   sudo tests/cli/stt_fake_upstream.sh   (see that script)

class _Echo(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        ln = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(ln)
        _Echo.seen.append({"key": self.headers.get("xi-api-key", ""),
                           "ct": self.headers.get("content-type", ""),
                           "body": body.decode("latin-1")})
        out = json.dumps({"text": "echoed transcript", "language_code": "eng",
                          "audio_duration_secs": 1.0}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.mark.skipif(not os.environ.get("KB_STT_FAKE_PORT"),
                    reason="needs the hub restarted with KB_STT_URL at a local echo "
                           "server; see tests/cli/stt_fake_upstream.sh")
def test_outgoing_request_shape(k):
    port = int(os.environ["KB_STT_FAKE_PORT"])
    srv = HTTPServer(("127.0.0.1", port), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _Echo.seen.clear()
        r = post_audio(k)
        assert r.status_code == 200, r.text
        assert r.json()["text"] == "echoed transcript"
        assert len(_Echo.seen) == 1
        sent = _Echo.seen[0]
        assert sent["key"], "the hub must send the xi-api-key header"
        assert sent["ct"].startswith("multipart/form-data")
        # Exactly the parameters a low-latency dictation call wants, and no
        # billed extras. tag_audio_events defaults TRUE upstream, which would
        # drop "(laughter)" into somebody's document.
        for want in ['name="model_id"', "scribe_v2",
                     'name="tag_audio_events"', 'name="enable_logging"',
                     'name="timestamps_granularity"', "none",
                     'name="diarize"']:
            assert want in sent["body"], want
        assert "false" in sent["body"]
        assert "keyterms" not in sent["body"]
        assert "entity_detection" not in sent["body"]
    finally:
        srv.shutdown()


@pytest.mark.skipif(not os.environ.get("KB_STT_FAKE_PORT"), reason="needs the echo-server drop-in")
def test_upstream_error_body_is_not_forwarded(k):
    """An upstream auth error can carry key metadata. The hub must replace the
    body with its own message, not proxy it."""
    port = int(os.environ["KB_STT_FAKE_PORT"])

    class Boom(_Echo):
        def do_POST(self):
            secret = b'{"detail":"api key sk_leaky_do_not_forward is invalid"}'
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(secret)))
            self.end_headers()
            self.wfile.write(secret)

    srv = HTTPServer(("127.0.0.1", port), Boom)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = post_audio(k)
        assert r.status_code == 502
        assert "sk_leaky_do_not_forward" not in r.text
        assert "unavailable" in r.json()["error"]
    finally:
        srv.shutdown()


# ---- quota ------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("KB_STT_QUOTA_BYTES"),
                    reason="set KB_STT_QUOTA_BYTES to the hub's configured cap to run this")
def test_quota_eventually_429s(k):
    """The brake against one insider burning the company's credit. Needs the hub
    started with a small KB_STT_DAILY_BYTES or this uploads for a very long time."""
    cap = int(os.environ["KB_STT_QUOTA_BYTES"])
    chunk = b"\x00" * 4096
    sent, last = 0, None
    deadline = time.time() + 60
    while sent <= cap + len(chunk) and time.time() < deadline:
        last = post_audio(k, body=chunk * 4, ctype="audio/wav")
        sent += len(chunk) * 4
        if last.status_code == 429:
            break
    assert last is not None and last.status_code == 429, "quota never engaged"
    assert "limit" in last.json()["error"]
