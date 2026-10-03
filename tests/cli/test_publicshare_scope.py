"""What a public link reaches, checked on a toy tree: no root, no mount, no
container. The host half grants the container's account into a folder; these
tests read the ACLs back and hold down three rules.

  - A folder link never reaches into `_secrets/` or `.trash/` — not when it is
    made, and not later: a `_secrets` created inside after the link inherits
    the folder's default ACL, and the sweep takes it away again.
  - Nothing is reached through a link: a symlink anywhere on the path is
    refused, and a symlink inside the folder is neither followed nor granted.
  - The caller's authorization is asked about the inode that is pinned, before
    anything is granted.
"""
import os
import pwd
import subprocess

import pytest

from kb_platform import common, publicshare

pytestmark = pytest.mark.skipif(
    subprocess.run(["getent", "passwd", publicshare.SHARE_USER], capture_output=True).returncode != 0,
    reason="no kbshare account on this box (public links not installed)")


def _has(path) -> bool:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if os.path.isdir(path) else 0))
    try:
        return publicshare._has_entry(fd, os.path.isdir(path))
    finally:
        os.close(fd)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "kb"
    run = tmp_path / "run"
    run.mkdir()
    share = root / "company" / "share"
    for d in ("sub", "_secrets", ".trash", "sub/_secrets"):
        (share / d).mkdir(parents=True)
    for f in ("a.md", "sub/b.md", "_secrets/key.env", ".trash/old.md", "sub/_secrets/k2.env"):
        (share / f).write_text("x\n")
    other = root / "company" / "other"
    other.mkdir()
    (other / "private.md").write_text("not shared\n")
    (share / "link").symlink_to(other)
    (share / "filelink.md").symlink_to(other / "private.md")
    monkeypatch.setattr(common, "REPO_ROOT", root)
    monkeypatch.setattr(common, "RUN_DIR", run)
    monkeypatch.setattr(publicshare, "STORE", tmp_path / "shares.json")
    return root


def test_a_folder_link_stops_at_secrets_and_the_trash(repo):
    share = repo / "company" / "share"
    with publicshare.Pinned("company/share") as pin:
        publicshare.grant(pin, "view")
    for p in ("", "a.md", "sub", "sub/b.md"):
        assert _has(share / p), p
    for p in ("_secrets", "_secrets/key.env", ".trash", ".trash/old.md",
              "sub/_secrets", "sub/_secrets/k2.env"):
        assert not _has(share / p), p
    # a link inside the folder is not followed: what it points at is untouched
    assert not _has(repo / "company" / "other")
    assert not _has(repo / "company" / "other" / "private.md")


def test_a_secrets_folder_made_after_the_link_is_taken_back(repo):
    share = repo / "company" / "share"
    with publicshare.Pinned("company/share") as pin:
        publicshare.grant(pin, "view")
    late = share / "sub" / "_secrets"
    (late / "new.env").write_text("x\n")
    # what the kernel does to a folder and a file created under a default ACL
    for p, is_dir in ((late, True), (late / "new.env", False)):
        fd = os.open(p, os.O_RDONLY | (os.O_DIRECTORY if is_dir else 0))
        try:
            common.acl_apply_fd(fd, is_dir, ["-m", f"u:{publicshare.SHARE_USER}:rX"])
        finally:
            os.close(fd)
    assert _has(late) and _has(late / "new.env")
    assert publicshare.regrant({"path": "company/share", "kind": "folder", "mode": "view"}) is True
    assert not _has(late), "the sweep's re-grant pass strips it"
    assert not _has(late / "new.env"), "…and everything inside it"


def test_nothing_is_reached_through_a_link(repo):
    for rel in ("company/share/link", "company/share/link/private.md",
                "company/share/filelink.md", "company/../company/share", ""):
        with pytest.raises(ValueError):
            publicshare.Pinned(rel)


def test_a_single_file_link_is_one_file_and_a_search_bit(repo):
    share = repo / "company" / "share"
    with publicshare.Pinned("company/share/a.md") as pin:
        assert pin.kind == "file"
        publicshare.grant(pin, "view")
    assert _has(share / "a.md")
    assert not _has(share / "sub" / "b.md")
    getfacl = subprocess.run(["getfacl", "-c", "-p", str(share)], capture_output=True, text=True).stdout
    assert f"user:{publicshare.SHARE_USER}:--x" in getfacl, getfacl


def test_revoking_takes_every_entry_back(repo):
    share = repo / "company" / "share"
    with publicshare.Pinned("company/share") as pin:
        publicshare.grant(pin, "edit")
        publicshare.revoke_acl(pin)
    for p in ("", "a.md", "sub", "sub/b.md", "_secrets", ".trash"):
        assert not _has(share / p), p


def test_authorization_is_asked_about_the_pinned_inode(repo):
    seen = []

    def may_publish(st):
        seen.append(st.st_ino)
        return False

    with pytest.raises(publicshare.NotAllowed):
        publicshare.create("company/share", by="x", may_publish=may_publish)
    assert seen == [os.stat(repo / "company" / "share").st_ino]
    assert not _has(repo / "company" / "share"), "nothing was granted"


def test_a_secret_or_the_trash_cannot_be_published(repo):
    for rel in ("company/share/_secrets", "company/share/_secrets/key.env", "company/share/.trash"):
        with pytest.raises(ValueError):
            publicshare.create(rel, by="x", may_publish=lambda st: True)


def _default_acl(path) -> str:
    return subprocess.run(["getfacl", "-c", "-p", "-d", str(path)], capture_output=True, text=True).stdout


def test_a_link_never_closes_a_folder_to_its_own_team(repo):
    """A folder with no default ACL used to come out of a grant (or a grant and
    a revoke) with `default:group::---`, so every file made there afterwards
    was closed to the team and the indexer. The default it gets must carry the
    folder's own entries."""
    share = repo / "company" / "share"
    for d in (share, share / "sub"):
        os.chmod(d, 0o2775)
    with publicshare.Pinned("company/share") as pin:
        publicshare.grant(pin, "view")
        publicshare.revoke_acl(pin)
    for d in (share, share / "sub"):
        acl = _default_acl(d)
        assert "group::---" not in acl and "other::---" not in acl, (d, acl)
        assert "group::rwx" in acl, (d, acl)


def test_a_restore_from_the_trash_is_still_served(repo):
    share = repo / "company" / "share"
    row = {"path": "company/share", "kind": "folder", "mode": "view"}
    with publicshare.Pinned("company/share") as pin:
        publicshare.grant(pin, "view")
    # deleted after the link: keeps its entry in the trash, and comes back with it
    os.rename(share / "a.md", share / ".trash" / "a.md")
    publicshare.regrant(row)
    os.rename(share / ".trash" / "a.md", share / "a.md")
    assert _has(share / "a.md")
    # deleted BEFORE the link (never granted), then restored: the sweep heals it
    os.rename(share / ".trash" / "old.md", share / "old.md")
    assert not _has(share / "old.md")
    assert publicshare.regrant(row) is True
    assert _has(share / "old.md")
    assert not _has(share / "_secrets" / "key.env")
