"""Who OPENED it — the half of the audit trail that did not exist.

`share.set`/`props.set` record who changed access. Nothing recorded that access
being used, so exfiltration by a colleague who legitimately held the permission
— the likeliest incident shape on a box where everyone is authenticated — left
no trace at all. Three events close the meaningful part of that gap:

    document.open   a collaborative session was joined  (/ws/doc/*, accepted)
    file.preview    the server served an attachment inline
    file.download   the server served it as an explicit download (dl=1)

The hard part is what must NOT appear. Tree polling, search, presence, `/api/vc`,
autosaves and CRDT frames are the app breathing; a 403 or a 404 is a refusal,
not a read. Any of those showing up as `result=ok` would make the trail both
noise and a lie, so most of this file asserts silence.

Two layers, deliberately:
  * pure unit tests of the classifier — no box, no journal, every edge case;
  * integration tests against the running hub, reading the real journal, which
    is the only way to prove the line actually gets written and that nothing
    sensitive rides along with it.
"""
import asyncio
import itertools
import logging
import secrets
import subprocess
import time
import urllib.parse

import httpx
import pytest

from kb_platform import common
from kb_platform.hub import (ACCESS_EVENTS, _access_event, _audit_access,
                             _audit_path, _range_from_zero)
from kbenv import BASE, CREDS, U, doc, home

TOK = secrets.token_hex(3)


# --------------------------------------------------------------------------
# the classifier, in isolation
# --------------------------------------------------------------------------

class FakeReq:
    """Enough of web.Request for _access_event."""

    def __init__(self, path="/api/attachment", query=None, method="GET", headers=None):
        self.path, self.query, self.method = path, query or {}, method
        self.headers = headers or {}


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A throwaway REPO_ROOT, so path normalization is tested against a tree
    nothing else can be reading."""
    root = tmp_path.resolve()
    monkeypatch.setattr(common, "REPO_ROOT", root)
    return root


def test_the_three_events_are_the_whole_list():
    # A fourth event must be a deliberate decision with its own tests and its
    # own line in the docs, not something that appears because a proxy grew a
    # hook. Reads are the surveillance-shaped half of an audit trail.
    assert ACCESS_EVENTS == ("document.open", "file.preview", "file.download")


@pytest.mark.parametrize("raw,want", [
    ("company/x.md", "company/x.md"),
    ("/company/x.md", "company/x.md"),          # leading slash is not traversal
    ("company//sub/x.md", "company/sub/x.md"),
    ("company/./x.md", "company/x.md"),
    ("company/sub/../x.md", "company/x.md"),
    ("../../etc/passwd", None),                 # escapes the repo entirely
    ("", None),                                 # the root is not a document
    ("/", None),
    (".", None),
])
def test_paths_are_normalized_or_refused(repo, raw, want):
    assert _audit_path(raw) == want


@pytest.mark.parametrize("value,want", [
    ("bytes=0-", True),
    ("bytes=0-1023", True),
    ("bytes = 0-1023", True),
    ("BYTES=0-", True),
    ("bytes=0-99,200-299", True),
    ("bytes=1024-", False),                     # a seek inside an open file
    ("bytes=-500", False),                      # the tail, not the open
    ("", False),
    ("garbage", False),
])
def test_only_the_opening_byte_range_counts(value, want):
    assert _range_from_zero(value) is want


def test_an_inline_attachment_is_a_preview(repo):
    assert _access_event(FakeReq(query={"path": "company/x.md"}), 200) == \
        ("file.preview", "company/x.md")


def test_an_explicit_download_is_a_download(repo):
    assert _access_event(FakeReq(query={"path": "company/x.md", "dl": "1"}), 200) == \
        ("file.download", "company/x.md")


@pytest.mark.parametrize("dl", ["0", "true", "yes", ""])
def test_only_dl_1_is_a_download(repo, dl):
    """The classifier must read the flag EXACTLY as the attachment handler
    does, or the trail says "download" about a response that went out inline."""
    ev, _ = _access_event(FakeReq(query={"path": "company/x.md", "dl": dl}), 200)
    assert ev == "file.preview"


@pytest.mark.parametrize("status", [401, 403, 404, 409, 500, 502, 304, 301])
def test_a_refusal_is_never_a_read(repo, status):
    assert _access_event(FakeReq(query={"path": "company/x.md"}), status) is None


def test_the_opening_range_is_a_read_and_a_seek_is_not(repo):
    q = {"path": "company/clip.mp4"}
    assert _access_event(FakeReq(query=q, headers={"Range": "bytes=0-"}), 206) == \
        ("file.preview", "company/clip.mp4")
    assert _access_event(FakeReq(query=q, headers={"Range": "bytes=900-"}), 206) is None
    # 206 without a Range header is nonsense; refuse rather than guess.
    assert _access_event(FakeReq(query=q), 206) is None


@pytest.mark.parametrize("route", [
    "/api/tree", "/api/search", "/api/file", "/api/presence", "/api/vc/log",
    "/api/artifact/raw", "/api/whoami", "/api/tasks", "/api/cron",
    "/api/attachment/", "/api/attachments", "/attachment",
])
def test_no_other_route_is_an_access_event(repo, route):
    assert _access_event(FakeReq(path=route, query={"path": "company/x.md"}), 200) is None


@pytest.mark.parametrize("method", ["POST", "PUT", "HEAD", "DELETE", "OPTIONS"])
def test_only_a_get_serves_bytes(repo, method):
    assert _access_event(FakeReq(query={"path": "company/x.md"}, method=method), 200) is None


@pytest.mark.parametrize("raw", ["", "../../etc/shadow", "/"])
def test_a_path_that_does_not_resolve_is_not_audited(repo, raw):
    assert _access_event(FakeReq(query={"path": raw}), 200) is None


def test_the_line_carries_the_facts_and_nothing_else(caplog):
    with caplog.at_level(logging.INFO, logger="kb.hub"):
        _audit_access("file.download", "alice", "company/x.md", "", bytes=41)
    line = caplog.records[-1].getMessage()
    assert "AUDIT file.download actor=alice result=ok" in line
    assert "path='company/x.md'" in line
    assert "bytes=41" in line
    # A local caller has no meaningful source, and an empty one is omitted
    # rather than written as a bare quote-quote.
    assert "source" not in line


def test_a_trusted_source_is_recorded_when_there_is_one(caplog):
    with caplog.at_level(logging.INFO, logger="kb.hub"):
        _audit_access("document.open", "alice", "company/x.md", "203.0.113.4")
    assert "source='203.0.113.4'" in caplog.records[-1].getMessage()


def test_auditing_can_never_break_the_request(caplog):
    """The _audit contract: a bad field must not raise into the proxy that is
    mid-response. An unserializable value is the easy way to prove it."""
    class Boom:
        def __repr__(self):
            raise RuntimeError("nope")

    with caplog.at_level(logging.INFO, logger="kb.hub"):
        _audit_access("file.preview", "alice", "company/x.md", None, bytes=Boom())


# --------------------------------------------------------------------------
# against the running hub and the real journal
# --------------------------------------------------------------------------

JOURNAL = ["sudo", "-n", "journalctl", "-u", "kb-hub", "-g", "AUDIT",
           "--since", "15 min ago", "--no-pager", "-o", "cat"]


def _journal() -> str | None:
    r = subprocess.run(JOURNAL, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


journal_required = pytest.mark.skipif(
    _journal() is None,
    reason="the hub's journal needs root/sudo/adm — CI runs as an ordinary account")


def hits(needle: str, want: int = 1, timeout: float = 25.0) -> list[str]:
    """Audit lines containing `needle`, waiting for journald to catch up.

    Poll positive expectations: the write is asynchronous with respect to the
    HTTP response, so a single read races it.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = [l for l in (_journal() or "").splitlines() if needle in l]
        if len(found) >= want or time.monotonic() > deadline:
            return found
        time.sleep(0.5)


_fences = itertools.count(1)


def fence(c) -> None:
    """Wait until the journal has caught up past everything just done.

    Absence cannot be polled for. So a known-audited action is pushed through
    afterwards and waited on: once ITS line is visible, anything logged before
    it is visible too, and the single-shot negative assertion that follows is
    sound rather than a race that happens to pass.
    """
    p = attach(c, f"fence{next(_fences)}", "fence\n")
    r = c.get("/api/attachment", params={"path": p})
    assert r.status_code == 200, r.text
    assert hits(f"path='{p}'"), "the fence event never reached the journal"


def attach(c, name: str, content: str) -> str:
    """This run's own file, created and written through the product."""
    p = doc(f"aud_{TOK}_{name}.md")
    r = c.post("/api/file", json={"path": p})
    assert r.status_code in (200, 409), r.text
    r = c.post("/api/artifact/write", json={"path": p, "content": content})
    assert r.status_code == 200, r.text
    return p


def _client(who: str) -> httpx.Client:
    cl = httpx.Client(base_url=BASE, timeout=30)
    r = cl.post("/login", data={"username": U(who), "password": CREDS[who]})
    assert r.status_code == 200, r.text
    return cl


@pytest.fixture(scope="module")
def c():
    return _client("alice")


@pytest.fixture(scope="module")
def bob():
    return _client("bob")


async def _join(cookie: str, path: str, epoch: str, frames: int = 0) -> bool:
    """Join a document the way the editor does; returns whether syncd admitted us.

    The client handshake ALWAYS succeeds, even on a refusal: the hub prepares
    our socket before it asks syncd, so a 403/404/409 reaches us as the bridge
    closing rather than as a failed connect. And `ws.closed` only flips once
    that close frame has been read — so read the socket instead of polling it.
    An admitted session answers with the y-protocol SYNC_STEP1 straight away.
    """
    import aiohttp
    from pycrdt import Doc, create_sync_message

    url = (BASE.replace("http://", "ws://") + "/ws/doc/"
           + urllib.parse.quote(path) + "?e=" + urllib.parse.quote(epoch))
    async with aiohttp.ClientSession(
            headers={"Cookie": f"{common.COOKIE_NAME}={cookie}"}) as s:
        async with s.ws_connect(url, heartbeat=None) as ws:
            try:
                first = await asyncio.wait_for(ws.receive(), timeout=10)
            except asyncio.TimeoutError:
                await ws.close()
                return False
            admitted = first.type in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT)
            if admitted:
                for _ in range(frames):
                    await ws.send_bytes(create_sync_message(Doc()))
                    await asyncio.sleep(0.1)
                await asyncio.sleep(0.3)
            await ws.close()
            return admitted


def join(c, path: str, frames: int = 0) -> bool:
    epoch = c.get("/api/doc-epoch", params={"path": path}).json().get("epoch", "")
    return asyncio.run(_join(c.cookies[common.COOKIE_NAME], path, epoch, frames))


@journal_required
def test_a_preview_is_recorded(c):
    body = "preview me\n"
    p = attach(c, "preview", body)
    r = c.get("/api/attachment", params={"path": p})
    assert r.status_code == 200, r.text
    found = hits(f"file.preview actor={U('alice')} result=ok path='{p}'")
    assert found, "an attachment was served and the audit trail says nothing"
    assert f"bytes={len(body)}" in found[-1], \
        "the served byte count is missing from the line"


@journal_required
def test_an_explicit_download_is_recorded_as_one(c):
    p = attach(c, "download", "download me\n")
    r = c.get("/api/attachment", params={"path": p, "dl": "1"})
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers.get("content-disposition", ""), \
        "dl=1 stopped forcing a save — the event would be labelling the wrong thing"
    assert hits(f"file.download actor={U('alice')} result=ok path='{p}'"), \
        "an explicit download left no audit line"
    fence(c)
    # The same request must not also read as a preview, or every download
    # doubles and the two events stop meaning different things.
    assert not [l for l in (_journal() or "").splitlines()
                if "file.preview" in l and f"path='{p}'" in l]


@journal_required
def test_the_line_never_carries_the_request(c):
    """Not the query string, not the cookie, not the user agent, not the body."""
    p = attach(c, "clean", "secret content that must never be logged\n")
    r = c.get("/api/attachment", params={"path": p, "dl": "1"},
              headers={"User-Agent": "kb-audit-probe/1.0"})
    assert r.status_code == 200, r.text
    found = hits(f"file.download actor={U('alice')} result=ok path='{p}'")
    assert found, "no line to inspect"
    line = found[-1]
    for leak in ("kb-audit-probe", "secret content", "dl=1", "?", "Cookie",
                 common.COOKIE_NAME, "User-Agent"):
        assert leak not in line, f"{leak!r} leaked into the audit trail: {line}"


@journal_required
def test_a_refused_attachment_is_not_a_read(c, bob):
    """404 and 403 must produce no result=ok record. Bob's private area is the
    403: the kernel refuses alice's backend, so the bytes never move."""
    missing = doc(f"aud_{TOK}_nosuchfile.md")
    assert c.get("/api/attachment", params={"path": missing}).status_code == 404

    private = home("bob", f"aud_{TOK}_private.md")
    r = bob.post("/api/file", json={"path": private})
    assert r.status_code in (200, 409), r.text
    denied = c.get("/api/attachment", params={"path": private})
    assert denied.status_code in (403, 404), \
        f"alice could read bob's private file ({denied.status_code}) — a permissions bug"

    fence(c)
    lines = (_journal() or "").splitlines()
    for p in (missing, private):
        assert not [l for l in lines if f"path='{p}'" in l and "result=ok" in l], \
            f"a refused request for {p} was recorded as a successful read"


@journal_required
def test_the_app_breathing_is_not_an_access_event(c):
    """Tree, search, presence, history and reading a file's JSON are the app
    working, not a person opening a document. None may produce an event."""
    p = attach(c, "noise", "noise\n")
    c.get("/api/tree")
    c.get("/api/search", params={"q": TOK})
    c.get("/api/presence")
    c.get("/api/vc/log", params={"path": p})
    c.get("/api/file", params={"path": p})
    c.get("/api/whoami")
    c.post("/api/artifact/write", json={"path": p, "content": "noise 2\n"})

    fence(c)
    lines = [l for l in (_journal() or "").splitlines() if f"path='{p}'" in l]
    assert not [l for l in lines if any(e in l for e in ACCESS_EVENTS)], \
        f"routine app traffic produced an access event: {lines}"


@journal_required
def test_opening_a_document_is_recorded_once(c):
    """One accepted collaborative session -> exactly one line, however much
    CRDT traffic crosses it afterwards."""
    p = attach(c, "open", "# open me\n")
    assert join(c, p, frames=3), "the document session was refused"

    needle = f"document.open actor={U('alice')} result=ok path='{p}'"
    assert hits(needle), "joining a document left no audit line"
    fence(c)
    found = [l for l in (_journal() or "").splitlines() if needle in l]
    assert len(found) == 1, f"expected one open, got {len(found)}: {found}"
    assert "?" not in found[0] and "e=" not in found[0], \
        f"the lineage query string leaked into the line: {found[0]}"


@journal_required
def test_a_refused_document_open_is_not_an_open(c, bob):
    """syncd answers 403 before the handshake, so the bridge never accepts —
    and a refusal must not be recorded as someone opening the document."""
    private = home("bob", f"aud_{TOK}_privatedoc.md")
    r = bob.post("/api/file", json={"path": private})
    assert r.status_code in (200, 409), r.text
    assert not join(c, private), "alice joined bob's private document"

    fence(c)
    assert not [l for l in (_journal() or "").splitlines()
                if "document.open" in l and f"path='{private}'" in l], \
        "a refused document open was recorded as a successful one"


@journal_required
def test_a_stale_lineage_is_not_an_open(c):
    """409 from the lineage gate is a refusal like any other."""
    p = attach(c, "stale", "# stale\n")
    assert not asyncio.run(_join(c.cookies[common.COOKIE_NAME], p, "not-the-epoch")), \
        "a stale lineage was admitted to the live session"
    fence(c)
    assert not [l for l in (_journal() or "").splitlines()
                if "document.open" in l and f"path='{p}'" in l], \
        "a stale-lineage refusal was recorded as an open"
