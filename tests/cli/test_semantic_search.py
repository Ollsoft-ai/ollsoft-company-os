"""Semantic search through the real doors: /api/search as each test account,
and kb-search. docs/semantic-search.md.

The permission tests hold whatever provider is configured — with none, search
is full-text and they still assert nothing leaks. The ones that need vectors
wait for this run's fixtures to be embedded (a file is only sent once it has
been quiet for `search.embed.quiet_seconds`) and skip when semantic search is
not set up. On CI the provider is `fake`: free, deterministic, words only —
so the paraphrase test runs only against a real model.

The pure fusion tests are in test_hybrid_fusion.py; they run everywhere.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from kbenv import BASE, CREDS, U, proj

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kb_platform import hybrid  # noqa: E402

PLAN = proj("plan.md")


def client(user):
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200
    return c


def search(c, q, final=False):
    j = c.get("/api/search", params={"q": q, **({"final": "1"} if final else {})}).json()
    assert j.get("db") is True, "index must be online"
    return j


def status(c):
    return c.get("/api/search/status").json()


def readable(c, path) -> bool:
    """The kernel's answer, asked as that user (their backend reads the file)."""
    return c.get("/api/file", params={"path": path}).status_code == 200


@pytest.fixture(scope="module")
def alice():
    return client("alice")


@pytest.fixture(scope="module")
def semantic(alice):
    """The provider in use, once this run's plan is embedded; else skip."""
    st = status(alice)
    if not st.get("available") or not st.get("configured") or not st.get("enabled", True):
        pytest.skip(f"semantic search not set up here ({st.get('why') or st.get('paused')})")
    deadline = time.time() + 360           # quiet period (≤ 2 min by default) + a few batches
    while time.time() < deadline:
        j = search(alice, "hospital integration spec")
        if any(r["path"] == PLAN and r.get("why") in ("meaning", "both") for r in j["results"]):
            return st.get("provider")
        time.sleep(5)
    pytest.fail("this run's project plan was never embedded — is kb-embedd paused? "
                + json.dumps({k: st.get(k) for k in ("paused", "reason", "pending")}))


# ---- nothing leaks, whatever the provider -----------------------------------------
QUERIES = ["hospital integration spec", "zebrafish protocol compliance",
           "medical centre software rollout", "private notes secret keyword", "onboarding first week"]


@pytest.mark.parametrize("user", ["alice", "bob", "carol"])
def test_every_hit_is_a_file_the_account_can_read(user):
    c = client(user)
    for q in QUERIES:
        for final in (False, True):
            for r in search(c, q, final=final)["results"]:
                assert readable(c, r["path"]), f"{user} got {r['path']} for {q!r} but cannot read it"


def test_carol_never_reaches_the_restricted_plan_by_meaning(semantic):
    c = client("carol")
    for q in QUERIES + ["confidential project milestones", "Codename zebrafish"]:
        for final in (False, True):
            paths = {r["path"] for r in search(c, q, final=final)["results"]}
            assert PLAN not in paths and not any(p.startswith(proj() + "/") for p in paths)


def test_bob_finds_the_plan_by_meaning_and_it_says_so(semantic):
    j = search(client("bob"), "hospital integration spec")
    assert j["semantic"] is True
    hit = next(r for r in j["results"] if r["path"] == PLAN)
    assert hit["why"] in ("meaning", "both") and hit["line"] >= 1


def test_a_paraphrase_with_no_shared_words_finds_it(semantic):
    if semantic == "fake":
        pytest.skip("the fake provider matches letters, not meaning")
    # none of these words is in the plan; "hospital integration spec" is
    j = search(client("bob"), "clinic software interoperability requirements", final=True)
    assert PLAN in {r["path"] for r in j["results"][:10]}, [r["path"] for r in j["results"][:10]]


# ---- the paid step is never per keystroke -------------------------------------------
def test_typing_never_reranks_and_a_pause_reranks_once(semantic):
    c = client("bob")
    before = status(c)["me"]["rerank"]
    q = "how this knowledgebase works"          # both seeded company pages answer it
    for i in range(2, len(q) + 1):
        j = search(c, q[:i])
        assert j["reranked"] is False
    assert status(c)["me"]["rerank"] == before, "a keystroke search reranked"
    j = search(c, q, final=True)
    st = status(c)
    if not (st.get("rerank") or {}).get("configured"):
        pytest.skip("no rerank provider configured")
    assert len(j["results"]) > 1, "a single result is never reranked; the query must find several"
    assert j["reranked"] is True
    assert st["me"]["rerank"] == before + 1


def test_status_is_for_members_and_shows_coverage(alice, semantic):
    st = status(alice)
    assert st["available"] is True and st["chunks"] > 0 and st["embedded"] > 0
    assert set(st["spend"]) == {"today", "month"}
    assert st["me"]["user"] == U("alice")


# ---- kb-search, the agents' door ---------------------------------------------------------
KB_SEARCH = shutil.which("kb-search") or "/usr/local/bin/kb-search"


@pytest.mark.skipif(not os.path.exists(KB_SEARCH), reason="kb-search is not installed here")
def test_kb_search_answers_as_the_caller_in_json():
    r = subprocess.run([KB_SEARCH, "--json", "--no-rerank", "--k", "5", "onboarding first week"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    j = json.loads(r.stdout)
    assert set(j) >= {"results", "semantic", "reranked", "notes"} and j["reranked"] is False
    assert len(j["results"]) <= 5
    for hit in j["results"]:
        # peer auth: the runner's own rights — the kernel agrees for every hit
        assert os.access(hybrid.common.REPO_ROOT / hit["path"], os.R_OK), hit["path"]


@pytest.mark.skipif(not os.path.exists(KB_SEARCH), reason="kb-search is not installed here")
def test_kb_search_status_reads_like_a_report():
    r = subprocess.run([KB_SEARCH, "--status"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and "unavailable" in r.stderr:
        pytest.skip("kb-embedd is not running here")
    assert r.returncode == 0, r.stderr
    assert "sections" in r.stdout and "spend today" in r.stdout
