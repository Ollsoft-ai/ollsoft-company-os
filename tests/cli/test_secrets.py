"""Secrets are ordinary kernel-protected files in `_secrets/` folders — shared
with the normal permissions UI — but the platform guarantees their contents
never escape the permission check: not into git history (which outlives both
deletion and chmod), not into the search index, and never through the CRDT
relay. `.git` itself is root-only for the same reason."""
import asyncio
import grp
import json
import os
import pwd
import stat
import time

import aiohttp
import httpx
import pytest
from kbenv import BASE, CREDS, U, doc, people

TAG = str(int(time.time()))
SECRET = doc(f"_secrets/apikey_{TAG}.env")
MARKER = f"verysecret_{TAG}"


def _from_secret(results, marker):
    """Hits that could only have come from a secret: its path, or its text."""
    return [r for r in results if "_secrets/" in r["path"] or marker in r.get("text", "")]


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


@pytest.fixture(scope="module")
def secret_file():
    k = cl("alice")
    k.post("/api/fs/mkdir", json={"path": doc("_secrets")})   # idempotent-ish
    r = k.post("/fs/newfile", json={"path": SECRET})
    assert r.status_code == 200, r.text
    w = k.post("/api/artifact/write", json={"path": SECRET, "content": f"ELEVEN_KEY={MARKER}\n"})
    assert w.status_code == 200, w.text
    yield SECRET
    k.post("/api/fs/delete", json={"path": SECRET})


def test_secret_is_born_private(secret_file):
    # Through /fs/props, not a local os.stat: a _secrets folder is born 0700 and
    # owned by whoever made it, so the account running the suite cannot even
    # traverse into it — which is the guarantee, not a failure.
    pr = cl("alice").get("/fs/props", params={"path": secret_file}).json()
    assert pr["mode"] == "600", pr
    assert pr["owner"] == U("alice"), pr
    assert cl("bob").get("/api/file", params={"path": secret_file}).status_code == 403


def test_sharing_works_like_any_file(secret_file):
    k = cl("alice")
    r = k.post("/fs/props", json={"path": secret_file,
                                  "acl_add": [{"type": "user", "name": U("bob"), "perms": "r"}]})
    assert r.status_code == 200, r.text
    j = cl("bob").get("/api/file", params={"path": secret_file})
    assert j.status_code == 200 and MARKER in j.json()["content"]
    k.post("/fs/props", json={"path": secret_file,
                              "acl_remove": [{"type": "user", "name": U("bob")}]})
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
        # Not "no results": semantic search may offer a page NEAR in meaning
        # (one about accounts and 2FA). The property: nothing from the secret.
        assert _from_secret(res, MARKER) == [], f"secret content leaked into the index: {res}"
    finally:
        k.post("/fs/props", json={"path": secret_file,
                                  "acl_remove": [{"type": "group", "name": "kb-users"}]})


def test_never_in_git_and_git_is_root_only(secret_file):
    k = cl("alice")
    probe = doc(f"gitprobe_{TAG}.md")
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


# --- sharing a `_secrets` FOLDER ------------------------------------------
# The share panel used to refuse every scope but "private" on anything under
# _secrets, which read as a guarantee and was not one (the same grant could be
# made through /fs/props all along) — it just meant a team sharing a project
# had no way to hand each other a credential. What actually contains a secret
# is unchanged and asserted above: never in git, never in the index, never a
# live CRDT session. Who may OPEN one is now an ordinary sharing decision.
def _folder(alice, tag):
    d = doc(f"_secrets_share_{tag}")
    alice.post("/api/fs/mkdir", json={"path": d})
    sec = f"{d}/_secrets"
    assert alice.post("/api/fs/mkdir", json={"path": sec}).status_code == 200
    return d, sec


def _write(c, path, body):
    assert c.post("/fs/newfile", json={"path": path}).status_code == 200, path
    assert c.post("/api/artifact/write", json={"path": path,
                                               "content": body}).status_code == 200


def _reads(user, path, want):
    r = cl(user).get("/api/file", params={"path": path})
    return r.status_code == 200 and want in r.json().get("content", "")


def test_a_secrets_folder_is_still_born_closed():
    alice = cl("alice")
    tag = f"{TAG}_born"
    d, sec = _folder(alice, tag)
    try:
        pr = alice.get("/fs/props", params={"path": sec}).json()
        assert pr["mode"] == "700", pr
        assert pr["owner"] == U("alice"), pr
    finally:
        alice.post("/api/fs/delete", json={"path": d})


def test_sharing_the_folder_shares_the_keys_in_it():
    """The complaint this fixes: the owner of a project's `_secrets/` could not
    give a colleague a credential at all. Sharing the folder must reach both the
    keys already in it and the ones added afterwards — otherwise the share
    silently does nothing, which is exactly what used to happen."""
    alice = cl("alice")
    tag = f"{TAG}_share"
    d, sec = _folder(alice, tag)
    old, new = f"{sec}/old_{tag}.env", f"{sec}/new_{tag}.env"
    try:
        _write(alice, old, f"OLD={MARKER}\n")
        assert not _reads("bob", old, MARKER), "precondition: born closed"

        r = alice.post("/fs/share", json={"path": sec, "scope": "people",
                                          "people": people([("bob", "view")])})
        assert r.status_code == 200, r.text
        assert _reads("bob", old, MARKER), "a key already in the folder must follow the share"

        _write(alice, new, f"NEW={MARKER}\n")
        assert _reads("bob", new, MARKER), "a key added afterwards must follow too"

        # …and taking it back is one call
        assert alice.post("/fs/share", json={"path": sec,
                                             "scope": "private"}).status_code == 200
        assert not _reads("bob", old, MARKER)
        assert not _reads("bob", new, MARKER)
    finally:
        alice.post("/api/fs/delete", json={"path": d})


def test_a_shared_secret_is_still_never_indexed():
    alice = cl("alice")
    tag = f"{TAG}_idx"
    d, sec = _folder(alice, tag)
    marker = f"sharedsecret_{tag}"
    try:
        _write(alice, f"{sec}/k_{tag}.env", f"KEY={marker}\n")
        assert alice.post("/fs/share", json={"path": sec, "scope": "people",
                                             "people": people([("bob", "view")])
                                             }).status_code == 200
        time.sleep(3)
        for who in ("alice", "bob"):
            res = cl(who).get("/api/search", params={"q": marker}).json()["results"]
            assert _from_secret(res, marker) == [], f"a shared secret reached {who}'s index: {res}"
        # the indexer is not quietly a member of the audience either
        g = alice.get("/fs/share", params={"path": sec}).json()["advanced"]["group"]
        assert "kbindexer" not in grp.getgrnam(g).gr_mem, f"{g} carries the indexer"
    finally:
        alice.post("/api/fs/delete", json={"path": d})


def test_a_secrets_folder_can_follow_its_project():
    """"Same as the folder it's in" — the other thing the owner asked for. It is
    the only way a `_secrets` folder widens by itself; a move or a copy into a
    team folder must still leave it closed."""
    alice = cl("alice")
    tag = f"{TAG}_inherit"
    d, sec = _folder(alice, tag)
    key = f"{sec}/k_{tag}.env"
    try:
        _write(alice, key, f"KEY={MARKER}\n")
        assert alice.post("/fs/share", json={"path": d, "scope": "people",
                                             "people": people([("bob", "edit")])
                                             }).status_code == 200
        assert not _reads("bob", key, MARKER), "the folder share must stop at _secrets/"
        assert alice.post("/fs/share", json={"path": sec,
                                             "scope": "inherit"}).status_code == 200
        assert _reads("bob", key, MARKER), "_secrets asked to follow its project must follow it"
    finally:
        alice.post("/api/fs/delete", json={"path": d})


def test_only_the_owner_may_widen_someone_elses_secret():
    alice, bob = cl("alice"), cl("bob")
    tag = f"{TAG}_owner"
    d, sec = _folder(alice, tag)
    try:
        assert alice.post("/fs/share", json={"path": sec, "scope": "people",
                                             "people": people([("bob", "edit")])
                                             }).status_code == 200
        # bob can now write in the folder, but it is not his to hand around
        r = bob.post("/fs/share", json={"path": sec, "scope": "everyone"})
        assert r.status_code == 403, r.text
        assert bob.get("/fs/share", params={"path": sec}).json()["can_edit"] is False
    finally:
        alice.post("/api/fs/delete", json={"path": d})
