"""Tree UI: create a folder with ⊞, a file inside it with ＋, then delete both
with ✕ — the app's own prompt/confirm dialogs handled like a real user would."""
import time

from conftest import dlg_fill, dlg_ok, login
from kbenv import doc


def test_create_folder_and_file_then_delete(browser):
    name = f"uifs_{int(time.time())}"
    ctx = browser.new_context()
    page = login(ctx, "alice")

    # new folder under company (⊞ appears on hover)
    row = page.locator('.tree-item[data-path="company"]')
    row.hover()
    row.locator('button[title="New folder here"]').click()
    dlg_fill(page, name)
    page.wait_for_selector(f'.tree-item[data-path=doc("{name}")]', timeout=8000)

    # new file inside it (＋) — opens as a tab when created
    row = page.locator(f'.tree-item[data-path=doc("{name}")]')
    row.hover()
    row.locator('button[title="New file here"]').click()
    dlg_fill(page, "note.md")
    page.wait_for_selector(f'.tab.active[data-path=doc("{name}/note.md")]', timeout=8000)

    # delete the file (✕) — its tab must retire too
    row = page.locator(f'.tree-item[data-path=doc("{name}/note.md")]')
    row.hover()
    row.locator("button.danger").click()
    dlg_ok(page)
    page.wait_for_selector(f'.tab[data-path=doc("{name}/note.md")]', state="detached", timeout=8000)
    page.wait_for_selector(f'.tree-item[data-path=doc("{name}/note.md")]', state="detached", timeout=8000)

    # delete the folder
    row = page.locator(f'.tree-item[data-path=doc("{name}")]')
    row.hover()
    row.locator("button.danger").click()
    dlg_ok(page)
    page.wait_for_selector(f'.tree-item[data-path=doc("{name}")]', state="detached", timeout=8000)
    ctx.close()
