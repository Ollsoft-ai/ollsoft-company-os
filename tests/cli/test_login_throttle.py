"""LoginThrottle: the only thing standing between POST /login and unlimited
password guessing (PAM here is bare pam_unix, no faillock).

Pure unit tests — deliberately NOT integration. Driving the real endpoint would
mean locking a real account for LOGIN_LOCK seconds and leaving the rest of the
suite to trip over it.
"""
import pytest

from kb_platform.hub import (LOGIN_LOCK, LOGIN_MAX_FAILS, LOGIN_MAX_KEYS,
                             LOGIN_WINDOW, LoginThrottle, _login_keys)


class FakeReq:
    """Enough of web.Request for _login_keys."""
    def __init__(self, headers=None, remote="127.0.0.1"):
        self.headers = headers or {}
        self.remote = remote


def burst(t, keys, n, now=1000.0):
    locked = []
    for i in range(n):
        locked += t.record_failure(keys, now + i * 0.1)
    return locked


def test_open_until_the_limit_then_locked():
    t = LoginThrottle()
    burst(t, ["user:bob"], LOGIN_MAX_FAILS - 1)
    assert t.retry_after(["user:bob"], 1000.0) == 0, "locked one guess too early"
    burst(t, ["user:bob"], 1, now=1000.0)
    assert t.retry_after(["user:bob"], 1000.0) > 0, "did not lock at the limit"


def test_lock_expires():
    t = LoginThrottle()
    burst(t, ["user:bob"], LOGIN_MAX_FAILS)
    assert t.retry_after(["user:bob"], 1000.0 + LOGIN_LOCK - 1) > 0
    assert t.retry_after(["user:bob"], 1000.0 + LOGIN_LOCK + 1) == 0


def test_failures_age_out_of_the_window():
    # Slow guessing spread wider than the window must never accumulate a lock.
    t = LoginThrottle()
    for i in range(LOGIN_MAX_FAILS * 3):
        t.record_failure(["user:bob"], 1000.0 + i * (LOGIN_WINDOW + 1))
    assert t.retry_after(["user:bob"], 1000.0 + 99 * (LOGIN_WINDOW + 1)) == 0


def test_success_forgives_history():
    t = LoginThrottle()
    burst(t, ["user:bob"], LOGIN_MAX_FAILS - 1)
    t.clear(["user:bob"])
    burst(t, ["user:bob"], LOGIN_MAX_FAILS - 1)
    assert t.retry_after(["user:bob"], 1000.0) == 0


def test_keys_lock_independently():
    # Locking one account must not lock the colleague sharing its office IP.
    t = LoginThrottle()
    burst(t, ["user:bob", "from:1.2.3.4"], LOGIN_MAX_FAILS)
    assert t.retry_after(["user:bob"], 1000.0) > 0
    assert t.retry_after(["user:carol"], 1000.0) == 0
    assert t.retry_after(["from:5.6.7.8"], 1000.0) == 0


def test_source_key_catches_username_enumeration():
    # One attacker, a different username every time: the account counter never
    # trips, so only the source counter can stop this.
    t = LoginThrottle()
    for i in range(LOGIN_MAX_FAILS):
        t.record_failure([f"user:guess{i}", "from:1.2.3.4"], 1000.0 + i)
    assert t.retry_after(["from:1.2.3.4"], 1000.0) > 0
    assert t.retry_after(["user:guess0"], 1000.0) == 0


def test_locked_message_reports_whole_minutes_remaining():
    t = LoginThrottle()
    burst(t, ["user:bob"], LOGIN_MAX_FAILS)
    wait = t.retry_after(["user:bob"], 1000.0)
    assert 0 < wait <= LOGIN_LOCK + 1


@pytest.mark.parametrize("headers,remote,expected", [
    ({"CF-Connecting-IP": "9.9.9.9"}, "127.0.0.1", ["user:bob", "from:9.9.9.9"]),
    ({"X-Forwarded-For": "9.9.9.9, 10.0.0.1"}, "127.0.0.1", ["user:bob", "from:9.9.9.9"]),
    ({}, "127.0.0.1", ["user:bob"]),          # bare loopback: account key only
    ({}, "::1", ["user:bob"]),
    ({}, "10.0.0.7", ["user:bob", "from:10.0.0.7"]),
])
def test_login_keys(headers, remote, expected):
    assert _login_keys(FakeReq(headers, remote), "Bob") == expected


def test_username_case_folds_to_one_key():
    # Otherwise "Bob"/"BOB"/"bob" are three free budgets for the same account.
    assert _login_keys(FakeReq(), "BOB") == _login_keys(FakeReq(), "bob")


def test_key_table_stays_bounded():
    # Reachable only by forging the source header from on-box, but an unbounded
    # dict in a root daemon is not something to leave to good manners.
    t = LoginThrottle()
    for i in range(LOGIN_MAX_KEYS * 2):
        t.record_failure([f"from:10.0.{i // 256}.{i % 256}"], 1000.0 + i * 0.001)
    assert len(t._fails) <= LOGIN_MAX_KEYS
