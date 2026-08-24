"""The materialized visibility table (kb.visible_files) — the set RLS gates on.

Two halves:
  * pure-function tests of compute_visibility(), the Python mirror of
    kb.can_read() — every mode-class/ACL/traverse rule the SQL oracle encodes;
  * live parity: nothing in the materialized set that the oracle would deny
    (the over-grant direction is the security-critical one), and RLS keeps
    every user inside their own rows.

The live half runs the audit AS SEVERAL USERS, not just the runner. Both
kb.can_read and the visible_files_self policy key off session_user, so a psql
check only ever audits whoever launched the suite (krystof here, alice on CI) —
an over-grant affecting a narrow-visibility account like carol would pass
unnoticed. /api/artifact/query executes SQL as the logged-in viewer, so it
audits each user from their own session.

Self-contained: the unit half feeds synthetic rows; the live half only reads.
"""
import json
import subprocess
import time

import httpx

from kb_platform.indexer import compute_visibility
from kbenv import BASE, CREDS, U, doc

# Deliberately spans visibility profiles: alice/bob are in acme, carol is not.
AUDIT_USERS = ["alice", "bob", "carol"]


def _f(path, owner="alice", group="kb-users", mode=0o644,
       au=None, ag=None, xu=None, xg=None):
    return (path, owner, group, mode, au or [], ag or [], xu or [], xg or [])


GROUPS = [("alice", "kb-users"), ("bob", "kb-users"), ("bob", "acme")]


def vis(files, users=("alice", "bob", "carol")):
    return compute_visibility(files, GROUPS, users)


# NOTE: everything below feeds SYNTHETIC rows to compute_visibility and never
# touches the filesystem, so these paths are deliberately literal — running them
# through this run's namespace would leave a gap at the intermediate directory
# and every ancestor-traversal case would fail for the wrong reason.
# --- one mode class applies, and its denial is final ------------------------

def test_owner_class_denial_is_final():
    # 0044: owner bit 0 — alice (owner) must NOT be rescued by group/other bits.
    got = vis([_f("company", mode=0o755), _f("company/a.md", mode=0o044)])
    assert ("alice", "company/a.md") not in got
    assert ("bob", "company/a.md") in got      # group class, r bit set
    assert ("carol", "company/a.md") in got    # other class, r bit set


def test_group_class_denial_is_final():
    # 0604 root-ish shape: group bit 0 — a kb-users member is denied even
    # though `other` could read. THE historical RLS bug (see kb._has comment).
    got = vis([_f("company", mode=0o755), _f("company/a.md", owner="carol", mode=0o604)])
    assert ("bob", "company/a.md") not in got
    assert ("alice", "company/a.md") not in got
    assert ("carol", "company/a.md") in got    # owner


def test_other_class():
    got = vis([_f("p", mode=0o711), _f("p/a.md", owner="carol", group="acme", mode=0o640)])
    assert ("carol", "p/a.md") in got          # owner
    assert ("bob", "p/a.md") in got            # acme member -> group r
    assert ("alice", "p/a.md") not in got      # other class, o bits 0


# --- named ACLs -------------------------------------------------------------

def test_read_acl_user_and_group():
    files = [_f("p", mode=0o711),
             _f("p/a.md", owner="alice", mode=0o600, au=["carol"]),
             _f("p/b.md", owner="alice", mode=0o600, ag=["acme"])]
    got = vis(files)
    assert ("carol", "p/a.md") in got          # named-user READ grant
    assert ("bob", "p/b.md") in got            # named-group READ via membership
    assert ("carol", "p/b.md") not in got      # not in acme
    assert ("bob", "p/a.md") not in got


def test_traverse_acl_reaches_through_closed_dir():
    files = [_f("locked", owner="alice", mode=0o700, xu=["bob"], xg=["acme"]),
             _f("locked/a.md", owner="alice", mode=0o644)]
    got = vis(files)
    assert ("alice", "locked/a.md") in got
    assert ("bob", "locked/a.md") in got       # x via named-user AND group ACL
    assert ("carol", "locked/a.md") not in got # no traverse


# --- ancestor traversal -----------------------------------------------------

def test_every_ancestor_must_traverse():
    files = [_f("a", mode=0o755), _f("a/b", mode=0o700), _f("a/b/c.md", mode=0o644)]
    got = vis(files)
    assert ("alice", "a/b/c.md") in got        # owns the 0700 dir
    assert ("bob", "a/b/c.md") not in got      # blocked at a/b
    assert ("carol", "a/b/c.md") not in got


def test_missing_ancestor_row_denies():
    # can_read: NOT FOUND on any prefix -> false. Mirror exactly.
    got = vis([_f("ghost/a.md", mode=0o644)])
    assert got == set()


def test_sealed_file_is_invisible_to_everyone():
    # mode 0 + no ACLs is reconcile_perms' seal for unverifiable files: nobody,
    # the owner included, may see it through the index.
    got = vis([_f("company", mode=0o755), _f("company/a.md", mode=0)])
    assert not {p for p in got if p[1] == "company/a.md"}


def test_top_level_file_has_no_ancestor_check():
    got = vis([_f("readme.md", mode=0o644)])
    assert {("alice", "readme.md"), ("bob", "readme.md"), ("carol", "readme.md")} <= got


# --- live parity with the SQL oracle ---------------------------------------

def _psql(q):
    out = subprocess.run(["psql", "-d", "kb", "-tAc", q],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_materialized_set_is_populated():
    # Eventually consistent: the indexer populates within seconds of starting.
    deadline = time.time() + 30
    n = 0
    while time.time() < deadline:
        n = int(_psql("SELECT count(*) FROM kb.visible_files"))
        if n:
            break
        time.sleep(1)
    assert n > 0, "indexer must have materialized this user's visibility"


def test_no_pair_the_oracle_would_deny():
    """Over-grant is the security failure mode: every (me, path) pair in the
    materialized set must also pass the live-computed kb.can_read(). (The
    under-grant direction shows up as missing search results and is covered by
    the search/RLS suites.) Transient disagreement is possible mid-sweep, so
    poll to zero."""
    deadline = time.time() + 10
    bad = "?"
    while time.time() < deadline:
        bad = _psql("SELECT count(*) FROM kb.visible_files v WHERE NOT kb.can_read(v.path)")
        if bad == "0":
            return
        time.sleep(1)
    assert bad == "0", f"{bad} materialized pairs the can_read oracle denies"


def test_rls_confines_each_user_to_their_own_rows():
    assert _psql("SELECT count(*) FROM kb.visible_files WHERE usr <> session_user") == "0"


# --- the same audit, per user, through each user's OWN session --------------

def _as_user(user, sql):
    """Run SQL as `user` through the artifact bridge (peer auth as them)."""
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, f"login failed for {user}"
    r = c.post("/api/artifact/query", json={"sql": sql, "params": []})
    assert r.status_code == 200, f"{user}: {r.text[:200]}"
    return r.json()["rows"][0][0]


def test_every_user_sees_only_pairs_their_own_oracle_allows():
    """The over-grant audit, per user. Polls: a sweep may be mid-write."""
    for user in AUDIT_USERS:
        deadline = time.time() + 10
        bad = None
        while time.time() < deadline:
            bad = _as_user(
                user, "SELECT count(*) FROM kb.visible_files v WHERE NOT kb.can_read(v.path)")
            if bad == 0:
                break
            time.sleep(1)
        assert bad == 0, f"{user}: {bad} materialized pairs their can_read oracle denies"


def test_no_user_can_read_another_users_visibility_rows():
    for user in AUDIT_USERS:
        n = _as_user(user, "SELECT count(*) FROM kb.visible_files WHERE usr <> session_user")
        assert n == 0, f"{user} can see {n} visibility rows belonging to someone else"


def test_content_search_stays_permission_scoped_per_user():
    """The blocks policy itself, per user, independent of the /api/search
    ranking layer: no user may reach a block whose file is outside their
    materialized visibility."""
    for user in AUDIT_USERS:
        n = _as_user(user, "SELECT count(*) FROM kb.blocks b WHERE b.file_path NOT IN "
                           "(SELECT v.path FROM kb.visible_files v WHERE v.usr = session_user)")
        assert n == 0, f"{user} can read {n} blocks outside their visibility set"
