"""Root-side mutations must never act through a symlink.

`os.chown` takes `follow_symlinks=False` and this codebase uses it
(common.py, hub.py). `os.chmod` has no such parameter and Linux has no
`lchmod`, so every unguarded chmod on a path a user can influence is a write
to whatever that path resolves to.

The live hole these tests were written for: POST /fs/share {scope:"inherit"}
reaches `common.reset_audience_tree`, which walked a user-owned folder and
chmod'd every entry by PATH. A symlink planted in that folder made the root
hub chmod the target — reproduced in a sandbox as 0600 -> 0666 on a file and
0700 -> 0777 on a directory before the fix.

These are pure unit tests: no server, no fixtures, no root. They must stay
that way so CI runs them on every push.
"""
import os
import stat

import pytest

from kb_platform import common


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture
def tree(tmp_path):
    """A victim file and dir, plus an attacker-owned folder holding symlinks
    to them. `outer` is wide open, which is what makes the inherited birth
    mode permissive enough to widen the targets."""
    victim_f = tmp_path / "victim" / "secret.txt"
    victim_d = tmp_path / "victim" / "dir"
    victim_d.mkdir(parents=True)
    victim_f.write_text("private")
    os.chmod(victim_f, 0o600)
    os.chmod(victim_d, 0o700)

    share = tmp_path / "outer" / "share"
    share.mkdir(parents=True)
    # Both must be wide: `outer` is what `share` inherits from, and `share` is
    # what its children inherit from. With a restrictive umask the birth mode
    # lands back on 0600 and the test would pass without proving anything.
    os.chmod(tmp_path / "outer", 0o777)
    os.chmod(share, 0o777)
    os.symlink(victim_f, share / "f_link")
    os.symlink(victim_d, share / "d_link")
    return tmp_path, share, victim_f, victim_d


def test_reset_to_parent_audience_refuses_a_symlinked_file(tree):
    _, share, victim_f, _ = tree
    common.reset_to_parent_audience(share / "f_link", False)
    assert _mode(victim_f) == 0o600, "root chmod'd through a symlink to a file"


def test_reset_to_parent_audience_refuses_a_symlinked_dir(tree):
    _, share, _, victim_d = tree
    common.reset_to_parent_audience(share / "d_link", True)
    assert _mode(victim_d) == 0o700, "root chmod'd through a symlink to a directory"


def test_reset_audience_tree_leaves_every_symlink_target_untouched(tree):
    """The whole scope="inherit" path, not just one entry."""
    _, share, victim_f, victim_d = tree
    common.reset_audience_tree(share)
    assert _mode(victim_f) == 0o600, "inherit widened a file through a symlink"
    assert _mode(victim_d) == 0o700, "inherit widened a directory through a symlink"


def test_reset_audience_tree_still_does_its_job_on_real_entries(tree):
    """The guard must not cost the feature: real children still inherit."""
    tmp_path, share, _, _ = tree
    real = share / "note.md"
    real.write_text("x")
    os.chmod(real, 0o600)
    os.chmod(share, 0o777)
    common.reset_audience_tree(share)
    assert _mode(real) & 0o060, "a real file stopped inheriting its folder's audience"


# --- the helper the fix is built on ----------------------------------------

def test_open_pinned_refuses_a_symlink(tree):
    _, share, _, _ = tree
    assert common.open_pinned(share / "f_link", False) is None
    assert common.open_pinned(share / "d_link", True) is None


def test_open_pinned_opens_real_paths(tree):
    tmp_path, share, _, _ = tree
    f = share / "real.md"
    f.write_text("x")
    for path, is_dir in ((f, False), (share, True)):
        fd = common.open_pinned(path, is_dir)
        assert fd is not None, f"refused a real path: {path}"
        try:
            assert os.fstat(fd).st_ino == os.stat(path).st_ino
        finally:
            os.close(fd)


def test_open_pinned_returns_none_for_a_missing_path(tmp_path):
    assert common.open_pinned(tmp_path / "nope", False) is None
