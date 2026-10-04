"""Folder create + delete through the per-user backend: both run AS the caller,
so the kernel (write+x on the parent, ancestor traverse) is the entire
authorization story — same as mkdir/rm in that user's terminal."""
import json
import time

import httpx
from kbenv import BASE, CREDS, U, doc, proj



def cl(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def mkdir(c, path):
    return c.post("/api/fs/mkdir", json={"path": path})


def delete(c, path):
    return c.post("/api/fs/delete", json={"path": path})


def tree_paths(c):
    def flatten(nodes, acc):
        for n in nodes:
            acc.append(n["path"])
            flatten(n.get("children", []), acc)
        return acc
    return set(flatten(c.get("/api/tree").json()["tree"], []))


def test_mkdir_create_file_delete_roundtrip():
    j = cl("bob")
    d = doc(f"fsops_{int(time.time())}")
    assert mkdir(j, d).status_code == 200
    assert mkdir(j, d).status_code == 409, "duplicate mkdir must 409"
    # the folder is real and writable: create a doc inside it via the artifact API
    r = j.post("/api/artifact/write", json={"path": f"{d}/note.md", "content": "# hi\n"})
    assert r.status_code == 200, r.text
    assert d in tree_paths(j) and f"{d}/note.md" in tree_paths(j)
    # another user can see it (company tree is shared) and delete THEIR OWN access is moot —
    # alice also has group write on company, so the kernel allows his delete of the file
    assert delete(j, f"{d}/note.md").status_code == 200
    r = delete(j, d)
    assert r.status_code == 200 and r.json()["was_dir"] is True
    assert d not in tree_paths(j)


def test_recursive_delete_of_populated_folder():
    j = cl("bob")
    d = doc(f"fsops_rec_{int(time.time())}")
    assert mkdir(j, d).status_code == 200
    assert mkdir(j, f"{d}/sub").status_code == 200
    assert j.post("/api/artifact/write",
                  json={"path": f"{d}/sub/deep.md", "content": "x"}).status_code == 200
    assert delete(j, d).status_code == 200
    assert d not in tree_paths(j)


def test_kernel_denies_outside_your_authority():
    carol = cl("carol")
    # carol is not on the acme project: no traverse/write there
    assert mkdir(carol, proj("sneaky")).status_code == 403
    assert delete(carol, proj("plan.md")).status_code == 403
    # nobody can delete root-owned agent config through the UI path either
    assert delete(carol, ".agents/skills/kb-orientation/SKILL.md").status_code == 403


def test_top_level_and_traversal_guards():
    k = cl("alice")
    for p in ("company", "projects", "users"):
        r = delete(k, p)
        assert r.status_code == 400, f"deleting top-level {p} must be refused"
    assert delete(k, "../etc/passwd").status_code == 400
    assert mkdir(k, "../outside").status_code == 400
    assert delete(k, doc("does_not_exist_xyz")).status_code == 404


def _children(c, path):
    """The tree rows directly inside `path`, in the order the API returns them."""
    node = None
    stack = list(c.get("/api/tree").json()["tree"])
    while stack:
        n = stack.pop()
        if n["path"] == path:
            node = n
            break
        stack.extend(n.get("children", []))
    assert node is not None, f"{path} not in the tree"
    return node["children"]


def test_tree_orders_folders_by_name_and_files_newest_first():
    """Folders stay alphabetical (they are navigation); files come back newest
    first with an mtime the UI can print."""
    j = cl("bob")
    d = doc(f"fsops_sort_{int(time.time())}")
    assert mkdir(j, d).status_code == 200
    try:
        # folders out of alphabetical order on purpose
        for sub in ("zeta", "alpha", "mid"):
            assert mkdir(j, f"{d}/{sub}").status_code == 200
        # files written oldest -> newest, with names that sort the OTHER way,
        # so name ordering and mtime ordering cannot be confused
        for name in ("a_oldest.md", "b_middle.md", "c_newest.md"):
            assert j.post("/api/artifact/write",
                          json={"path": f"{d}/{name}", "content": name}).status_code == 200
            time.sleep(1.1)   # one-second mtime resolution is the tie-break floor

        kids = _children(j, d)
        dirs = [n["name"] for n in kids if n.get("dir")]
        files = [n["name"] for n in kids if not n.get("dir")]
        assert [n.get("dir", False) for n in kids] == [True] * len(dirs) + [False] * len(files), \
            "folders must still come before files"
        assert dirs == ["alpha", "mid", "zeta"], "folders stay ordered by name"
        assert files == ["c_newest.md", "b_middle.md", "a_oldest.md"], \
            "files come back newest first"
        mtimes = [n["mtime"] for n in kids if not n.get("dir")]
        assert mtimes == sorted(mtimes, reverse=True)
        assert all(isinstance(m, int) for m in mtimes), "the UI prints these"
        assert not any("mtime" in n for n in kids if n.get("dir")), \
            "folders carry no stamp — theirs tracks the listing, not the work"
    finally:
        delete(j, d)
