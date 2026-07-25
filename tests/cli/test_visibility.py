"""Visibility presets in the permissions modal: private really locks everyone
else out (kernel modes), and ACL grants + auto-traverse make "just me and one
colleague" work end to end — the Peter_Zusammenarbeit scenario."""
import json
import os
import stat
import time

import httpx
import pytest

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))
TAG = str(int(time.time()))
DIR = f"company/vis_{TAG}"
DOC = f"{DIR}/plan.md"


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": user, "password": CREDS[user]})
    return c


def mode_of(path):
    return stat.S_IMODE(os.stat(f"/srv/kb/{path}").st_mode)


@pytest.fixture(scope="module")
def folder():
    k = cl("alice")
    assert k.post("/api/fs/mkdir", json={"path": DIR}).status_code == 200
    assert k.post("/api/file", json={"path": DOC}).status_code == 200
    k.post("/api/artifact/write", json={"path": DOC, "content": "# secret plan\n"})
    yield
    cl("alice").post("/api/fs/delete", json={"path": DIR})


def test_private_locks_out_everyone_else(folder):
    k = cl("alice")
    assert cl("bob").get("/api/file", params={"path": DOC}).status_code == 200  # company default
    r = k.post("/fs/props", json={"path": DIR, "visibility": "private"})
    assert r.status_code == 200, r.text
    assert mode_of(DIR) == 0o700
    assert cl("bob").get("/api/file", params={"path": DOC}).status_code == 403


def test_acl_grant_reopens_for_one_colleague(folder):
    k = cl("alice")
    # "only me and bob": private dir + one ACL grant
    r = k.post("/fs/props", json={"path": DIR,
                                  "acl_add": [{"type": "user", "name": "bob", "perms": "rwx"}]})
    assert r.status_code == 200, r.text
    assert cl("bob").get("/api/file", params={"path": DOC}).status_code == 200
    assert cl("carol").get("/api/file", params={"path": DOC}).status_code == 403
    # revoke -> locked out again
    k.post("/fs/props", json={"path": DIR, "acl_remove": [{"type": "user", "name": "bob"}]})
    assert cl("bob").get("/api/file", params={"path": DOC}).status_code == 403


def test_back_to_company_visibility(folder):
    k = cl("alice")
    assert k.post("/fs/props", json={"path": DIR, "visibility": "company"}).status_code == 200
    assert mode_of(DIR) == 0o2775
    assert cl("bob").get("/api/file", params={"path": DOC}).status_code == 200
    # file-level presets work too
    assert k.post("/fs/props", json={"path": DOC, "visibility": "private"}).status_code == 200
    assert mode_of(DOC) == 0o600
    assert cl("bob").get("/api/file", params={"path": DOC}).status_code == 403
    assert k.post("/fs/props", json={"path": DOC, "visibility": "company"}).status_code == 200
    assert cl("bob").get("/api/file", params={"path": DOC}).status_code == 200
