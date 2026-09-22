"""Ctrl+K with semantic search: the paid step happens once per pause, never per
keystroke; a question finds a page by meaning; admins see the index's state
and spend. docs/semantic-search.md."""
import time

import httpx
import pytest
from conftest import BASE, CREDS, login, user_menu
from kbenv import U, proj

PLAN = proj("plan.md")


def api(user):
    c = httpx.Client(base_url=BASE, timeout=30)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def status(user):
    return api(user).get("/api/search/status").json()


def wait_embedded(user, timeout=360):
    """This run's fixtures were seeded minutes ago; a file is embedded only once
    it has been quiet for search.embed.quiet_seconds (120 s by default)."""
    c = api(user)
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = c.get("/api/search", params={"q": "hospital integration spec"}).json()
        if any(r["path"] == PLAN and r.get("why") in ("meaning", "both") for r in j.get("results", [])):
            return
        time.sleep(5)
    pytest.fail("this run's plan was never embedded")


def open_palette(page):
    page.keyboard.press("Control+p")
    page.wait_for_selector('[data-testid="palette"]')


def test_typing_asks_without_final_and_a_pause_asks_once_with_it(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "bob")
    try:
        asked = []
        page.on("request", lambda r: asked.append(r.url) if "/api/search?" in r.url else None)
        open_palette(page)
        box = page.locator('[data-testid="palette-input"]')
        # page.wait_for_timeout, not time.sleep: the sync API delivers the
        # request events only while it is pumping its own loop
        for ch in "how this knowledgebase works":
            box.press(ch if ch != " " else "Space")
            page.wait_for_timeout(120)          # a typist: well under the 700 ms pause
        assert not [u for u in asked if "final=1" in u], "a keystroke asked for a rerank"
        assert asked, "typing searched the documents"
        page.wait_for_timeout(1500)             # the pause
        finals = [u for u in asked if "final=1" in u]
        assert len(finals) == 1, finals
        page.wait_for_timeout(1000)
        assert len([u for u in asked if "final=1" in u]) == 1, "one pause, one rerank"
    finally:
        ctx.close()


def test_a_question_finds_the_plan_by_meaning(browser):
    st = status("bob")
    if not st.get("available") or not st.get("configured") or st.get("provider") == "fake":
        pytest.skip("needs a real embedding model")
    wait_embedded("bob")
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "bob")
    try:
        open_palette(page)
        # none of these words is in the plan ("Finalize the hospital integration spec")
        page.fill('[data-testid="palette-input"]', "clinic software interoperability requirements")
        row = page.locator(f'[data-testid="palette-item"][data-path^="{PLAN}:"]')
        row.first.wait_for(timeout=20000)
        assert "≈" in row.first.inner_text(), "a hit found only by meaning is marked ≈"
    finally:
        ctx.close()


def test_admins_see_the_index_state_and_spend_in_company_settings(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = login(ctx, "alice")                  # the test admin
    try:
        user_menu(page)
        page.click('[data-testid="settings-btn"]')
        page.click('[data-testid="settings-tab-company"]')
        box = page.locator('[data-testid="search-status"]')
        box.wait_for(timeout=10000)
        page.wait_for_function(
            "() => !document.querySelector('[data-testid=search-status]').innerText.includes('loading')",
            timeout=10000)
        text = box.inner_text()
        st = status("alice")
        if st.get("available") and st.get("configured"):
            assert "Indexed" in text and "sections" in text and "Today" in text and "This month" in text
        else:
            assert "full-text only" in text
    finally:
        ctx.close()
