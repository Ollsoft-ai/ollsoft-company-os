"""Settings over the product APIs: one registry (kb_platform/settings.py), two
flat JSON files, three endpoints. A value resolves per key — shipped default,
then the company layer (.os/settings.json, admin-written via the hub), then the
person's own (users/<me>/.os/settings.json, written as them).

The company layer is REAL shared config: the module fixture snapshots it and
puts it back. Every test leaves the seeded users' own layers empty."""
import json
import os
from pathlib import Path

import httpx
import pytest
from kbenv import BASE, CREDS, U, full, home

KEY = "ui.theme"          # the one shipped setting; the assertions are about layering
VAL = "deep-blue"


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def _clear_users():
    for u in ("bob", "carol"):
        c = cl(u)
        own = c.get("/api/settings").json()["user"]["values"]
        if own:
            c.post("/api/settings", json={"unset": list(own)})


@pytest.fixture(scope="module", autouse=True)
def _restore_company_layer():
    a = cl("alice")
    before = a.get("/api/settings").json()["company"]["values"]
    yield
    now = a.get("/api/settings").json()["company"]["values"]
    if now:
        a.post("/admin/settings", json={"unset": list(now)})
    if before:
        a.post("/admin/settings", json={"set": before})
    _clear_users()


@pytest.fixture(autouse=True)
def _clean_slate():
    cl("alice").post("/admin/settings", json={"unset": [KEY]})
    _clear_users()
    yield


def test_shape_and_defaults():
    j = cl("bob").get("/api/settings").json()
    assert set(j) == {"schema", "defaults", "company", "user", "effective", "source", "logoRev"}
    keys = [e["key"] for e in j["schema"]]
    assert KEY in keys
    for k in keys:
        assert k in j["effective"] and k in j["source"] and k in j["defaults"]
    assert j["source"][KEY] == "default" and j["effective"][KEY] == j["defaults"][KEY]
    assert j["user"]["values"] == {} and j["user"]["error"] is None


def test_layering_order():
    a, b = cl("alice"), cl("bob")
    assert a.post("/admin/settings", json={"set": {KEY: VAL}}).status_code == 200
    assert b.get("/api/settings").json()["source"][KEY] == "company"
    r = b.post("/api/settings", json={"set": {KEY: VAL}})
    assert r.status_code == 200 and r.json()["source"][KEY] == "user"
    assert b.post("/api/settings", json={"unset": [KEY]}).json()["source"][KEY] == "company"
    assert a.post("/admin/settings", json={"unset": [KEY]}).status_code == 200
    assert b.get("/api/settings").json()["source"][KEY] == "default"


@pytest.mark.parametrize("body", [
    {"set": {"no.such": 1}},
    {"set": {KEY: "neon"}},
    {"set": [KEY]},
    {"unset": KEY},
    {"unset": ["no.such"]},
    ["not", "an", "object"],
])
def test_validation_is_a_400_on_both_layers(body):
    assert cl("bob").post("/api/settings", json=body).status_code == 400
    assert cl("alice").post("/admin/settings", json=body).status_code == 400
    assert cl("bob").post("/api/settings", content=b"not json",
                          headers={"content-type": "application/json"}).status_code == 400


def test_company_layer_is_admin_only():
    for u in ("bob", "carol"):
        assert cl(u).post("/admin/settings", json={"set": {KEY: VAL}}).status_code == 403
    assert cl("alice").post("/admin/settings", json={"set": {}}).status_code == 200


def test_own_layer_is_private():
    b, c = cl("bob"), cl("carol")
    assert b.post("/api/settings", json={"set": {KEY: VAL}}).status_code == 200
    assert c.get("/api/settings").json()["user"]["values"] == {}
    p = b.get("/fs/props", params={"path": home("bob", ".os/settings.json")}).json()
    assert p["mode"] == "600" and p["owner"] == U("bob")
    assert c.get("/fs/props", params={"path": home("bob", ".os/settings.json")}).status_code in (403, 404)
    if os.geteuid() != 0:                      # the runner is never bob
        with pytest.raises(PermissionError):
            os.stat(full(home("bob", ".os/settings.json")))


def test_agents_edit_the_file_and_bad_edits_are_reported_not_fatal():
    b = cl("bob")
    b.post("/api/settings", json={"set": {KEY: VAL}})            # makes .os/ exist
    path = home("bob", ".os/settings.json")

    def write(obj_or_text):
        body = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
        r = b.post("/api/artifact/write", json={"path": path, "content": body})
        assert r.status_code == 200, r.text
        return b.get("/api/settings").json()

    j = write({KEY: VAL, "_note": "reserved keys are ignored, not rejected"})
    assert j["source"][KEY] == "user" and j["user"]["rejected"] == {} and j["user"]["error"] is None
    j = write({KEY: "neon", "no.such": 1})
    assert j["user"]["values"] == {} and j["source"][KEY] == "default"
    assert set(j["user"]["rejected"]) == {KEY, "no.such"}
    j = write("{not json")
    assert j["user"]["values"] == {} and "JSONDecodeError" in j["user"]["error"]
    # the next save through the API rewrites the whole file: repaired
    j = b.post("/api/settings", json={"set": {KEY: VAL}}).json()
    assert j["user"]["error"] is None and j["source"][KEY] == "user"


def test_theme_overrides_replace_whole_not_merge():
    a, b = cl("alice"), cl("bob")
    K = "ui.theme.custom"
    try:
        assert a.post("/admin/settings", json={"set": {K: {"accent": "#FF0000", "bg": "#101010"}}}).status_code == 200
        j = b.get("/api/settings").json()
        assert j["effective"][K] == {"accent": "#ff0000", "bg": "#101010"} and j["source"][K] == "company"
        assert b.post("/api/settings", json={"set": {K: {"ink": "#ffffff"}}}).status_code == 200
        j = b.get("/api/settings").json()
        assert j["effective"][K] == {"ink": "#ffffff"}                 # the user's map replaces, no merge
        assert b.post("/api/settings", json={"set": {K: {"shadow": "#000000"}}}).status_code == 400   # not customisable
        assert b.post("/api/settings", json={"set": {K: {"accent": "blue"}}}).status_code == 400
        assert b.post("/api/settings", json={"unset": [K]}).status_code == 200
        assert b.get("/api/settings").json()["source"][K] == "company"
    finally:
        a.post("/admin/settings", json={"unset": [K]})
        b.post("/api/settings", json={"unset": [K]})


def test_docs_and_skill_name_every_setting():
    from kb_platform import settings as s
    root = Path(__file__).resolve().parents[2]
    docs = (root / "docs" / "settings.md").read_text()
    skill = (root / "company-skills" / "kb-settings" / "SKILL.md").read_text()
    for e in s.REGISTRY:
        assert f"`{e['key']}`" in docs, f"docs/settings.md does not list {e['key']}"
        assert f"`{e['key']}`" in skill, f"kb-settings skill does not list {e['key']}"
