"""P2/P3 gates in a real browser: web mirror (tree, terminal) and the core
promise — Google-Docs-style multiplayer on plain markdown files, where the file
on disk is itself a CRDT peer that vim/agents/scripts can edit and have merged.
"""
import json as _json
import subprocess
import time

import httpx
import pytest
from conftest import BASE, CREDS, login, open_doc, doc_text, insert_at, insert_at_end

COLLAB = "company/collab.md"


@pytest.fixture(scope="module", autouse=True)
def _remove_fixture_doc_afterwards():
    yield
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": "alice", "password": CREDS["alice"]})
    c.post("/api/fs/delete", json={"path": COLLAB})


def git_count():
    # .git is root-only (its objects would bypass file permissions); syncd
    # publishes repo stats to a world-readable tmpfs file instead.
    try:
        return _json.load(open("/run/kb/git-state.json"))["commits"]
    except (OSError, ValueError, KeyError):
        return 0


def _ensure_collab_doc():
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": "alice", "password": CREDS["alice"]})
    c.post("/api/file", json={"path": COLLAB})  # 200 or 409 if it already exists
    # seed one line so editors have content to sync
    with open(f"/srv/kb/{COLLAB}", "w") as f:
        f.write("collab doc seed line\n")
    time.sleep(0.6)


def test_web_mirror_tree_and_identity(browser):
    ctx = browser.new_context()
    page = login(ctx, "bob")
    assert "bob" in page.text_content('[data-testid="whoami"]')
    # bob sees company + acme, never alice's private dir
    tree = page.inner_text('[data-testid="tree"]')
    assert "overview.md" in tree
    assert "plan.md" in tree  # acme, bob is on the team
    ctx.close()


def test_terminal_runs_as_user(browser):
    ctx = browser.new_context()
    page = login(ctx, "carol")
    page.click('[data-testid="toggle-term"]')
    page.wait_for_selector('#terminal .xterm-rows')
    time.sleep(1.2)
    page.keyboard.type("whoami\n")
    # poll the terminal buffer for the username
    got = False
    for _ in range(20):
        txt = page.inner_text('#terminal')
        if "carol" in txt:
            got = True
            break
        time.sleep(0.3)
    assert got, "terminal shell must run as the logged-in user (carol)"
    ctx.close()


def test_multiplayer_convergence(browser):
    _ensure_collab_doc()
    ca = browser.new_context(); cb = browser.new_context()
    pa = login(ca, "alice"); pb = login(cb, "bob")
    open_doc(pa, COLLAB); open_doc(pb, COLLAB)
    # wait until both see the same seed
    for _ in range(20):
        if doc_text(pa) == doc_text(pb) and doc_text(pa):
            break
        time.sleep(0.25)

    # Concurrent edits from two different users at different positions.
    insert_at(pa, 0, "ALPHA ")
    insert_at_end(pb, " OMEGA")
    time.sleep(0.2)
    insert_at(pa, 0, "ALPHA2 ")
    insert_at_end(pb, " OMEGA2")

    # Converge.
    ta = tb = None
    for _ in range(40):
        ta, tb = doc_text(pa), doc_text(pb)
        if ta == tb and "ALPHA" in ta and "OMEGA" in ta:
            break
        time.sleep(0.25)
    assert ta == tb, f"editors diverged:\nA={ta!r}\nB={tb!r}"
    assert "ALPHA" in ta and "ALPHA2" in ta and "OMEGA" in ta and "OMEGA2" in ta

    # And the file on disk reflects the converged state (file IS the CRDT peer).
    time.sleep(1.0)
    disk = open(f"/srv/kb/{COLLAB}").read()
    assert disk.strip() == ta.strip(), f"disk != editor\ndisk={disk!r}\nedit={ta!r}"
    ca.close(); cb.close()


def test_external_file_edit_merges_into_browser(browser):
    """The killer feature: an edit made to the file directly (as any writer)
    is diffed and merged into the live editing session."""
    _ensure_collab_doc()
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, COLLAB)
    for _ in range(20):
        if doc_text(page):
            break
        time.sleep(0.25)

    marker = "EXTERNAL_EDIT_ZQ7"
    # Append to the file the way vim / an agent / git would (plain write, as bob).
    subprocess.run(
        ["bash", "-c", f'printf "\\n{marker} appended out of band\\n" >> /srv/kb/{COLLAB}'],
        check=True)

    appeared = False
    for _ in range(24):  # ~6s
        if marker in doc_text(page):
            appeared = True
            break
        time.sleep(0.25)
    assert appeared, "external file edit must merge into the live browser session"
    ctx.close()


def test_git_autocommit_advances(browser):
    _ensure_collab_doc()
    before = git_count()
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, COLLAB)
    time.sleep(0.5)
    insert_at_end(page, "\ngit-commit-trigger line\n")
    time.sleep(6)  # git debounce is ~4s
    after = git_count()
    assert int(after) > int(before or "0"), f"git commits did not advance ({before}->{after})"
    ctx.close()
