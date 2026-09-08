"""The rich (rendered-but-editable) markdown editor.

The rendered mode is a decoration layer over the SAME Y.Text markdown, so every
assertion here also implies the source stays canonical (multiplayer, indexer and
todos keep working). Covers: live rendering, the cursor-reveals-syntax model,
the mode switch (+ persistence), the toolbar, checkbox write-back, and — the
part the brief cares most about — drag-drop / screenshot-paste of files & images
rendered in place with the bytes actually stored and served.
"""
import base64
import json
import time

import httpx
from conftest import BASE, CREDS, dlg_fill, expand_folder, login
from kbenv import U, doc as kbdoc

# a real 2x2 red PNG (so naturalWidth>0 proves it decoded, not just embedded)
RED_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGP8"
               "z8Dwn4EIwDiqEAAqfQb0hZTVfwAAAABJRU5ErkJggg==")


def api(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


def new_doc(page, path):
    page.click('[data-testid="newdoc"]')
    dlg_fill(page, path)
    page.wait_for_function(f"() => window.__kbview && window.__kbpath === '{path}'", timeout=10000)


def set_source(page, text):
    page.evaluate("(t) => window.__kbview.dispatch({changes:{from:0,to:window.__kbview.state.doc.length,insert:t}})", text)
    page.wait_for_timeout(250)


def source(page):
    return page.evaluate("() => window.__kbview.state.doc.toString()")


def move_cursor(page, pos):
    page.evaluate("(p) => window.__kbview.dispatch({selection:{anchor:p}})", pos)
    page.wait_for_timeout(150)


def cleanup(paths):
    c = api("alice")
    for p in paths:
        c.post("/api/fs/delete", json={"path": p})


def test_rich_default_renders_constructs(browser):
    doc = kbdoc(f"rich_r_{int(time.time())}.md")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        # Rich is the default mode: switch + toolbar are visible on a writable doc.
        assert not page.locator("#modeswitch").is_hidden()
        assert page.locator("#mode-rich.active").count() == 1
        assert not page.locator("#mdbar").is_hidden()

        set_source(page,
                   "# Heading\n\nA **bold** word, an *italic*, and ~~struck~~.\n\n"
                   "- [ ] open task\n- [x] done task\n- plain bullet\n\n"
                   "> a quote\n\n[link text](https://example.com)\n\n---\n")
        move_cursor(page, source(page).__len__())  # cursor at end, off every construct

        assert page.locator(".cm-h1").count() == 1
        assert page.locator(".cm-h1").inner_text().strip() == "Heading"  # '#' hidden
        assert page.locator(".cm-task-toggle").count() == 2
        assert page.locator(".cm-task-toggle:checked").count() == 1
        assert page.locator(".cm-bullet").count() == 1
        assert page.locator(".cm-md-link").count() == 1
        assert page.locator(".cm-md-link").inner_text() == "link text"  # url + []() hidden
    finally:
        cleanup([doc])
        ctx.close()


def test_cursor_reveals_syntax(browser):
    doc = kbdoc(f"rich_c_{int(time.time())}.md")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        set_source(page, "# Title\n\nbody\n")
        move_cursor(page, source(page).__len__())
        assert page.locator(".cm-h1").inner_text().strip() == "Title"
        # put the cursor on the heading line -> the raw '# ' comes back
        move_cursor(page, 2)
        assert "#" in page.locator(".cm-h1").inner_text()
    finally:
        cleanup([doc])
        ctx.close()


def test_mode_switch_persists_across_reload(browser):
    doc = kbdoc(f"rich_m_{int(time.time())}.md")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        page.click('[data-testid="mode-source"]')
        page.wait_for_timeout(200)
        assert page.locator(".cm-lineNumbers").count() > 0, "source mode shows line numbers"
        assert page.locator("#mdbar").is_hidden(), "toolbar is rich-only"

        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item')
        page.click(f'.tree-item[data-path="{doc}"]')
        page.wait_for_function("() => window.__kbview")
        page.wait_for_timeout(300)
        assert page.locator("#mode-source.active").count() == 1, "mode choice persisted"
    finally:
        cleanup([doc])
        ctx.close()


def test_toolbar_bold_and_heading(browser):
    doc = kbdoc(f"rich_t_{int(time.time())}.md")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        set_source(page, "make me bold\n")
        # select "bold" (chars 8..12) and hit the B button
        page.evaluate("() => window.__kbview.dispatch({selection:{anchor:8,head:12}})")
        page.click('#mdbar button[data-md="bold"]')
        page.wait_for_timeout(150)
        assert "make me **bold**" in source(page)

        # heading toggle on line 1
        page.evaluate("() => window.__kbview.dispatch({selection:{anchor:0}})")
        page.click('#mdbar button[data-md="h2"]')
        page.wait_for_timeout(150)
        assert source(page).startswith("## make me")
        # pressing the same heading again removes it
        page.click('#mdbar button[data-md="h2"]')
        page.wait_for_timeout(150)
        assert source(page).startswith("make me")
    finally:
        cleanup([doc])
        ctx.close()


def test_toolbar_task_list(browser):
    doc = kbdoc(f"rich_tl_{int(time.time())}.md")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        set_source(page, "buy milk\n")
        page.evaluate("() => window.__kbview.dispatch({selection:{anchor:0}})")
        page.click('#mdbar button[data-md="task"]')
        page.wait_for_timeout(200)
        assert source(page).startswith("- [ ] buy milk")
        move_cursor(page, source(page).__len__())  # off the line so the box renders
        assert page.locator(".cm-task-toggle").count() == 1
    finally:
        cleanup([doc])
        ctx.close()


def test_checkbox_toggle_writes_back_to_source(browser):
    doc = kbdoc(f"rich_cb_{int(time.time())}.md")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        set_source(page, "- [ ] task one\n- [ ] task two\n")
        move_cursor(page, source(page).__len__())
        page.locator(".cm-task-toggle").first.click()
        page.wait_for_timeout(200)
        assert source(page).startswith("- [x] task one"), source(page)
        # and the checked state shows in the DOM
        assert page.locator(".cm-task-toggle:checked").count() == 1
    finally:
        cleanup([doc])
        ctx.close()


def test_screenshot_paste_stores_and_renders(browser):
    doc = kbdoc(f"rich_paste_{int(time.time())}.md")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        set_source(page, "# Report\n\nEvidence:\n")
        move_cursor(page, source(page).__len__())
        src = page.evaluate("""async (b64) => {
          const bin=atob(b64),arr=new Uint8Array(bin.length);
          for(let i=0;i<bin.length;i++)arr[i]=bin.charCodeAt(i);
          const dt=new DataTransfer(); dt.items.add(new File([arr],'image.png',{type:'image/png'}));
          document.querySelector('.cm-content').dispatchEvent(
            new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));
          await new Promise(r=>setTimeout(r,1400));
          return window.__kbview.state.doc.toString();
        }""", RED_PNG_B64)
        assert "![screenshot](_files/pasted-" in src, src
        # move off the image so the widget renders (cursor reveals raw md)
        page.evaluate("() => window.__kbview.dispatch({selection:{anchor:0}})")
        page.wait_for_timeout(400)
        assert page.locator(".cm-img-embed img").count() == 1
        nat = page.evaluate("() => document.querySelector('.cm-img-embed img').naturalWidth")
        assert nat == 2, f"image must actually decode (2x2 png), got {nat}"

        # the bytes are really stored and served through the attachment endpoint
        import re
        link = re.search(r"\((_files/[^)]+)\)", src).group(1)
        r = api("alice").get("/api/attachment", params={"path": kbdoc(link)})
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
        cleanup([doc, kbdoc(link)])
    finally:
        ctx.close()


def test_file_drop_inserts_link_not_image(browser):
    doc = kbdoc(f"rich_drop_{int(time.time())}.md")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        set_source(page, "attached:\n")
        move_cursor(page, source(page).__len__())
        src = page.evaluate("""async () => {
          const dt=new DataTransfer();
          dt.items.add(new File(['hello world'],'notes.txt',{type:'text/plain'}));
          const box=document.querySelector('.cm-content').getBoundingClientRect();
          const ev=new DragEvent('drop',{bubbles:true,cancelable:true,
            clientX:box.left+20,clientY:box.top+8});
          Object.defineProperty(ev,'dataTransfer',{value:dt});
          document.querySelector('.cm-content').dispatchEvent(ev);
          await new Promise(r=>setTimeout(r,1200));
          return window.__kbview.state.doc.toString();
        }""")
        assert "[notes.txt](_files/notes.txt)" in src, src
        assert "![notes.txt]" not in src, "a non-image must be a plain link, not an embed"
        cleanup([doc, kbdoc("_files/notes.txt")])
    finally:
        ctx.close()


# Dragging a row OUT of the tree and into the text links the file that is
# already there — the other half of drag-drop (a desktop file uploads, a tree
# row links). Driven as a real drag: the DataTransfer the tree row fills on
# dragstart is the exact object handed to the editor's drop.
DRAG_LINK_JS = """async ([src, xoff]) => {
  const dt = new DataTransfer();
  const row = document.querySelector('.tree-item[data-path="' + src + '"]');
  if (!row) return {err: 'no tree row for ' + src};
  if (!row.draggable) return {err: 'row is not draggable'};
  const ds = new DragEvent('dragstart', {bubbles: true, cancelable: true});
  Object.defineProperty(ds, 'dataTransfer', {value: dt});
  row.dispatchEvent(ds);
  const carried = dt.getData('application/x-kb-path');

  const content = document.querySelector('.cm-content');
  const box = content.getBoundingClientRect();
  const ev = new DragEvent('drop', {bubbles: true, cancelable: true,
    clientX: box.left + xoff, clientY: box.top + 8});
  Object.defineProperty(ev, 'dataTransfer', {value: dt});
  content.dispatchEvent(ev);
  await new Promise(r => setTimeout(r, 400));
  return {carried, src: window.__kbview.state.doc.toString()};
}"""


def test_tree_row_dropped_into_doc_links_it(browser):
    tag = f"linkdrop_{int(time.time())}"
    folder = kbdoc(f"{tag}")
    doc = f"{folder}/deep/note.md"
    c = api("alice")
    assert c.post("/api/fs/mkdir", json={"path": folder}).status_code == 200
    assert c.post("/api/fs/mkdir", json={"path": f"{folder}/deep"}).status_code == 200
    # a name with a space: the link target has to be percent-encoded or the
    # markdown parser never sees a link at all
    assert c.post("/api/file", json={"path": f"{folder}/spec notes.md"}).status_code in (200, 409)
    up = c.post(f"/api/upload?dir={folder}",
                files={"file": ("pic.png", base64.b64decode(RED_PNG_B64), "image/png")})
    assert up.status_code == 200, up.text

    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        set_source(page, "see also:\n")
        move_cursor(page, len(source(page)))

        # a sibling document: plain link, ".md" dropped from the label, "../"
        # climbed out of deep/, the space encoded
        r = page.evaluate(DRAG_LINK_JS, [f"{folder}/spec notes.md", 20])
        assert not r.get("err"), r
        assert r["carried"] == f"{folder}/spec notes.md", r
        assert "[spec notes](../spec%20notes.md)" in r["src"], r["src"]

        # an image: embeds, on its own block
        expand_folder(page, f"{folder}/_files")
        move_cursor(page, len(source(page)))
        r = page.evaluate(DRAG_LINK_JS, [f"{folder}/_files/pic.png", 20])
        assert not r.get("err"), r
        assert "![pic.png](../_files/pic.png)" in r["src"], r["src"]

        # the payoff of writing the link RELATIVE: it actually renders. An
        # absolute path would be handed to the browser as a literal src.
        page.wait_for_timeout(600)
        assert page.evaluate(
            "() => {const i=document.querySelector('.cm-img-embed img');"
            " return i && i.complete && i.naturalWidth > 0;}"), "embed did not load"

        # ...and the document link opens the document as a tab, not a download
        page.evaluate("""() => {
          const i = window.__kbview.state.doc.toString().indexOf('spec notes]');
          const c = window.__kbview.coordsAtPos(i);
          const ev = new MouseEvent('dblclick', {bubbles: true, cancelable: true,
            clientX: c.left + 1, clientY: (c.top + c.bottom) / 2});
          document.querySelector('.cm-content').dispatchEvent(ev);
        }""")
        page.wait_for_function(
            f"() => window.__kbpath === '{folder}/spec notes.md'", timeout=8000)
    finally:
        ctx.close()
        c.post("/api/fs/delete", json={"path": folder})


def test_readonly_doc_has_no_toolbar_and_locked_checkboxes(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        # .claude/CLAUDE.md is root:kb-users 644 -> read-only to everyone
        page.click('[data-testid="hidden-toggle"]')   # dot-entries hidden by default
        expand_folder(page, ".claude")                # .claude auto-collapses
        page.click('.tree-item[data-path=".claude/CLAUDE.md"]')
        page.wait_for_function("() => window.__kbview && window.__kbview.state.doc.length > 0", timeout=10000)
        page.wait_for_timeout(300)
        assert page.locator("#mdbar").is_hidden(), "no editing toolbar on a read-only doc"
        # the mode switch is still offered (you can read as rendered or source)
        assert not page.locator("#modeswitch").is_hidden()
        # any rendered checkboxes are disabled
        boxes = page.locator(".cm-task-toggle")
        for i in range(boxes.count()):
            assert boxes.nth(i).is_disabled()
    finally:
        ctx.close()


def test_fenced_code_block_renders_with_copy_button(browser):
    """``` fences render as a styled block whose copy button puts the code —
    and only the code, not the fences — on the clipboard."""
    doc = kbdoc(f"codeblk_{int(time.time())}.md")
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        code = "print('hi')\nx = 1"
        set_source(page, f"# t\n\n```py\n{code}\n```\n\ntail\n")
        # render can lag under a heavy suite; give the live-preview time to paint
        page.wait_for_selector(".cm-codeblock-first", timeout=12000)
        page.wait_for_selector(".cm-codeblock-last", timeout=5000)
        btn = page.locator(".cm-code-copy")
        assert btn.count() == 1
        btn.click()
        page.wait_for_selector(".cm-code-copy.done", timeout=3000)
        assert page.evaluate("() => navigator.clipboard.readText()") == code
    finally:
        cleanup([doc])
        ctx.close()


def test_spaces_in_a_dropped_filename_still_make_a_link(browser):
    """A dropped file whose name has spaces. Two halves:

    the link WRITTEN must be percent-encoded (CommonMark refuses a bare space
    in a destination, so "[x](_files/a b.xlsx)" is not a link at all — it sat
    in documents as plain unclickable text), and the raw-space form already
    written into existing documents must still render and open, because those
    links are not going to re-encode themselves.
    """
    stamp = int(time.time())
    doc = kbdoc(f"rich_space_{stamp}.md")
    name = f"tender eval {stamp} with comments.xlsx"
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, doc)
        set_source(page, "attached:\n")
        move_cursor(page, len(source(page)))
        src = page.evaluate("""async (name) => {
          const dt=new DataTransfer();
          dt.items.add(new File(['x'],name,{type:'application/vnd.ms-excel'}));
          const box=document.querySelector('.cm-content').getBoundingClientRect();
          const ev=new DragEvent('drop',{bubbles:true,cancelable:true,
            clientX:box.left+20,clientY:box.top+8});
          Object.defineProperty(ev,'dataTransfer',{value:dt});
          document.querySelector('.cm-content').dispatchEvent(ev);
          await new Promise(r=>setTimeout(r,1500));
          return window.__kbview.state.doc.toString();
        }""", name)
        encoded = name.replace(" ", "%20")
        assert f"[{name}](_files/{encoded})" in src, src
        move_cursor(page, len(source(page)))
        page.wait_for_timeout(300)
        assert page.locator(".cm-md-link").count() == 1, "encoded link must render"
        assert page.locator(".cm-md-link").inner_text() == name

        # the legacy form: raw spaces, exactly what used to be written
        set_source(page, f"see [{name}](_files/{name}) here\n")
        move_cursor(page, len(source(page)))
        page.wait_for_timeout(300)
        assert page.locator(".cm-md-link").count() == 1, "raw-space link must render"
        assert page.locator(".cm-md-link").get_attribute("data-url") == f"_files/{name}"
        # and it resolves to the file that was actually uploaded
        page.dblclick(".cm-md-link")
        page.wait_for_timeout(500)
        assert api("alice").get("/api/attachment",
                                params={"path": kbdoc(f"_files/{name}")}).status_code == 200
    finally:
        cleanup([doc, kbdoc(f"_files/{name}")])
        ctx.close()


def test_internal_md_link_opens_a_tab_not_a_download(browser):
    """Following a link to another note must OPEN it. Every internal link used
    to go to /api/attachment, so a phone offered to download the markdown
    instead of opening it — and ../ was never resolved, so a sibling link could
    not have found the file anyway."""
    stamp = int(time.time())
    folder = kbdoc(f"linkdir_{stamp}")
    target = kbdoc(f"linked note {stamp}.md")          # a space -> %20 in the link
    doc = f"{folder}/linkfrom_{stamp}.md"
    api("alice").post("/api/fs/mkdir", json={"path": folder})
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, target)
        set_source(page, "# The linked note\n")
        new_doc(page, doc)
        # links the way an agent writes them: relative, ../, percent-encoded
        href = f"../linked%20note%20{stamp}.md"
        set_source(page, f"see [the note]({href}) and "
                         f"[its folder](../linkdir_{stamp})\n")
        move_cursor(page, len(source(page)))
        page.wait_for_timeout(300)
        page.dblclick(".cm-md-link >> nth=0")
        page.wait_for_function(f'() => window.__kbpath === "{target}"', timeout=8000)
        assert page.locator(f'.tab[data-path="{target}"]').count() == 1
        assert page.evaluate("() => window.__kbkind") == "doc"
        # a link to a folder shows it in the tree instead of doing nothing
        page.click(f'.tab[data-path="{doc}"]')
        page.wait_for_function(f'() => window.__kbpath === "{doc}"')
        # the click left the cursor inside the first link, which reveals its raw
        # syntax — step off it so both links are rendered again
        move_cursor(page, len(source(page)))
        page.wait_for_timeout(300)
        page.dblclick(".cm-md-link >> nth=1")
        page.wait_for_selector(f'.tree-item[data-path="{folder}"]', timeout=5000)
        assert page.evaluate("() => window.__kbpath") == doc   # no navigation away
    finally:
        cleanup([doc, target, folder])
        ctx.close()
