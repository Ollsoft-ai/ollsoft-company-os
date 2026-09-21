"""The To-dos artifact: a default, everyone-accessible artifact that aggregates
tasks via the SQL bridge (RLS-scoped as the viewer), with @assignee filtering
and checkbox toggling through the narrow kb-toggle bridge action.
"""
import time

import httpx
from conftest import BASE, CREDS, login
from kbenv import U, doc, full, proj

TODOS = doc("todos.html")


def q(user, sql, params=None):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c.post("/api/artifact/query", json={"sql": sql, "params": params or []}).json()


# --- the todo data is RLS-scoped exactly like everything else --------------
def test_todo_data_is_rls_scoped():
    sql = "SELECT file_path FROM kb.blocks WHERE kind='task'"
    kry = {r[0] for r in q("alice", sql)["rows"]}
    carol = {r[0] for r in q("carol", sql)["rows"]}
    assert any(p.startswith(proj()) for p in kry), "alice should see acme tasks"
    assert not any(p.startswith(proj()) for p in carol), \
        "carol must NOT see acme tasks in the todo aggregate"
    assert any(p.startswith("company/") for p in carol), "carol sees company tasks"


def test_assigned_to_me_query():
    # Self-contained: a real box's company docs may hold no @alice task (the
    # demo seed skips documents that already exist), so plant our OWN task doc
    # via the product API, wait for the indexer, and assert the 'mine' query
    # (current_user = ANY(assignees), still under RLS) surfaces exactly it.
    path = doc(f"kbtest_todo_{int(time.time())}.md")
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    assert c.post("/api/file", json={"path": path}).status_code == 200
    c.post("/api/artifact/write", json={
        "path": path,
        "content": "# rollout\n\n- [ ] verify the rollout @alice #kbtest\n"})
    try:
        # the indexer is inotify-driven; give it a generous grace period
        deadline = time.time() + 60
        indexed = []
        while time.time() < deadline:
            indexed = q("alice", "SELECT file_path FROM kb.blocks WHERE kind='task' "
                                 "AND file_path=%s", [path]).get("rows", [])
            if indexed:
                break
            time.sleep(0.5)
        assert indexed, f"indexer never picked up {path}"
        rows = q("alice", "SELECT file_path FROM kb.blocks WHERE kind='task' "
                            "AND %s = ANY(assignees)", ["alice"])["rows"]
        paths = {r[0] for r in rows}
        assert path in paths, f"'assigned to me' missed {path}, got {paths}"
    finally:
        c.post("/api/fs/delete", json={"path": path})


def test_todos_artifact_mine_and_all(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click(f'.tree-item[data-path="{doc("todos.html")}"]')
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
    page.click(f'.tree-item[data-path="{doc("todos.html")}"]')
    frame = page.frame_locator("iframe.artifact-frame")
    frame.locator("#who").wait_for(timeout=10000)
    frame.locator("#tab-all").click()
    time.sleep(0.8)
    body = frame.locator("#content").inner_text()
    assert "hospital" not in body and "zebrafish" not in body, "carol saw restricted acme tasks"


def test_toggle_from_todos_writes_file(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.click(f'.tree-item[data-path="{doc("todos.html")}"]')
    frame = page.frame_locator("iframe.artifact-frame")
    frame.locator("#who").wait_for(timeout=10000)
    frame.locator("#tab-all").click()
    frame.locator("#showdone").check()
    time.sleep(0.8)
    # onboarding.md 'Read the security policy @carol' is a company task alice can write.
    src = full(doc("onboarding.md"))
    line = next(i for i, t in enumerate(src.read_text().splitlines(), 1)
                if "Read the security policy" in t)
    before = src.read_text().splitlines()[line - 1]
    # Find that task's checkbox and click it. The artifact rebuilds every row
    # on each load (and once more 1.4 s after a toggle, to reconcile with the
    # index), so a click can land on a row that has just been replaced: take
    # the checkbox afresh and make sure the click registered before waiting on
    # the file.
    # The row must be THIS run's: the artifact lists every task the account
    # can read, and a leftover namespace on the box would otherwise put an
    # older copy of the same sentence first. Its `.loc` carries path:line.
    def row():
        return frame.locator(".task", has=frame.locator(f'.loc:text-is("{doc("onboarding.md")}:{line}")')).first

    def toggle_once():
        row().locator("input[type=checkbox]").click()
        for _ in range(15):
            if src.read_text().splitlines()[line - 1] != before:
                return True
            time.sleep(0.3)
        return False

    # Every row is rebuilt on each load (and once more 1.4 s after a toggle,
    # to reconcile with the index), so a click can land on a row that has
    # just been replaced: one retry, then it is a real failure.
    ok = toggle_once() or toggle_once()
    assert ok, "toggling in the todos artifact must flip the source file"
    # restore
    row().locator("input[type=checkbox]").click()
    for _ in range(15):
        if src.read_text().splitlines()[line - 1] == before:
            break
        time.sleep(0.3)
    ctx.close()
