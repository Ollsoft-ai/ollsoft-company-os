"""A loop that dies must be loud.

flush_loop, watch_loop and git_loop were bare asyncio.create_task. When one
raises, the task simply ends: asyncio mentions it at garbage-collection time as
"Task exception was never retrieved", the daemon keeps serving, /health keeps
answering ok and /run/kb/git-state.json keeps looking green — while the KB has
quietly stopped being versioned. Each loop IS a guarantee (flush: your edits
reach disk; watch: external edits reach the doc; git: history exists), so losing
one silently is the worst available outcome.

One restart, then exit. A transient — a git index.lock, an inotify hiccup — is
worth a retry rather than dropping every live editing session; a persistent
fault must not spin, which is how one incident put 61,895 identical lines in the
journal. SystemExit hands the problem to systemd, which has a ladder: restart,
then give up into `failed`, which fires the alert unit.
"""
import asyncio

import pytest

from kb_platform import common, syncd


@pytest.fixture()
def d(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(syncd, "DOC_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(common, "load_session_key", lambda: b"k")
    return syncd.SyncDaemon()


class _Died:
    """A finished task, as the done-callback sees it."""
    def __init__(self, exc=None, cancelled=False):
        self._exc, self._cancelled = exc, cancelled

    def cancelled(self):
        return self._cancelled

    def exception(self):
        return self._exc


@pytest.mark.asyncio
async def test_a_transient_death_is_restarted_and_the_loop_keeps_running(d, caplog):
    runs = []

    async def flaky():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("index.lock")
        await asyncio.sleep(3600)          # the retry stays up

    d.supervise("git", flaky)
    await asyncio.sleep(0.05)
    assert len(runs) == 2, f"expected exactly one retry, ran {len(runs)}x"
    assert any("LOOP git STOPPED" in r.getMessage() for r in caplog.records), \
        "a dead loop must be logged loudly, not swallowed"
    for task in d.tasks:
        task.cancel()


def test_a_second_death_inside_the_window_exits_the_process(d, monkeypatch):
    """Not a restart storm — hand it to systemd, which knows how to give up.

    Driven through _loop_died rather than a live task: SystemExit raised in a
    done-callback escapes via the event loop, not into the awaiting coroutine,
    so pytest.raises around an await would never see it.
    """
    monkeypatch.setattr(d, "supervise", lambda n, f: None)   # no running loop here
    d._loop_died("git", lambda: None, _Died(RuntimeError("first")))
    with pytest.raises(SystemExit) as e:
        d._loop_died("git", lambda: None, _Died(RuntimeError("again")))
    assert e.value.code == 1, "status 1, or systemd's Restart=on-failure never fires"


def test_a_death_outside_the_window_is_just_another_restart(d, monkeypatch):
    """A daemon up for weeks may hit two unrelated blips. That is not a fault."""
    calls = []
    monkeypatch.setattr(d, "supervise", lambda n, f: calls.append(n))
    d._loop_died("git", lambda: None, _Died(RuntimeError("first")))
    d._loop_deaths["git"] -= d._RESTART_WINDOW * 2      # long ago
    d._loop_died("git", lambda: None, _Died(RuntimeError("much later")))
    assert calls == ["git", "git"], "an old death must not count toward the limit"


def test_cancelling_a_loop_at_shutdown_is_not_a_fault(d, monkeypatch):
    calls = []
    monkeypatch.setattr(d, "supervise", lambda n, f: calls.append(n))
    d._loop_died("flush", lambda: None, _Died(cancelled=True))
    assert calls == [], "shutdown cancellation must not trigger a restart"


def test_a_loop_that_returns_is_also_a_fault(d, monkeypatch):
    """These loops are `while True`. Returning at all means something broke."""
    calls = []
    monkeypatch.setattr(d, "supervise", lambda n, f: calls.append(n))
    d._loop_died("watch", lambda: None, _Died(exc=None))
    assert calls == ["watch"], "a loop returning early is a death like any other"
