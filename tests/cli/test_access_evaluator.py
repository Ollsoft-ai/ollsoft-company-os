"""common.can — the one access evaluator, against the kernel itself.

After the theme-3 collapse, hub._fs_can and syncd.fs_can are both one-line
adapters over common.can, so this single function decides every privileged read
and write in the product. It had no direct test.

The oracle here is the KERNEL, not another Python implementation. Checking an
access model against a second model written by the same author catches nothing:
both halves drift together and the parity test stays green. Every assertion
below compares common.can to what `runuser -u <user> -- test -r/-w/-x` actually
says, on a real file with a real ACL.

REPO_ROOT is monkeypatched, because can() refuses anything outside the repo
tree — that guard is itself asserted at the end.
"""
import getpass
import os
import pwd
import subprocess
from pathlib import Path

import pytest

from kb_platform import common

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 and subprocess.run(["sudo", "-n", "true"],
                                         capture_output=True).returncode != 0,
    reason="needs root or passwordless sudo to ask the kernel as another user",
)

OTHER = "nobody"


def _kernel_can(user: str, path, want: str) -> bool:
    """The truth: does the kernel let `user` do this?"""
    flag = {"r": "-r", "w": "-w", "x": "-x"}[want]
    cmd = ["runuser", "-u", user, "--", "/usr/bin/test", flag, str(path)]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    return subprocess.run(cmd, capture_output=True).returncode == 0


def _ids(user: str):
    e = pwd.getpwnam(user)
    return e.pw_uid, os.getgrouplist(user, e.pw_gid)


def _setfacl(path, spec):
    cmd = ["setfacl", "-m", spec, str(path)]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    assert subprocess.run(cmd, capture_output=True).returncode == 0, f"setfacl {spec} failed"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway tree standing in for REPO_ROOT, reachable by everyone."""
    root = tmp_path / "kb"
    root.mkdir()
    # Every ancestor up to /tmp must be traversable. can() stops walking at
    # REPO_ROOT because the real /srv/kb is reachable by everyone; pytest's
    # tmp dirs are 0700, so without this the kernel refuses at a level can()
    # never looks at and the two disagree for a reason that is pure fixture.
    p = root
    while str(p) != "/tmp" and str(p) != "/":
        os.chmod(p, 0o755)
        p = p.parent
    monkeypatch.setattr(common, "REPO_ROOT", root)
    return root


def _assert_agrees(path, user, want_bit, want_flag, note):
    uid, gids = _ids(user)
    ours = common.can(path, uid, gids, want_bit)
    theirs = _kernel_can(user, path, want_flag)
    assert ours == theirs, (
        f"{note}: common.can said {ours}, the kernel said {theirs} "
        f"for {user} on {path}"
    )


def test_world_readable_file(repo):
    f = repo / "public.md"
    f.write_text("x")
    os.chmod(f, 0o644)
    _assert_agrees(f, OTHER, 4, "r", "plain world-readable file")


def test_owner_only_file_is_refused(repo):
    f = repo / "private.md"
    f.write_text("x")
    os.chmod(f, 0o600)
    _assert_agrees(f, OTHER, 4, "r", "0600 file")


def test_named_acl_grant_is_honoured(repo):
    """Bare mode bits UNDER-grant: the share panel grants by named entry."""
    f = repo / "shared.md"
    f.write_text("x")
    os.chmod(f, 0o600)
    _setfacl(f, f"u:{OTHER}:r--")
    _assert_agrees(f, OTHER, 4, "r", "named-user ACL grant")


def test_the_mask_caps_a_named_grant(repo):
    """Bare mode bits OVER-grant: st_mode's group bits are the MASK."""
    f = repo / "masked.md"
    f.write_text("x")
    os.chmod(f, 0o600)
    _setfacl(f, f"u:{OTHER}:rw-")
    _setfacl(f, "m::r--")
    _assert_agrees(f, OTHER, 2, "w", "write capped by the ACL mask")
    _assert_agrees(f, OTHER, 4, "r", "read still allowed under the mask")


def test_a_world_readable_file_inside_an_unreachable_dir(repo):
    """The whole reason can() walks ancestors.

    hub._fs_can used to evaluate the inode alone and so authorised reads the
    kernel refuses — a 0644 file inside a 0700 folder. Verified on the live box
    before this landed: five accounts were being told they could read project
    files they could not open.
    """
    d = repo / "closed"
    d.mkdir()
    f = d / "leak.md"
    f.write_text("x")
    os.chmod(f, 0o644)
    os.chmod(d, 0o700)
    _assert_agrees(f, OTHER, 4, "r", "world-readable file in a non-traversable dir")


def test_traversal_restored_makes_it_readable_again(repo):
    d = repo / "openable"
    d.mkdir()
    f = d / "doc.md"
    f.write_text("x")
    os.chmod(f, 0o644)
    os.chmod(d, 0o755)
    _assert_agrees(f, OTHER, 4, "r", "same file once the parent is traversable")


def test_a_symlink_is_refused(repo):
    """stat_and_acl pins the inode; a symlink must never be evaluated."""
    f = repo / "real.md"
    f.write_text("x")
    os.chmod(f, 0o644)
    link = repo / "link.md"
    link.symlink_to(f)
    uid, gids = _ids(OTHER)
    assert common.can(link, uid, gids, 4) is False, "a symlink was evaluated"


def test_a_path_outside_the_repo_is_refused(repo, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    uid, gids = _ids(OTHER)
    assert common.can(outside, uid, gids, 4) is False, \
        "can() authorised a path outside REPO_ROOT"
