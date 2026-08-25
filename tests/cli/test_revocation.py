"""Revocation has to reach a document someone already has open.

syncd decided read and write ONCE, at websocket connect, then served the room
for the life of the socket. Nothing could close it: hub._share_apply restarts
the user's backend but has no channel to syncd at all. Un-share a document from
someone editing it and they kept receiving every co-editor's keystroke and kept
writing — persisted by root into a file the kernel would now refuse them.

The decision logic is what is tested here: enforce_access against a real file
whose ACL really changes, with fake channels standing in for sockets. The socket
plumbing (a send after revoke_read, the close) is exercised by the channel's own
state, not by a browser.
"""
import os

import pytest

from kb_platform import common, syncd


class FakeChannel:
    def __init__(self, can_write=True):
        self.can_write, self.readable = can_write, True
        self.told = []

    def revoke_write(self):
        self.can_write = False
        self.told.append("write")

    def revoke_read(self):
        self.can_write = self.readable = False
        self.told.append("read")


@pytest.fixture()
def d(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(syncd, "DOC_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(common, "load_session_key", lambda: b"k")
    return syncd.SyncDaemon()


def _join(d, name, ch, user="nobody", uid=65534):
    d._presence_add(name, id(ch), syncd.Session(user, uid, ch))


def test_losing_read_cuts_the_session(d, tmp_path):
    # 0o666 so the FIRST sweep is a genuine no-op: at 0o644 `nobody` already
    # lacks write, and the write revoke would be mistaken for the read one.
    f = tmp_path / "doc.md"; f.write_text("x"); os.chmod(f, 0o666)
    ch = FakeChannel(); _join(d, "doc.md", ch)
    d.enforce_access()
    assert ch.readable and ch.told == [], "a session with full access was disturbed"

    os.chmod(f, 0o600)                      # un-shared
    d.enforce_access()
    assert not ch.readable and ch.told == ["read"], \
        "a revoked reader kept receiving the document"


def test_losing_only_write_keeps_the_socket(d, tmp_path):
    """The tab goes read-only with its unsaved buffer intact."""
    f = tmp_path / "doc.md"; f.write_text("x"); os.chmod(f, 0o666)
    ch = FakeChannel(); _join(d, "doc.md", ch)
    d.enforce_access()
    assert ch.can_write and ch.readable

    os.chmod(f, 0o644)                      # still readable, no longer writable
    d.enforce_access()
    assert ch.readable, "losing write must not cut the socket"
    assert not ch.can_write and ch.told == ["write"]


def test_an_empty_filter_sweeps_everything(d, tmp_path):
    """`"users": []` off the wire arrives as an empty set, not None."""
    f = tmp_path / "doc.md"; f.write_text("x"); os.chmod(f, 0o600)
    ch = FakeChannel(); _join(d, "doc.md", ch)
    d.enforce_access(rooms=set(), users=set())
    assert not ch.readable, "an empty filter checked nothing at all"


def test_a_missing_path_is_not_reported_as_revoked(d, tmp_path):
    """An external editor saving by rename makes the path briefly absent."""
    f = tmp_path / "doc.md"; f.write_text("x"); os.chmod(f, 0o644)
    ch = FakeChannel(); _join(d, "doc.md", ch)
    f.unlink()
    d.enforce_access()
    assert ch.readable and ch.told == [], "a rename window was treated as a revoke"


@pytest.mark.asyncio
async def test_retiring_a_moved_document_cuts_its_readers(d, tmp_path):
    """The un-share-by-move case enforce_access deliberately skips."""
    f = tmp_path / "doc.md"; f.write_text("x"); os.chmod(f, 0o644)
    ch = FakeChannel(); _join(d, "doc.md", ch)
    f.unlink()                                    # moved into a private folder
    await d._retire_room("doc.md")
    assert not ch.readable and ch.told == ["read"], \
        "a moved document kept streaming to readers who lost access"
