"""_evict_for_new_session — the only code that kills a live shell unbidden.

Three outcomes: under the cap (no-op), at the cap with detached sessions (kill
the OLDEST detached one), at the cap with everything attached (refuse). Only the
first and third were covered. The middle branch is LRU by one character —
`min(detached, key=...)` vs `max` — and getting it backwards kills the shell
someone is most likely to come back to, while leaving the stale one to rot.

Unit tests with stand-in sessions: the real PtySession forks a shell, and the
branch under test only reads `.q` and `.detached_at` and calls `.kill()`.
"""
import pytest

from kb_platform import user_server as us


class FakeSession:
    """Enough of PtySession for the eviction branch; kill() de-registers the
    way PtySession._teardown does."""

    def __init__(self, sid, detached_at, attached=False):
        self.sid = sid
        self.detached_at = detached_at
        self.q = object() if attached else None      # q is None == detached
        self.killed = False

    def kill(self):
        self.killed = True
        us.PTY_SESSIONS.pop(self.sid, None)


@pytest.fixture
def sessions(monkeypatch):
    reg = {}
    monkeypatch.setattr(us, "PTY_SESSIONS", reg)
    return reg


def _fill(reg, n, **kw):
    for i in range(n):
        s = FakeSession(f"s{i}", detached_at=100.0 + i, **kw)
        reg[s.sid] = s
    return reg


def test_under_the_cap_kills_nothing(sessions):
    _fill(sessions, us.PTY_MAX_SESSIONS - 1)
    assert us._evict_for_new_session() is True
    assert not any(s.killed for s in sessions.values())


def test_at_the_cap_with_everything_attached_refuses(sessions):
    _fill(sessions, us.PTY_MAX_SESSIONS, attached=True)
    assert us._evict_for_new_session() is False, \
        "an attached terminal was sacrificed for a new one"
    assert not any(s.killed for s in sessions.values())


def test_at_the_cap_evicts_the_OLDEST_detached(sessions):
    """The branch that had no test."""
    _fill(sessions, us.PTY_MAX_SESSIONS, attached=True)
    # make three of them detached, at known times
    for sid, when in (("s3", 500.0), ("s7", 100.0), ("s9", 300.0)):
        sessions[sid].q = None
        sessions[sid].detached_at = when
    victims = {sid: s for sid, s in sessions.items()}

    assert us._evict_for_new_session() is True
    killed = [sid for sid, s in victims.items() if s.killed]
    assert killed == ["s7"], (
        f"expected the oldest detached session (s7, detached_at=100) to go; killed {killed}. "
        "Killing the newest instead takes the shell someone just stepped away from."
    )


def test_an_attached_session_is_never_the_victim(sessions):
    _fill(sessions, us.PTY_MAX_SESSIONS, attached=True)
    sessions["s5"].q = None            # exactly one detached, and it is not the oldest sid
    sessions["s5"].detached_at = 999.0
    assert us._evict_for_new_session() is True
    assert sessions.get("s5") is None or sessions["s5"].killed
    assert not any(s.killed for sid, s in sessions.items() if sid != "s5")
