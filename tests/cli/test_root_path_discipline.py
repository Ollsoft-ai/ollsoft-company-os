"""Guard tests: keep the next root-side path mutation from reopening the class.

Three privilege-escalation paths were found in one review, all the same shape —
a root or service process handing a user-influenced path STRING to something
that follows symlinks:

  * common.reset_audience_tree -> os.chmod  (share scope="inherit", local root)
  * hub.admin_create_user      -> install -d  (chowned a symlink's target)
  * kb-heartbeat.sh            -> psql -c   (filename became SQL as superuser)

The fixes are in. These tests exist so the FOURTH one fails in CI instead of in
an audit. They are deliberately textual: the point is to make a new unpinned
mutation impossible to add without someone consciously editing an allowlist.
"""
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "kb_platform"
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

# Modules that run as root or as a service account. user_server.py is excluded
# on purpose: it runs AS the user, so the kernel already bounds what it can do.
ROOT_MODULES = ["hub.py", "common.py", "syncd.py", "convert.py", "indexer.py"]

# Path-based chmod/chown that are NOT reachable from a user-controlled name.
# Adding to this list is a decision: state why the path cannot be attacker
# influenced. Anything else must go through common.open_pinned + os.fchmod.
ALLOWED = {
    # fixed module constants, root-owned locations
    "CRON_DENY", "PROFILES_FILE", "common.USER_SOCK_DIR", "DOC_STATE_DIR",
    "common.VC_SOCK", "common.SYNCD_SOCK", "common.ATTRIB_DIR",
    "common.REPO_ROOT", "git_dir",
    # names this process just created itself, in a directory it controls
    "tmpf", "tmpd", "tmp", "sock",
    # /run/kb/users/<u>: parent is root-owned 0755, not group-writable
    "udir",
    # common.mkdir_with_mode, immediately after its own successful os.mkdir,
    # and it runs as the USER, not as root
    "path",
}

_CALL = re.compile(r"\bos\.(chmod|chown|lchown)\(\s*([^,\)]+)")


def _sites():
    for mod in ROOT_MODULES:
        p = SRC / mod
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if "follow_symlinks=False" in line:
                continue          # explicitly non-following: already safe
            for fn, arg in _CALL.findall(line):
                yield mod, n, fn, arg.strip(), line.strip()


@pytest.mark.parametrize("mod", ROOT_MODULES)
def test_no_unpinned_path_mutation_in_root_modules(mod):
    bad = [(n, fn, arg, line) for m, n, fn, arg, line in _sites()
           if m == mod and arg not in ALLOWED]
    assert not bad, (
        f"{mod}: path-based os.{bad[0][1]} on an un-reviewed target "
        f"at line {bad[0][0]}:\n    {bad[0][3]}\n"
        "os.chmod follows symlinks and Linux has no lchmod. Open the target "
        "with common.open_pinned() and use os.fchmod/os.fchown, or add the "
        "expression to ALLOWED here with a reason it cannot be user-influenced."
    )


def test_reset_to_parent_audience_never_mutates_by_path():
    """The specific function the root escalation ran through."""
    body = (SRC / "common.py").read_text()
    fn = body.split("def reset_to_parent_audience(")[1].split("\ndef ")[0]
    assert "os.fchmod(" in fn, "the pinned chmod is gone"
    assert "open_pinned(" in fn, "the symlink guard is gone"
    assert "os.chmod(" not in fn, "a path-based chmod came back into this function"


def test_heartbeat_never_interpolates_a_path_into_sql():
    """$rel is a filename off a group-writable directory."""
    body = (SCRIPTS / "kb-heartbeat.sh").read_text()
    assert "path='$rel'" not in body, (
        "kb-heartbeat.sh interpolates a filename into SQL run as the postgres "
        "superuser. Bind it instead: psql -v rel=\"$rel\" <<<\"... path=:'rel'\""
    )
    assert ":'rel'" in body, "the bound form is gone"


def test_no_install_d_into_the_repo():
    """`install -d` resolves its path and follows symlinks; users/ is group-writable."""
    for mod in ROOT_MODULES:
        body = (SRC / mod).read_text()
        assert '"install", "-d"' not in body, (
            f"{mod}: `install -d` follows a pre-planted symlink. Use "
            "common.opendir_beneath + os.mkdir(dir_fd=...)."
        )


def test_proc_self_fd_arguments_are_passed_to_the_child():
    """`/proc/self/fd/N` names a descriptor in the CHILD's table.

    subprocess closes inherited fds by default, so handing that path to a
    helper without pass_fds fails ENOENT every single time. It did: the
    fd-pinned setfacl in convert.py shipped without pass_fds and left every
    sidecar it wrote owner-only, which the code then logged as "fail closed"
    rather than as the bug it was. Silent, and only visible in a journal line.
    """
    import re
    bad = []
    for mod in ROOT_MODULES:
        lines = (SRC / mod).read_text().splitlines()
        for i, line in enumerate(lines):
            if "/proc/self/fd/" not in line:
                continue
            # Comments are stripped first: an explanatory comment that merely
            # MENTIONS pass_fds must not satisfy this check. It did, on the
            # first draft of this very test.
            window = "\n".join(
                l.split("#", 1)[0] for l in lines[max(0, i - 8):i + 9])
            if "subprocess.run(" not in window and "Popen(" not in window:
                continue          # not handing it to a child process
            if "pass_fds" not in window:
                bad.append(f"{mod}:{i + 1}: {line.strip()}")
    assert not bad, (
        "a /proc/self/fd/N path is handed to a child without pass_fds, so it "
        "cannot resolve there:\n  " + "\n  ".join(bad)
    )
