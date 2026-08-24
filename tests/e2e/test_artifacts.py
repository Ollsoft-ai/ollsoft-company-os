"""The full artifacts scenario: an agent-written script feeds a per-user table,
a sandboxed dashboard artifact renders it live with edit/delete, shared with a
specific colleague — and everyone else is bounded by Postgres grants + file ACLs.

Self-contained on purpose: a real box's company/dashboards/randoms.html and
u_alice.readings have lived a life of their own (edited perms, renamed columns),
so this module builds its OWN copy of the whole scenario through the product
APIs as alice — a unique table, the stock dashboard pointed at it, a private
file ACL-shared with carol — and tears every bit of it down again.
"""
import time
from pathlib import Path

import httpx
import pytest
from conftest import BASE, CREDS, login
from kbenv import U, doc

TAG = str(int(time.time()))
DIR = doc(f"kbtest_art_{TAG}")
ART = f"{DIR}/dash.html"
TABLE = f"u_alice.kbtest_readings_{TAG}"


def q(user, sql, params=None):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c.post("/api/artifact/query", json={"sql": sql, "params": params or []}).json()


@pytest.fixture(scope="module", autouse=True)
def scenario():
    """Build the scenario exactly the way the demo seed tells the story, but
    under unique names so nothing pre-existing is touched or depended on."""
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    assert r.status_code == 200, r.text

    def sql(stmt, params=None):
        rr = c.post("/api/artifact/query", json={"sql": stmt, "params": params or []})
        assert rr.status_code == 200, rr.text
        return rr.json()

    # The per-user table + the sharing grants (SELECT/UPDATE/DELETE to carol,
    # nothing to bob). The schema USAGE grant is idempotent — the demo seed
    # already gives carol USAGE on u_alice — and is deliberately NOT revoked in
    # teardown, so a seeded box's own sharing story keeps working.
    sql(f"CREATE TABLE {TABLE} (id bigserial PRIMARY KEY, value int NOT NULL, "
        "at timestamptz DEFAULT now())")
    sql(f"INSERT INTO {TABLE} (value) SELECT (random()*100)::int FROM generate_series(1,20)")
    sql("GRANT USAGE ON SCHEMA u_alice TO carol")
    sql(f"GRANT SELECT, UPDATE, DELETE ON {TABLE} TO carol")

    # The dashboard: the stock randoms.html artifact, pointed at OUR table.
    src = Path(__file__).resolve().parents[2] / "defaults" / "artifacts" / "randoms.html"
    html = src.read_text().replace("u_alice.readings", TABLE)
    assert TABLE in html
    assert c.post("/api/fs/mkdir", json={"path": DIR}).status_code == 200
    assert c.post("/fs/newfile", json={"path": ART}).status_code in (200, 409)
    assert c.post("/api/artifact/write", json={"path": ART, "content": html}).status_code == 200
    # File side of the boundary: private + an explicit read ACL for carol only.
    r = c.post("/fs/props", json={"path": ART, "visibility": "private",
                                  "acl_add": [{"type": "user", "name": "carol", "perms": "r"}]})
    assert r.status_code == 200, r.text
    yield
    c.post("/api/fs/delete", json={"path": DIR})
    sql(f"DROP TABLE IF EXISTS {TABLE}")


def tree_paths(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    out, stack = [], list(c.get("/api/tree").json()["tree"])
    while stack:
        n = stack.pop()
        out.append(n["path"])
        stack.extend(n.get("children", []))
    return out


def _wait_db(sql, params, want, tries=15):
    for _ in range(tries):
        v = q("alice", sql, params)["rows"][0][0]
        if v == want:
            return True
        time.sleep(0.3)
    return False


def test_owner_sees_live_table_and_can_edit_and_delete(browser):
    # The table auto-refreshes every 2s, so we pin a SPECIFIC row by its id and
    # poll the DB — never rely on "the first row" staying put.
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click(f'.tree-item[data-path="{ART}"]')
    frame = page.frame_locator("iframe.artifact-frame")
    frame.locator("tbody tr").first.wait_for(timeout=15000)
    rid = int(frame.locator("tbody tr").first.get_attribute("data-id"))

    # Edit that specific row (UPDATE as alice, through the artifact's bridge).
    inp = frame.locator(f'tr[data-id="{rid}"] input.val')
    inp.fill("77777")
    inp.dispatch_event("change")
    assert _wait_db(f"SELECT value FROM {TABLE} WHERE id=%s", [rid], 77777), \
        "edit via artifact must persist (UPDATE as viewer)"

    # Delete that specific row (DELETE as alice).
    frame.locator(f'tr[data-id="{rid}"] button.del').click()
    assert _wait_db(f"SELECT count(*) FROM {TABLE} WHERE id=%s", [rid], 0), \
        "delete via artifact must remove the row"
    ctx.close()


def test_shared_colleague_carol_sees_data(browser):
    # carol has the file ACL AND the DB grant -> can open and query.
    assert ART in tree_paths("carol"), "carol should see the shared artifact"
    r = q("carol", f"SELECT count(*) FROM {TABLE}")
    assert "error" not in r, f"carol should read shared data, got {r}"
    assert r["rows"][0][0] >= 0

    # And carol was granted UPDATE too.
    ctx = browser.new_context()
    page = login(ctx, "carol")
    page.click(f'.tree-item[data-path="{ART}"]')
    frame = page.frame_locator("iframe.artifact-frame")
    frame.locator("tbody tr").first.wait_for(timeout=15000)
    ctx.close()


def test_uninvited_bob_is_blocked_two_ways():
    # 1. File ACL: bob can't even see the artifact in his tree...
    assert ART not in tree_paths("bob"), "bob must not see the un-shared artifact file"
    # 2. ...and even the raw query is denied by Postgres (no schema grant).
    r = q("bob", f"SELECT count(*) FROM {TABLE}")
    assert "error" in r, f"bob must be denied the data, got {r}"
    assert "permission denied" in r["error"].lower()


def test_query_runs_as_the_viewer():
    # The bridge proves identity: current_user is the viewer, not a shared role.
    assert q("alice", "SELECT current_user")["rows"][0][0] == "alice"
    assert q("carol", "SELECT current_user")["rows"][0][0] == "carol"
