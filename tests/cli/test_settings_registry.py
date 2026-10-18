"""kb_platform/settings.py without a server: the validators, the tolerant
loader and the per-key resolver. Pure unit tests, so CI runs them on every push."""
import json

import pytest

from kb_platform import settings as s

ENTRY = {"key": "t.int", "type": "int", "min": 1, "max": 5, "default": 3, "scopes": ("company", "user")}
STR = {"key": "t.str", "type": "string", "pattern": r"[a-z]{2,3}|", "maxlen": 3, "default": "", "scopes": ("user",)}
BOOL = {"key": "t.bool", "type": "bool", "default": False, "scopes": ("company",)}
ENUM = {"key": "t.enum", "type": "enum", "options": ["a", "b"], "default": "a", "scopes": ("company", "user")}
MAP = {"key": "t.map", "type": "map", "keys": ("accent", "bg"), "pattern": r"#[0-9a-f]{6}", "default": {},
       "scopes": ("company", "user")}
MAP2 = {"key": "t.map2", "type": "map", "keys": {"accent": r"#[0-9a-f]{6}", "size": r"\d{2}px"}, "default": {},
        "scopes": ("user",)}


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """A synthetic registry, so the tests do not depend on what ships."""
    reg = [ENTRY, STR, BOOL, ENUM, MAP, MAP2]
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
    ("t.map", {"accent": "#FF0000"}, True), ("t.map", {}, True), ("t.map", {"nope": "#ff0000"}, False),
    ("t.map", {"accent": "red"}, False), ("t.map", ["#ff0000"], False),
    ("t.map2", {"size": "18px"}, True), ("t.map2", {"size": "18"}, False), ("t.map2", {"accent": "#ABCDEF"}, True),
    ("nope", 1, False),
])
def test_validate_value(key, value, ok):
    clean, err = s.validate_value(key, value)
    assert (err is None) is ok, err
    if ok and not key.startswith("t.map"):
        assert clean == value
    if ok and key.startswith("t.map"):
        assert clean == {k: (v.lower() if v.startswith("#") else v) for k, v in value.items()}   # hex normalised


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
    eff, src = s.resolve({"t.int": 2, "t.bool": True, "t.map": {"bg": "#000000"}}, {"t.int": 5, "t.str": "cs"})
    assert eff == {"t.int": 5, "t.str": "cs", "t.bool": True, "t.enum": "a", "t.map": {"bg": "#000000"}, "t.map2": {}}
    assert src == {"t.int": "user", "t.str": "user", "t.bool": "company", "t.enum": "default", "t.map": "company", "t.map2": "default"}
    eff, src = s.resolve({"t.int": 2}, {})
    assert (eff["t.int"], src["t.int"]) == (2, "company")


def test_apply_change_and_dumps_round_trip():
    v = s.apply_change({"t.int": 2, "t.enum": "b"}, {"set": {"t.int": 4}, "unset": ["t.enum", "absent"]})
    assert v == {"t.int": 4}
    assert json.loads(s.dumps(v)) == v and s.dumps(v).endswith(b"\n")


def test_shortcut_ids_match_the_apps_tables():
    """keys.bindings may only name commands the app has, and must be able to
    name every editable one: BINDINGS (minus gototab, nine keys in one entry)
    and EDIT_BINDINGS in frontend/src/app.js."""
    import importlib
    import re
    from pathlib import Path
    live = importlib.reload(s)
    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "app.js").read_text()

    def ids(table):
        body = src[src.index(f"const {table} = ["):]
        body = body[:body.index("\n];") if table == "BINDINGS" else body.index("\n].map(")]
        return re.findall(r'\{ id: "([a-z0-9]+)"', body)
    assert set(live.APP_SHORTCUTS) == set(ids("BINDINGS")) - {"gototab"}
    assert set(live.EDIT_SHORTCUTS) == set(ids("EDIT_BINDINGS"))
    assert set(live.BY_KEY["keys.terminal"]["keys"]) == set(live.APP_SHORTCUTS)


@pytest.mark.parametrize("value,ok", [
    ("Mod+B", True), ("Mod+P, Mod+K", True), ("?, F1, Mod+Slash", True), ("", True),
    ("Ctrl+Shift+Backquote", True), ("Alt+BracketRight", True), ("F9", True),
    ("Mod+b", False), ("Mod+B,Mod+K", False), ("Mod+B; rm -rf", False),
    ("Mod+B, Mod+C, Mod+D, Mod+E, Mod+F", False),
])
def test_shortcut_values(value, ok):
    import importlib
    live = importlib.reload(s)
    clean, why = live.validate_value("keys.bindings", {"bold": value})
    assert (why is None) == ok, why
