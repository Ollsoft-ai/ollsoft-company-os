"""The To-dos artifact: a default, everyone-accessible artifact that aggregates
tasks via the SQL bridge (RLS-scoped as the viewer), with @assignee filtering
and checkbox toggling through the narrow kb-toggle bridge action.
"""
import time

import httpx
from conftest import BASE, CREDS, login

TODOS = "company/todos.html"


def q(user, sql, params=None):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": user, "password": CREDS[user]})
    return c.post("/api/artifact/query", json={"sql": sql, "params": params or []}).json()


# --- the todo data is RLS-scoped exactly like everything else --------------
def test_todo_data_is_rls_scoped():
    sql = "SELECT file_path FROM kb.blocks WHERE kind='task'"
    kry = {r[0] for r in q("alice", sql)["rows"]}
    carol = {r[0] for r in q("carol", sql)["rows"]}
    assert any(p.startswith("projects/acme/") for p in kry), "alice should see acme tasks"
    assert not any(p.startswith("projects/acme/") for p in carol), \
        "carol must NOT see acme tasks in the todo aggregate"
    assert any(p.startswith("company/") for p in carol), "carol sees company tasks"


def test_assigned_to_me_query():
    # The 'mine' view is: current_user = ANY(assignees), still under RLS.
    rows = q("alice", "SELECT file_path FROM kb.blocks WHERE kind='task' "
                        "AND %s = ANY(assignees)", ["alice"])["rows"]
    paths = {r[0] for r in rows}
    assert "company/overview.md" in paths      # 'Ship the knowledgebase platform @alice'


def test_todos_artifact_mine_and_all(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click('.tree-item[data-path="company/todos.html"]')
    frame = page.frame_locator("iframe.artifact-frame")
    frame.locator("#who").wait_for(timeout=10000)
    frame.locator(".task, .empty").first.wait_for(timeout=10000)
    time.sleep(0.8)
    # 'Assigned to me' is the default; switch to 'All visible' and expect >= as many.
    mine = frame.locator(".task").count()
    frame.locator("#tab-all").click()
    time.sleep(0.4)
    allc = frame.locator(".task").count()
    assert allc >= mine
    ctx.close()


def test_todos_artifact_hides_acme_for_carol(browser):
    ctx = browser.new_context()
    page = login(ctx, "carol")
    page.click('.tree-item[data-path="company/todos.html"]')
    frame = page.frame_locator("iframe.artifact-frame")
    frame.locator("#who").wait_for(timeout=10000)
    frame.locator("#tab-all").click()
    time.sleep(0.8)
    body = frame.locator("#content").inner_text()
    assert "hospital" not in body and "zebrafish" not in body, "carol saw restricted acme tasks"


def test_toggle_from_todos_writes_file(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click('.tree-item[data-path="company/todos.html"]')
    frame = page.frame_locator("iframe.artifact-frame")
    frame.locator("#who").wait_for(timeout=10000)
    frame.locator("#tab-all").click()
    frame.locator("#showdone").check()
    time.sleep(0.8)
    # onboarding.md 'Read the security policy @carol' is a company task alice can write.
    line = 5
    before = open("/srv/kb/company/onboarding.md").read().splitlines()[line - 1]
    # find that task's checkbox and click it
    task = frame.locator('.task', has_text="Read the security policy").first
    task.locator("input[type=checkbox]").click()
    ok = False
    for _ in range(15):
        now = open("/srv/kb/company/onboarding.md").read().splitlines()[line - 1]
        if now != before:
            ok = True
            break
        time.sleep(0.3)
    assert ok, "toggling in the todos artifact must flip the source file"
    # restore
    task2 = frame.locator('.task', has_text="Read the security policy").first
    task2.locator("input[type=checkbox]").click()
    time.sleep(1.0)
    ctx.close()
