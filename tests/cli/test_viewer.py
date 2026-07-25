"""Viewer accounts: full webapp, zero execution. The login shell in /etc/passwd
is the switch (nologin), /etc/cron.deny closes the cron door (cron ignores
login shells), and the per-user backend refuses /pty and cron. File editing
keeps working — that is governed by groups/ACLs, not the shell."""
import asyncio
import json
import pwd
import time

import aiohttp
import httpx
import pytest

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))
VU = f"vw{int(time.time()) % 100000}"
PW = "ViewPass01"
NOLOGIN = "/usr/sbin/nologin"


def cl(user, password=None):
    c = httpx.Client(base_url=BASE, timeout=25)
    r = c.post("/login", data={"username": user, "password": password or CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def pty_gives_shell(client) -> bool:
    """True iff /pty hands out a LIVE shell for this session (pty bytes
    arrive). A plain HTTP status can't prove this — the hub bridges the
    websocket, so only a real ws handshake exercises the backend's gate.
    Persistent sessions send TEXT control frames ({"reset"}…) before the
    first pty BINARY bytes — skip those; only real shell output counts."""
    cookies = dict(client.cookies)

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            try:
                async with s.ws_connect(BASE + "/pty") as ws:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        msg = await asyncio.wait_for(
                            ws.receive(), max(0.05, deadline - time.monotonic()))
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            return True
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            return False
                    return False
            except Exception:
                return False
    return asyncio.run(go())


def cron_deny():
    try:
        return open("/etc/cron.deny").read().split()
    except OSError:
        return []


@pytest.fixture(scope="module")
def viewer():
    a = cl("alice")
    r = a.post("/admin/users", json={"username": VU, "first": "View", "last": "Only",
                                     "email": f"{VU}@example.com", "password": PW,
                                     "kind": "viewer"})
    assert r.status_code == 200, r.text
    yield VU
    a.post("/admin/users/delete", json={"username": VU})   # idempotent teardown


def test_viewer_is_nologin_and_cron_denied_at_os_level(viewer):
    assert pwd.getpwnam(viewer).pw_shell == NOLOGIN
    assert viewer in cron_deny()


def test_viewer_logs_into_webapp_but_cannot_exec(viewer):
    v = cl(viewer, PW)                       # PAM web login works without a shell
    who = v.get("/api/whoami").json()
    assert who["user"] == viewer and who["shell"] is False
    assert not pty_gives_shell(v), "a viewer must never receive a live pty"
    assert v.get("/api/cron").json()["available"] is False
    assert v.post("/api/cron/add",
                  json={"schedule": "* * * * *", "command": "id"}).status_code == 403


def test_viewer_can_still_edit_files(viewer):
    v = cl(viewer, PW)
    path = f"company/vwdoc_{VU}.md"
    assert v.post("/fs/newfile", json={"path": path}).status_code == 200
    assert v.post("/api/fs/delete", json={"path": path}).status_code == 200


def test_admin_list_and_principals_show_viewers(viewer):
    a = cl("alice")
    users = {u["username"]: u for u in a.get("/admin/list").json()["users"]}
    assert users[viewer]["shell"] is False
    assert viewer in a.get("/api/principals").json()["users"]   # shareable-with


def test_toggle_between_viewer_and_full(viewer):
    a = cl("alice")
    assert a.post("/admin/users/shell", json={"username": viewer, "shell": True}).status_code == 200
    assert pwd.getpwnam(viewer).pw_shell == "/bin/bash"
    assert viewer not in cron_deny()
    v = cl(viewer, PW)
    assert v.get("/api/whoami").json()["shell"] is True    # backend was recycled
    assert pty_gives_shell(v), "a full account gets a live shell"
    assert a.post("/admin/users/shell", json={"username": viewer, "shell": False}).status_code == 200
    assert pwd.getpwnam(viewer).pw_shell == NOLOGIN
    assert viewer in cron_deny()


def test_shell_toggle_guards(viewer):
    a = cl("alice")
    assert a.post("/admin/users/shell", json={"username": "alice", "shell": False}).status_code == 400
    assert a.post("/admin/users/shell", json={"username": "root", "shell": True}).status_code == 400
    assert cl("bob").post("/admin/users/shell",
                           json={"username": "carol", "shell": False}).status_code == 403


def test_delete_clears_cron_deny(viewer):
    a = cl("alice")
    assert a.post("/admin/users/delete", json={"username": viewer}).status_code == 200
    assert viewer not in cron_deny()
