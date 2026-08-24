"""Sharing a file with someone who can't traverse its folder is no longer a dead
grant: fs_props auto-grants traverse-only (x) on the ancestors, so the share
actually works — surgically (reach the one file, can't list the folder)."""
import json
import time

import httpx
from kbenv import BASE, CREDS, U, proj

PATH = proj("plan.md")   # carol can't traverse projects/acme (2770)


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def zebra(c):
    return c.post("/api/artifact/query", json={
        "sql": "SELECT count(*) FROM kb.blocks WHERE text ILIKE %s", "params": ["%zebrafish%"]
    }).json()["rows"][0][0]


def test_share_grants_ancestor_traverse_surgically():
    k, i = cl("alice"), cl("carol")
    # baseline: carol cannot reach the file (the dead-grant scenario)
    assert i.get("/api/file", params={"path": PATH}).status_code == 403
    assert zebra(i) == 0

    r = k.post("/fs/props", json={"path": PATH,
                                  "acl_add": [{"type": "user", "name": U("carol"), "perms": "r"}]})
    try:
        assert r.status_code == 200, r.text
        assert proj() in r.json()["granted_traverse"], "must grant traverse on ancestors"

        # carol can now READ the shared file (kernel allows immediately)
        assert i.get("/api/file", params={"path": PATH}).status_code == 200

        # ...and it becomes visible in search once the index reconciles
        found = False
        for _ in range(12):
            if zebra(i) >= 1:
                found = True
                break
            time.sleep(0.4)
        assert found, "shared file must become searchable for the grantee"

        # ...but the share is SURGICAL: carol still can't reach anything else in
        # the folder (traverse-only, no listing / no other file)
        assert i.get("/api/file", params={"path": proj("_files")}).status_code == 403
        assert proj() not in {n["path"] for n in _flat(i.get("/api/tree").json()["tree"])}
    finally:
        # undo the file share AND the ancestor traverse it auto-granted
        for pth in (PATH, proj()):
            k.post("/fs/props", json={"path": pth, "acl_remove": [{"type": "user", "name": U("carol")}]})

    # un-share removes access again
    assert i.get("/api/file", params={"path": PATH}).status_code == 403
    for _ in range(10):
        if zebra(i) == 0:
            break
        time.sleep(0.4)
    assert zebra(i) == 0, "un-share must revoke search visibility"


def _flat(nodes, acc=None):
    acc = acc if acc is not None else []
    for n in nodes:
        acc.append(n)
        _flat(n.get("children", []), acc)
    return acc
