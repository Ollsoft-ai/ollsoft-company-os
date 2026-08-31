"""Tree UI: create a folder with ⊞, a file inside it with ＋, then delete both
with ✕ — the app's own prompt/confirm dialogs handled like a real user would."""
import time

from conftest import dlg_fill, dlg_ok, login
from kbenv import AREA, doc


def test_create_folder_and_file_then_delete(browser):
    name = f"uifs_{int(time.time())}"
    ctx = browser.new_context()
    page = login(ctx, "alice")

    # new folder under company (⊞ appears on hover)
    row = page.locator(f'.tree-item[data-path="{AREA}"]')
    row.hover()
    row.locator('button[title="New folder here"]').click()
    dlg_fill(page, name)
    page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=8000)

    # new file inside it (＋) — opens as a tab when created
    row = page.locator(f'.tree-item[data-path="{doc(name)}"]')
    row.hover()
    row.locator('button[title="New file here"]').click()
    dlg_fill(page, "note.md")
    page.wait_for_selector(f'.tab.active[data-path="{doc(f"{name}/note.md")}"]', timeout=8000)

    # delete the file (✕) — its tab must retire too
    row = page.locator(f'.tree-item[data-path="{doc(f"{name}/note.md")}"]')
    row.hover()
    row.locator("button.danger").click()
    dlg_ok(page)
    page.wait_for_selector(f'.tab[data-path="{doc(f"{name}/note.md")}"]', state="detached", timeout=8000)
    page.wait_for_selector(f'.tree-item[data-path="{doc(f"{name}/note.md")}"]', state="detached", timeout=8000)

    # delete the folder
    row = page.locator(f'.tree-item[data-path="{doc(name)}"]')
    row.hover()
    row.locator("button.danger").click()
    dlg_ok(page)
    page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', state="detached", timeout=8000)
    ctx.close()


def test_file_rows_show_a_last_modified_stamp_and_newest_sits_on_top(browser):
    """The tree orders files newest-first; the stamp is what makes that visible.
    Folders keep their alphabetical place and carry no stamp."""
    name = f"uimt_{int(time.time())}"
    ctx = browser.new_context()
    page = login(ctx, "alice")

    row = page.locator(f'.tree-item[data-path="{AREA}"]')
    row.hover()
    row.locator('button[title="New folder here"]').click()
    dlg_fill(page, name)
    page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', timeout=8000)
    try:
        # oldest first, names deliberately in the opposite order
        for f in ("a_old.md", "b_new.md"):
            r = page.locator(f'.tree-item[data-path="{doc(name)}"]')
            r.hover()
            r.locator('button[title="New file here"]').click()
            dlg_fill(page, f)
            page.wait_for_selector(f'.tree-item[data-path="{doc(f"{name}/{f}")}"]', timeout=8000)
            time.sleep(1.2)   # mtime resolution is one second

        # a today file stamps the time of day — non-empty, and short
        stamp = page.locator(f'.tree-item[data-path="{doc(f"{name}/b_new.md")}"] .tmtime')
        page.wait_for_function(
            """p => { const e = document.querySelector(
                    '.tree-item[data-path="' + CSS.escape(p) + '"] .tmtime');
                 return e && e.textContent.trim().length > 0; }""",
            arg=doc(f"{name}/b_new.md"), timeout=12000)
        assert len(stamp.inner_text().strip()) <= 12
        assert "Last modified" in stamp.get_attribute("title")

        # newest on top, despite sorting last by name
        order = page.eval_on_selector_all(
            f'.tree-item[data-path^="{doc(name)}/"]', "els => els.map(e => e.dataset.path)")
        assert order == [doc(f"{name}/b_new.md"), doc(f"{name}/a_old.md")], order

        # the folder itself carries no stamp
        assert page.locator(f'.tree-item[data-path="{doc(name)}"] .tmtime').count() == 0
    finally:
        r = page.locator(f'.tree-item[data-path="{doc(name)}"]')
        r.hover()
        r.locator("button.danger").click()
        dlg_ok(page)
        page.wait_for_selector(f'.tree-item[data-path="{doc(name)}"]', state="detached", timeout=8000)
        ctx.close()
