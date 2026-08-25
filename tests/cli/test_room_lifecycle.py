"""A YRoom must not outlive the file behind it.

pycrdt runs with auto_clean_rooms=False, so this daemon owns every room's death
— and it never took ownership. `_retire_room` dropped six of its own maps but
left `server.rooms` alone, so the next `get_room()` on that path handed back the
DEAD document. Both of `seed_room`'s "is the text empty?" guards then see the
old content and refuse to load the file now living there: the editor shows the
deleted document, and every flush afterwards defers on the last_written
mismatch, so nothing the user types ever reaches disk. Delete-and-recreate,
rename A->B->A, or a `git checkout` that restores a path all reach it.

Only the retire path is covered here. Freeing idle rooms was designed alongside
this and deliberately NOT shipped: the free pops `text_handles`, which is the
only map flush_loop iterates to notice a file has gone, so freeing a room made
its later deletion invisible and stranded the .y/.e state forever.
"""
import asyncio
import contextlib

import pytest

from kb_platform import common, syncd


@pytest.fixture()
def d(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(common, "REPO_ROOT", repo)
    monkeypatch.setattr(syncd, "DOC_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(common, "load_session_key", lambda: b"k")
    daemon = syncd.SyncDaemon()
    daemon.repo = repo
    return daemon


async def _open(d, name):
    """What ws_doc does to join a room."""
    room = await d.server.get_room(name)
    await d.seed_room(room, name)
    return room


async def _one_flush_pass(d):
    task = asyncio.create_task(d.flush_loop())
    await asyncio.sleep(3 * syncd.FLUSH_DEBOUNCE)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_recreated_path_reads_the_new_file_not_the_dead_room(d):
    (d.repo / "a.md").write_text("old\n")
    async with d.server:
        room = await _open(d, "a.md")
        assert str(d.text_handles["a.md"]) == "old\n"

        (d.repo / "a.md").unlink()
        await _one_flush_pass(d)                      # notices, retires
        assert "a.md" not in d.server.rooms, \
            "the room outlived its file; the next open will serve the dead document"

        (d.repo / "a.md").write_text("new\n")
        room2 = await _open(d, "a.md")
        assert room2 is not room, "get_room handed back the retired room"
        assert str(d.text_handles["a.md"]) == "new\n", \
            "seed_room refused the new file because the dead text was not empty"


@pytest.mark.asyncio
async def test_a_retire_leaves_no_state_behind_for_the_next_open(d):
    """The maps and the on-disk lineage go together.

    A retire that dropped the room but kept .y/.e would let a stale tab
    reconnect onto the old lineage instead of being 409'd into a fresh open.
    """
    (d.repo / "b.md").write_text("bye\n")
    async with d.server:
        await _open(d, "b.md")
        d.doc_epoch("b.md")
        sp = d._state_path("b.md")
        (d.repo / "b.md").unlink()
        await _one_flush_pass(d)

        for m in (d.server.rooms, d.text_handles, d.last_written, d.meta, d.epochs):
            assert "b.md" not in m, "retire left state behind"
        assert not sp.exists() and not sp.with_suffix(".e").exists(), \
            "retire left the saved CRDT state or the epoch file on disk"


@pytest.mark.asyncio
async def test_dropping_a_room_that_is_not_there_is_harmless(d):
    """_drop_room runs on every retire, including paths never opened."""
    async with d.server:
        await d._drop_room("never-opened.md")      # must not raise
