"""Secrets are ordinary kernel-protected files in `_secrets/` folders — shared
with the normal permissions UI — but the platform guarantees their contents
never escape the permission check: not into git history (which outlives both
deletion and chmod), not into the search index, and never through the CRDT
relay. `.git` itself is root-only for the same reason."""
import asyncio
import json
import os
import pwd
import stat
import time

import aiohttp
import httpx
import pytest

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))
TAG = str(int(time.time()))
SECRET = f"company/_secrets/apikey_{TAG}.env"
MARKER = f"verysecret_{TAG}"


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": user, "password": CREDS[user]})
    return c


@pytest.fixture(scope="module")
def secret_file():
    k = cl("alice")
    k.post("/api/fs/mkdir", json={"path": "company/_secrets"})   # idempotent-ish
    r = k.post("/fs/newfile", json={"path": SECRET})
    assert r.status_code == 200, r.text
    w = k.post("/api/artifact/write", json={"path": SECRET, "content": f"ELEVEN_KEY={MARKER}\n"})
    assert w.status_code == 200, w.text
    yield SECRET
    k.post("/api/fs/delete", json={"path": SECRET})


def test_secret_is_born_private(secret_file):
    st = os.stat(f"/srv/kb/{secret_file}")
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert pwd.getpwuid(st.st_uid).pw_name == "alice"
    assert cl("bob").get("/api/file", params={"path": secret_file}).status_code == 403


def test_sharing_works_like_any_file(secret_file):
    k = cl("alice")
    r = k.post("/fs/props", json={"path": secret_file,
                                  "acl_add": [{"type": "user", "name": "bob", "perms": "r"}]})
    assert r.status_code == 200, r.text
    j = cl("bob").get("/api/file", params={"path": secret_file})
    assert j.status_code == 200 and MARKER in j.json()["content"]
    k.post("/fs/props", json={"path": secret_file,
                              "acl_remove": [{"type": "user", "name": "bob"}]})
    assert cl("bob").get("/api/file", params={"path": secret_file}).status_code == 403


def test_never_indexed_even_when_group_readable(secret_file):
    # share with kb-users (which kbindexer belongs to) — the indexer COULD read
    # it; the _secrets exclusion is what keeps it out of the index
    k = cl("alice")
    k.post("/fs/props", json={"path": secret_file,
                              "acl_add": [{"type": "group", "name": "kb-users", "perms": "r"}]})
    try:
        time.sleep(3)   # indexer watch + settle
        res = k.get("/api/search", params={"q": MARKER}).json()["results"]
        assert res == [], f"secret content leaked into the index: {res}"
    finally:
        k.post("/fs/props", json={"path": secret_file,
                                  "acl_remove": [{"type": "group", "name": "kb-users"}]})


def test_never_in_git_and_git_is_root_only(secret_file):
    k = cl("alice")
    probe = f"company/gitprobe_{TAG}.md"
    k.post("/api/file", json={"path": probe})       # force a snapshot cycle
    state = {}
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            state = json.load(open("/run/kb/git-state.json"))
        except (OSError, ValueError):
            state = {}
        if state.get("commits"):
            break
        time.sleep(1)
    k.post("/api/fs/delete", json={"path": probe})
    assert state.get("commits"), "syncd should have snapshotted the probe file"
    assert state.get("tracked_secrets") == 0, "a _secrets file is tracked by git!"
    # .git itself must be unreadable to users — its objects hold every committed
    # version of every file, bypassing file permissions
    assert not os.access("/srv/kb/.git", os.R_OK)


def test_no_crdt_session_for_secrets(secret_file):
    k = cl("alice")
    cookies = dict(k.cookies)
    # even WITH a valid lineage epoch, secrets are refused
    ep = k.get("/api/doc-epoch", params={"path": secret_file}).json().get("epoch", "")

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            try:
                async with s.ws_connect(BASE + "/ws/doc/" + secret_file + "?e=" + ep) as ws:
                    msg = await asyncio.wait_for(ws.receive(), 6)
                    return msg.type in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT)
            except Exception:
                return False
    assert asyncio.run(go()) is False, "the owner must not get a live session on a secret"
