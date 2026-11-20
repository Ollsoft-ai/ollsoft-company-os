"""Extra environment per chat agent (/admin/agent-env): admins only, names
shown and values never, code-loading names refused, one agent's change leaves
the rest alone. Runs against the deployed platform."""
import time

import httpx
from kbenv import BASE, CREDS, U

TAG = str(int(time.time()) % 10 ** 6)
NAME = f"COS_TEST_FLAG_{TAG}"
VALUE = f"value-{TAG}-never-shown"


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def test_only_admins_and_never_a_value():
    a, b = cl("alice"), cl("bob")
    assert b.get("/admin/agent-env").status_code == 403
    assert b.post("/admin/agent-env", json={"agent": "opencode", "set": {NAME: "1"}}).status_code == 403
    try:
        r = a.post("/admin/agent-env", json={"agent": "opencode", "set": {NAME: VALUE}})
        assert r.status_code == 200, r.text
        assert NAME in r.json()["agents"]["opencode"] and VALUE not in r.text
        g = a.get("/admin/agent-env")
        assert NAME in g.json()["agents"].get("opencode", []) and VALUE not in g.text, g.text
        # refused: what would run code as every person who opens a chat
        for bad in ("LD_PRELOAD", "NODE_OPTIONS", "PATH", "KB_ANYTHING", "1BAD"):
            r = a.post("/admin/agent-env", json={"agent": "opencode", "set": {bad: "x"}})
            assert r.status_code == 400, (bad, r.text)
        assert a.post("/admin/agent-env", json={"agent": "opencode", "set": {NAME: "a\nb"}}).status_code == 400
        assert a.post("/admin/agent-env", json={"agent": "nope", "set": {NAME: "1"}}).status_code == 400
    finally:
        r = a.post("/admin/agent-env", json={"agent": "opencode", "unset": [NAME]})
        assert r.status_code == 200, r.text
        assert NAME not in r.json()["agents"].get("opencode", [])
