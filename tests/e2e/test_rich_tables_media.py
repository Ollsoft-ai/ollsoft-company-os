"""Rich editing of the things markdown usually hides: tables are a real grid you
type into, video/audio embed and play, and clicking any of them NEVER swaps the
rendered thing for raw markdown (which re-flowed the page under the pointer).
The document itself stays plain GFM markdown — that is what everything else on
the platform reads."""
import time

import httpx
import pytest

from conftest import BASE, CREDS, login
from kbenv import U, doc as kbdoc

# A tiny synthetic MP4 generated at test time. The point of these tests is the
# upload/scope/CSP path, not the codec — so we never depend on a real media file
# living somewhere on the box.
def _fixture_mp4(tmp_path_factory=None):
    # A REAL one-second MP4 via ffmpeg: test_video_embeds_… waits for the
    # embed's <video> to survive, and MediaWidget replaces the node with a
    # "video not found" note when decoding errors — which the old 133-byte
    # empty-moov stub always did. Stub kept as the no-ffmpeg fallback;
    # PLAYABLE lets the playback test skip honestly there.
    import base64, shutil, subprocess, tempfile, os
    fd, path = tempfile.mkstemp(suffix=".mp4", prefix="kb-test-")
    os.close(fd)
    if shutil.which("ffmpeg"):
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "testsrc=duration=1:size=64x64:rate=10",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", path],
            capture_output=True)
        if r.returncode == 0 and os.path.getsize(path) > 1000:
            return path, True
    data = base64.b64decode(
        "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAABIbW9vdgAAAGxtdmhkAAAAAAAA"
        "AAAAAAAAAAAAAAAAA+gAAAAAAAEAAAEAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAABAAAA"
        "AAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAC"
    )
    with open(path, "wb") as fh:
        fh.write(data)
    return path, False


SRC_MP4, PLAYABLE = _fixture_mp4()

TABLE_DOC = """# Sprint

| Task | Owner |
| --- | --- |
| Ship it | alice |
| Review | carol |

after the table
"""


def api():
    c = httpx.Client(base_url=BASE, timeout=60)
    r = c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="module")
def doc():
    c = api()
    rel = kbdoc(f"tbltest_{int(time.time())}.md")
    assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
    assert c.post("/api/artifact/write",
                  json={"path": rel, "content": TABLE_DOC}).status_code == 200
    yield rel
    c.post("/api/fs/delete", json={"path": rel})


@pytest.fixture(scope="module")
def video_doc():
    c = api()
    folder = kbdoc(f"vidembed_{int(time.time())}")
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


def cell(page, r, c):
    """A cell renders its markdown until you put the cursor in it, so clicking
    the cell is what produces the <input> these tests type into."""
    page.locator(f'table.cm-table [data-cell="{r},{c}"]').click()
    inp = page.locator(f'table.cm-table textarea[data-cell="{r},{c}"]')
    inp.wait_for(state="visible", timeout=4000)
    return inp


def cell_text(page, r, c):
    return page.locator(f'table.cm-table [data-cell="{r},{c}"] .cm-cell-md').inner_text()


def test_table_renders_as_a_grid_and_edits_write_markdown(browser, doc):
    ctx, page = open_doc(browser, doc)
    page.wait_for_selector("table.cm-table", timeout=10000)
    # header + two body rows, two columns
    assert page.locator("table.cm-table tr").count() == 3
    assert cell_text(page, 0, 0) == "Task"

    # typing in a cell writes back into the markdown source
    inp = cell(page, 1, 1)
    inp.fill("peter")
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
    cell(page, 1, 0)
    page.wait_for_timeout(400)
    assert page.locator("table.cm-table").count() == 1, "table turned back into markdown"
    after = page.locator("table.cm-table").bounding_box()
    assert abs(after["y"] - before["y"]) < 4, "the table moved when clicked"
    ctx.close()


def test_structural_buttons_add_and_remove(browser, doc):
    ctx, page = open_doc(browser, doc)
    page.wait_for_selector("table.cm-table", timeout=10000)
    cell(page, 1, 0)
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
    if not PLAYABLE:
        pytest.skip("no ffmpeg on this box — the fallback fixture cannot decode")
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
    rel = kbdoc(f"longtbl_{int(time.time())}.md")
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


# ---- what krystof asked for on 2026-09-22 ---------------------------------
# "markdown in tables is not rendered, but should be. and also ctrl z in tables
#  doesnt work. also the botto + row and - row and column should always
#  append/remove to the latest row/column. but i should be able to rightclick
#  somewhere on a row or column and create a new one in the middle"

MD_TABLE_DOC = """# Rich cells

| What | State |
| --- | --- |
| **Ship it** | `done` |
| ~~Old plan~~ | [the brief](brief.md) |

after the table
"""


@pytest.fixture
def fresh(browser):
    """A table document of this test's own — these rearrange the grid, and a
    shared fixture would hand the next test somebody else's leftovers."""
    c = api()
    rel = kbdoc(f"tblrc_{int(time.time() * 1000)}.md")
    assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
    assert c.post("/api/artifact/write",
                  json={"path": rel, "content": TABLE_DOC}).status_code == 200
    ctx, page = open_doc(browser, rel)
    page.wait_for_selector("table.cm-table", timeout=10000)
    yield page
    ctx.close()
    c.post("/api/fs/delete", json={"path": rel})


@pytest.fixture(scope="module")
def md_doc():
    c = api()
    rel = kbdoc(f"tblmd_{int(time.time())}.md")
    assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
    assert c.post("/api/artifact/write",
                  json={"path": rel, "content": MD_TABLE_DOC}).status_code == 200
    yield rel
    c.post("/api/fs/delete", json={"path": rel})


def test_a_cell_renders_its_markdown_until_you_edit_it(browser, md_doc):
    ctx, page = open_doc(browser, md_doc)
    page.wait_for_selector("table.cm-table", timeout=10000)
    # rendered: the marks are gone and real elements are there instead
    assert cell_text(page, 1, 0) == "Ship it"
    assert page.locator('[data-cell="1,0"] .cm-cell-md strong').count() == 1
    assert page.locator('[data-cell="1,1"] .cm-cell-md code').inner_text() == "done"
    assert page.locator('[data-cell="2,0"] .cm-cell-md s').count() == 1
    link = page.locator('[data-cell="2,1"] .cm-cell-md a')
    assert link.inner_text() == "the brief"
    assert "brief.md" in (link.get_attribute("data-open-path") or "")
    # a scheme that is not http(s)/mailto/tel never becomes a real href: an
    # anchor in the app's own origin would run `javascript:` on activation
    inp = cell(page, 2, 1)
    inp.fill("[gotcha](javascript:alert(1))")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    bad = page.locator('[data-cell="2,1"] .cm-cell-md a')
    assert bad.inner_text() == "gotcha"
    assert bad.get_attribute("href") is None
    assert bad.get_attribute("data-open-path") is None

    # …and the raw markdown comes back the moment the cell is yours to type in
    assert cell(page, 1, 0).input_value() == "**Ship it**"
    # leaving it puts the rendering back, with the source unchanged
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert page.locator('[data-cell="1,0"] .cm-cell-md strong').count() == 1
    assert "| **Ship it** | `done` |" in doc_text(page)
    ctx.close()


def test_ctrl_z_inside_a_cell_undoes_the_document(browser, fresh):
    """A cell is an <input> the editor cannot see, so Ctrl+Z used to do
    nothing at all there."""
    page = fresh
    inp = cell(page, 1, 1)
    inp.fill("peter")
    page.wait_for_timeout(700)                      # debounced writeback
    assert "| Ship it | peter |" in doc_text(page)
    page.keyboard.press("Control+z")
    page.wait_for_timeout(600)
    src = doc_text(page)
    assert "| Ship it | alice |" in src, src
    assert "peter" not in src
    assert page.locator("table.cm-table").count() == 1, "the grid survived the undo"


def test_the_bar_always_works_on_the_end_of_the_table(browser, fresh):
    """Whichever cell you are in, `+ row` adds one at the bottom and `− row`
    takes the last one away — that is what a row of buttons under a grid looks
    like it does."""
    page = fresh
    cell(page, 1, 0)                                # sitting in the FIRST body row
    page.locator('.cm-tbl-bar button:has-text("+ row")').click()
    page.wait_for_timeout(700)
    rows = [r.strip() for r in doc_text(page).splitlines() if r.startswith("|")]
    assert rows[2].startswith("| Ship it"), rows     # the new row is not here
    assert rows[-1] == "|  |  |", rows                # it is at the bottom
    page.locator('.cm-tbl-bar button:has-text("− row")').click()
    page.wait_for_timeout(700)
    assert "| Ship it | alice |" in doc_text(page)
    assert page.locator("table.cm-table tr").count() == 3


def test_right_click_inserts_a_row_in_the_middle(browser, fresh):
    page = fresh
    page.locator('table.cm-table [data-cell="2,0"]').click(button="right")
    page.wait_for_selector('[data-testid="ctx-menu"]', timeout=4000)
    page.locator('[data-testid="ctx-menu"] button:has-text("Insert row above")').click()
    page.wait_for_timeout(700)
    rows = [r.strip() for r in doc_text(page).splitlines() if r.startswith("|")]
    # header, delimiter, Ship it, the new empty row, Review
    assert rows[2].startswith("| Ship it"), rows
    assert rows[3] == "|  |  |", rows
    assert rows[4].startswith("| Review"), rows

    # …and a column in the middle, from the same menu
    page.locator('table.cm-table [data-cell="0,1"]').click(button="right")
    page.wait_for_selector('[data-testid="ctx-menu"]', timeout=4000)
    page.locator('[data-testid="ctx-menu"] button:has-text("Insert column left")').click()
    page.wait_for_timeout(700)
    assert "| Task |  | Owner |" in doc_text(page)


def test_right_click_deletes_the_row_you_point_at(browser, fresh):
    page = fresh
    page.locator('table.cm-table [data-cell="1,0"]').click(button="right")
    page.wait_for_selector('[data-testid="ctx-menu"]', timeout=4000)
    page.locator('[data-testid="ctx-menu"] button:has-text("Delete this row")').click()
    page.wait_for_timeout(700)
    src = doc_text(page)
    assert "| Ship it | alice |" not in src, src
    assert "| Review | carol |" in src
    # the header row can never be deleted, so its menu does not offer it
    page.locator('table.cm-table [data-cell="0,0"]').click(button="right")
    page.wait_for_selector('[data-testid="ctx-menu"]', timeout=4000)
    assert page.locator('[data-testid="ctx-menu"] button:has-text("Delete this row")').count() == 0
    assert page.locator('[data-testid="ctx-menu"] button:has-text("Insert row above")').count() == 0
    page.keyboard.press("Escape")


# ---- sizing: show the content, use the page, move nothing ------------------
# "when I click inside a table it weirdly jumps, it weirdly resizes… I have to
#  scroll inside that cell instead of it being stretched wide or high… they
#  must always be automatically resized to basically show all the content"
#  — krystof, 2026-09-22

LONG_CELL = ("A sentence far too long to sit on one line in a narrow cell, "
             "which is exactly the case that used to make you scroll sideways "
             "inside the cell instead of the cell simply getting taller.")


def _geom(page):
    return page.evaluate("""() => {
      const sc = document.querySelector('.cm-scroller');
      const wrap = document.querySelector('.cm-table-wrap');
      const t = wrap.querySelector('table');
      return {
        docScrollsSideways: sc.scrollWidth > sc.clientWidth + 1,
        wrap: wrap.clientWidth, table: Math.round(t.getBoundingClientRect().width),
        cells: [...t.querySelectorAll('td[data-cell], th[data-cell]')].map(td => ({
          k: td.dataset.cell, w: Math.round(td.getBoundingClientRect().width),
          h: Math.round(td.getBoundingClientRect().height),
          scrolls: td.scrollWidth > td.clientWidth + 1,
        })),
      };
    }""")


@pytest.fixture
def sized(browser):
    """A wide table and a small one in a document of this test's own."""
    c = api()
    rel = kbdoc(f"tblsize_{int(time.time() * 1000)}.md")
    wide = ("| " + " | ".join(f"Column {i}" for i in range(8)) + " |\n"
            "| " + " | ".join("---" for _ in range(8)) + " |\n"
            "| " + " | ".join(f"value {i} that is fairly long" for i in range(8)) + " |\n")
    body = (f"# Sizing\n\n| Task | Notes | Who |\n| --- | --- | --- |\n"
            f"| Short | {LONG_CELL} | alice |\n| Ship | fine | bob |\n\n"
            f"| A | B |\n| --- | --- |\n| x | y |\n\n{wide}\nafter\n")
    assert c.post("/api/file", json={"path": rel}).status_code in (200, 409)
    assert c.post("/api/artifact/write",
                  json={"path": rel, "content": body}).status_code == 200
    ctx, page = open_doc(browser, rel)
    page.wait_for_selector("table.cm-table", timeout=10000)
    page.wait_for_timeout(500)
    yield page
    ctx.close()
    c.post("/api/fs/delete", json={"path": rel, "permanent": True})


def test_a_long_cell_gets_taller_instead_of_scrolling(sized):
    page = sized
    g = _geom(page)
    assert not g["docScrollsSideways"], "the document scrolls sideways because of a table"
    assert g["table"] <= g["wrap"] + 1, g          # the table fits the page
    long_cell = next(c for c in g["cells"] if c["k"] == "1,1")
    assert not long_cell["scrolls"], "the cell scrolls instead of wrapping"
    assert long_cell["h"] > 40, f"the long cell did not get taller: {long_cell}"


def test_a_small_table_stays_small_and_a_wide_one_uses_the_page(sized):
    page = sized
    sizes = page.evaluate("""() => [...document.querySelectorAll('.cm-table-wrap')].map(w => ({
      table: Math.round(w.querySelector('table').getBoundingClientRect().width),
      wrap: w.clientWidth,
      scrolls: w.scrollWidth > w.clientWidth + 1,
    }))""")
    wide, small, eight = sizes[0], sizes[1], sizes[2]
    assert small["table"] < small["wrap"] * 0.6, f"a two-cell table was stretched: {small}"
    assert wide["table"] > wide["wrap"] * 0.9, f"a table with a long cell stayed narrow: {wide}"
    assert eight["table"] <= eight["wrap"] + 1 and not eight["scrolls"], \
        f"eight columns did not fit by wrapping: {eight}"


def test_opening_a_cell_moves_nothing(sized):
    page = sized
    before = _geom(page)
    page.locator("table.cm-table").first.locator('[data-cell="1,1"]').click()
    page.locator('table.cm-table textarea[data-cell="1,1"]').wait_for(state="visible")
    page.wait_for_timeout(300)
    after = _geom(page)
    assert before["table"] == after["table"], (before["table"], after["table"])
    for b, a in zip(before["cells"], after["cells"]):
        if b["k"] == a["k"]:
            assert abs(b["w"] - a["w"]) <= 1, f"column {b['k']} moved: {b} -> {a}"
    assert not after["docScrollsSideways"], "opening a cell pushed the page sideways"


def test_a_cell_grows_as_you_type_and_shift_enter_breaks_the_line(sized):
    page = sized
    first = page.locator("table.cm-table").first
    first.locator('[data-cell="1,0"]').click()
    box = page.locator('table.cm-table textarea[data-cell="1,0"]')
    box.wait_for(state="visible")
    before = first.locator('td[data-cell="1,0"]').bounding_box()
    page.keyboard.type(" and quite a lot more text than this column is wide")
    page.wait_for_timeout(300)
    after = first.locator('td[data-cell="1,0"]').bounding_box()
    assert abs(after["width"] - before["width"]) <= 1, "the column widened under the pointer"
    assert after["height"] > before["height"] + 10, "the cell did not grow downwards"
    assert not page.evaluate("""() => {const t = document.querySelector('.cm-cell-edit');
        return t.scrollHeight > t.clientHeight + 1;}"""), "the textarea scrolls"

    page.keyboard.press("Shift+Enter")
    page.keyboard.type("second line")
    page.wait_for_timeout(800)
    assert "<br>second line" in doc_text(page)
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    assert page.locator('table.cm-table td[data-cell="1,0"] br').count() == 1
