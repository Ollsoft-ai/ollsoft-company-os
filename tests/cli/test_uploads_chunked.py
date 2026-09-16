"""Chunked uploads — the protocol that removed the upload ceiling.

A single POST cannot cross the edge in front of this box (a request body over
100 MB is refused there, which is what "larger than the server's upload limit"
always was). So both tiers take a file a chunk at a time: the hub's
`/fs/upload/*` into a folder, the per-user backend's `/api/upload/*` into a
document's `_files/`. What matters here is that the assembled bytes are exactly
the bytes sent, that a retried or misordered chunk cannot corrupt the file, that
the half-arrived file is invisible while it is in flight, and that the
authorisation is the same as the single-shot upload it replaces.
"""
import hashlib
import os
import time

import httpx
import pytest
from kbenv import AREA, BASE, CREDS, U, doc, proj

TAG = str(int(time.time()))
# Three chunks and a bit: enough that offsets, resumes and the final short
# chunk are all exercised, small enough to stay a fast test.
CHUNK = 64 * 1024
PAYLOAD = os.urandom(CHUNK * 3 + 1234)


def client(user):
    c = httpx.Client(base_url=BASE, timeout=60)
    assert c.post("/login", data={"username": U(user),
                                  "password": CREDS[user]}).status_code == 200
    return c


def names():
    return [doc(f"chunk_{TAG}.bin"), doc(f"_files/chunk_{TAG}.bin"),
            doc(f"resume_{TAG}.bin"), doc(f"empty_{TAG}.bin"),
            doc(f"short_{TAG}.bin"), doc(f"over_{TAG}.bin"),
            doc(f"ghost_{TAG}.bin"), doc(f"mine_{TAG}.bin"),
            doc(f"big_{TAG}.bin")]


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    k = client("alice")
    for p in names():
        k.post("/api/fs/delete", json={"path": p})


def push(c, base, folder, name, data, chunk=CHUNK, files=False):
    """Drive the whole protocol the way the browser does. Returns the finish
    response so a test can assert on what the server reported."""
    r = c.post(f"{base}/begin", json={"dir": folder, "name": name,
                                      "size": len(data), "files": files})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    off = 0
    while off < len(data):
        end = min(off + chunk, len(data))
        rc = c.post(f"{base}/chunk", params={"id": sid, "offset": off},
                    content=data[off:end])
        assert rc.status_code == 200, rc.text
        off = rc.json()["offset"]
    return sid, c.post(f"{base}/finish", json={"id": sid})


def fetched(c, path):
    r = c.get("/api/attachment", params={"path": path})
    assert r.status_code == 200, r.text
    return r.content


# --- the hub tier: a file, in pieces, into a folder -------------------------
def test_chunked_upload_into_folder_reassembles_exactly():
    alice = client("alice")
    name = f"chunk_{TAG}.bin"
    sid, fin = push(alice, "/fs/upload", AREA, name, PAYLOAD)
    assert fin.status_code == 200, fin.text
    assert fin.json()["size"] == len(PAYLOAD)
    got = fetched(alice, doc(name))
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(PAYLOAD).hexdigest()


def test_chunked_upload_inherits_folder_owner_like_single_shot():
    """The spool is born in the destination folder and renamed into place, so
    the finished file must be indistinguishable from one /fs/upload wrote: this
    run's area is owned root:kb-users, so bob's upload is owned by root."""
    bob = client("bob")
    name = f"resume_{TAG}.bin"
    _sid, fin = push(bob, "/fs/upload", AREA, name, PAYLOAD)
    assert fin.status_code == 200, fin.text
    p = bob.get("/fs/props", params={"path": doc(name)}).json()
    assert p["owner"] == "root", p
    assert p["group"] == "kb-users", p


def test_zero_byte_file_needs_no_chunks():
    alice = client("alice")
    name = f"empty_{TAG}.bin"
    _sid, fin = push(alice, "/fs/upload", AREA, name, b"")
    assert fin.status_code == 200, fin.text
    assert fetched(alice, doc(name)) == b""


# --- retries and misordering cannot corrupt the file ------------------------
def test_resent_chunk_is_idempotent_and_a_gap_is_refused():
    alice = client("alice")
    name = f"chunk_{TAG}.bin"
    r = alice.post("/fs/upload/begin", json={"dir": AREA, "name": name,
                                             "size": len(PAYLOAD)})
    sid = r.json()["id"]
    alice.post("/fs/upload/chunk", params={"id": sid, "offset": 0},
               content=PAYLOAD[:CHUNK])
    # the same chunk again — a client that lost the answer and retried
    again = alice.post("/fs/upload/chunk", params={"id": sid, "offset": 0},
                       content=PAYLOAD[:CHUNK])
    assert again.status_code == 200
    assert again.json()["offset"] == CHUNK, "a re-sent chunk must not advance twice"
    # a chunk that would leave a hole is refused, and says where we really are
    gap = alice.post("/fs/upload/chunk", params={"id": sid, "offset": CHUNK * 2},
                     content=PAYLOAD[CHUNK * 2:CHUNK * 3])
    assert gap.status_code == 409, gap.text
    assert gap.json()["offset"] == CHUNK
    # and the upload survives that: resume from the reported offset
    for off in range(CHUNK, len(PAYLOAD), CHUNK):
        alice.post("/fs/upload/chunk", params={"id": sid, "offset": off},
                   content=PAYLOAD[off:off + CHUNK])
    fin = alice.post("/fs/upload/finish", json={"id": sid})
    assert fin.status_code == 200, fin.text
    assert fetched(alice, doc(name)) == PAYLOAD


def test_finishing_short_is_refused():
    alice = client("alice")
    r = alice.post("/fs/upload/begin", json={"dir": AREA, "name": f"short_{TAG}.bin",
                                             "size": len(PAYLOAD)})
    sid = r.json()["id"]
    alice.post("/fs/upload/chunk", params={"id": sid, "offset": 0},
               content=PAYLOAD[:CHUNK])
    fin = alice.post("/fs/upload/finish", json={"id": sid})
    assert fin.status_code == 409, "a truncated file must never be published"
    assert alice.get("/fs/props", params={"path": doc(f"short_{TAG}.bin")}
                     ).status_code == 404
    alice.post("/fs/upload/abort", json={"id": sid})


def test_more_bytes_than_declared_are_refused():
    alice = client("alice")
    r = alice.post("/fs/upload/begin", json={"dir": AREA, "name": f"over_{TAG}.bin",
                                             "size": CHUNK})
    sid = r.json()["id"]
    over = alice.post("/fs/upload/chunk", params={"id": sid, "offset": 0},
                      content=PAYLOAD[:CHUNK * 2])
    assert over.status_code == 400, over.text
    alice.post("/fs/upload/abort", json={"id": sid})


# --- the half-arrived file is invisible, and leaves nothing behind ----------
def test_spool_is_invisible_in_the_tree_and_removed_on_abort():
    alice = client("alice")
    r = alice.post("/fs/upload/begin", json={"dir": AREA, "name": f"ghost_{TAG}.bin",
                                             "size": len(PAYLOAD)})
    sid = r.json()["id"]
    alice.post("/fs/upload/chunk", params={"id": sid, "offset": 0},
               content=PAYLOAD[:CHUNK])
    tree = alice.get("/api/tree").text
    assert ".kbup-" not in tree, "a half-uploaded file must not appear in the tree"
    assert f"ghost_{TAG}.bin" not in tree, "nor under its final name"
    assert alice.post("/fs/upload/abort", json={"id": sid}).status_code == 200
    # the session is gone, and so is its spool
    assert alice.post("/fs/upload/chunk", params={"id": sid, "offset": CHUNK},
                      content=b"x").status_code == 404
    assert ".kbup-" not in alice.get("/api/tree").text


# --- authorisation is exactly the single-shot upload's ----------------------
def test_begin_denied_without_folder_write():
    carol = client("carol")
    r = carol.post("/fs/upload/begin", json={"dir": proj(), "name": f"no_{TAG}.bin",
                                             "size": 10})
    assert r.status_code == 403, r.text


def test_another_user_cannot_append_to_my_upload():
    alice, bob = client("alice"), client("bob")
    r = alice.post("/fs/upload/begin", json={"dir": AREA, "name": f"mine_{TAG}.bin",
                                             "size": len(PAYLOAD)})
    sid = r.json()["id"]
    hijack = bob.post("/fs/upload/chunk", params={"id": sid, "offset": 0},
                      content=b"not yours")
    assert hijack.status_code == 404, hijack.text
    assert bob.post("/fs/upload/finish", json={"id": sid}).status_code == 404
    alice.post("/fs/upload/abort", json={"id": sid})


def test_unauthenticated_cannot_start_an_upload():
    anon = httpx.Client(base_url=BASE, timeout=15)
    assert anon.post("/fs/upload/begin",
                     json={"dir": AREA, "name": "x.bin", "size": 1}).status_code == 401


def test_traversal_in_the_name_is_refused():
    alice = client("alice")
    for bad in ("../escape.bin", "a/b.bin", "", ".."):
        r = alice.post("/fs/upload/begin", json={"dir": AREA, "name": bad, "size": 1})
        assert r.status_code == 400, f"{bad!r} was accepted: {r.text}"


def test_chunk_larger_than_the_cap_is_refused():
    alice = client("alice")
    from kb_platform import uploads
    r = alice.post("/fs/upload/begin", json={"dir": AREA, "name": f"big_{TAG}.bin",
                                             "size": uploads.MAX_CHUNK * 2})
    if r.status_code == 507:
        pytest.skip("no disk space for the cap test")
    sid = r.json()["id"]
    over = alice.post("/fs/upload/chunk", params={"id": sid, "offset": 0},
                      content=b"\0" * (uploads.MAX_CHUNK + 4096))
    assert over.status_code == 413, over.text
    alice.post("/fs/upload/abort", json={"id": sid})


# --- the backend tier: an attachment for a document -------------------------
def test_chunked_attachment_lands_in_files_and_links_like_the_old_one():
    alice = client("alice")
    name = f"chunk_{TAG}.bin"
    _sid, fin = push(alice, "/api/upload", AREA, name, PAYLOAD, files=True)
    assert fin.status_code == 200, fin.text
    j = fin.json()
    assert j["link"] == f"_files/{name}", j
    assert j["path"] == doc(f"_files/{name}"), j
    assert fetched(alice, j["path"]) == PAYLOAD


def test_attachment_upload_refuses_a_folder_it_cannot_reach():
    carol = client("carol")
    r = carol.post("/api/upload/begin", json={"dir": proj(), "name": f"no_{TAG}.bin",
                                              "size": 10, "files": True})
    assert r.status_code in (400, 403), r.text
