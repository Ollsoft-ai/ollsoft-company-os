"""Sharing as PEOPLE (/fs/share), and moves that re-home what they move.

The panel says "alice can edit, carol can view"; the filesystem says owning
group, mode bits and POSIX ACLs. These tests pin the translation at the only
level that matters — can this person actually open the file — and pin the two
properties the design exists for: adding someone never rewrites the files, and
restricting a subfolder never rewrites the folder above it.
"""
import json
import os
import stat
import subprocess
import time

import httpx
import pytest
from kbenv import BASE, CREDS, L, U, doc, people

TAG = str(int(time.time()))
DIR = doc(f"share_{TAG}")


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def backend_v(c) -> int:
    r = c.get("/api/cron")
    return r.json().get("v", 0) if r.status_code == 200 else 0


def share(c, path, scope, pairs=()):
    return c.post("/fs/share", json={"path": path, "scope": scope,
                                     "people": people(pairs)})


def state(c, path):
    r = c.get("/fs/share", params={"path": path})
    assert r.status_code == 200, r.text
    return r.json()


def roles(st):
    # Back to logical names, so the assertions read "bob", not kbt_<ns>_bob.
    return {L(p["user"]): p["role"] for p in st["people"]}


def can_open(user, path):
    return cl(user).get("/api/file", params={"path": path}).status_code == 200


def group_of(c, path):
    """Read through the product, never with a local stat(). This suite runs as
    krystof on the box and as alice on CI, and neither is a member of the group
    a restricted folder gets handed — stat() just raises PermissionError, which
    looks exactly like a product bug and is not one."""
    return c.get("/fs/share", params={"path": path}).json()["advanced"]["group"]


@pytest.fixture(scope="module")
def k():
    c = cl("alice")
    if backend_v(c) < 21:
        pytest.skip("alice's backend predates /fs/share — restart backends first")
    assert c.post("/api/fs/mkdir", json={"path": DIR}).status_code == 200
    yield c
    c.post("/api/fs/delete", json={"path": DIR})


@pytest.fixture
def folder(k):
    """A fresh sub-folder per test, so nothing leaks between them."""
    name = f"{DIR}/f{int(time.time() * 1000) % 10 ** 8}"
    assert k.post("/api/fs/mkdir", json={"path": name}).status_code == 200
    doc = f"{name}/plan.md"
    assert k.post("/api/file", json={"path": doc}).status_code == 200
    k.post("/api/artifact/write", json={"path": doc, "content": "# plan\n"})
    yield name, doc
    k.post("/api/fs/delete", json={"path": name})


# --- the everyday thing: pick people, pick view or edit ----------------------

def test_specific_people_view_and_edit(k, folder):
    name, doc = folder
    r = share(k, name, "people", [("bob", "edit"), ("carol", "view")])
    assert r.status_code == 200, r.text
    assert can_open("bob", doc), "an editor can open it"
    assert can_open("carol", doc), "a viewer can open it"
    assert cl("bob").post("/api/artifact/write",
                          json={"path": doc, "content": "# plan\nedited\n"}).status_code == 200
    assert cl("carol").post("/api/artifact/write",
                            json={"path": doc, "content": "nope\n"}).status_code == 403, \
        "a viewer must not be able to write"
    got = roles(state(k, name))
    assert got.get("bob") == "edit" and got.get("carol") == "view", got
    assert got.get("alice") == "owner"
    assert "kbindexer" not in got, "the service account is not a person"


def test_removing_a_person_revokes(k, folder):
    name, doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    assert can_open("bob", doc)
    assert share(k, name, "people", []).status_code == 200
    assert not can_open("bob", doc), "removing someone must actually revoke"


def test_adding_a_person_does_not_touch_the_files(k, folder):
    """The whole point of putting the audience in a group: the second person
    costs one gpasswd, not a walk over everything in the folder."""
    name, doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    r = share(k, name, "people", [("bob", "edit"), ("carol", "edit")])
    assert r.status_code == 200, r.text
    j = r.json()
    # the contract: a membership change touches no files at all
    assert j["regrouped"] == 0 and j["acl_walked"] == 0, j
    # …and still takes effect immediately for the person added
    assert can_open("carol", doc)


def test_private_locks_out_but_stays_in_owner_search(k, folder):
    name, doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    assert can_open("bob", doc)
    assert share(k, name, "private").status_code == 200
    assert not can_open("bob", doc)
    assert can_open("alice", doc)
    # kbindexer keeps read, or the owner loses their own note from search
    out = subprocess.run(["getfacl", "-cpE", "--", f"/srv/kb/{name}"],
                         capture_output=True, text=True).stdout
    assert "user:kbindexer:r-x" in out, out


def test_everyone(k, folder):
    name, doc = folder
    assert share(k, name, "everyone").status_code == 200
    assert group_of(k, name) == "kb-users"
    assert can_open("carol", doc)


# --- overrides: a file may disagree with its folder --------------------------

def test_file_overrides_its_folder(k, folder):
    name, doc = folder
    assert share(k, name, "people", [("bob", "edit"), ("carol", "edit")]).status_code == 200
    other = f"{name}/other.md"
    assert k.post("/api/file", json={"path": other}).status_code == 200
    assert can_open("bob", doc) and can_open("bob", other)

    assert share(k, doc, "people", [("carol", "view")]).status_code == 200
    assert not can_open("bob", doc), "the file's own list wins over the folder's"
    assert can_open("carol", doc)
    assert can_open("bob", other), "…and the rest of the folder is untouched"


def test_inherited_is_reported_so_saving_changes_nothing(k, folder):
    name, doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    st = state(k, doc)
    assert st["inherited"] is True, "a file following its folder must say so"
    assert st["parent"] == name


# --- the property the fork rule protects -------------------------------------

def test_restricting_a_subfolder_does_not_rewrite_the_parent(k, folder):
    name, _doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    sub = f"{name}/sub"
    assert k.post("/api/fs/mkdir", json={"path": sub}).status_code == 200
    subdoc = f"{sub}/notes.md"
    assert k.post("/api/file", json={"path": subdoc}).status_code == 200

    r = share(k, sub, "people", [("carol", "edit")])
    assert r.status_code == 200, r.text
    assert r.json()["forked"] is True, "a subfolder must fork, never rewrite the parent"
    assert group_of(k, sub) != group_of(k, name)
    assert roles(state(k, name)).get("bob") == "edit", "parent's people list untouched"
    assert can_open("carol", subdoc) and not can_open("bob", subdoc)


def test_only_the_owner_can_change_access(k, folder):
    name, _doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    assert share(cl("bob"), name, "everyone").status_code == 403


def test_secrets_are_never_shared(k, folder):
    name, _doc = folder
    sec = f"{name}/_secrets"
    assert k.post("/api/fs/mkdir", json={"path": sec}).status_code == 200
    key = f"{sec}/token.md"
    assert k.post("/api/file", json={"path": key}).status_code == 200
    assert share(k, name, "people", [("bob", "edit"), ("carol", "view")]).status_code == 200
    assert not can_open("bob", key), "a share must not reach into _secrets/"
    assert not can_open("carol", key)
    assert share(k, key, "everyone").status_code == 400, "_secrets cannot be opened up at all"


def mode_of(c, path):
    return int(c.get("/fs/share", params={"path": path}).json()["advanced"]["mode"], 8)


def test_new_document_never_born_wider_than_its_folder(k, folder):
    """The New-document button used to create every file from a hardcoded 0644
    and only ever ADD bits, so a note in a team folder came out world-readable
    and one in a private folder came out 0644. Nothing may be born readable by
    people who cannot read the folder it lands in."""
    name, _doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    made = f"{name}/fresh.md"
    assert k.post("/fs/newfile", json={"path": made}).status_code == 200
    assert mode_of(k, made) & 0o004 == 0, "a new file must not be world-readable here"
    assert not can_open("carol", made), "someone outside the folder must not read it"
    assert can_open("bob", made), "…while the folder's people still can"

    # and the same for a folder nobody else can even enter
    assert share(k, name, "private").status_code == 200
    solo = f"{name}/solo.md"
    assert k.post("/fs/newfile", json={"path": solo}).status_code == 200
    assert state(k, solo)["scope"] == "private", "a new file in a private folder is owner-only"
    assert mode_of(k, solo) & 0o004 == 0, "…and certainly not world-readable"
    assert not can_open("bob", solo)


# --- moving something now re-homes it ----------------------------------------

def test_move_takes_the_destination_audience(k, folder):
    name, _doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    loose = f"{DIR}/loose_{int(time.time() * 1000) % 10 ** 6}.md"
    assert k.post("/api/file", json={"path": loose}).status_code == 200
    assert share(k, loose, "private").status_code == 200
    assert not can_open("bob", loose)

    dst = f"{name}/{os.path.basename(loose)}"
    r = k.post("/api/fs/rename", json={"src": loose, "dst": dst})
    assert r.status_code == 200, r.text
    assert r.json()["rehomed"] is True
    assert group_of(k, dst) == group_of(k, name), "a moved file joins the folder it landed in"
    assert can_open("bob", dst), "…and the folder's people can actually read it"


def test_move_can_keep_the_original_audience(k, folder):
    name, _doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    loose = f"{DIR}/keep_{int(time.time() * 1000) % 10 ** 6}.md"
    assert k.post("/api/file", json={"path": loose}).status_code == 200
    assert share(k, loose, "private").status_code == 200
    dst = f"{name}/{os.path.basename(loose)}"
    r = k.post("/api/fs/rename", json={"src": loose, "dst": dst, "audience": "keep"})
    assert r.status_code == 200, r.text
    assert r.json()["rehomed"] is False
    assert not can_open("bob", dst), "opting out must really opt out"


def test_move_preview_flags_the_change(k, folder):
    name, _doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    loose = f"{DIR}/prev_{int(time.time() * 1000) % 10 ** 6}.md"
    assert k.post("/api/file", json={"path": loose}).status_code == 200
    assert share(k, loose, "private").status_code == 200
    r = k.post("/api/fs/move-preview", json={"src": loose, "dst": f"{name}/x.md"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changes"] is True and j["mine"] is True
    assert j["from"]["scope"] == "private" and j["to"]["scope"] == "people"
    # a plain rename inside the same folder is not an audience change
    r2 = k.post("/api/fs/move-preview", json={"src": loose, "dst": f"{DIR}/renamed.md"})
    assert r2.json()["changes"] is False
    k.post("/api/fs/delete", json={"path": loose})


def test_rename_in_place_keeps_permissions(k, folder):
    """A rename is not a move: nothing about who can open it should change."""
    name, doc = folder
    assert share(k, name, "people", [("bob", "edit")]).status_code == 200
    dst = f"{name}/renamed.md"
    assert k.post("/api/fs/rename", json={"src": doc, "dst": dst}).status_code == 200
    assert can_open("bob", dst)
    assert group_of(k, dst) == group_of(k, name)
