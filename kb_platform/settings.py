"""Settings: one registry, two flat JSON files, three endpoints.

A setting is one REGISTRY entry — key, type, default, and which layers may
set it. Values resolve per key, lowest to highest: the shipped default, the
company file (<REPO>/.os/settings.json, root:kb-users 0644, written by the hub
for an admin), the person's own file (users/<me>/.os/settings.json, 0600,
written by their backend as them — or by them, or an agent running as them,
with any editor). One value, from exactly one layer; the UI says which.

The files are flat maps: {"ui.theme": "deep-blue"}. Keys starting with "_"
are reserved for future metadata and ignored. A bad key is dropped alone and
reported; a malformed file yields empty values and an `error` string. Nothing
here ever blanks the rest, and nothing here raises on file content.

Adding a setting is one REGISTRY entry: the validation, the files, the API
and the dialog row follow from it. Dependency-light on purpose (stdlib only),
like common.py — the user backend and the hub both import this.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import common

SCOPES = ("company", "user")
FILE_NAME = "settings.json"

# type: bool | int (min, max) | enum (options, optional labels) | string (pattern, maxlen)
#       | image (a file in .os/, set only through its upload endpoint; the value
#         is the file's name, "" = the built-in default)
#       | map (an object of allowed keys -> values: `keys` is a dict key -> pattern,
#         or a tuple of keys sharing `pattern`; a layer replaces the map whole)
LOGO_FILES = ("logo.svg", "logo.png")
# The tokens a theme can be customised on, per person or company-wide, each
# with the pattern its value must match. Colours: the base ones the stylesheet
# derives everything else from (plain hex in style.css, so a picker can start
# from what the theme paints). Type and space: the dozen dimensions that make
# the feel. Names without the leading "--".
HEX = r"#[0-9a-f]{6}"
LEN = r"(\d{1,3}(\.\d{1,2})?|\.\d{1,2})(px|rem)"
FONT = r"[A-Za-z0-9 ,\"'._-]{1,120}"
THEME_TOKENS = {
    "bg": HEX, "chassis": HEX, "panel": HEX, "panel2": HEX, "border": HEX, "ink": HEX, "muted": HEX,
    "faint": HEX, "heading": HEX, "accent": HEX, "accent-deep": HEX, "ok": HEX, "warn": HEX,
    "danger": HEX, "code-bg": HEX, "code-ink": HEX, "term-bg": HEX, "term-fg": HEX,
    "blame-me": HEX, "blame-other": HEX, "blame-machine": HEX,
    "sans": FONT, "mono": FONT,
    "font-size": r"(1[0-9]|2[0-4])px", "editor-size": LEN,
    "editor-lh": r"[12](\.\d{1,2})?", "rich-lh": r"[12](\.\d{1,2})?",
    "content-x": LEN, "content-x-narrow": LEN, "content-y": LEN, "content-max": r"\d{3,6}px", "source-x": LEN,
    "row-y": LEN, "tab-y": LEN, "pad": LEN, "r": r"\d{1,2}px",
}
REGISTRY: list[dict] = [
    {"key": "brand.name", "type": "string", "pattern": r"\s*\S.*", "maxlen": 40, "default": "Company OS",
     "scopes": ("company",), "group": "Brand", "label": "Product name",
     "help": "Shown next to the logo in the app and on the sign-in page."},
    {"key": "brand.logo", "type": "image", "default": "",
     "scopes": ("company",), "group": "Brand", "label": "Logo",
     "help": "SVG or PNG, up to 512 KB. Replaces the mark in the app and on the sign-in page; "
             "the built-in Ollsoft mark when unset."},
    {"key": "ui.theme.custom", "type": "map", "keys": THEME_TOKENS, "default": {},
     "scopes": ("company", "user"), "group": "Appearance", "label": "Customize the theme",
     "help": "Override single colours, fonts and spacing of the chosen theme — the accent, the "
             "background, the base size, the column width… Your set replaces the company's; "
             "empty means the theme as shipped."},
    {"key": "ai.agent", "type": "enum",
     "options": ["claude", "codex", "gemini", "copilot", "grok", "qwen", "opencode", "hermes", "deepseek"],
     "labels": {"claude": "Claude Code", "codex": "Codex", "gemini": "Gemini CLI", "copilot": "Copilot CLI",
                "grok": "Grok Build", "qwen": "Qwen Code", "opencode": "OpenCode", "hermes": "Hermes",
                "deepseek": "DeepSeek Harness"},
     "default": "claude", "scopes": ("company", "user"), "group": "Agents", "label": "Agent for new chats",
     "help": "Which agent a new chat opens with. Every installed agent stays one click away in the "
             "chat's picker; signing in is per person (docs/agent-chat.md)."},
    {"key": "editor.blame", "type": "bool", "default": True,
     "scopes": ("company", "user"), "group": "Editor", "label": "Who wrote each line",
     "help": "A thin stripe beside every line: you, a colleague, or a machine (an agent, a script "
             "or a sync — anything that changed the file outside the editor). Click it for who, "
             "when and the change. Colours: blame-me, blame-other, blame-machine in the theme."},
    {"key": "ui.theme", "type": "enum", "options": ["deep-blue", "dark", "light"], "default": "deep-blue",
     "labels": {"deep-blue": "Deep blue", "dark": "Dark", "light": "Light"},
     "scopes": ("company", "user"), "group": "Appearance", "label": "Theme",
     "help": "Colours for the whole app, editor and terminal included. Deep blue is the house look; "
             "Dark and Light follow Notion's greys and paper."},
    # ---- semantic search (docs/semantic-search.md). Read by kb-embedd every
    # minute; every limit here is enforced BEFORE a paid call is made.
    {"key": "search.enabled", "type": "bool", "default": True,
     "scopes": ("company",), "group": "Search & AI", "label": "Semantic search",
     "help": "Embed documents and use meaning, not just words, in search. Off stops every paid "
             "call at once; full-text search keeps working."},
    {"key": "search.rerank.enabled", "type": "bool", "default": True,
     "scopes": ("company",), "group": "Search & AI", "label": "Rerank results",
     "help": "Reorder the best hits with the reranking model — only when you press Enter or stop "
             "typing, never per keystroke."},
    {"key": "search.embed.scope", "type": "enum", "options": ["all", "company+projects"],
     "default": "all", "labels": {"all": "Everything readable", "company+projects": "company/ and projects/ only"},
     "scopes": ("company",), "group": "Search & AI", "label": "What is embedded",
     "help": "Text sent to the embedding provider. Secrets are never sent. Narrowing this also "
             "removes vectors already made for what falls outside."},
    {"key": "search.embed.quiet_seconds", "type": "int", "min": 30, "max": 86400, "default": 120,
     "scopes": ("company",), "group": "Search & AI", "label": "Embed after (seconds of quiet)",
     "help": "A document is embedded only once nobody has changed it for this long — while you type "
             "nothing is sent. Only the sections whose text changed are re-embedded."},
    {"key": "search.budget.embed_day_usd", "type": "int", "min": 0, "max": 500, "default": 10,
     "scopes": ("company",), "group": "Search & AI", "label": "Embedding budget per day (USD)",
     "help": "Embedding stops for the rest of the UTC day when this is spent. 0 stops it now."},
    {"key": "search.budget.embed_month_usd", "type": "int", "min": 0, "max": 5000, "default": 100,
     "scopes": ("company",), "group": "Search & AI", "label": "Embedding budget per month (USD)",
     "help": "Embedding stops for the rest of the calendar month when this is spent."},
    {"key": "search.budget.rerank_day_usd", "type": "int", "min": 0, "max": 500, "default": 20,
     "scopes": ("company",), "group": "Search & AI", "label": "Rerank budget per day (USD)",
     "help": "Past it, search keeps working without the reranking step until the UTC day rolls."},
    {"key": "search.rerank.per_user_day", "type": "int", "min": 0, "max": 5000, "default": 200,
     "scopes": ("company",), "group": "Search & AI", "label": "Reranks per person per day",
     "help": "One person (or their agent in a loop) cannot spend the company's rerank budget alone."},
    {"key": "search.price.embed_per_1m_usd", "type": "string", "pattern": r"\d{1,4}(\.\d{1,6})?",
     "maxlen": 12, "default": "0.13",
     "scopes": ("company",), "group": "Search & AI", "label": "Embedding price (USD per 1M tokens)",
     "help": "Turns the tokens the provider reports into dollars for the ledger and the budgets. "
             "Set it to your contract price."},
    {"key": "search.price.rerank_per_1k_usd", "type": "string", "pattern": r"\d{1,4}(\.\d{1,6})?",
     "maxlen": 12, "default": "2.75",
     "scopes": ("company",), "group": "Search & AI", "label": "Rerank price (USD per 1,000 searches)",
     "help": "Turns the search units the provider reports into dollars for the ledger and the "
             "budgets. Set it to your contract price."},
]
BY_KEY = {e["key"]: e for e in REGISTRY}


def public_schema() -> list[dict]:
    """What the browser receives: every field, scopes (and keys) as lists."""
    return [dict(e, scopes=list(e["scopes"]),
                 **({"keys": (dict(e["keys"]) if isinstance(e["keys"], dict) else list(e["keys"]))}
                    if "keys" in e else {}))
            for e in REGISTRY]


def defaults() -> dict:
    return {e["key"]: e["default"] for e in REGISTRY}


def validate_value(key: str, value) -> tuple[object | None, str | None]:
    """(clean, None) or (None, why) — strict, one value against its entry."""
    e = BY_KEY.get(key)
    if e is None:
        return None, f"unknown setting: {key}"
    t = e["type"]
    if t == "bool":
        if type(value) is not bool:
            return None, f"{key}: expected true or false"
    elif t == "int":                       # `type is int`: bool is an int subclass
        if type(value) is not int:
            return None, f"{key}: expected a whole number"
        if not e["min"] <= value <= e["max"]:
            return None, f"{key}: must be between {e['min']} and {e['max']}"
    elif t == "enum":
        if value not in e["options"]:
            return None, f"{key}: must be one of {', '.join(e['options'])}"
    elif t == "image":
        if value != "" and value not in LOGO_FILES:
            return None, f"{key}: not a logo file"
    elif t == "map":
        if not isinstance(value, dict):
            return None, f"{key}: expected an object"
        pats = e["keys"] if isinstance(e["keys"], dict) else {k: e["pattern"] for k in e["keys"]}
        clean = {}
        for k, v in value.items():
            if k not in pats:
                return None, f"{key}: unknown entry {k}"
            if not isinstance(v, str):
                return None, f"{key}: {k} must be text"
            v = v.strip()
            if pats[k] == HEX:
                v = v.lower()
            if not re.fullmatch(pats[k], v):
                return None, f"{key}: {k} must match {pats[k]}"
            clean[k] = v
        return clean, None
    elif t == "string":
        maxlen = e.get("maxlen", 200)
        if (not isinstance(value, str) or len(value) > maxlen
                or any(ord(c) < 32 for c in value)):
            return None, f"{key}: must be one line of at most {maxlen} characters"
        if "pattern" in e and not re.fullmatch(e["pattern"], value):
            return None, f"{key}: not a valid value"
    else:                                   # a registry bug, not a caller error
        return None, f"{key}: unsupported type {t}"
    return value, None


def validate_change(data, scope: str) -> tuple[dict | None, str | None]:
    """A {"set": {...}, "unset": [...]} request against ONE layer. Strict: the
    first bad thing is the error and nothing is written. Every key must allow
    `scope` — a company-only key cannot be set by a user, and vice versa."""
    if scope not in SCOPES:
        return None, "bad scope"
    if not isinstance(data, dict):
        return None, "expected an object"
    sets, unsets = data.get("set", {}), data.get("unset", [])
    if not isinstance(sets, dict) or not isinstance(unsets, list):
        return None, "set must be an object and unset a list"
    out = {"set": {}, "unset": []}
    for k, v in sets.items():
        if k not in BY_KEY or scope not in BY_KEY[k]["scopes"]:
            return None, f"{k}: not a {scope} setting"
        if BY_KEY[k]["type"] == "image":
            return None, f"{k}: upload it in Settings (it is a file, not a value)"
        clean, err = validate_value(k, v)
        if err:
            return None, err
        out["set"][k] = clean
    for k in unsets:
        if not isinstance(k, str) or k not in BY_KEY or scope not in BY_KEY[k]["scopes"]:
            return None, f"{k}: not a {scope} setting"
        out["unset"].append(k)
    return out, None


def load_layer(path, scope: str) -> dict:
    """Tolerant: never raises. {"values": clean, "rejected": {key: why},
    "error": str | None}. A malformed FILE -> empty values + error; a bad KEY
    inside a good file -> dropped alone and listed in `rejected`."""
    path = Path(path)
    out = {"values": {}, "rejected": {}, "error": None}
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return out
    except (OSError, ValueError) as e:
        out["error"] = f"{path.name}: {e.__class__.__name__}: {str(e)[:120]}"
        return out
    if not isinstance(raw, dict):
        out["error"] = f"{path.name}: expected an object of \"key\": value"
        return out
    for k, v in raw.items():
        if not isinstance(k, str) or k.startswith("_"):
            continue                        # reserved for future metadata
        if k not in BY_KEY:
            out["rejected"][k] = "unknown setting"
        elif scope not in BY_KEY[k]["scopes"]:
            out["rejected"][k] = f"not a {scope} setting"
        else:
            clean, err = validate_value(k, v)
            if err:
                out["rejected"][k] = err
            else:
                out["values"][k] = clean
    return out


def resolve(company_values: dict, user_values: dict) -> tuple[dict, dict]:
    """Per key, lowest -> highest: default -> company -> user. Returns
    (effective, source) where source[key] is "default" | "company" | "user"."""
    eff, src = {}, {}
    for e in REGISTRY:
        k = e["key"]
        v, s = e["default"], "default"
        if "company" in e["scopes"] and k in company_values:
            v, s = company_values[k], "company"
        if "user" in e["scopes"] and k in user_values:
            v, s = user_values[k], "user"
        eff[k], src[k] = v, s
    return eff, src


def apply_change(current: dict, change: dict) -> dict:
    values = dict(current)
    values.update(change["set"])
    for k in change["unset"]:
        values.pop(k, None)
    return values


def dumps(values: dict) -> bytes:
    return (json.dumps(values, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def company_file() -> Path:
    return common.company_config(FILE_NAME)


def user_file(user: str) -> Path:
    return common.user_config(user, FILE_NAME)


def logo_rev() -> int:
    """A number that changes whenever the custom logo file does (its mtime),
    0 without one — the cache-buster for /brand/logo."""
    for name in LOGO_FILES:
        try:
            return int(common.company_config(name).stat().st_mtime)
        except OSError:
            continue
    return 0


def snapshot(user: str) -> dict:
    """The GET /api/settings body, computed AS `user` (the company file is
    group-readable; the user file is theirs)."""
    co = load_layer(company_file(), "company")
    us = load_layer(user_file(user), "user")
    eff, src = resolve(co["values"], us["values"])
    return {"schema": public_schema(), "defaults": defaults(),
            "company": {"values": co["values"], "rejected": co["rejected"], "error": co["error"]},
            "user": {"values": us["values"], "rejected": us["rejected"], "error": us["error"]},
            "effective": eff, "source": src, "logoRev": logo_rev()}
