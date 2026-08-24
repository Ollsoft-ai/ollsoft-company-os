"""Artifact file read/write via the bridge (kb-read / kb-write), all AS the
viewer's OS user — the kernel is the boundary, exactly like the terminal."""
import json

import httpx
from kbenv import BASE, CREDS, U, doc, home, proj



def cl(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def read(c, path):
    return c.post("/api/artifact/read", json={"path": path})


def write(c, path, content):
    return c.post("/api/artifact/write", json={"path": path, "content": content})


def test_write_then_read_roundtrip():
    k = cl("alice")
    try:
        r = write(k, doc("io_roundtrip.md"), "# hello\nvalue=RT_OK\n")
        assert r.status_code == 200, r.text
        back = read(k, doc("io_roundtrip.md"))
        assert back.status_code == 200 and "RT_OK" in back.json()["content"]
    finally:
        k.post("/api/fs/delete", json={"path": doc("io_roundtrip.md")})


def test_write_bounded_by_kernel_permissions():
    carol = cl("carol")
    # carol cannot write into the acme project (not on the team)
    assert write(carol, proj("hack.md"), "x").status_code == 403
    # carol cannot read acme either
    assert read(carol, proj("plan.md")).status_code == 403


def test_cannot_write_readonly_config():
    # .claude/CLAUDE.md is root-owned 644 -> nobody but an admin can write it
    assert write(cl("alice"), ".claude/CLAUDE.md", "hacked").status_code == 403


def test_write_requires_existing_parent_and_stays_in_repo():
    k = cl("alice")
    assert write(k, doc("nope/deep/x.md"), "x").status_code == 400   # parent missing
    # path traversal is refused by resolve_repo_path
    assert read(k, "../../etc/passwd").status_code in (400, 404, 403)


def test_read_is_kernel_scoped():
    # bob cannot read alice's private note; alice can
    assert read(cl("bob"), home("alice", "private.md")).status_code == 403
    r = read(cl("alice"), home("alice", "private.md"))
    assert r.status_code == 200 and "aardvark" in r.json()["content"]
