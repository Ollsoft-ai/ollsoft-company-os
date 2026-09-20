"""kb_platform/settings.py without a server: the validators, the tolerant
loader and the per-key resolver. Pure unit tests, so CI runs them on every push."""
import json

import pytest

from kb_platform import settings as s

ENTRY = {"key": "t.int", "type": "int", "min": 1, "max": 5, "default": 3, "scopes": ("company", "user")}
STR = {"key": "t.str", "type": "string", "pattern": r"[a-z]{2,3}|", "maxlen": 3, "default": "", "scopes": ("user",)}
BOOL = {"key": "t.bool", "type": "bool", "default": False, "scopes": ("company",)}
ENUM = {"key": "t.enum", "type": "enum", "options": ["a", "b"], "default": "a", "scopes": ("company", "user")}


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """A synthetic registry, so the tests do not depend on what ships."""
    reg = [ENTRY, STR, BOOL, ENUM]
    monkeypatch.setattr(s, "REGISTRY", reg)
    monkeypatch.setattr(s, "BY_KEY", {e["key"]: e for e in reg})


def test_every_shipped_entry_is_well_formed():
    import importlib
    live = importlib.reload(s)          # the real registry, not the fixture's
    try:
        for e in live.REGISTRY:
            assert set(e) >= {"key", "type", "default", "scopes", "group", "label"}, e["key"]
            assert set(e["scopes"]) <= set(live.SCOPES)
            assert live.validate_value(e["key"], e["default"])[1] is None, e["key"]
    finally:
        importlib.reload(s)


@pytest.mark.parametrize("key,value,ok", [
    ("t.int", 3, True), ("t.int", 0, False), ("t.int", 6, False), ("t.int", "3", False),
    ("t.int", True, False),                       # bool is an int in Python; not here
    ("t.bool", True, True), ("t.bool", 1, False),
    ("t.enum", "b", True), ("t.enum", "c", False),
    ("t.str", "cs", True), ("t.str", "", True), ("t.str", "czech", False), ("t.str", "a\nb", False),
    ("nope", 1, False),
])
def test_validate_value(key, value, ok):
    clean, err = s.validate_value(key, value)
    assert (err is None) is ok, err
    if ok:
        assert clean == value


def test_validate_change_respects_scope_and_shape():
    assert s.validate_change({"set": {"t.int": 2}, "unset": ["t.enum"]}, "user")[0] == \
        {"set": {"t.int": 2}, "unset": ["t.enum"]}
    assert "not a user setting" in s.validate_change({"set": {"t.bool": True}}, "user")[1]
    assert "not a company setting" in s.validate_change({"unset": ["t.str"]}, "company")[1]
    assert s.validate_change([], "user")[1] == "expected an object"
    assert s.validate_change({"set": []}, "user")[1].startswith("set must be")
    assert s.validate_change({"set": {"t.int": 9}}, "user")[1].startswith("t.int: must be between")
    assert s.validate_change({}, "nope")[1] == "bad scope"
    assert s.validate_change({}, "user")[0] == {"set": {}, "unset": []}


def test_load_layer_is_tolerant(tmp_path):
    p = tmp_path / "settings.json"
    assert s.load_layer(p, "user") == {"values": {}, "rejected": {}, "error": None}
    p.write_text(json.dumps({"t.int": 4, "t.enum": "zzz", "t.bool": True, "ghost": 1, "_note": "kept"}))
    out = s.load_layer(p, "user")
    assert out["values"] == {"t.int": 4}                       # the good key survives alone
    assert out["rejected"] == {"t.enum": "t.enum: must be one of a, b",
                               "t.bool": "not a user setting", "ghost": "unknown setting"}
    assert out["error"] is None                                # "_note" is reserved, not an error
    p.write_text("{not json")
    out = s.load_layer(p, "user")
    assert out["values"] == {} and "JSONDecodeError" in out["error"]
    p.write_text("[1, 2]")
    assert "expected an object" in s.load_layer(p, "user")["error"]


def test_resolve_takes_one_layer_per_key():
    eff, src = s.resolve({"t.int": 2, "t.bool": True}, {"t.int": 5, "t.str": "cs"})
    assert eff == {"t.int": 5, "t.str": "cs", "t.bool": True, "t.enum": "a"}
    assert src == {"t.int": "user", "t.str": "user", "t.bool": "company", "t.enum": "default"}
    eff, src = s.resolve({"t.int": 2}, {})
    assert (eff["t.int"], src["t.int"]) == (2, "company")


def test_apply_change_and_dumps_round_trip():
    v = s.apply_change({"t.int": 2, "t.enum": "b"}, {"set": {"t.int": 4}, "unset": ["t.enum", "absent"]})
    assert v == {"t.int": 4}
    assert json.loads(s.dumps(v)) == v and s.dumps(v).endswith(b"\n")
