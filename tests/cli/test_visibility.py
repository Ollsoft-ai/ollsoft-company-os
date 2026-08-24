"""Visibility presets in the permissions modal: private really locks everyone
else out (kernel modes), and ACL grants + auto-traverse make "just me and one
colleague" work end to end — the Peter_Zusammenarbeit scenario."""
import json
import os
import stat
import subprocess
import time

import httpx
import pytest
from kbenv import BASE, CREDS, U, doc

TAG = str(int(time.time()))
DIR = doc(f"vis_{TAG}")
DOC = f"{DIR}/plan.md"


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def mode_of(path):
    """The EFFECTIVE permission bits. On an ACL-bearing inode st_mode's group
    bits are the mask, not the owning group's own permission — and "private"
    now leaves one entry behind (u:kbindexer:r, so a private note stays in its
    owner's search), so raw S_IMODE would report 0640 for a file nobody but the
    owner can open."""
    full = f"/srv/kb/{path}"
    mode = stat.S_IMODE(os.stat(full).st_mode)
    out = subprocess.run(["getfacl", "-cpE", "--", full],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.strip().startswith("group::"):
            g = line.strip().split(":")[2]
            bits = (4 if "r" in g else 0) | (2 if "w" in g else 0) | (1 if "x" in g else 0)
            return (mode & ~0o070) | (bits << 3)
    return mode


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
                                  "acl_add": [{"type": "user", "name": U("bob"), "perms": "rwx"}]})
    assert r.status_code == 200, r.text
    assert cl("bob").get("/api/file", params={"path": DOC}).status_code == 200
    assert cl("carol").get("/api/file", params={"path": DOC}).status_code == 403
    # revoke -> locked out again
    k.post("/fs/props", json={"path": DIR, "acl_remove": [{"type": "user", "name": U("bob")}]})
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
