"""Admin user & group management — privileged, sudo-group-only, run as root."""
import json
import time

import httpx
import pytest
from kbenv import BASE, CREDS, U

TU = f"tu{int(time.time()) % 100000}"      # unique temp username
TG = f"tg{int(time.time()) % 100000}"      # unique temp group


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def admin():
    return cl("alice")


# --- gating -----------------------------------------------------------------
def test_only_admins_reach_admin_api():
    assert cl("carol").get("/admin/list").status_code == 403
    assert cl("bob").get("/admin/list").status_code == 403
    assert admin().get("/admin/list").status_code == 200
    assert cl("carol").get("/admin/me").json()["admin"] is False
    assert admin().get("/admin/me").json()["admin"] is True


# --- full user + group lifecycle -------------------------------------------
def test_user_and_group_lifecycle():
    a = admin()
    try:
        r = a.post("/admin/users", json={"username": TU, "first": "Test", "last": "User",
                                         "email": f"{TU}@example.com", "password": "TempPassphrase01"})
        assert r.status_code == 200, r.text
        # shows up in the list with profile + kb-users membership
        users = {u["username"]: u for u in a.get("/admin/list").json()["users"]}
        assert TU in users and users[TU]["email"] == f"{TU}@example.com"
        assert "kb-users" in users[TU]["groups"]
        # the new user can actually log in (PAM) and gets a working backend identity
        nc = httpx.Client(base_url=BASE, timeout=15)
        assert nc.post("/login", data={"username": TU, "password": "TempPassphrase01"}).status_code == 200
        assert nc.get("/api/whoami").json()["user"] == TU

        # create a group and assign the user
        assert a.post("/admin/groups", json={"name": TG}).status_code == 200
        assert a.post("/admin/groups/member",
                      json={"group": TG, "username": TU, "action": "add"}).status_code == 200
        groups = {g["name"]: g for g in a.get("/admin/list").json()["groups"]}
        assert TG in groups and TU in groups[TG]["members"]
        # and remove them again
        assert a.post("/admin/groups/member",
                      json={"group": TG, "username": TU, "action": "remove"}).status_code == 200
        groups = {g["name"]: g for g in a.get("/admin/list").json()["groups"]}
        assert TU not in groups[TG]["members"]
        # and the group itself can be deleted again
        assert a.post("/admin/groups/delete", json={"name": TG}).status_code == 200
        assert TG not in {g["name"] for g in a.get("/admin/list").json()["groups"]}
    finally:
        a.post("/admin/users/delete", json={"username": TU})
        a.post("/admin/groups/delete", json={"name": TG})   # idempotent cleanup
    # fully removed from the listing
    assert TU not in {u["username"] for u in a.get("/admin/list").json()["users"]}


# --- guards -----------------------------------------------------------------
def test_guards_reject_dangerous_actions():
    a = admin()
    assert a.post("/admin/users/delete", json={"username": U("alice")}).status_code == 400  # self/protected
    assert a.post("/admin/users/delete", json={"username": "root"}).status_code == 400
    assert a.post("/admin/groups/member",
                  json={"group": "sudo", "username": U("carol"), "action": "add"}).status_code == 400
    assert a.post("/admin/groups/delete", json={"name": "kb-users"}).status_code == 400   # platform group
    assert a.post("/admin/groups/delete", json={"name": "sudo"}).status_code == 400       # system group
    assert cl("bob").post("/admin/groups/delete", json={"name": "proj-acme"}).status_code == 403
    # input validation
    assert a.post("/admin/users", json={"username": "Bad Name", "first": "a", "last": "b",
                                        "email": "x@y.z", "password": "TempPassphrase01"}).status_code == 400
    assert a.post("/admin/users", json={"username": "okname", "first": "a", "last": "b",
                                        "email": "not-an-email", "password": "TempPassphrase01"}).status_code == 400
    assert a.post("/admin/users", json={"username": "okname", "first": "a", "last": "b",
                                        "email": "x@y.z", "password": "Eleven-char"}).status_code == 400


def test_non_admin_cannot_create_user():
    assert cl("carol").post("/admin/users", json={"username": "evil", "first": "e", "last": "e",
                                                   "email": "e@e.e", "password": "TempPassphrase01"}).status_code == 403
