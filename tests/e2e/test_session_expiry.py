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
from conftest import BASE, CREDS

USER = "alice"


def login(ctx, user=USER):
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', user)
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
    """No user action at all: the 4s tree poll notices and bounces."""
    page = login(ctx)
    ctx.clear_cookies()
    page.wait_for_url("**/login", timeout=20000)
    assert page.locator('input[name="username"]').count() == 1, \
        "must land on a usable login form, not a dead app"


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
    page.wait_for_url("**/login", timeout=20000)
    page.fill('input[name="username"]', USER)
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
