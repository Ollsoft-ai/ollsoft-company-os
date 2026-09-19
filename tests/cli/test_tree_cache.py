"""The tree answers 304 when nothing moved, and 200 the moment something does.

Every open tab polls /api/tree every 4 s. The backend now hashes a cheap
signature of the repo (names, modes, sizes, mtimes, ctimes — no ACL reads) and
only rebuilds the tree when that moved; the JSON's own hash is the ETag, so an
unchanged poll costs a signature and no bytes. What has to stay true:

  * a matching If-None-Match is a 304, and the shape of a 200 is unchanged;
  * a change made THROUGH this backend is in the very next poll — no hold;
  * a change made elsewhere (the hub, as root, changing a mode: no request
    ever reaches this backend) is seen within the signature TTL. That is the
    ctime path — a permission change touches no name, size or mtime.
"""
import time

import httpx
from kbenv import BASE, CREDS, U, doc


def client(user="alice"):
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def flatten(nodes, acc):
    for n in nodes:
        acc[n["path"]] = n
        if n.get("dir"):
            flatten(n.get("children", []), acc)
    return acc


def test_a_matching_etag_is_a_304_and_a_200_keeps_its_shape():
    c = client()
    r1 = c.get("/api/tree")
    assert r1.status_code == 200, r1.text
    etag = r1.headers.get("etag")
    assert etag, "no ETag — every poll is a full answer again"
    j = r1.json()
    assert "root" in j and isinstance(j["tree"], list) and j["tree"], "the shape moved"
    r2 = c.get("/api/tree", headers={"If-None-Match": etag})
    assert r2.status_code == 304, f"{r2.status_code} {r2.text[:120]}"
    assert r2.content == b"", "a 304 carrying a body is a 200 in disguise"
    assert r2.headers.get("etag") == etag


def test_your_own_change_is_in_the_very_next_poll():
    c = client()
    etag = c.get("/api/tree").headers["etag"]
    p = doc(f"treecache_{int(time.time())}.md")
    try:
        assert c.post("/api/file", json={"path": p}).status_code == 200
        r = c.get("/api/tree", headers={"If-None-Match": etag})
        assert r.status_code == 200, "a file you just created must not sit behind the hold"
        assert r.headers["etag"] != etag
        assert p in flatten(r.json()["tree"], {}), "new file missing from the fresh tree"
    finally:
        c.post("/api/fs/delete", json={"path": p})


def test_a_permission_change_made_by_the_hub_is_seen_within_the_ttl():
    """No request touches this backend: the hub chmods as root. Only ctime
    moves, and the tree's audience marker on that row has to follow."""
    c = client()
    p = doc(f"treecache_priv_{int(time.time())}.md")
    try:
        assert c.post("/api/file", json={"path": p}).status_code == 200
        r0 = c.get("/api/tree")
        assert "aud" not in flatten(r0.json()["tree"], {})[p], "already marked — nothing to test"
        etag = r0.headers["etag"]
        assert c.post("/fs/props", json={"path": p, "visibility": "private"}).status_code == 200
        deadline = time.time() + 8          # TTL is 2 s; the client polls at 4 s
        r = None
        while time.time() < deadline:
            r = c.get("/api/tree", headers={"If-None-Match": etag})
            if r.status_code == 200:
                break
            time.sleep(0.4)
        assert r is not None and r.status_code == 200, "the hub's chmod never reached the tree"
        assert flatten(r.json()["tree"], {})[p].get("aud") == "solo"
    finally:
        c.post("/api/fs/delete", json={"path": p})


def test_whoami_carries_the_protocol_version():
    j = client().get("/api/whoami").json()
    assert isinstance(j.get("v"), int) and j["v"] >= 29
