"""Uploading a FOLDER (not just loose files) through the UI, both ways in:

  * right-click a folder → "Upload folder" (a `webkitdirectory` picker, which
    gives every file a `webkitRelativePath`), and
  * dragging a folder from the desktop onto a tree row (a drop carries
    directory *entries*, which `dataTransfer.files` cannot describe at all).

Both must recreate the structure under the target folder — subfolders included,
and for the drop path even folders that hold no files. A dropped folder is
synthesised here: Playwright cannot produce a real OS folder drop, so the test
hands the drop handler the same webkitGetAsEntry() tree Chromium would."""
import json
import time

import httpx
from conftest import BASE, dlg_ok, login
from kbenv import CREDS, U, doc

TAG = str(int(time.time()))


def http(user):
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def test_upload_folder_picker_recreates_the_tree(browser, tmp_path):
    top = f"fup_pick_{TAG}"
    src = tmp_path / top
    (src / "sub" / "deep").mkdir(parents=True)
    (src / "a.md").write_text("# A\n")
    (src / "sub" / "b.md").write_text("# B\n")
    (src / "sub" / "deep" / "c.txt").write_text("see")
    (src / ".DS_Store").write_bytes(b"\x00mac junk")

    c = http("alice")
    ctx = browser.new_context()
    try:
        page = login(ctx, "alice")
        row = '.tree-item[data-path="company"]'
        page.wait_for_selector(row, timeout=8000)
        page.click(row, button="right")
        page.wait_for_selector('[data-testid="ctx-menu"]')
        with page.expect_file_chooser() as fc:
            page.click('.ctx-item:has-text("Upload folder")')
        fc.value.set_files(str(src))       # a directory: webkitdirectory input

        # the ghost rows name the path INSIDE the folder, not just the basename
        page.wait_for_selector(".tree-item.uploading", timeout=8000)
        page.wait_for_selector(f'.tree-item[data-path=doc("{top}/a.md")]', timeout=30000)
        page.wait_for_selector(".tree-item.uploading", state="hidden", timeout=15000)

        # every file landed where it came from, with its bytes
        for rel, want in [("a.md", "# A\n"), ("sub/b.md", "# B\n"),
                          ("sub/deep/c.txt", "see")]:
            r = c.get("/api/attachment", params={"path": doc(f"{top}/{rel}")})
            assert r.status_code == 200, f"{rel}: {r.status_code} {r.text}"
            assert r.text == want, f"{rel}: {r.text!r}"
        # OS junk is not content and must not be uploaded
        assert c.get("/api/attachment",
                     params={"path": doc(f"{top}/.DS_Store")}).status_code == 404
    finally:
        ctx.close()
        c.post("/api/fs/delete", json={"path": doc(f"{top}")})


# The entry tree Chromium hands a drop handler for a folder, stubbed: files
# resolve to real File objects, directories page their children out through a
# reader that returns an empty batch to signal the end (as the real API does).
DROP_JS = """([target, top]) => {
  const mkFile = (name, text) => ({
    isFile: true, isDirectory: false, name,
    file: (cb) => cb(new File([text], name, { type: "text/plain" })),
  });
  const mkDir = (name, kids) => ({
    isFile: false, isDirectory: true, name,
    createReader: () => {
      // one batch, then an empty one to end it — asynchronously, as the real
      // FileSystemDirectoryReader is
      let sent = false;
      return { readEntries: (cb) => {
        const batch = sent ? [] : kids;
        sent = true;
        setTimeout(() => cb(batch), 0);
      } };
    },
  });
  const root = mkDir(top, [
    mkFile("a.md", "# A\\n"),
    mkDir("sub", [mkFile("b.md", "# B\\n")]),
    mkDir("empty", []),
    mkDir(".git", [mkFile("HEAD", "ref: refs/heads/main\\n")]),
  ]);
  const dt = {
    items: [{ kind: "file", webkitGetAsEntry: () => root }],
    files: [], getData: () => "",
  };
  const ev = new Event("drop", { bubbles: true, cancelable: true });
  Object.defineProperty(ev, "dataTransfer", { value: dt });
  document.querySelector('.tree-item[data-path="' + target + '"]').dispatchEvent(ev);
}"""


def test_drop_folder_uploads_subfolders_and_empty_dirs(browser):
    top = f"fup_drop_{TAG}"
    c = http("alice")
    ctx = browser.new_context()
    try:
        page = login(ctx, "alice")
        page.wait_for_selector('.tree-item[data-path="company"]', timeout=8000)
        page.evaluate(DROP_JS, ["company", top])

        # a folder drop gets no browser confirmation, so the app asks once —
        # with the file count, because a mis-aimed drop can be enormous
        page.wait_for_selector('[data-testid="dlg-ok"]', timeout=5000)
        assert "2 files" in page.inner_text('[data-testid="dlg"]')
        dlg_ok(page)

        page.wait_for_selector(f'.tree-item[data-path=doc("{top}/a.md")]', timeout=30000)
        page.wait_for_selector(".tree-item.uploading", state="hidden", timeout=15000)

        for rel, want in [("a.md", "# A\n"), ("sub/b.md", "# B\n")]:
            r = c.get("/api/attachment", params={"path": doc(f"{top}/{rel}")})
            assert r.status_code == 200, f"{rel}: {r.status_code} {r.text}"
            assert r.text == want, f"{rel}: {r.text!r}"
        # a folder with no files in it still arrives (the picker cannot do this)
        r = c.get("/fs/props", params={"path": doc(f"{top}/empty")})
        assert r.status_code == 200, r.text
        # ...but a nested .git would become a gitlink in the KB's own audit repo
        assert c.get("/fs/props",
                     params={"path": doc(f"{top}/.git")}).status_code == 404
    finally:
        ctx.close()
        c.post("/api/fs/delete", json={"path": doc(f"{top}")})
