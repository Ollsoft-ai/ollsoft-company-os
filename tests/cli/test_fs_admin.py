"""Privileged filesystem-admin endpoints (/fs/*) on the hub, which run as root.
The authorization is the whole point: create needs write-to-parent; property
changes need own-or-admin. Everything must fail closed.
"""
import json
import time

import httpx
import pytest

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))
TAG = str(int(time.time()))


@pytest.fixture(scope="module", autouse=True)
def _sweep_test_files_afterwards():
    """Every file this module creates carries TAG — remove them all at the end
    (as the users who can: kernel-checked deletes, exactly like the UI)."""
    yield
    k = client("alice")
    for p in (f"company/jnote_{TAG}.md", f"company/aclfile_{TAG}.md",
              f"company/drop_{TAG}.txt", f"users/alice/inj_{TAG}.md"):
        k.post("/api/fs/delete", json={"path": p})
    client("bob").post("/api/fs/delete", json={"path": f"users/bob/mine_{TAG}.md"})


def client(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    assert c.post("/login", data={"username": user, "password": CREDS[user]}).status_code == 200
    return c


def props(c, path):
    return c.get("/fs/props", params={"path": path}).json()


# --- created files inherit the folder's owner/group (needs root) ------------
def test_created_file_inherits_folder_owner():
    bob = client("bob")
    path = f"company/jnote_{TAG}.md"
    r = bob.post("/fs/newfile", json={"path": path})
    assert r.status_code == 200, r.text
    # company/ is owned root:kb-users, so bob's new file is owned by ROOT, not bob.
    p = props(bob, path)
    assert p["owner"] == "root", f"expected inherited root owner, got {p}"
    assert p["group"] == "kb-users"


def test_newfile_denied_without_folder_write():
    carol = client("carol")
    r = carol.post("/fs/newfile", json={"path": f"projects/acme/x_{TAG}.md"})
    assert r.status_code == 403, "carol has no write to acme -> create must be denied"


# --- property changes: owner-or-admin only ---------------------------------
def test_owner_can_change_own_file_props():
    bob = client("bob")
    path = f"users/bob/mine_{TAG}.md"   # in bob's private dir -> bob owns it
    assert bob.post("/fs/newfile", json={"path": path}).status_code == 200
    assert props(bob, path)["owner"] == "bob"
    assert props(bob, path)["can_edit"] is True
    r = bob.post("/fs/props", json={"path": path, "group": "kb-users"})
    assert r.status_code == 200, r.text
    assert props(bob, path)["group"] == "kb-users"


def test_non_owner_cannot_change_props():
    bob = client("bob")
    # overview.md is owned by alice; bob is neither owner nor admin.
    assert props(bob, "company/overview.md")["can_edit"] is False
    r = bob.post("/fs/props", json={"path": "company/overview.md", "group": "bob"})
    assert r.status_code == 403


def test_admin_can_change_any_props():
    # alice is in the sudo group -> platform admin -> can edit anything.
    kry = client("alice")
    assert props(kry, "company/onboarding.md")["can_edit"] is True
    r = kry.post("/fs/props", json={"path": "company/onboarding.md", "group": "kb-users"})
    assert r.status_code == 200, r.text


def test_acl_add_and_remove():
    kry = client("alice")
    path = f"company/aclfile_{TAG}.md"
    assert kry.post("/fs/newfile", json={"path": path}).status_code == 200
    # grant carol an explicit read ACL
    r = kry.post("/fs/props", json={"path": path,
                                    "acl_add": [{"type": "user", "name": "carol", "perms": "r"}]})
    assert r.status_code == 200, r.text
    acls = props(kry, path)["acls"]
    assert any(a["type"] == "user" and a["name"] == "carol" and "r" in a["perms"] for a in acls), acls
    # remove it
    assert kry.post("/fs/props", json={"path": path,
                                       "acl_remove": [{"type": "user", "name": "carol"}]}).status_code == 200
    acls = props(kry, path)["acls"]
    assert not any(a["name"] == "carol" for a in acls)


def test_uploaded_file_inherits_folder_owner():
    bob = client("bob")
    files = {"file": (f"drop_{TAG}.txt", b"dropped content", "text/plain")}
    r = bob.post("/fs/upload", params={"dir": "company"}, files=files)
    assert r.status_code == 200, r.text
    # dropped into company/ (owned root) -> inherits root ownership
    assert props(bob, f"company/drop_{TAG}.txt")["owner"] == "root"


def test_upload_denied_without_folder_write():
    carol = client("carol")
    files = {"file": (f"x_{TAG}.txt", b"x", "text/plain")}
    r = carol.post("/fs/upload", params={"dir": "projects/acme"}, files=files)
    assert r.status_code == 403


def test_props_rejects_injection_and_bad_names():
    kry = client("alice")
    path = f"users/alice/inj_{TAG}.md"
    assert kry.post("/fs/newfile", json={"path": path}).status_code == 200
    for bad in [{"group": "kb-users; rm -rf /"}, {"owner": "root && evil"},
                {"acl_add": [{"type": "user", "name": "carol", "perms": "rwxs"}]}]:
        r = kry.post("/fs/props", json={"path": path, **bad})
        assert r.status_code == 400, f"bad input accepted: {bad}"
