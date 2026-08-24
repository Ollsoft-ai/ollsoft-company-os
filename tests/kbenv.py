"""Where this run's fixtures actually live.

The suite talks about people and documents by LOGICAL name — "alice",
"overview.md" — but a namespaced run puts them at kbt_<ns>_alice and
company/kbtest-<ns>/overview.md so that a test can never collide with, or
delete, real company content. This module is the only place that knows the
difference, so namespacing costs the tests one import instead of an edit each.

The mapping is written by scripts/seed-demo.sh; the root conftest.py seeds a
fresh namespace before collection and tears it down afterwards.

    from kbenv import BASE, U, PW, doc, proj

    c.post("/login", data={"username": U("alice"), "password": PW("alice")})
    c.get("/api/file", params={"path": doc("overview.md")})
"""
import json
import os
from pathlib import Path


def _creds_path() -> Path:
    if os.environ.get("KB_TEST_CREDS"):
        return Path(os.environ["KB_TEST_CREDS"])
    ns = os.environ.get("KB_TEST_NS", "")
    return Path(f"/tmp/kb-test-creds-{ns}.json" if ns else "/tmp/kb-test-creds.json")


CREDS_FILE = _creds_path()
try:
    _d = json.loads(CREDS_FILE.read_text())
except FileNotFoundError as e:                        # pragma: no cover
    raise RuntimeError(
        f"{CREDS_FILE} is missing. The suite seeds its own fixtures in "
        "conftest.pytest_configure; set KB_TEST_NO_SEED=1 only if you have "
        "seeded them yourself with scripts/seed-demo.sh."
    ) from e

# Back-compat: before namespacing, the seeder wrote a flat {"alice": "<pw>"}.
if "users" not in _d:
    _d = {"ns": "", "repo": "/srv/kb", "area": "company",
          "project": "projects/acme", "project_group": "proj-acme",
          "admin_group": "sudo",
          "users": {k: {"name": k, "password": v} for k, v in _d.items()}}

NS = _d["ns"]
REPO = Path(_d["repo"])
AREA = _d["area"]                    # this run's "company"
PROJECT = _d["project"]              # this run's restricted project
PROJECT_GROUP = _d["project_group"]
ADMIN_GROUP = _d["admin_group"]
BASE = f"http://127.0.0.1:{os.environ.get('KB_HUB_PORT', '8300')}"

_USERS = _d["users"]
LOGICAL = tuple(_USERS)              # ("alice", "bob", "carol")
_REAL_TO_LOGICAL = {v["name"]: k for k, v in _USERS.items()}


def U(logical: str) -> str:
    """Logical name -> the account that actually exists on this box.

    An unknown name passes through unchanged: tests that create their own
    account (the viewer tests mint one through /admin/users) already hold a real
    username, and they should be able to hand it to the same helper as the
    seeded ones rather than special-casing.
    """
    who = _USERS.get(logical)
    return who["name"] if who else logical


def PW(logical: str) -> str:
    return _USERS[logical]["password"]


def L(real: str) -> str:
    """The inverse of U(): a real account name -> the logical name the tests use.

    Needed whenever a username comes BACK from the API — a share panel lists
    kbt_<ns>_bob, and the assertion next to it says "bob". Unknown names pass
    through, so accounts a test minted itself compare as themselves.
    """
    return _REAL_TO_LOGICAL.get(real, real)


def people(pairs):
    """[("bob", "edit")] -> the API's [{"user": <real>, "role": "edit"}]."""
    return [{"user": U(u), "role": r} for u, r in pairs]


# Keyed by LOGICAL name, because that is what the suite already writes:
# `CREDS[user]` with user="alice" keeps working untouched. Real account names
# are accepted too, so a test that already resolved one still looks up.
CREDS = {k: v["password"] for k, v in _USERS.items()}
CREDS.update({v["name"]: v["password"] for v in _USERS.values()})


def doc(name: str) -> str:
    """A repo-relative path inside this run's shared company area."""
    return f"{AREA}/{name}"


def proj(name: str = "") -> str:
    """A repo-relative path inside this run's restricted project."""
    return f"{PROJECT}/{name}".rstrip("/")


def home(logical: str, name: str = "") -> str:
    """A repo-relative path inside a person's private area."""
    return f"users/{U(logical)}/{name}".rstrip("/")


def full(rel: str) -> Path:
    """Repo-relative -> absolute on disk."""
    return REPO / rel
