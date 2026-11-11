"""The account switcher in a real browser: add a second account from the user
menu, switch back and forth in one click, and keep each account's browser
state (open tabs, recents …) its own."""
import json
import os

from conftest import login, user_menu

SHOTS = os.environ.get("KB_SHOTS")   # a directory: save screenshots for review


def shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def add_account(page, user):
    from kbenv import BASE, CREDS, U
    user_menu(page)
    page.click('[data-testid="account-add"]')
    page.wait_for_url("**/login?add=1")
    assert page.locator("#login-cancel").is_visible()
    shot(page, "switcher-2-add-account")
    page.fill('input[name="username"]', U(user))
    page.fill('input[name="password"]', CREDS[user])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')


def test_add_switch_and_separate_browser_state(browser):
    from kbenv import BASE, U
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = login(ctx, "alice")
    page.evaluate("() => localStorage.setItem('kbRecent', JSON.stringify(['alice-only.md']))")

    add_account(page, "bob")
    assert page.locator("#user-name").text_content() == U("bob")
    # bob does not inherit alice's browser state
    assert "alice-only.md" not in (page.evaluate("() => localStorage.getItem('kbRecent')") or "")
    page.evaluate("() => localStorage.setItem('kbRecent', JSON.stringify(['bob-only.md']))")

    user_menu(page)
    row = page.locator('[data-testid="account"][data-user="%s"]' % U("alice"))
    row.wait_for()
    assert page.locator('[data-testid="logout-all"]').is_visible()
    shot(page, "switcher-1-menu")
    row.click()
    page.wait_for_url(BASE + "/")
    page.wait_for_function("u => document.querySelector('#user-name').textContent === u", arg=U("alice"))
    assert json.loads(page.evaluate("() => localStorage.getItem('kbRecent')")) == ["alice-only.md"], \
        "switching back must restore alice's own browser state"

    # and back to bob: his state is where he left it
    user_menu(page)
    page.click('[data-testid="account"][data-user="%s"]' % U("bob"))
    page.wait_for_function("u => document.querySelector('#user-name').textContent === u", arg=U("bob"))
    assert json.loads(page.evaluate("() => localStorage.getItem('kbRecent')")) == ["bob-only.md"]
    ctx.close()


def test_a_second_tab_follows_a_switch(browser):
    """Two tabs, one browser: switching in one must not leave the other acting
    as the previous account."""
    from kbenv import U
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = login(ctx, "alice")
    add_account(page, "bob")
    other = ctx.new_page()
    other.goto(page.url)
    other.wait_for_function("u => document.querySelector('#user-name').textContent === u", arg=U("bob"))
    user_menu(page)
    page.click('[data-testid="account"][data-user="%s"]' % U("alice"))
    other.wait_for_function("u => document.querySelector('#user-name').textContent === u",
                            arg=U("alice"), timeout=15000)
    ctx.close()


def test_sign_out_hands_over_then_lists_the_signed_out_account(browser):
    from kbenv import BASE, U
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    page = login(ctx, "alice")
    add_account(page, "bob")
    user_menu(page)
    page.click('[data-testid="logout"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_function("u => document.querySelector('#user-name').textContent === u", arg=U("alice"))
    user_menu(page)
    ended = page.locator('[data-testid="account"][data-user="%s"]' % U("bob"))
    ended.wait_for()
    assert "Signed out" in ended.text_content()
    shot(page, "switcher-3-signed-out-row")
    ended.click()
    page.wait_for_url("**/login?add=1&user=*")
    assert page.input_value('input[name="username"]') == U("bob")
    ctx.close()


def test_sign_in_form_is_one_a_password_manager_can_find_and_submit(browser):
    """Password managers find a sign-in by its markup: a form with a method
    and action, labelled fields with ids, autocomplete hints. Some submit the
    form themselves (form.submit(), which skips the page's script) — that must
    still sign in and land in the app."""
    from kbenv import BASE, CREDS, U
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(BASE + "/login")
    form = page.evaluate("""() => {
      const f = document.querySelector('form');
      const u = f.querySelector('input[autocomplete="username"]');
      const p = f.querySelector('input[type="password"][autocomplete="current-password"]');
      return { method: f.method, action: new URL(f.action).pathname,
               user: !!u && !!document.querySelector('label[for="' + u.id + '"]'),
               pass: !!p && !!document.querySelector('label[for="' + p.id + '"]') };
    }""")
    assert form == {"method": "post", "action": "/login", "user": True, "pass": True}
    page.fill("#username", U("alice"))
    page.fill("#password", CREDS["alice"])
    page.evaluate("() => document.querySelector('form').submit()")
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    ctx.close()
