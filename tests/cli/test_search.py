"""Search finds files by NAME, not just by what is written inside them.

The original report: a colleague searched for a file by its filename and got
nothing. Two reasons — the query only ever ran against block text, and half the
repo (artifacts, images, attachments) is not in the Postgres index at all. The
filename half now walks the repo AS the logged-in user, so the kernel is the
permission check and every file type is covered.
"""
import json
import time

import httpx
import pytest

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))


def client(user):
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/login", data={"username": user, "password": CREDS[user]})
    assert r.status_code == 200
    return c


def search(user, q):
    j = client(user).get("/api/search", params={"q": q}).json()
    assert j.get("db") is True, "index must be online"
    return j


def files(user, q):
    return [f["path"] for f in search(user, q)["files"]]


def contents(user, q):
    return {r["path"] for r in search(user, q)["results"]}


# --- the reported bug -------------------------------------------------------

def test_finds_a_document_by_its_filename():
    assert files("alice", "overview.md")[0] == "company/overview.md"


def test_finds_a_file_the_postgres_index_never_sees():
    """.html artifacts have no kb.blocks and no kb.files row — before this they
    were unfindable by any means except scrolling the tree."""
    assert "company/todos.html" in files("alice", "todos")
    assert "company/todos.html" in files("alice", "todos.html")


def test_partial_name_is_enough():
    assert "company/onboarding.md" in files("alice", "onbo")


def test_name_beats_a_content_mention():
    """A file called X ranks above a file that merely mentions X."""
    assert files("alice", "onboarding")[0] == "company/onboarding.md"


def test_words_in_any_order_across_the_path():
    assert "projects/acme/plan.md" in files("alice", "acme plan")


def test_folders_are_findable_too():
    assert "projects/acme" in files("alice", "acme")


def test_diacritics_are_folded():
    """`lekarska` must find `lékařská zpráva.png` — people type without accents."""
    c = client("alice")
    rel = "company/_files/kbtest_ěščřž.md"
    assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
    try:
        time.sleep(0.4)
        got = files("alice", "kbtest_escrz")
        assert rel in got, got
    finally:
        c.post("/api/fs/delete", json={"path": rel})


# --- content search still works, and got better -----------------------------

def test_content_search_is_unchanged():
    assert "projects/acme/plan.md" in contents("alice", "zebrafish")


def test_content_prefix_matching():
    """plainto_tsquery could not do this: a prefix of a real word matched nothing."""
    assert "company/overview.md" in contents("alice", "quarter")


def test_no_single_file_floods_the_results():
    rows = search("alice", "the")["results"]
    per_file = {}
    for r in rows:
        per_file[r["path"]] = per_file.get(r["path"], 0) + 1
    assert not [p for p, n in per_file.items() if n > 3], per_file


# --- the invariants that must not bend --------------------------------------

def test_secrets_are_not_searchable_by_name():
    """The secret viewer promises "never in git history, search, or the live-doc
    relay". Filename search must keep that literally true."""
    c = client("alice")
    rel = "company/_secrets/kbtest_findme.key"
    assert c.post("/fs/newfile", json={"path": rel}).status_code in (200, 409)
    try:
        time.sleep(0.4)
        assert files("alice", "kbtest_findme") == []
        assert files("alice", "findme.key") == []
    finally:
        c.post("/api/fs/delete", json={"path": rel})


def test_filenames_are_permission_scoped():
    """carol cannot traverse projects/acme, so its filenames must not leak."""
    assert "projects/acme/plan.md" in files("alice", "plan.md")
    assert [p for p in files("carol", "plan.md") if p.startswith("projects/acme")] == []


def test_private_dirs_stay_private():
    assert "users/alice/private.md" in files("alice", "private.md")
    assert [p for p in files("carol", "private") if p.startswith("users/alice")] == []


@pytest.mark.parametrize("q", [
    "'; DROP TABLE kb.files; --",
    "%", "_", "%%", "\\", "x':*&'y", "a & b | !c", "*:*", "((((", "'",
])
def test_hostile_queries_are_answered_not_crashed(q):
    r = client("alice").get("/api/search", params={"q": q})
    assert r.status_code == 200, r.text
    j = r.json()
    assert isinstance(j["results"], list) and isinstance(j["files"], list)
    # the index must still be there afterwards
    assert "company/overview.md" in files("alice", "overview.md")


def test_like_metacharacters_are_escaped():
    """'%' must mean a literal percent sign, not "match everything"."""
    assert files("alice", "%") == []


def test_empty_query_is_cheap_and_empty():
    j = client("alice").get("/api/search", params={"q": "  "}).json()
    assert j["results"] == [] and j["files"] == []
