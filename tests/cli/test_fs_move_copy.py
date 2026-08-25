"""Rename/move + copy through the per-user backend — both run AS the caller,
so the kernel (write+x on both parent folders) is the entire authorization
story, same as `mv`/`cp -r` in that user's terminal. Plus: attachment
downloads must carry the real filename, not save as "attachment"."""
import json
import os
import time

import httpx
import pytest
from kbenv import BASE, CREDS, U, doc, backend_v

TAG = str(int(time.time()))
DIR = doc(f"mvcp_{TAG}")


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c



def write(c, path, content):
    r = c.post("/api/artifact/write", json={"path": path, "content": content})
    assert r.status_code == 200, r.text


@pytest.fixture(scope="module")
def k():
    c = cl("alice")
    if backend_v(c) < 9:
        pytest.skip("alice's backend predates fs rename/copy — restart backends first")
    assert c.post("/api/fs/mkdir", json={"path": DIR}).status_code == 200
    yield c
    c.post("/api/fs/delete", json={"path": DIR})


def test_rename_file_moves_content(k):
    k.post("/api/file", json={"path": f"{DIR}/a.md"})
    write(k, f"{DIR}/a.md", "hello\n")
    r = k.post("/api/fs/rename", json={"src": f"{DIR}/a.md", "dst": f"{DIR}/b.md"})
    assert r.status_code == 200, r.text
    assert not os.path.exists(f"/srv/kb/{DIR}/a.md")
    assert open(f"/srv/kb/{DIR}/b.md").read() == "hello\n"


def test_rename_refuses_overwrite(k):
    k.post("/api/file", json={"path": f"{DIR}/c.md"})
    k.post("/api/file", json={"path": f"{DIR}/d.md"})
    r = k.post("/api/fs/rename", json={"src": f"{DIR}/c.md", "dst": f"{DIR}/d.md"})
    assert r.status_code == 409


def test_move_folder_with_contents(k):
    assert k.post("/api/fs/mkdir", json={"path": f"{DIR}/sub"}).status_code == 200
    k.post("/api/file", json={"path": f"{DIR}/sub/inner.md"})
    write(k, f"{DIR}/sub/inner.md", "inner\n")
    r = k.post("/api/fs/rename", json={"src": f"{DIR}/sub", "dst": f"{DIR}/sub2"})
    assert r.status_code == 200, r.text
    assert open(f"/srv/kb/{DIR}/sub2/inner.md").read() == "inner\n"
    assert not os.path.exists(f"/srv/kb/{DIR}/sub")


def test_guards(k):
    # top-level areas are immovable; the repo root is not a destination;
    # a folder can never move into itself
    assert k.post("/api/fs/rename", json={"src": "company", "dst": "company2"}).status_code == 400
    assert k.post("/api/fs/rename", json={"src": f"{DIR}/b.md", "dst": "b.md"}).status_code == 400
    assert k.post("/api/fs/rename", json={"src": DIR, "dst": f"{DIR}/inside"}).status_code == 400
    assert k.post("/api/fs/copy", json={"src": DIR, "dst": f"{DIR}/inside"}).status_code == 400
    assert k.post("/api/fs/rename", json={"src": f"{DIR}/missing.md", "dst": f"{DIR}/x.md"}).status_code == 404


def test_copy_file_owned_by_copier(k):
    r = k.post("/api/fs/copy", json={"src": f"{DIR}/b.md", "dst": f"{DIR}/b copy.md"})
    assert r.status_code == 200, r.text
    assert open(f"/srv/kb/{DIR}/b copy.md").read() == "hello\n"
    assert open(f"/srv/kb/{DIR}/b.md").read() == "hello\n"       # source untouched
    import pwd
    assert pwd.getpwuid(os.stat(f"/srv/kb/{DIR}/b copy.md").st_uid).pw_name == U("alice")
    # no silent overwrite
    assert k.post("/api/fs/copy", json={"src": f"{DIR}/b.md", "dst": f"{DIR}/b copy.md"}).status_code == 409


def test_copy_folder_recursive(k):
    r = k.post("/api/fs/copy", json={"src": f"{DIR}/sub2", "dst": f"{DIR}/sub3"})
    assert r.status_code == 200, r.text
    assert open(f"/srv/kb/{DIR}/sub3/inner.md").read() == "inner\n"


def test_rename_needs_kernel_write(k):
    """bob may not move a file out of alice's private folder — the kernel
    (no w+x on the 0700 dir) is the gate, not any app-level check."""
    j = cl("bob")
    if backend_v(j) < 9:
        pytest.skip("bob's backend predates fs rename/copy — restart backends first")
    assert k.post("/api/fs/mkdir", json={"path": f"{DIR}/priv"}).status_code == 200
    k.post("/api/file", json={"path": f"{DIR}/priv/x.md"})
    r = k.post("/fs/props", json={"path": f"{DIR}/priv", "visibility": "private"})
    assert r.status_code == 200, r.text
    r = j.post("/api/fs/rename", json={"src": f"{DIR}/priv/x.md", "dst": f"{DIR}/stolen.md"})
    assert r.status_code == 403
    r = j.post("/api/fs/copy", json={"src": f"{DIR}/priv/x.md", "dst": f"{DIR}/stolen.md"})
    assert r.status_code == 403
    assert not os.path.exists(f"/srv/kb/{DIR}/stolen.md")


def test_attachment_carries_real_filename(k):
    write(k, f"{DIR}/report.docx", "not really a docx\n")
    r = k.get("/api/attachment", params={"path": f"{DIR}/report.docx"})
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert cd.startswith("inline"), cd          # images/PDFs keep rendering in-tab
    assert 'filename="report.docx"' in cd, cd   # downloads save under the real name
