"""A `_private` folder is born owner-only, wherever it is made.

Pure: a scratch folder under pytest's tmp_path carries a default ACL (the way a
team folder does), and the platform's own helpers create, move and share inside
it. No accounts, no hub. Skips where the filesystem has no POSIX ACLs."""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kb_platform import common  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("setfacl") is None, reason="needs setfacl")


@pytest.fixture
def team(tmp_path):
    """A folder shared the way a team folder is: setgid, group-readable, and a
    default ACL that hands a named user every new child."""
    d = tmp_path / "team"
    d.mkdir()
    os.chmod(d, 0o2775)
    r = subprocess.run(["setfacl", "-m", "u:nobody:rx", "-d", "-m", "u:nobody:rx", str(d)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("no POSIX ACLs here: " + r.stderr.strip())
    return d


def has_acl(p) -> bool:
    for attr in (common.ACL_XATTR, common.ACL_DEFAULT_XATTR):
        try:
            os.getxattr(p, attr)
            return True
        except OSError:
            pass
    return False


def mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_a_private_folder_is_born_owner_only_with_no_inherited_grants(team):
    common.mkdir_with_mode(team / "_private")
    assert mode(team / "_private") == 0o700
    assert not has_acl(team / "_private"), "the team folder's default ACL did not come along"
    # an ordinary sibling still takes the team's audience, as before
    common.mkdir_with_mode(team / "notes")
    assert has_acl(team / "notes")


def test_what_is_made_inside_a_private_folder_stays_private(team):
    common.mkdir_with_mode(team / "_private")
    common.mkdir_with_mode(team / "_private" / "sub")
    common.create_with_mode(team / "_private" / "note.md", exclusive=True)
    assert mode(team / "_private" / "sub") & 0o077 == 0
    assert mode(team / "_private" / "note.md") & 0o077 == 0
    assert not has_acl(team / "_private" / "note.md")


def test_a_new_document_on_a_path_through_a_missing_private_folder(team):
    """What the New document box does for `a/_private/b/x.md`: every folder on
    the way is made, and the `_private` one is born closed."""
    common.makedirs_with_mode(team / "a" / "_private" / "b")
    assert has_acl(team / "a"), "an ordinary folder on the way takes the team's audience"
    assert mode(team / "a" / "_private") == 0o700
    assert not has_acl(team / "a" / "_private")
    assert mode(team / "a" / "_private" / "b") & 0o077 == 0


def test_a_folder_moved_or_renamed_to_private_is_locked_down(team):
    """A move keeps the inode as it was; the platform then resets it to where it
    landed — which, for a `_private` folder, means owner-only."""
    common.mkdir_with_mode(team / "drafts")
    assert has_acl(team / "drafts")
    os.rename(team / "drafts", team / "_private")
    common.reset_to_parent_audience(team / "_private", True)
    assert mode(team / "_private") == 0o700
    assert not has_acl(team / "_private")


def test_only_an_explicit_share_opens_it_to_the_folder_around_it(team):
    common.mkdir_with_mode(team / "_private")
    common.reset_audience_tree(team / "_private", secret_root_follows=True)   # the share panel's "Same as the folder it's in"
    assert has_acl(team / "_private"), "asked for deliberately, it follows the folder it is in"
