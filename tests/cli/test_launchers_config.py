"""Personal launcher buttons live in users/<me>/.os/launchers.json — 0600 in a
0700 directory that the backend creates on demand, AS the user. An install
from before 2026-09 kept them at users/<me>/.launchers.json; the first read
after the upgrade moves that file into place. Product APIs only, so this runs
against whatever backend is deployed."""
import json

import httpx
from kbenv import BASE, CREDS, U, home


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def test_legacy_personal_launchers_move_into_the_os_dir():
    c = cl("alice")
    new, legacy = home("alice", ".os/launchers.json"), home("alice", ".launchers.json")
    try:
        c.post("/api/fs/delete", json={"path": new})             # 404 is fine: absent
        w = c.post("/api/artifact/write", json={
            "path": legacy,
            "content": json.dumps({"buttons": [{"label": "legacy", "kind": "term", "target": "true"}]})})
        assert w.status_code == 200, w.text
        assert [b["label"] for b in c.get("/api/launchers").json()["mine"]] == ["legacy"]
        p = c.get("/fs/props", params={"path": new})
        assert p.status_code == 200, p.text
        assert p.json()["mode"] == "600" and p.json()["owner"] == U("alice")
        assert c.get("/fs/props", params={"path": legacy}).status_code == 404
        d = c.get("/fs/props", params={"path": home("alice", ".os")}).json()
        assert d["is_dir"] and d["mode"] == "700"
        # a write lands in the new place and the list round-trips
        r = c.post("/api/launchers", json={"buttons": [{"label": "new", "kind": "file", "target": "x.md"}]})
        assert r.status_code == 200, r.text
        assert [b["label"] for b in c.get("/api/launchers").json()["mine"]] == ["new"]
    finally:
        c.post("/api/launchers", json={"buttons": []})
        c.post("/api/fs/delete", json={"path": legacy})
