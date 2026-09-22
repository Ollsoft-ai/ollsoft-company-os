"""The inbox: one append-only file per person, written by whoever saw the
event. Pure unit tests — a temporary repo, no server, no root."""
import getpass
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kb_platform import common  # noqa: E402


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    me = getpass.getuser()
    (tmp_path / "users" / me).mkdir(parents=True)
    monkeypatch.setattr(common, "REPO_ROOT", tmp_path)
    return tmp_path, me


def test_an_event_lands_in_the_persons_own_file(repo):
    root, me = repo
    assert common.add_inbox_event(me, {"kind": "mention", "path": "company/x.md", "line": 3})
    p = root / "users" / me / ".os" / "inbox.jsonl"
    assert p.is_file()
    assert oct(p.stat().st_mode & 0o777) == "0o600", "an inbox is nobody else's business"
    ev = json.loads(p.read_text().splitlines()[0])
    assert ev["kind"] == "mention" and ev["id"] and ev["at"] and ev["read"] is False


def test_events_accumulate_oldest_first_and_are_capped(repo):
    root, me = repo
    for i in range(common.INBOX_MAX + 25):
        common.add_inbox_event(me, {"kind": "mention", "path": f"company/{i}.md"})
    got = common.read_inbox(me)
    assert len(got) == common.INBOX_MAX
    assert got[0]["path"].endswith(".md") and got[-1]["path"] == f"company/{common.INBOX_MAX + 24}.md"


def test_a_damaged_line_is_skipped_not_fatal(repo):
    root, me = repo
    common.add_inbox_event(me, {"kind": "mention", "path": "company/good.md"})
    p = root / "users" / me / ".os" / "inbox.jsonl"
    p.write_text("{not json\n" + p.read_text() + "\n\n")
    got = common.read_inbox(me)
    assert [e["path"] for e in got] == ["company/good.md"]


def test_an_unknown_account_gets_nothing(repo):
    assert common.add_inbox_event("nobody-by-that-name", {"kind": "mention"}) is False


def _git(root, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _repo_with_git(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text("")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "root")


def test_a_new_name_is_news_and_an_old_one_is_not(repo, monkeypatch):
    """syncd's rule, decided against the commit it just built on: the names a
    document already had are not an event, a name that was not in the parent
    commit is — and a brand-new document is all news."""
    root, me = repo
    from kb_platform import syncd

    _repo_with_git(root)
    s = syncd.SyncDaemon.__new__(syncd.SyncDaemon)
    monkeypatch.setattr(syncd.SyncDaemon, "_can_read_as", lambda self, u, rel, cache: True)
    doc = root / "company" / "notes.md"
    doc.parent.mkdir(parents=True)

    # a NEW document that names you: news, even though we have never seen it
    doc.write_text(f"hello @{me}\n")
    _git(root, "add", "-A"); _git(root, "commit", "-qm", "one")
    s._notify_mentions("company/notes.md", "someone", {})
    got = common.read_inbox(me)
    assert len(got) == 1 and got[0]["line"] == 1 and got[0]["actor"] == "someone"
    assert me in got[0]["text"]

    # editing around a name that was already there: not news
    doc.write_text(f"hello @{me}\nand more text\n")
    _git(root, "add", "-A"); _git(root, "commit", "-qm", "two")
    s._notify_mentions("company/notes.md", "someone", {})
    assert len(common.read_inbox(me)) == 1, "told twice about one mention"


def test_you_are_never_told_about_your_own_mention_or_one_you_cannot_read(repo, monkeypatch):
    root, me = repo
    from kb_platform import syncd

    _repo_with_git(root)
    s = syncd.SyncDaemon.__new__(syncd.SyncDaemon)
    monkeypatch.setattr(syncd.SyncDaemon, "_can_read_as", lambda self, u, rel, cache: True)
    (root / "company").mkdir(parents=True, exist_ok=True)
    (root / "company" / "a.md").write_text(f"@{me} — written by me\n")
    s._notify_mentions("company/a.md", me, {})
    assert common.read_inbox(me) == [], "mentioning yourself is not an event"

    monkeypatch.setattr(syncd.SyncDaemon, "_can_read_as", lambda self, u, rel, cache: False)
    (root / "company" / "b.md").write_text(f"@{me} in a file you cannot open\n")
    s._notify_mentions("company/b.md", "someone", {})
    assert common.read_inbox(me) == [], "a notification must not name a document you cannot read"


def test_a_secret_never_notifies(repo, monkeypatch):
    root, me = repo
    from kb_platform import syncd

    _repo_with_git(root)
    s = syncd.SyncDaemon.__new__(syncd.SyncDaemon)
    monkeypatch.setattr(syncd.SyncDaemon, "_can_read_as", lambda self, u, rel, cache: True)
    (root / "company" / "_secrets").mkdir(parents=True, exist_ok=True)
    (root / "company" / "_secrets" / "k.md").write_text(f"@{me} the password is hunter2\n")
    s._notify_mentions("company/_secrets/k.md", "someone", {})
    assert common.read_inbox(me) == []


def test_a_symlinked_config_dir_cannot_steer_a_root_write(repo, monkeypatch):
    """A home belongs to the person living in it: they can replace their own
    `.os` with a symlink. syncd and the hub write inboxes AS ROOT, so every
    step is O_NOFOLLOW — otherwise "you were mentioned" becomes "root wrote a
    file wherever I pointed"."""
    root, me = repo
    victim = root / "elsewhere"
    victim.mkdir()
    os.symlink(victim, root / "users" / me / ".os")
    assert common.add_inbox_event(me, {"kind": "mention", "path": "company/x.md"}) is False
    assert list(victim.iterdir()) == [], "nothing may be written through the link"


def test_a_symlinked_home_cannot_either(repo):
    root, me = repo
    victim = root / "elsewhere2"
    victim.mkdir()
    home = root / "users" / me
    for p in home.iterdir():
        p.unlink()
    home.rmdir()
    os.symlink(victim, home)
    assert common.add_inbox_event(me, {"kind": "mention", "path": "company/x.md"}) is False
    assert list(victim.iterdir()) == []
