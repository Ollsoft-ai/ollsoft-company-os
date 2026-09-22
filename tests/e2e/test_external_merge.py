"""The scenario that corrupted a real session: two people have a doc open and
are typing, while an external tool (claude code) does read-modify-write on the
file from a STALE read. The daemon must merge the external change as a
coherent, anchored edit — concurrent human keystrokes survive contiguously,
nothing gets interleaved mid-word, and everyone (both browsers + disk)
converges to the same text."""
import time

import httpx
import pytest
from conftest import BASE, CREDS, dlg_fill, login
from kbenv import U, doc as kbdoc

TAG = str(int(time.time()))
DOC = kbdoc(f"extmerge_{TAG}.md")
DISK = f"/srv/kb/{DOC}"

BASE_TEXT = (
    "# Meeting notes\n\n"
    "line one about the alpha project\n"
    "line two about the beta project\n"
    "line three about the gamma project\n"
    "line four about the delta project\n"
)


def api(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


@pytest.fixture()
def doc():
    """A document of this test's OWN, never a shared path.

    These tests used one module-level DOC, deleting and recreating it between
    them. Deleting the file is what retires its CRDT room, and that happens on
    the watcher's schedule — so recreating the same path immediately could land
    the next test's content in the PREVIOUS test's still-live room, merging one
    test's text into another. That produced spliced lines like
    '# Meeting  hh hhhhhnotes' (fragments of two different tests' typing) and
    made these tests fail only when run together, which read exactly like the
    merge bug they exist to catch. A unique path per test removes the shared
    state entirely.
    """
    global DOC, DISK
    DOC = kbdoc(f"extmerge_{int(time.time() * 1000)}.md")
    DISK = f"/srv/kb/{DOC}"
    k = api("alice")
    assert k.post("/api/file", json={"path": DOC}).status_code in (200, 409)
    assert k.post("/api/artifact/write", json={"path": DOC, "content": BASE_TEXT}).status_code == 200
    time.sleep(1.0)   # let the daemon/watcher settle on the seeded content
    yield DOC
    k.post("/api/fs/delete", json={"path": DOC})


def open_both(browser):
    ck = browser.new_context()
    k = login(ck, "alice")
    k.click(f'.tree-item[data-path="{DOC}"]')
    k.wait_for_function("() => window.__kbview && window.__kbview.state.doc.length > 0")
    cj = browser.new_context()
    j = login(cj, "bob")
    j.click(f'.tree-item[data-path="{DOC}"]')
    j.wait_for_function("() => window.__kbview && window.__kbview.state.doc.length > 0")
    return ck, k, cj, j


def text_of(page):
    return page.evaluate("() => window.__kbview.state.doc.toString()")


def wait_converged(pages, timeout=15.0):
    """Wait until every open editor AND the file on disk hold the same text."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        texts = [text_of(p) for p in pages]
        disk = open(DISK).read()
        if all(t == disk for t in texts):
            return disk
        time.sleep(0.3)
    return open(DISK).read()


def type_at(page, anchor_snippet, text):
    """Insert `text` right after `anchor_snippet` via the live editor."""
    page.evaluate(
        """([snippet, t]) => {
             const v = window.__kbview;
             const pos = v.state.doc.toString().indexOf(snippet) + snippet.length;
             v.dispatch({ changes: { from: pos, insert: t } });
           }""", [anchor_snippet, text])


def _fresh_doc():
    """Another document of this test's own, seeded with BASE_TEXT.

    A retry cannot reuse the previous attempt's file: that one already holds the
    external tool's rewrite, so "the agent's edit landed exactly once" would be
    asserted against text that starts with it.
    """
    global DOC, DISK
    DOC = kbdoc(f"extmerge_{int(time.time() * 1000)}.md")
    DISK = f"/srv/kb/{DOC}"
    k = api("alice")
    assert k.post("/api/file", json={"path": DOC}).status_code in (200, 409)
    assert k.post("/api/artifact/write", json={"path": DOC, "content": BASE_TEXT}).status_code == 200
    time.sleep(1.0)   # let the daemon/watcher settle on the seeded content
    return DOC


def test_stale_external_rewrite_keeps_concurrent_typing_intact(browser, doc):
    """The merge only happens when bob's keystrokes are still UNFLUSHED as the
    external write lands — that is the scenario. The daemon flushes on its own
    ~250 ms tick, so on a loaded machine (CI) the flush can win the race; the
    external tool then writes text it had ALREADY seen on disk, which is a
    legitimate revert and not this test's subject. So check the precondition —
    is bob's typing still absent from disk? — and if it is not, run the whole
    thing again on a fresh document instead of asserting about a race that
    never ran.
    """
    spares = []
    try:
        for attempt in range(4):
            if attempt:
                spares.append(_fresh_doc())     # a retry needs virgin text
            ck, k, cj, j = open_both(browser)
            try:
                # the external tool reads the file NOW (this is its stale base)...
                stale = open(DISK).read()
                external = stale.replace(
                    "line four about the delta project",
                    "line four about the delta project (REWRITTEN BY AGENT)"
                ) + "\n## Agent addendum\n\nadded by the external tool\n"

                # ...meanwhile bob types into line two (unflushed for ~250ms)...
                type_at(j, "line two", " [bob-was-here]")
                unflushed = "[bob-was-here]" not in open(DISK).read()

                # ...and the external tool writes its modification of the STALE
                # text: edit line four + append a section (a classic claude code
                # Edit).
                open(DISK, "w").write(external)
                if not unflushed:
                    continue                      # the flush won; try again

                # bob keeps typing while the daemon merges — the real-session killer
                time.sleep(0.15)
                type_at(j, "line three", " [more-typing]")

                # let everything converge (merge + flush + relay)
                wait_converged([k, j])
                tk, tj = text_of(k), text_of(j)
                disk = open(DISK).read()
                assert tk == tj == disk, f"all peers+disk must converge:\nK={tk!r}\nJ={tj!r}\nD={disk!r}"
                # human keystrokes survive, contiguous, exactly once, where they were typed
                for needle in ("line two [bob-was-here] about the beta project",
                               "line three [more-typing] about the gamma project"):
                    assert tk.count(needle) == 1, f"typed text mangled: wanted {needle!r} in:\n{tk}"
                # the external edit landed too, exactly once
                assert tk.count("(REWRITTEN BY AGENT)") == 1, tk
                assert tk.count("## Agent addendum") == 1, tk
                # and nothing got duplicated wholesale
                assert tk.count("line four about the delta project") == 1, tk
                return
            finally:
                ck.close()
                cj.close()
        pytest.fail("bob's keystrokes reached disk before the external write on "
                    "every attempt — the merge path was never exercised")
    finally:
        c = api("alice")
        for p in spares:
            c.post("/api/fs/delete", json={"path": p})


def test_repetitive_tokens_never_mutate_midword(browser, doc):
    """The 'tegiy' bug: a doc full of near-identical tokens (tegy/tegym/tegyho)
    plus concurrent typing made a fuzzy hunk anchor on the wrong lookalike and
    inject characters mid-word. With line-granular merging that must be
    structurally impossible: every tegy-ish token in the result is still a
    legal one, and the human's typing stays contiguous."""
    import re
    k = api("alice")
    base = ("povim vam pohadku o tegym\n\n"
            "tegy je ollsoft robot\n\n"
            "ten problem s tegym byl vzdycky\n\n"
            "naucit tegyho vse o kafi\n\n"
            "zastavit tegyho hned\n")
    assert k.post("/api/artifact/write", json={"path": DOC, "content": base}).status_code == 200
    time.sleep(1.0)
    ck, kk, cj, j = open_both(browser)
    try:
        stale = open(DISK).read()
        # human types mid-doc (the merge will have to be fuzzy)...
        type_at(j, "ten problem", " hhhh")
        # ...external tool rewrites from the stale base: touches one tegy-line
        # and inserts a sentence that itself contains a lone "i" token
        external = stale.replace(
            "zastavit tegyho hned",
            "zastavit tegyho pred prepsanim knowledgebase"
        ).replace(
            "tegy je ollsoft robot",
            "tegy je ollsoft robot, uvaril si kafe (i kdyz roboti kafe nepiji)")
        open(DISK, "w").write(external)
        time.sleep(0.15)
        type_at(j, "naucit tegyho", " uz")
        wait_converged([kk, j])
        tk, tj = text_of(kk), text_of(j)
        disk = open(DISK).read()
        assert tk == tj == disk, f"convergence broke:\nK={tk!r}\nD={disk!r}"
        # every tegy-ish token is a legal one — no tegiy-style mid-word injections
        bad = [t for t in re.findall(r"teg\w*", tk) if t not in ("tegy", "tegym", "tegyho")]
        assert not bad, f"mid-word mutation happened: {bad} in\n{tk}"
        assert tk.count("ten problem hhhh s tegym") == 1, tk
        assert tk.count("naucit tegyho uz vse o kafi") == 1, tk
        assert tk.count("(i kdyz roboti kafe nepiji)") == 1, tk
        assert tk.count("pred prepsanim knowledgebase") == 1, tk
    finally:
        ck.close()
        cj.close()


def test_long_similar_lines_never_splice(browser, doc):
    """The second-generation 'tegiy' bug: dmp's patch_apply silently split long
    hunks into <=32-char chunks and anchored each independently, splicing
    fragments into similar long lines. With deterministic line-level diff3 the
    output can only contain WHOLE lines from one side — verify with long,
    near-identical Czech-style checkbox lines under concurrent typing."""
    k = api("alice")
    base = ("# pohadka\n\n"
            "- [ ] naucit tegyho, ze kafe opravdu neni pro roboty ani pro androidy\n"
            "- [x] zastavit tegyho pred prepsanim cele knowledgebase do binarky\n\n"
            "konec pohadky. dobrou noc.\n")
    assert k.post("/api/artifact/write", json={"path": DOC, "content": base}).status_code == 200
    time.sleep(1.0)
    ck, kk, cj, j = open_both(browser)
    try:
        stale = open(DISK).read()
        # human types INTO the first long line while...
        type_at(j, "naucit tegyho,", " hhh")
        # ...the external tool (stale read) rewrites the second long line and
        # inserts another long, similar sentence
        external = stale.replace(
            "- [x] zastavit tegyho pred prepsanim cele knowledgebase do binarky",
            "- [x] zastavit tegyho pred prepsanim knowledgebase\n\n"
            "ve stredu se tegy polepsil: misto prepisovani knowledgebase do binarky zacal psat hezke markdown soubory.")
        open(DISK, "w").write(external)
        wait_converged([kk, j])
        tk = text_of(kk)
        assert tk == text_of(j) == open(DISK).read()
        legal = set((base + external).splitlines()) | {
            "- [ ] naucit tegyho, hhh ze kafe opravdu neni pro roboty ani pro androidy"}
        for line in tk.splitlines():
            assert line in legal, f"SPLICED line appeared: {line!r}\nfull:\n{tk}"
        assert tk.count("- [ ] naucit tegyho, hhh ze kafe") == 1
        assert tk.count("ve stredu se tegy polepsil") == 1
    finally:
        ck.close()
        cj.close()


def test_plain_external_rewrite_still_syncs(browser, doc):
    """No concurrency: an external rewrite while people just watch must arrive
    verbatim (the boring case must keep working)."""
    ck, k, cj, j = open_both(browser)
    try:
        new = BASE_TEXT.replace("alpha", "ALPHA") + "\nplain tail\n"
        open(DISK, "w").write(new)
        wait_converged([k, j])
        assert text_of(k) == text_of(j) == new
    finally:
        ck.close()
        cj.close()


# ---- the audience has to survive a flush ------------------------------------
def _facl(path) -> str:
    import subprocess
    return subprocess.run(["sudo", "getfacl", "-p", "-c", str(path)],
                          capture_output=True, text=True).stdout


def test_a_flush_keeps_the_files_audience(browser):
    """A document's audience IS its ACL, and the daemon replaces the file on
    every save. Restoring only owner/group/mode — which is what it did until
    2026-09-22 — erased the named people and the indexer on the first
    keystroke after a share, and, because the stored mode is `0660` against a
    group of `kb-users`, handed the whole company read and write instead.

    Found while chasing why a public link could not save: the container's
    entry was being wiped the same way, by the same line.
    """
    import subprocess
    from kbenv import full

    a = api("alice")
    b = httpx.Client(base_url=BASE, timeout=25)
    b.post("/login", data={"username": U("bob"), "password": CREDS["bob"]})
    rel = kbdoc(f"aclflush_{int(time.time())}.md")
    assert a.post("/api/file", json={"path": rel}).status_code in (200, 409)
    assert a.post("/api/artifact/write",
                  json={"path": rel, "content": "# Audience\n\nbody\n"}).status_code == 200
    assert a.post("/fs/share", json={"path": rel, "scope": "people",
                                     "people": [{"user": U("bob"), "role": "edit"}]
                                     }).status_code == 200
    p = full(rel)
    before = _facl(p)
    assert f"user:{U('bob')}:rw" in before, before
    assert "user:kbindexer:r" in before, before
    assert "group::---" in before, before

    ctx = browser.new_context()
    page = login(ctx, "alice")
    try:
        page.goto(f"{BASE}/{rel}")
        page.wait_for_function(f"() => window.__kbpath === {rel!r}", timeout=15000)
        page.wait_for_selector(".cm-content", timeout=10000)
        page.locator(".cm-line").first.click()
        page.keyboard.press("End")
        page.keyboard.type(" typed in the app")
        # wait for the flush to land on disk
        for _ in range(30):
            if "typed in the app" in a.get("/api/file", params={"path": rel}).text:
                break
            time.sleep(0.4)
        else:
            raise AssertionError("the edit never reached disk")
        after = _facl(p)
        assert f"user:{U('bob')}:rw" in after, f"bob's access was erased by a save:\n{after}"
        assert "user:kbindexer:r" in after, f"the document fell out of search:\n{after}"
        assert "group::---" in after, f"the whole group was let in:\n{after}"
    finally:
        ctx.close()
        a.post("/api/fs/delete", json={"path": rel, "permanent": True})
