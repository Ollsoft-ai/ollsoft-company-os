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


# --- chmod propagates to search visibility quickly -------------------------
def test_chmod_revokes_search_within_2s():
    path = "/srv/kb/company/overview.md"
    assert "company/overview.md" in search("carol", "quarterly")
    subprocess.run(["chmod", "600", path], check=True)   # alice owns it
    try:
        deadline = time.time() + 2.5
        gone = False
        while time.time() < deadline:
            if "company/overview.md" not in search("carol", "quarterly"):
                gone = True
                break
            time.sleep(0.3)
        assert gone, "chmod 600 must remove the file from carol's search < 2.5s"
        # alice (owner) still sees it.
        assert "company/overview.md" in search("alice", "quarterly")
    finally:
        subprocess.run(["chmod", "664", path], check=True)
        # let the index reconcile back
        for _ in range(10):
            if "company/overview.md" in search("carol", "quarterly"):
                break
            time.sleep(0.3)
