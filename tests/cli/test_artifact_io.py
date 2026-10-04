"""Artifact file tools via the bridge (kb-read / kb-write / kb-list / kb-mkdir /
kb-delete), all AS the viewer's OS user — the kernel is the boundary, exactly
like the terminal."""
import json

import httpx
from kbenv import BASE, CREDS, U, doc, full, home, proj



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
    # AGENTS.md and the platform's skills are root-owned 644 -> nobody but an
    # admin can write them (the repo root and .agents/skills are sticky)
    assert write(cl("alice"), "AGENTS.md", "hacked").status_code == 403
    assert write(cl("alice"), ".agents/skills/kb-orientation/SKILL.md", "hacked").status_code == 403


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


# ---- folder tools: list / mkdir / delete -----------------------------------
# Same authority (the viewer's OS user) and same containment (the artifact's own
# folder) as read/write — the scope is enforced server-side too, because these
# verbs create and destroy.

ART = doc("dashboards/iotest.html")     # a real artifact; its folder is the scope


def ls(c, path, depth=1, artifact=ART):
    return c.post("/api/artifact/list",
                  json={"artifact": artifact, "path": path, "depth": depth})


def mkdir(c, path, artifact=ART):
    return c.post("/api/artifact/mkdir", json={"artifact": artifact, "path": path})


def rm(c, path, recursive=False, artifact=ART):
    return c.post("/api/artifact/delete",
                  json={"artifact": artifact, "path": path, "recursive": recursive})


def test_list_shows_the_artifacts_own_folder():
    k = cl("alice")
    assert write(k, doc("dashboards/list_probe.md"), "probe\n").status_code == 200
    try:
        r = ls(k, doc("dashboards"))
        assert r.status_code == 200, r.text
        names = {e["name"] for e in r.json()["entries"]}
        assert "iotest.html" in names and "list_probe.md" in names
        probe = next(e for e in r.json()["entries"] if e["name"] == "list_probe.md")
        assert probe["dir"] is False and probe["size"] == 6 and probe["write"] is True
    finally:
        rm(k, doc("dashboards/list_probe.md"))


def test_mkdir_creates_missing_parents_and_list_recurses():
    k = cl("alice")
    try:
        assert mkdir(k, doc("dashboards/probe_dir/nested")).status_code == 200
        assert full(doc("dashboards/probe_dir/nested")).is_dir()
        assert write(k, doc("dashboards/probe_dir/nested/n.md"), "deep\n").status_code == 200
        shallow = {e["path"] for e in ls(k, doc("dashboards")).json()["entries"]}
        assert doc("dashboards/probe_dir/nested/n.md") not in shallow   # depth 1 stops
        deep = {e["path"] for e in ls(k, doc("dashboards"), depth=3).json()["entries"]}
        assert doc("dashboards/probe_dir/nested/n.md") in deep
        assert mkdir(k, doc("dashboards/probe_dir")).status_code == 409   # already there
    finally:
        rm(k, doc("dashboards/probe_dir"), recursive=True)


def test_delete_needs_recursive_for_a_full_folder():
    k = cl("alice")
    try:
        assert mkdir(k, doc("dashboards/probe_rm")).status_code == 200
        assert write(k, doc("dashboards/probe_rm/x.md"), "x").status_code == 200
        assert rm(k, doc("dashboards/probe_rm")).status_code == 400      # not empty
        assert full(doc("dashboards/probe_rm/x.md")).exists()
        assert rm(k, doc("dashboards/probe_rm/x.md")).status_code == 200
        assert rm(k, doc("dashboards/probe_rm")).status_code == 200      # empty now
        assert not full(doc("dashboards/probe_rm")).exists()
    finally:
        rm(k, doc("dashboards/probe_rm"), recursive=True)


def test_list_never_shows_the_secret_store():
    # todos.html sits in the area root, next to _secrets/ — an artifact there
    # must not be able to enumerate it, nor list inside it.
    k = cl("alice")
    area_art = doc("todos.html")
    entries = ls(k, doc("").rstrip("/"), depth=3, artifact=area_art).json()["entries"]
    assert entries and not any("_secrets" in e["path"] for e in entries)
    assert ls(k, doc("_secrets"), artifact=area_art).status_code == 403


def test_folder_tools_stay_inside_the_artifacts_folder():
    k = cl("alice")
    outside = home("alice", "")
    for r in (ls(k, outside), mkdir(k, outside + "/evil"), rm(k, home("alice", "private.md"))):
        assert r.status_code == 403 and "outside" in r.json()["error"], r.text
    # ...and alice's own file is still there (read it as her — the runner is
    # deliberately not in these groups, so os.stat here would say nothing)
    assert read(k, home("alice", "private.md")).status_code == 200


def test_an_artifact_cannot_delete_itself_or_its_folder():
    k = cl("alice")
    assert rm(k, doc("dashboards"), recursive=True).status_code == 400
    assert rm(k, ART).status_code == 400
    assert full(ART).exists() and full(doc("dashboards")).is_dir()


def test_delete_is_bounded_by_kernel_permissions():
    # carol is not on the acme team: an artifact "in" that folder buys her nothing
    assert rm(cl("carol"), proj("plan.md"), artifact=proj("ghost.html")).status_code == 403
    assert read(cl("alice"), proj("plan.md")).status_code == 200   # still there, for the team
