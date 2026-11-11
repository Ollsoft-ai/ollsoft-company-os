"""The account switcher and the rolling session, over HTTP.

Several accounts can be signed in on one browser: kb_session is the active
one, kb_accounts holds the others. Switching needs no password (the account is
signed in already) and must never reach an account that is not.
"""
import time

import httpx
import pytest
from kbenv import BASE, CREDS, U

from kb_platform import common


def browser():
    return httpx.Client(base_url=BASE, timeout=15, follow_redirects=False)


def sign_in(c, user):
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, f"login {user}: {r.status_code} {r.text}"


def whoami(c):
    return c.get("/api/whoami").json()["user"]


def accounts(c):
    r = c.get("/accounts")
    assert r.status_code == 200, r.text
    return r.json()


def test_second_sign_in_adds_an_account_and_keeps_the_first():
    c = browser()
    sign_in(c, "alice")
    sign_in(c, "bob")
    assert whoami(c) == U("bob"), "the account signed in last is the active one"
    j = accounts(c)
    assert j["active"] == U("bob")
    assert [a["user"] for a in j["accounts"]] == [U("bob"), U("alice")]


def test_switch_is_one_request_and_needs_no_password():
    c = browser()
    sign_in(c, "alice")
    sign_in(c, "bob")
    r = c.post("/accounts/switch", json={"user": U("alice")})
    assert r.status_code == 200, r.text
    assert whoami(c) == U("alice"), "the backend must now run as the account switched to"
    assert [a["user"] for a in accounts(c)["accounts"]] == [U("alice"), U("bob")]


def test_cannot_switch_to_an_account_that_is_not_signed_in_here():
    c = browser()
    sign_in(c, "alice")
    r = c.post("/accounts/switch", json={"user": U("carol")})
    assert r.status_code == 404
    assert whoami(c) == U("alice")


def test_a_forged_accounts_cookie_switches_nobody():
    c = browser()
    sign_in(c, "alice")
    forged = common.make_token(b"not-the-key" * 3, {"user": U("carol")})
    c.cookies.set(common.ACCOUNTS_COOKIE, forged)
    assert c.post("/accounts/switch", json={"user": U("carol")}).status_code == 404
    assert [a["user"] for a in accounts(c)["accounts"]] == [U("alice")]


def test_cross_origin_switch_is_refused():
    c = browser()
    sign_in(c, "alice")
    sign_in(c, "bob")
    r = c.post("/accounts/switch", json={"user": U("alice")},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert whoami(c) == U("bob")


def test_sign_out_hands_over_to_the_next_account_and_all_signs_out_everyone():
    c = browser()
    sign_in(c, "alice")
    sign_in(c, "bob")
    r = c.get("/logout")
    assert r.status_code == 302 and r.headers["location"] == "/"
    assert whoami(c) == U("alice")
    assert [a["user"] for a in accounts(c)["accounts"]] == [U("alice")]
    sign_in(c, "bob")
    r = c.get("/logout?all=1")
    assert r.headers["location"] == "/login"
    assert c.get("/api/whoami").status_code == 401
    assert c.get("/accounts").status_code == 401


def test_signing_in_as_the_same_account_twice_lists_it_once():
    c = browser()
    sign_in(c, "alice")
    sign_in(c, "alice")
    assert [a["user"] for a in accounts(c)["accounts"]] == [U("alice")]


def test_responses_name_the_active_account():
    c = browser()
    sign_in(c, "alice")
    assert c.get("/api/whoami").headers.get("X-KB-Account") == U("alice")


def test_session_rolls_once_it_is_old_enough():
    """A token issued SESSION_REFRESH ago comes back re-issued with a full TTL;
    a fresh one is left alone (no Set-Cookie on every poll)."""
    c = browser()
    sign_in(c, "alice")
    assert "set-cookie" not in c.get("/api/whoami").headers, "a fresh session must not be re-issued"
    try:
        key = common.load_session_key()
    except OSError:
        pytest.skip("needs the session key (root) to mint an old token")
    old = common.make_token(key, {"user": U("alice")},
                            now=time.time() - common.SESSION_REFRESH - 5)
    c.cookies.set(common.COOKIE_NAME, old)
    r = c.get("/api/whoami")
    assert r.status_code == 200
    fresh = r.cookies.get(common.COOKIE_NAME)
    assert fresh and fresh != old, "an old session must be re-issued"
    exp = common.read_token(key, fresh)["exp"]
    assert exp > time.time() + common.SESSION_TTL - 60


def test_plain_form_post_gets_a_redirect_not_json():
    """A password manager that submits the form itself navigates: it must land
    in the app (or back on the form, with the error), not on a JSON body."""
    c = browser()
    nav = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}
    r = c.post("/login", data={"username": U("alice"), "password": "wrong", "next": "/"},
               headers=nav)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?")
    assert "error=invalid" in r.headers["location"]
    r = c.post("/login", data={"username": U("alice"), "password": CREDS["alice"],
                               "next": "//evil.example/"}, headers=nav)
    assert r.status_code == 303 and r.headers["location"] == "/", "next must stay on this site"
    assert whoami(c) == U("alice")
