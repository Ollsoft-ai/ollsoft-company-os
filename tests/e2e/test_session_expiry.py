"""An expired session must send you to the login page by itself.

The reported symptom: close the laptop, come back a day later (the cookie lives
12h — common.SESSION_TTL), and the tab still shows a fully painted app that
silently does nothing. Every call 401s, loadTree() swallows the error, and the
only way out was knowing to hit reload.

Clearing the cookie is exactly what the browser does when it expires, so that
is what these drive.
"""
import time

import pytest
from kbenv import U
from conftest import BASE, CREDS

USER = "alice"


def login(ctx, user=USER):
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', U(user))
    page.fill('input[name="password"]', CREDS[user])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    return page


@pytest.fixture
def ctx(browser):
    c = browser.new_context(viewport={"width": 1200, "height": 800})
    yield c
    c.close()


def test_expired_session_redirects_to_login_on_its_own(ctx):
    """No user action at all. Nothing polls any more; the two things that
    notice are the event stream — the hub ends it when the cookie it was
    opened with expires, and the reconnect meets a 401 — and any request a
    returning tab makes. A cookie cleared under a live stream is the browser's
    12 h expiry in miniature, except that the stream is still up: nudge the
    tab the way coming back does (`online`), and the bounce must follow at
    once — with no user action and no reload."""
    page = login(ctx)
    ctx.clear_cookies()
    page.evaluate("() => window.dispatchEvent(new Event('online'))")
    page.wait_for_url("**/login", timeout=20000)
    assert page.locator('input[name="username"]').count() == 1, \
        "must land on a usable login form, not a dead app"


def test_a_stream_error_probes_the_session_and_bounces(ctx):
    """The path the real expiry takes: the stream drops (here: closed from the
    page, as the hub does at `exp`), the client reconnects, the reconnect is a
    401 — which EventSource cannot see — so it probes with one ordinary fetch
    and the session guard sends it to login."""
    page = login(ctx)
    page.wait_for_function("() => window.__kbevents && window.__kbevents.mode === 'sse'", timeout=15000)
    ctx.clear_cookies()
    page.evaluate("() => window.__kbevents.reopen()")
    page.wait_for_url("**/login", timeout=20000)


def test_returning_to_the_tab_checks_immediately(ctx):
    """The real scenario is a tab that was asleep. Background tabs have their
    timers throttled to roughly once a minute, so waiting for the poll is
    exactly the wrong thing at the moment the user is looking at it."""
    page = login(ctx)
    ctx.clear_cookies()
    t0 = time.time()
    page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_url("**/login", timeout=20000)
    assert time.time() - t0 < 3, "coming back to the tab must not wait out the poll"


def test_can_sign_back_in_after_being_bounced(ctx):
    """Being redirected is only half of it — the session has to be re-establishable
    from where it lands, with no manual reload."""
    page = login(ctx)
    ctx.clear_cookies()
    page.evaluate("() => window.dispatchEvent(new Event('online'))")
    page.wait_for_url("**/login", timeout=20000)
    page.fill('input[name="username"]', U(USER))
    page.fill('input[name="password"]', CREDS[USER])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')


def test_a_403_does_not_bounce_you(ctx):
    """Only 401 (no session) may redirect. Permission denials are 403 and are a
    normal part of using a shared knowledgebase — being logged out for opening
    someone else's file would be its own bug."""
    page = login(ctx)
    status = page.evaluate("""async () => {
      const r = await fetch('/api/artifact/raw?path=users/krystof/nope-not-yours.md');
      return r.status;
    }""")
    assert status != 401, f"fixture precondition: expected a non-401, got {status}"
    page.wait_for_timeout(1500)
    assert "/login" not in page.url, f"a {status} must not sign the user out"
