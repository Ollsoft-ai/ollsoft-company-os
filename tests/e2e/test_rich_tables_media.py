"""Rich editing of the things markdown usually hides: tables are a real grid you
type into, video/audio embed and play, and clicking any of them NEVER swaps the
rendered thing for raw markdown (which re-flowed the page under the pointer).
The document itself stays plain GFM markdown — that is what everything else on
the platform reads."""
import time

import httpx
import pytest

from conftest import BASE, CREDS, login

# A tiny synthetic MP4 generated at test time. The point of these tests is the
# upload/scope/CSP path, not the codec — so we never depend on a real media file
# living somewhere on the box.
def _fixture_mp4(tmp_path_factory=None):
    import base64, tempfile, os
    # 133-byte minimal ISO-BMFF: ftyp + empty moov. Players reject it, but the
    # platform only ever stores, serves and permission-checks the bytes.
    data = base64.b64decode(
        "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAABIbW9vdgAAAGxtdmhkAAAAAAAA"
        "AAAAAAAAAAAAAAAAA+gAAAAAAAEAAAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAABAAAA"
        "AAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC"
    )
    fd, path = tempfile.mkstemp(suffix=".mp4", prefix="kb-test-")
    os.write(fd, data)
    os.close(fd)
    return path


SRC_MP4 = _fixture_mp4()

TABLE_DOC = """# Sprint

| Task | Owner |
| --- | --- |
| Ship it | alice |
| Review | carol |

after the table
"""


def api():
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/login", data={"username": "alice", "password": CREDS["alice"]})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="module")
def doc():
    c = api()
    rel = f"company/tbltest_{int(time.time())}.md"
    assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
    assert c.post("/api/artifact/write",
                  json={"path": rel, "content": TABLE_DOC}).status_code == 200
    yield rel
    c.post("/api/fs/delete", json={"path": rel})


@pytest.fixture(scope="module")
def video_doc():
    c = api()
    folder = f"company/vidembed_{int(time.time())}"
    assert c.post("/api/fs/mkdir", json={"path": folder}).status_code == 200
    with open(SRC_MP4, "rb") as fh:
        up = c.post(f"/api/upload?dir={folder}", files={"file": ("clip.mp4", fh, "video/mp4")})
    assert up.status_code == 200, up.text
    rel = f"{folder}/watch.md"
    assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
    body = "# Clip\n\n![the clip](_files/clip.mp4)\n\ntext after\n"
    assert c.post("/api/artifact/write",
                  json={"path": rel, "content": body}).status_code == 200
    yield rel
    c.post("/api/fs/delete", json={"path": folder})


def open_doc(browser, rel):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    page.goto(f"{BASE}/{rel}")
    page.wait_for_function(f"() => window.__kbpath === {rel!r}", timeout=15000)
    page.wait_for_selector(".cm-content", timeout=10000)
    return ctx, page


def doc_text(page):
    return page.evaluate("() => window.__kbview.state.doc.toString()")


def test_table_renders_as_a_grid_and_edits_write_markdown(browser, doc):
    ctx, page = open_doc(browser, doc)
    page.wait_for_selector("table.cm-table", timeout=10000)
    # header + two body rows, two columns
    assert page.locator("table.cm-table tr").count() == 3
    assert page.locator('table.cm-table input[data-cell="0,0"]').input_value() == "Task"

    # typing in a cell writes back into the markdown source
    cell = page.locator('table.cm-table input[data-cell="1,1"]')
    cell.click()
    cell.fill("peter")
    page.wait_for_timeout(700)          # debounced writeback
    src = doc_text(page)
    assert "| Ship it | peter |" in src, src
    assert "| Review | carol |" in src, "the other row must be untouched"

    # ...and the grid is still a grid: no raw pipes leaked into the view
    assert page.locator("table.cm-table").count() == 1
    ctx.close()


def test_clicking_a_table_does_not_reveal_markdown(browser, doc):
    """The complaint that started this: clicking rendered content turned it into
    `| a | b |` source and the page jumped."""
    ctx, page = open_doc(browser, doc)
    page.wait_for_selector("table.cm-table", timeout=10000)
    before = page.locator("table.cm-table").bounding_box()
    page.locator('table.cm-table input[data-cell="1,0"]').click()
    page.wait_for_timeout(400)
    assert page.locator("table.cm-table").count() == 1, "table turned back into markdown"
    after = page.locator("table.cm-table").bounding_box()
    assert abs(after["y"] - before["y"]) < 4, "the table moved when clicked"
    ctx.close()


def test_structural_buttons_add_and_remove(browser, doc):
    ctx, page = open_doc(browser, doc)
    page.wait_for_selector("table.cm-table", timeout=10000)
    page.locator('table.cm-table input[data-cell="1,0"]').click()
    page.locator('.cm-tbl-bar button:has-text("+ row")').click()
    page.wait_for_timeout(500)
    assert page.locator("table.cm-table tr").count() == 4
    page.locator('.cm-tbl-bar button:has-text("+ col")').click()
    page.wait_for_timeout(500)
    assert page.locator("table.cm-table tr").first.locator("th").count() == 3
    src = doc_text(page)
    assert src.count("|") > TABLE_DOC.count("|")
    assert "| --- | --- | --- |" in src, src
    ctx.close()


def test_video_embeds_and_survives_a_click(browser, video_doc):
    ctx, page = open_doc(browser, video_doc)
    page.wait_for_selector(".cm-img-embed video", timeout=15000)
    vid = page.locator(".cm-img-embed video")
    assert vid.count() == 1
    # it is a real, decodable video (metadata loaded => the src resolved)
    page.wait_for_function(
        "() => { const v = document.querySelector('.cm-img-embed video');"
        "        return v && v.readyState >= 1 && v.videoWidth > 0; }", timeout=20000)
    box = vid.bounding_box()
    vid.click(position={"x": 5, "y": 5})
    page.wait_for_timeout(400)
    assert page.locator(".cm-img-embed video").count() == 1, "click revealed markdown"
    after = page.locator(".cm-img-embed video").bounding_box()
    assert abs(after["y"] - box["y"]) < 4, "the video moved when clicked"
    assert "![the clip](_files/clip.mp4)" in doc_text(page), "source must be unchanged"
    ctx.close()


@pytest.fixture(scope="module")
def long_doc():
    """A table far below the fold. CodeMirror parses incrementally, so this one
    is NOT in the syntax tree when the document opens."""
    c = api()
    rel = f"company/longtbl_{int(time.time())}.md"
    body = "# Long\n\n" + ("filler paragraph line\n\n" * 400) + \
           "| A | B |\n| --- | --- |\n| x | y |\n"
    assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
    assert c.post("/api/artifact/write",
                  json={"path": rel, "content": body}).status_code == 200
    yield rel
    c.post("/api/fs/delete", json={"path": rel})


def test_table_below_the_fold_renders_when_scrolled_to(browser, long_doc):
    """Regression: the table decorations live in a StateField, which must also
    rebuild when the PARSER advances — not only when the document changes —
    or a table nobody has scrolled to yet stays raw pipes forever."""
    ctx, page = open_doc(browser, long_doc)
    page.wait_for_timeout(800)
    assert page.locator("table.cm-table").count() == 0, "the table starts off-screen"
    for _ in range(2):
        page.evaluate("() => { const s = document.querySelector('.cm-scroller');"
                      "        s.scrollTop = s.scrollHeight; }")
        page.wait_for_timeout(900)
    assert page.locator("table.cm-table").count() == 1, \
        "a table below the fold never rendered (parser progress ignored)"
    ctx.close()


def test_toolbar_inserts_a_table(browser, doc):
    ctx, page = open_doc(browser, doc)
    page.wait_for_selector("table.cm-table", timeout=10000)
    page.locator(".cm-content").click()
    page.keyboard.press("Control+End")
    page.click('#mdbar button[data-md="table"]')
    page.wait_for_timeout(600)
    assert page.locator("table.cm-table").count() == 2
    assert "| Column | Column |" in doc_text(page)
    ctx.close()
