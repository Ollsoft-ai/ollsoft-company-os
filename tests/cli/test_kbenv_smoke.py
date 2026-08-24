"""Smoke: the namespace the conftest seeded is real, isolated, and reachable."""
import httpx
from kbenv import BASE, U, PW, doc, proj, home, full, NS, AREA


def test_namespace_is_fresh():
    assert NS and NS.startswith("p"), NS
    assert AREA == f"company/kbtest-{NS}"


def test_seeded_documents_exist_only_in_the_namespace():
    assert full(doc("overview.md")).exists()
    # The restricted project is 2770 and group-owned by the test project group,
    # which the person running pytest is deliberately NOT in — so we check it
    # through someone who is, not from this process.
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": PW("alice")})
    r = c.get("/api/file", params={"path": proj("plan.md")})
    assert r.status_code == 200, r.status_code
    assert "zebrafish" in r.text


def test_ephemeral_users_can_log_in():
    for who in ("alice", "bob", "carol"):
        c = httpx.Client(base_url=BASE, timeout=15)
        r = c.post("/login", data={"username": U(who), "password": PW(who)})
        assert r.status_code in (200, 302), (who, r.status_code)


def _blocks_matching(who, needle):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(who), "password": PW(who)})
    return c.post("/api/artifact/query", json={
        "sql": "SELECT file_path FROM kb.blocks WHERE text ILIKE %s",
        "params": [f"%{needle}%"]}).json().get("rows", [])


def test_carol_cannot_see_the_restricted_project():
    # Assert the POSITIVE case first. "carol sees nothing" is also true when
    # nothing is indexed at all, which would make this test pass while proving
    # nothing — the exact failure mode of a suite racing the indexer.
    assert _blocks_matching("alice", "zebrafish"), \
        "fixture not indexed yet — the isolation check below would be vacuous"
    assert _blocks_matching("carol", "zebrafish") == []
