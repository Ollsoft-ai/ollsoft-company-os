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
    page.keyboard.press("Alt+N")
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
        # AGENTS.md is root:kb-users 644 -> read-only to everyone
        page.click('.tree-item[data-path="AGENTS.md"]')
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


def test_inline_code_carries_its_own_copy_button(browser):
    """A `code span` gets the same one-click copy a fenced block has — copying
    the code and not the backticks, and without putting anything in the file."""
    docp = kbdoc(f"inlinecopy_{int(time.time())}.md")
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = login(ctx, "alice")
    try:
        new_doc(page, docp)
        snippet = "kb-history --author me"
        set_source(page, f"run `{snippet}` first\n\nand a line with no code at all\n")
        page.wait_for_selector('[data-testid="inline-copy"]', timeout=12000)
        icons = page.locator('[data-testid="inline-copy"]')
        assert icons.count() == 1, "one code span, one icon — prose gets none"
        icons.first.click()
        page.wait_for_selector(".cm-inline-copy.done", timeout=3000)
        assert page.evaluate("() => navigator.clipboard.readText()") == snippet
        # the affordance is a decoration, never text: the document is untouched
        assert source(page) == f"run `{snippet}` first\n\nand a line with no code at all\n"
    finally:
        cleanup([docp])
        ctx.close()


def test_tagged_people_are_coloured_and_nothing_else_is(browser):
    """@someone lights up only when "someone" is a real account here, and never
    inside code — the colour has to mean the same thing the to-do index means."""
    docp = kbdoc(f"mention_{int(time.time())}.md")
    real = U("bob")
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, docp)
        set_source(page,
                   f"- [ ] ship it @{real} please\n\n"
                   f"not a person: @nobody_by_that_name_here\n\n"
                   f"`@{real}` inside code is just text\n")
        page.wait_for_selector(".cm-mention", timeout=12000)
        names = page.locator(".cm-mention").evaluate_all(
            "els => els.map(e => e.getAttribute('data-mention'))")
        assert names == [real], f"expected only @{real} to light up, got {names}"
        assert page.locator(".cm-mention").first.text_content() == "@" + real
    finally:
        cleanup([docp])
        ctx.close()


def test_your_own_tag_glows_and_a_colleagues_does_not(browser):
    """Being tagged yourself is the one mention you must not scroll past, so it
    carries its own class — and reading the SAME document as someone else must
    move the glow onto their name instead."""
    docp = kbdoc(f"mentionme_{int(time.time())}.md")
    alice, bob = U("alice"), U("bob")
    body = f"- [ ] @{alice} and @{bob} both\n"
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, docp)
        set_source(page, body)
        page.wait_for_selector(".cm-mention-me", timeout=12000)
        glowing = page.locator(".cm-mention-me").evaluate_all(
            "els => els.map(e => e.getAttribute('data-mention'))")
        assert glowing == [alice], f"alice should see only her own tag glow: {glowing}"
        # a glow, not just another colour: the box-shadow has to actually render
        shadow = page.locator(".cm-mention-me").first.evaluate(
            "e => getComputedStyle(e).boxShadow")
        assert shadow and shadow != "none", "the 'this is you' mention has no glow"

        # the same document, read by bob: the glow moves
        bctx = browser.new_context()
        bpage = login(bctx, "bob")
        try:
            bpage.goto(f"{BASE}/{docp}")
            bpage.wait_for_function(
                f"() => window.__kbview && window.__kbpath === '{docp}'", timeout=15000)
            bpage.wait_for_selector(".cm-mention-me", timeout=12000)
            assert bpage.locator(".cm-mention-me").evaluate_all(
                "els => els.map(e => e.getAttribute('data-mention'))") == [bob]
        finally:
            bctx.close()
    finally:
        cleanup([docp])
        ctx.close()


def paste_text(page, text):
    """A real paste event carrying text/plain, the way a browser delivers one."""
    page.evaluate("""(t) => {
        const dt = new DataTransfer();
        dt.setData('text/plain', t);
        window.__kbview.contentDOM.dispatchEvent(
            new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
    }""", text)
    page.wait_for_timeout(250)


def test_a_pasted_url_becomes_a_link(browser):
    """Over a selection it links that selection; on its own it links to itself.
    A bare URL is not a link in this dialect, so pasting one used to leave dead
    text that rendered as dead text."""
    docp = kbdoc(f"pastelink_{int(time.time())}.md")
    url = "https://example.com/a/b?q=1#frag"
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, docp)

        # on its own: links to itself
        set_source(page, "see \n")
        move_cursor(page, 4)
        paste_text(page, url)
        assert source(page) == f"see [{url}]({url})\n", source(page)

        # over a selection: that selection becomes the link text
        set_source(page, "read the docs here\n")
        page.evaluate("() => window.__kbview.dispatch({selection:{anchor:9,head:13}})")
        page.wait_for_timeout(150)
        paste_text(page, url)
        assert source(page) == f"read the [docs]({url}) here\n", source(page)
    finally:
        cleanup([docp])
        ctx.close()


def test_a_pasted_url_is_left_alone_where_it_is_content(browser):
    """Inside a code span or a fenced block a URL is content, and whitespace or
    a non-URL is an ordinary paste — CodeMirror's job, not ours."""
    docp = kbdoc(f"pasteraw_{int(time.time())}.md")
    url = "https://example.com/x"
    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        new_doc(page, docp)

        set_source(page, "run `x` here\n")
        move_cursor(page, 5)                       # inside the code span
        paste_text(page, url)
        assert source(page) == f"run `{url}x` here\n", source(page)

        set_source(page, "```\ncode\n```\n")
        move_cursor(page, 4)                       # inside the fence
        paste_text(page, url)
        assert source(page) == f"```\n{url}code\n```\n", source(page)

        # not a URL at all
        set_source(page, "x\n")
        move_cursor(page, 1)
        paste_text(page, "just some words")
        assert source(page) == "xjust some words\n", source(page)
    finally:
        cleanup([docp])
        ctx.close()


def test_a_list_hangs_and_its_wrapped_lines_line_up(browser):
    """A list item is one block: the marker sits in the margin, and every row
    after it — a soft wrap, or a line the item continues onto because the file
    is hard-wrapped — starts where the item's text starts."""
    path = kbdoc(f"hang_{int(time.time())}.md")
    ctx = browser.new_context(viewport={"width": 380, "height": 900})   # narrow: everything wraps
    page = login(ctx, "alice")
    try:
        new_doc(page, path)
        set_source(page,
                   "- **Ollsoft** is the technical company that owns the source code,\n"
                   "  the architecture and the brand, and ships every release.\n"
                   "- One item long enough to wrap by itself on a narrow pane with no hard break at all.\n"
                   "\n"
                   "1. A numbered point whose text continues in the file\n"
                   "   on the next line, indented by three spaces.\n"
                   "\n"
                   "- [ ] A task whose text also continues\n"
                   "  onto the next line of the file.\n")
        # the cursor reveals the syntax of the line it is on: park it at the end
        page.evaluate("() => window.__kbview.dispatch({selection: {anchor: window.__kbview.state.doc.length}})")
        page.wait_for_timeout(500)
        rows = page.evaluate("""() => [...document.querySelectorAll('.cm-rich .cm-line')].map((l) => {
          // from the first VISIBLE character: a line's own leading spaces are
          // real text, and measuring them would hide the alignment
          const r = document.createRange(); r.selectNodeContents(l);
          const w = document.createTreeWalker(l, NodeFilter.SHOW_TEXT);
          let n2;
          while ((n2 = w.nextNode())) { const i = n2.data.search(/\S/); if (i >= 0) { r.setStart(n2, i); break; } }
          // one entry per VISUAL ROW: the left edge of its leftmost fragment
          const byTop = new Map();
          for (const b of [...r.getClientRects()].filter((x) => x.width > 1)) {
            const k = Math.round(b.top / 8);   // one key per visual row, not per fragment box
            byTop.set(k, Math.min(byTop.has(k) ? byTop.get(k) : 1e9, Math.round(b.left)));
          }
          const marker = l.querySelector('.cm-bullet, .cm-task, .cm-olmark');
          return {cls: l.className, txt: l.textContent.trim().slice(0, 20),
                  markerLeft: marker ? Math.round(marker.getBoundingClientRect().left) : null,
                  rows: [...byTop.values()]}; }).filter((r) => r.txt)""")
        items, cur = [], None
        for r in rows:
            if "cm-listline" in r["cls"]:
                cur = {"item": r, "cont": []}
                items.append(cur)
            elif "cm-listcont" in r["cls"] and cur:
                cur["cont"].append(r)
        assert len(items) == 4, rows
        assert sum(len(i["cont"]) for i in items) == 3, rows
        for it in items:
            item = it["item"]
            assert item["markerLeft"] is not None, ("the marker is rendered", item)
            text_rows = [x for x in item["rows"] if x > item["markerLeft"]]
            assert text_rows, item
            assert max(text_rows) - min(text_rows) <= 2, ("an item's rows line up", item)
            for cont in it["cont"]:
                assert max(cont["rows"]) - min(cont["rows"]) <= 2, ("every row of a continuation", cont)
                assert abs(min(cont["rows"]) - min(text_rows)) <= 2, ("under the item's text", cont, item)
    finally:
        cleanup([path])
        ctx.close()


def test_a_click_lands_on_the_line_you_clicked_even_below_a_table(browser):
    """A block widget's spacing has to be inside its box.

    CodeMirror measures a widget with getBoundingClientRect, which excludes
    margins, so a vertical margin on the table (or image) widget makes every
    line below it paint lower than the editor believes — and a click then
    puts the caret one line off. Clicking the first checkbox and landing on
    the second is how it showed up (2026-09-22).
    """
    name = f"clickmap_{int(time.time())}.md"
    body = (
        "# Head\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
        "Plain paragraph one that is long enough to click into comfortably\n\n"
        "- Bullet one that is long enough to click into comfortably here\n\n"
        "- [ ] Task one that is long enough to click into comfortably here\n"
        "- [ ] Task two that is long enough to click into comfortably here\n\n"
        "> Quote one that is long enough to click into comfortably here\n\n"
        "Final paragraph that is long enough to click into comfortably\n"
    )
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = login(ctx, "alice")
    c = httpx.Client(base_url=BASE, timeout=20)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    assert c.post("/api/artifact/write", json={"path": kbdoc(name), "content": body}).status_code == 200
    try:
        page.goto(BASE + "/" + kbdoc(name))
        page.wait_for_function("() => window.__kbview && window.__kbview.state.doc.length > 10", timeout=20000)
        page.wait_for_timeout(900)
        bad = page.evaluate("""() => {
          const v = window.__kbview, out = [];
          for (const el of document.querySelectorAll('.cm-line')) {
            const shown = (el.textContent || '').trim();
            if (!shown) continue;
            const r = el.getBoundingClientRect();
            const pos = v.posAtCoords({x: r.left + Math.min(150, r.width - 20), y: r.top + r.height / 2});
            if (pos == null) { out.push([shown, '(nothing)']); continue; }
            const line = v.state.doc.lineAt(pos).text.trim();
            // the painted line may drop markup (- [ ], >, the bullet glyph),
            // so compare on the words that survive both
            const words = shown.replace(/^[•\u2022\s]+/, '').split(' ').slice(0, 3).join(' ');
            if (words && !line.includes(words)) out.push([shown.slice(0, 30), line.slice(0, 30)]);
          }
          return out;
        }""")
        assert bad == [], f"clicks landed on the wrong line: {bad}"
    finally:
        c.post("/api/fs/delete", json={"path": kbdoc(name), "permanent": True})
        ctx.close()


def test_ctrl_click_and_middle_click_open_a_linked_document(browser):
    """krystof, 2026-09-22: "why does middle mouse click or ctrl plus click on
    link like a mrkdown file linked in the editor not open it? only double
    click opens it?"

    Because both were handled on `click`, and the mousedown before it moves the
    selection into the link — which reveals `[label](url)` and re-flows the
    line, so the click's coordinates resolved somewhere else entirely. The
    second click of a double-click worked because by then the syntax was
    already showing.
    """
    c = api("alice")
    folder = kbdoc(f"linkopen_{int(time.time())}")
    assert c.post("/api/fs/mkdir", json={"path": folder}).status_code == 200
    target, home = f"{folder}/target.md", f"{folder}/home.md"
    for p, body in ((target, "# Target\n\nyou arrived\n"),
                    (home, "# Home\n\nsee [the target](target.md) for more.\n")):
        assert c.post("/api/file", json={"path": p}).status_code in (200, 409)
        assert c.post("/api/artifact/write",
                      json={"path": p, "content": body}).status_code == 200

    ctx = browser.new_context()
    page = login(ctx, "alice")

    def open_home():
        page.goto(f"{BASE}/{home}")
        page.wait_for_function(f"() => window.__kbpath === {home!r}", timeout=15000)
        page.wait_for_selector(".cm-md-link", timeout=10000)

    def link_point():
        # the middle of the rendered label, in page coordinates
        box = page.locator(".cm-md-link").first.bounding_box()
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    try:
        open_home()
        x, y = link_point()
        page.keyboard.down("Control")
        page.mouse.click(x, y)
        page.keyboard.up("Control")
        page.wait_for_function(f"() => window.__kbpath === {target!r}", timeout=8000)

        open_home()
        x, y = link_point()
        page.mouse.click(x, y, button="middle")
        page.wait_for_function(f"() => window.__kbpath === {target!r}", timeout=8000)

        # …and the middle button pasted nothing into the document it left
        # (on Linux it pastes the X selection into an editable area)
        body = c.get("/api/file", params={"path": home}).text
        assert body.count("see [the target](target.md) for more.") == 1, body
    finally:
        ctx.close()
        c.post("/api/fs/delete", json={"path": folder, "permanent": True})
