"""P5 gate: the Postgres index enforces the SAME permissions as the filesystem,
in the engine, via RLS — tested through the real search path (each user's
backend connects to PG as that user over peer auth).
"""
import json
import subprocess
import time

import httpx
import pytest

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))


def search(user, q):
    c = httpx.Client(base_url=BASE, timeout=15)
    r = c.post("/login", data={"username": user, "password": CREDS[user]})
    assert r.status_code == 200
    res = c.get("/api/search", params={"q": q}).json()
    assert res.get("db") is True, "index must be online"
    return {row["path"] for row in res["results"]}


def search_has(user, q, path, timeout=6):
    """Search is eventually consistent with the filesystem (the indexer catches
    up within a couple of seconds; under a heavy suite it can lag briefly). Poll
    the POSITIVE expectation. Negative expectations stay a single immediate call
    — we never want a lagging index to hide a leak."""
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        seen = search(user, q)
        if path in seen:
            return seen
        time.sleep(0.5)
    return seen


# --- RLS filters confidential indexed content per user ---------------------
def test_acme_secret_visible_only_to_team():
    # zebrafish lives in the confidential acme plan, which IS indexed.
    assert "projects/acme/plan.md" in search_has("alice", "zebrafish", "projects/acme/plan.md")
    assert "projects/acme/plan.md" in search_has("bob", "zebrafish", "projects/acme/plan.md")
    assert search("carol", "zebrafish") == set(), "carol must not see acme via search"


def test_company_content_visible_to_all():
    for u in ("alice", "bob", "carol"):
        assert "company/overview.md" in search_has(u, "quarterly", "company/overview.md")


def test_rls_direct_psql_as_alice():
    # DB-level proof: alice (peer auth) can see acme rows. Poll — the indexer
    # is eventually consistent, and can lag briefly under a heavy suite.
    deadline = time.time() + 6
    n = 0
    while time.time() < deadline:
        out = subprocess.run(
            ["psql", "-d", "kb", "-tAc",
             "SELECT count(*) FROM kb.blocks WHERE text ILIKE '%zebrafish%'"],
            capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        n = int(out.stdout.strip())
        if n >= 1:
            break
        time.sleep(0.5)
    assert n >= 1


# --- privacy propagates to search visibility quickly ------------------------
def test_making_a_file_private_revokes_search_quickly():
    """Self-contained on purpose: it creates its own document and drives the
    permission change through the product's own visibility API (as the owner),
    so it does not care who owns company/overview.md on this box or which OS
    user runs the suite — both burned earlier incarnations of this test.

    Sealing used to take the OWNER's own search down with it (570eb4c): a 0600
    file is unreadable to the indexer, so its rows were dropped for everyone.
    (Only meaningful for a file the actor owns — see the creation call below.)
    Making a note private and losing it from your own search is not a privacy
    guarantee, it is a bug, so "private" now also leaves `u:kbindexer:r` behind.
    RLS is unaffected and is what this test pins: the CONTENT must disappear
    from every other user's search, and stay in the owner's."""
    token = f"pangovault{int(time.time())}"
    rel = f"company/kbtest_privacy_{token}.md"
    c = httpx.Client(base_url=BASE, timeout=15)
    r = c.post("/login", data={"username": "alice", "password": CREDS["alice"]})
    assert r.status_code == 200
    # /api/file, not /fs/newfile: the latter gives a new file the PARENT
    # folder's owner (root, under company/), and this test is about what the
    # OWNER keeps — it has to actually create one.
    assert c.post("/api/file", json={"path": rel}).status_code == 200
    assert c.post("/api/artifact/write",
                  json={"path": rel, "content": f"# note\n\nthe {token} ledger\n"}).status_code == 200
    try:
        # indexed and company-visible first (files in company/ are born team/company readable)
        assert rel in search_has("carol", token, rel, timeout=10), "baseline: carol must see it"

        assert c.post("/fs/props", json={"path": rel, "visibility": "private"}).status_code == 200
        deadline = time.time() + 4
        gone = False
        while time.time() < deadline:
            if rel not in search("carol", token):
                gone = True
                break
            time.sleep(0.3)
        assert gone, "going private must remove the file from carol's search within seconds"
        # …but the owner keeps their own file in their own search.
        assert rel in search_has("alice", token, rel, timeout=10), \
            "going private must not take the file out of the OWNER's search"

        # …and coming back is symmetric.
        assert c.post("/fs/props", json={"path": rel, "visibility": "company"}).status_code == 200
        assert rel in search_has("carol", token, rel, timeout=10), "restore must re-index"
    finally:
        c.post("/api/fs/delete", json={"path": rel})
