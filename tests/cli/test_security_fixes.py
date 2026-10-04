"""Regression tests for the three confirmed findings from the adversarial review:
  1. RLS must honor ancestor-directory traversal (no leak of world-readable files
     inside a directory the caller can't traverse).
  2. kb.can_read must not be an arbitrary-user oracle.
  3. kb-toggle must only flip genuinely-indexed task lines.
"""
import json

import httpx
import pytest
from kbenv import BASE, CREDS, U, doc, proj



def q(user, sql, params=None):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c.post("/api/artifact/query", json={"sql": sql, "params": params or []}).json()


def toggle(user, path, line):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c.post("/api/tasks/toggle", json={"path": path, "line": line})


# --- Finding 2: ancestor-directory traversal is enforced -------------------
def test_world_readable_file_in_restricted_dir_is_hidden():
    # 'walrus' lives in projects/acme/leak.md (mode 0644, world-readable) but
    # inside the 2770 acme dir carol cannot traverse.
    kry = q("alice", "SELECT file_path FROM kb.blocks WHERE text ILIKE %s", ["%walrus%"])
    if not kry.get("rows"):
        pytest.skip("ancestor-traversal fixture (acme/leak.md) not present")
    # alice (acme member, can traverse) sees it; carol (cannot traverse) does not.
    carol = q("carol", "SELECT file_path FROM kb.blocks WHERE text ILIKE %s", ["%walrus%"])
    assert carol["rows"] == [], "world-readable file inside non-traversable dir leaked to carol"


def test_carol_sees_no_acme_rows_at_all():
    rows = q("carol", "SELECT file_path FROM kb.blocks WHERE file_path LIKE %s", [proj("%")])
    assert rows["rows"] == []


# --- Finding 3: the arbitrary-user oracle is gone --------------------------
def test_can_read_two_arg_oracle_removed():
    r = q("carol", "SELECT kb.can_read(%s, %s)", [proj("plan.md"), "alice"])
    assert "error" in r, "the 2-arg can_read oracle must not exist"
    # the single-arg form only reports the caller's own access
    own = q("carol", "SELECT kb.can_read(%s)", [doc("overview.md")])
    assert own["rows"][0][0] is True


# --- Finding 1: kb-toggle is scoped to real indexed tasks ------------------
def test_toggle_rejects_non_md():
    assert toggle("alice", doc("dashboards/randoms.html"), 1).status_code == 400


def test_toggle_rejects_index_excluded_config():
    # .agents is excluded from the index -> not a toggleable task
    assert toggle("alice", ".agents/skills/kb-orientation/SKILL.md", 1).status_code == 400


def test_toggle_rejects_non_task_line():
    # line 1 of overview.md is a heading, not a task
    assert toggle("alice", doc("overview.md"), 1).status_code == 400


def test_toggle_accepts_real_task_and_restores():
    r = toggle("alice", doc("onboarding.md"), 5)
    assert r.status_code == 200
    toggle("alice", doc("onboarding.md"), 5)  # restore
