"""Stand the suite's fixtures up in a namespace of their own, then remove them.

Why this is a ROOT conftest and uses pytest_configure rather than a fixture:
about twenty modules under tests/cli do `json.load(open(...))` at IMPORT time,
so the credentials file has to exist before collection. Fixtures run far too
late for that; pytest_configure runs before collection, which is exactly the
window we need. Rewriting all of those modules to be lazy would be a bigger and
riskier change than owning the lifecycle here.

What a run creates and destroys:
    company/kbtest-<ns>/          documents, dashboards
    projects/kbtest-<ns>-acme/    the restricted project
    users/kbt_<ns>_{alice,bob,carol}/
    accounts kbt_<ns>_*, their Postgres roles and u_* schemas

Nothing shares a name with real content, so a test cannot reach it and the
teardown cannot delete it. Teardown is manifest-driven (scripts/seed-demo.sh):
it removes what this run recorded creating, and nothing else.

Escape hatches:
    KB_TEST_NS=<id>      use an existing namespace; do not seed or tear down
    KB_TEST_NO_SEED=1    fixtures are already in place, leave them alone
"""
import getpass
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
SEEDER = ROOT / "scripts" / "seed-demo.sh"
# so `import kbenv` works from tests/cli and tests/e2e alike
sys.path.insert(0, str(ROOT / "tests"))

_OWNED_NS = None          # set only if WE seeded it, so we only remove our own


def _sudo(*args: str) -> subprocess.CompletedProcess:
    # KB_TEST_USER decides who ends up owning the 0600 credentials file. It must
    # be whoever is running pytest, which is not necessarily SUDO_USER: CI seeds
    # with sudo as `runner` but runs the suite as a demo account.
    return subprocess.run(
        ["sudo", "-n", "env", f"KB_TEST_USER={getpass.getuser()}",
         "bash", str(SEEDER), *args],
        capture_output=True, text=True)


def _wait_for_index(area: str, timeout: float = 90.0) -> None:
    """Block until the seeded documents are searchable.

    The indexer is eventually consistent. Without this, whichever test happens
    to run first races it and fails in a way that looks like a permissions bug.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(
            ["psql", "-d", os.environ.get("KB_PG_DB", "kb"), "-tAc",
             f"SELECT count(*) FROM kb.blocks WHERE file_path LIKE '{area}/%'"],
            capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip().isdigit() and int(out.stdout) > 0:
            return
        time.sleep(1)
    print(f"\nWARNING: {area} did not reach the index within {timeout:.0f}s; "
          "index-dependent tests may fail.", file=sys.stderr)


def pytest_configure(config):
    global _OWNED_NS
    if os.environ.get("KB_TEST_NO_SEED") or os.environ.get("KB_TEST_NS"):
        return
    ns = "p" + secrets.token_hex(3)
    print(f"\nseeding test fixtures in namespace {ns} …", file=sys.stderr)
    r = _sudo("--namespace", ns)
    if r.returncode != 0:
        raise RuntimeError(
            f"seed-demo.sh --namespace {ns} failed ({r.returncode}).\n"
            f"{r.stdout[-2000:]}\n{r.stderr[-2000:]}\n"
            "The suite needs passwordless sudo to create its throwaway users; "
            "seed by hand and set KB_TEST_NS to run without it.")
    os.environ["KB_TEST_NS"] = ns
    _OWNED_NS = ns
    import kbenv                     # first import binds to the file we just wrote
    _wait_for_index(kbenv.AREA)


def pytest_unconfigure(config):
    if not _OWNED_NS:
        return
    print(f"\nremoving test fixtures ({_OWNED_NS}) …", file=sys.stderr)
    r = _sudo("--namespace", _OWNED_NS, "--undo")
    if r.returncode != 0:
        # Loud, because the leftovers are real accounts on a real box.
        print(f"WARNING: teardown of {_OWNED_NS} failed:\n{r.stderr[-2000:]}\n"
              f"Remove by hand: sudo bash {SEEDER} --namespace {_OWNED_NS} --undo",
              file=sys.stderr)
