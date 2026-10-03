"""An artifact's file verbs stay inside its own folder, and the backend checks
that on the file it actually opened: a symlink a colleague planted in the
folder must not take kb-read, kb-write, kb-read-bytes or an upload anywhere
else, with the viewer's authority.

Runs against the deployed platform; deploy first."""
import os
import time

import httpx
from kbenv import BASE, CREDS, U, doc, full, home

TAG = str(int(time.time()))
DIR = doc(f"links_{TAG}")
ART = f"{DIR}/probe.html"


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=20)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def setup_module():
    a = cl("alice")
    assert a.post("/api/fs/mkdir", json={"path": DIR}).status_code == 200
    assert a.post("/api/artifact/write",
                  json={"path": ART, "content": "<!doctype html><p>probe</p>\n"}).status_code == 200


def teardown_module():
    cl("alice").post("/api/fs/delete", json={"path": DIR})


def test_file_verbs_never_follow_a_link_out_of_the_folder():
    a = cl("alice")
    victim = home("alice", f"victim_{TAG}.md")
    assert a.post("/api/artifact/write", json={"path": victim, "content": "ORIGINAL\n"}).status_code == 200
    link = full(f"{DIR}/link.md")
    os.symlink(full(victim), link)          # planted by whoever can write the folder
    try:
        r = a.post("/api/artifact/read", json={"artifact": ART, "path": f"{DIR}/link.md"})
        assert r.status_code == 403 and "ORIGINAL" not in r.text, r.text
        r = a.post("/api/artifact/write",
                   json={"artifact": ART, "path": f"{DIR}/link.md", "content": "PWNED\n"})
        assert r.status_code == 403, r.text
        r = a.get("/api/artifact/bytes", params={"artifact": ART, "path": f"{DIR}/link.md"})
        assert r.status_code == 403, r.text
        assert a.post("/api/artifact/read", json={"path": victim}).json()["content"] == "ORIGINAL\n"
    finally:
        link.unlink()
        a.post("/api/fs/delete", json={"path": victim})


def test_in_the_folder_everything_still_works():
    a = cl("alice")
    r = a.post("/api/artifact/write", json={"artifact": ART, "path": f"{DIR}/data.txt", "content": "hello\n"})
    assert r.status_code == 200, r.text
    r = a.post("/api/artifact/read", json={"artifact": ART, "path": f"{DIR}/data.txt"})
    assert r.status_code == 200 and r.json()["content"] == "hello\n"
    # someone else's page runs for bob just the same: no question is asked
    r = cl("bob").post("/api/artifact/read", json={"artifact": ART, "path": f"{DIR}/data.txt"})
    assert r.status_code == 200, r.text


def test_bytes_come_from_the_folder_as_a_download():
    a = cl("alice")
    assert a.post("/api/artifact/write", json={"path": f"{DIR}/bytes.txt", "content": "hi\n"}).status_code == 200
    r = a.get("/api/artifact/bytes", params={"artifact": ART, "path": f"{DIR}/bytes.txt"})
    assert r.status_code == 200 and r.content == b"hi\n"
    assert r.headers["content-disposition"].startswith("attachment")
    assert "script-src 'none'" in r.headers["content-security-policy"]
    out = a.get("/api/artifact/bytes", params={"artifact": ART, "path": home("alice", "private.md")})
    assert out.status_code == 403


def test_an_upload_never_lands_through_a_planted_link():
    a = cl("alice")
    victim = home("alice", f"upvictim_{TAG}.txt")
    assert a.post("/api/artifact/write", json={"path": victim, "content": "ORIGINAL\n"}).status_code == 200
    files = full(f"{DIR}/_files")
    files.mkdir(exist_ok=True)
    os.symlink(full(victim), files / "evil.txt")
    try:
        r = a.post(f"/api/upload?dir={DIR}", files={"file": ("evil.txt", b"PWNED\n")})
        assert r.status_code == 400, r.text
        assert a.post("/api/artifact/read", json={"path": victim}).json()["content"] == "ORIGINAL\n"
    finally:
        (files / "evil.txt").unlink()
        a.post("/api/fs/delete", json={"path": victim})
