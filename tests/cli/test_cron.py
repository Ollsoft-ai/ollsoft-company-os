"""The per-user cron API: every user has a personal crontab, managed through
/api/cron on their own backend — so every operation runs AS them (`crontab`
edits their own spool file; cron runs the jobs with their kernel identity).
No root, no shared state, nothing to over-grant."""
import json

import httpx
import pytest
from kbenv import BASE, CREDS, U


MARK = "kb-cron-test-marker"


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def listing(c):
    r = c.get("/api/cron")
    assert r.status_code == 200, r.text
    return r.json()


def marker_jobs(c):
    return [j for j in listing(c)["jobs"] if MARK in j["command"]]


def cleanup(c):
    while True:
        left = marker_jobs(c)
        if not left:
            return
        j = left[0]
        c.post("/api/cron/remove", json={"line": j["line"], "raw": j["raw"]})


@pytest.fixture(autouse=True)
def _cleanup_markers():
    yield
    for u in ("alice", "bob"):
        cleanup(cl(u))


def test_cron_available_to_every_user():
    for u in ("alice", "bob", "carol"):
        d = listing(cl(u))
        assert d["available"] is True, f"{u} must be able to use cron"
        assert d["user"] == U(u), "the crontab shown must belong to the caller"


def test_add_pause_resume_remove_lifecycle():
    c = cl("bob")
    r = c.post("/api/cron/add",
               json={"schedule": "*/9   * * *  *", "command": f"echo {MARK} >> /tmp/kb-cron-test.log"})
    assert r.status_code == 200, r.text
    jobs = [j for j in r.json()["jobs"] if MARK in j["command"]]
    assert len(jobs) == 1
    assert jobs[0]["schedule"] == "*/9 * * * *"       # whitespace normalized
    assert jobs[0]["paused"] is False

    j = jobs[0]
    r = c.post("/api/cron/toggle", json={"line": j["line"], "raw": j["raw"]})
    assert r.status_code == 200
    j = [x for x in r.json()["jobs"] if MARK in x["command"]][0]
    assert j["paused"] is True
    # a paused job is a comment line — cron will not run it
    assert r.json()["raw"].count("#kb:paused") == 1

    r = c.post("/api/cron/toggle", json={"line": j["line"], "raw": j["raw"]})
    j = [x for x in r.json()["jobs"] if MARK in x["command"]][0]
    assert j["paused"] is False

    r = c.post("/api/cron/remove", json={"line": j["line"], "raw": j["raw"]})
    assert r.status_code == 200
    assert not [x for x in r.json()["jobs"] if MARK in x["command"]]


def test_crontabs_are_per_user():
    bob, alice = cl("bob"), cl("alice")
    r = bob.post("/api/cron/add",
                  json={"schedule": "* * * * *", "command": f"echo {MARK}-isolation"})
    assert r.status_code == 200
    assert [j for j in listing(bob)["jobs"] if f"{MARK}-isolation" in j["command"]]
    assert not [j for j in listing(alice)["jobs"] if f"{MARK}-isolation" in j["command"]], \
        "alice must never see bob's crontab"


def test_schedule_validation():
    c = cl("alice")
    for bad in ("* * *", "60 24 * *", "*; rm -rf /", "`x` * * * *", "@nonsense", ""):
        r = c.post("/api/cron/add", json={"schedule": bad, "command": "true"})
        assert r.status_code == 400, f"schedule {bad!r} must be rejected"
    # crontab(1) itself is the second gate: syntactically shaped but invalid values
    r = c.post("/api/cron/add", json={"schedule": "99 99 * * *", "command": f"echo {MARK}"})
    assert r.status_code == 400, "crontab(1) must reject out-of-range fields"


def test_command_cannot_inject_extra_lines():
    c = cl("alice")
    r = c.post("/api/cron/add",
               json={"schedule": "* * * * *", "command": f"echo {MARK}\n* * * * * echo evil"})
    assert r.status_code == 400
    assert "evil" not in listing(c)["raw"]


def test_stale_line_edits_are_refused():
    c = cl("bob")
    r = c.post("/api/cron/add", json={"schedule": "* * * * *", "command": f"echo {MARK}-stale"})
    j = [x for x in r.json()["jobs"] if f"{MARK}-stale" in x["command"]][0]
    r = c.post("/api/cron/remove", json={"line": j["line"], "raw": "not what is there"})
    assert r.status_code == 409, "an edit against a changed crontab must be refused"
    r = c.post("/api/cron/remove", json={"line": 9999, "raw": j["raw"]})
    assert r.status_code == 400
    # the real line is still intact and removable
    r = c.post("/api/cron/remove", json={"line": j["line"], "raw": j["raw"]})
    assert r.status_code == 200
