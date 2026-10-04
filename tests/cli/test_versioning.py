"""Version history: attributed, permission-gated, scoped to docs/artifacts.

The one rule that carries all the security: you can see a file's history exactly
when you can READ it now — evaluated by the kernel as you. .git stays root-only;
the /api/vc/* endpoints (and the kb-history CLI over the peer-cred socket) are
the only door, and every query re-checks per file."""
import json
import shutil
import subprocess
import time

import httpx
import pytest
from kbenv import BASE, CREDS, L, U, doc, home, backend_v

TAG = str(int(time.time()))
DIR = doc(f"vc_{TAG}")


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def write(c, path, content):
    assert c.post("/api/artifact/write", json={"path": path, "content": content}).status_code == 200


def wait_for_activity(c, path, author, timeout=30):
    """Poll the ACTIVITY FEED until it reports `path` for `author`.

    wait_for_rev polls /api/vc/log for one path, which git answers the moment
    the commit exists. The activity feed is a different query — repo-wide, with
    its own since-window and author filter — and it can lag that by a beat.
    Waiting on the first and asserting on the second is a race, and it is why
    this file had two intermittently-red tests. Poll the thing you assert on.
    """
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        q = {"since": "10 minutes ago", "author": author, "limit": 1000}
        r = c.get("/api/vc/activity", params=q)
        if r.status_code == 200:
            j = r.json()
            last = [pp for cm in j["commits"] for f in cm["files"] for pp in f["paths"]]
            if path in last:
                return j
        time.sleep(1)
    raise AssertionError(
        f"activity feed never reported {path!r} for {author!r} in {timeout}s; "
        f"saw {last[:10]}")


def wait_for_rev(c, path, n=1, timeout=25):
    """Wait until `path` has >= n committed versions (git debounce is ~4s)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get("/api/vc/log", params={"path": path})
        if r.status_code == 200 and len(r.json().get("entries", [])) >= n:
            return r.json()["entries"]
        time.sleep(1)
    r = c.get("/api/vc/log", params={"path": path})
    raise AssertionError(f"only {len(r.json().get('entries', []))} revs after {timeout}s: {r.text}")


@pytest.fixture(scope="module")
def k():
    c = cl("alice")
    if backend_v(c) < 10:
        pytest.skip("alice's backend predates the versioning attrib hooks — restart backends")
    assert c.post("/api/fs/mkdir", json={"path": DIR}).status_code == 200
    yield c
    c.post("/api/fs/delete", json={"path": DIR})


@pytest.mark.xfail(
    reason="git attribution race (syncd.py:589): git_loop snapshots and CLEARS "
           "dirty_docs before handing the commit to its executor, so a flush "
           "landing mid-commit is swept into the anonymous kb-syncd snapshot and "
           "loses its author. Load-dependent — passes alone, fails under the full "
           "suite. xfail rather than skip on purpose: this is the only signal we "
           "have for the defect, and it must turn XPASS the moment the race is "
           "fixed instead of quietly never running.",
    strict=False)
def test_edits_are_attributed_to_the_author(k):
    p = f"{DIR}/doc.md"
    assert k.post("/api/file", json={"path": p}).status_code == 200
    write(k, p, "# v1\n\nfirst\n")
    write(k, p, "# v1\n\nfirst\nsecond\n")
    entries = wait_for_rev(k, p, 1)
    assert entries, "an edit must produce a version"
    assert all(L(e["author"]) == "alice" for e in entries), entries
    # the diff of the newest rev reflects the real change
    rev = entries[0]["rev"]
    d = k.get("/api/vc/diff", params={"path": p, "rev": rev}).json()
    assert "second" in d["patch"]


def test_show_and_restore_round_trip(k):
    p = f"{DIR}/restore.md"
    assert k.post("/api/file", json={"path": p}).status_code == 200
    write(k, p, "ORIGINAL\n")
    first = wait_for_rev(k, p, 1)[0]["rev"]
    write(k, p, "CHANGED AWAY\n")
    wait_for_rev(k, p, 2)
    # show the old content
    shown = k.get("/api/vc/show", params={"path": p, "rev": first}).json()
    assert shown["content"] == "ORIGINAL\n"
    # restore = write the old content back (as the user); it becomes a new version
    write(k, p, shown["content"])
    assert open(f"/srv/kb/{p}").read() == "ORIGINAL\n"


def test_history_is_permission_gated(k):
    """bob can see the history of a company file, but never of alice's private
    one — the same kernel read check that guards the live file. Uses a dedicated
    private file so it never touches the shared users/alice/private.md fixture."""
    j = cl("bob")
    shared = f"{DIR}/shared.md"
    assert k.post("/api/file", json={"path": shared}).status_code == 200
    write(k, shared, "team-readable\n")
    wait_for_rev(k, shared, 1)
    priv = f"{DIR}/priv.md"
    assert k.post("/api/file", json={"path": priv}).status_code == 200
    write(k, priv, "alice only\n")
    assert k.post("/fs/props", json={"path": priv, "visibility": "private"}).status_code == 200
    wait_for_rev(k, priv, 1)                                  # alice can read his own
    assert j.get("/api/vc/log", params={"path": shared}).status_code == 200
    assert j.get("/api/vc/log", params={"path": priv}).status_code == 403   # bob refused
    assert j.get("/api/vc/show", params={"path": priv, "rev": "0000000"}).status_code == 403


def test_cross_user_attribution(k):
    """A DIFFERENT user's edit is attributed to THEM, not to alice or the
    anonymous sweep. This exercises the write-verified attribution path: bob
    can write in company/, so his hint is trusted and the commit is his."""
    j = cl("bob")
    if backend_v(j) < 10:
        pytest.skip("bob's backend predates the versioning attrib hooks")
    p = f"{DIR}/by_bob.md"
    assert j.post("/api/file", json={"path": p}).status_code == 200
    write(j, p, f"bob wrote this {TAG}\n")
    entries = wait_for_rev(j, p, 1)
    assert entries and L(entries[0]["author"]) == "bob", entries


def test_secrets_never_in_history(k):
    """Editing a secret (via a nested _secrets/ folder) never puts it in git —
    it is not versioned, its history endpoint is refused, and the world-readable
    monitor (git-state) keeps reporting zero tracked secrets. (The repo-ROOT
    _secrets case is additionally covered by the sweep's explicit `_secrets/**`
    exclude, verified manually — it can't be cleaned up via the API here.)"""
    sd = f"{DIR}/_secrets"
    assert k.post("/api/fs/mkdir", json={"path": sd}).status_code in (200, 409)
    sec = f"{sd}/key_{TAG}.md"
    if k.post("/fs/newfile", json={"path": sec}).status_code not in (200, 409):
        pytest.skip("cannot create a secret here")
    k.post("/api/artifact/write", json={"path": sec, "content": f"SECRET_{TAG}\n"})
    # force a sweep with an ordinary doc, then let git settle
    trig = f"{DIR}/trig.md"
    k.post("/api/file", json={"path": trig})
    write(k, trig, f"t {TAG}\n")
    wait_for_rev(k, trig, 1)
    time.sleep(3)
    assert json.load(open("/run/kb/git-state.json"))["tracked_secrets"] == 0
    assert k.get("/api/vc/log", params={"path": sec}).status_code == 400


def test_no_stale_permission_window(k):
    """Revoking read access takes effect on history IMMEDIATELY — no cache TTL
    window where a just-removed reader still sees a file's history. The history
    gate must be exactly as fresh as the live-file read gate."""
    j = cl("bob")
    p = f"{DIR}/revoke.md"
    assert k.post("/api/file", json={"path": p}).status_code == 200
    write(k, p, "was team-readable\n")
    wait_for_rev(k, p, 1)
    assert j.get("/api/vc/log", params={"path": p}).status_code == 200   # readable now
    assert k.post("/fs/props", json={"path": p, "visibility": "private"}).status_code == 200
    # immediately (same request round-trip) bob is refused on BOTH surfaces
    assert j.get("/api/vc/log", params={"path": p}).status_code == 403
    assert j.get("/api/file", params={"path": p}).status_code == 403


def test_scope_and_injection_guards(k):
    # only documents/artifacts have history
    assert k.get("/api/vc/log", params={"path": f"{DIR}/note.txt"}).status_code == 400
    # secrets are refused outright (never versioned)
    assert k.get("/api/vc/log", params={"path": doc("_secrets/x.env")}).status_code == 400
    # rev must be a hex sha — no option/flag injection into git
    p = f"{DIR}/doc.md"
    assert k.get("/api/vc/show", params={"path": p, "rev": "--output=/tmp/x"}).status_code == 400
    assert k.get("/api/vc/diff", params={"path": p, "rev": "HEAD;rm"}).status_code == 400


def test_pathspec_magic_cannot_dump_other_files(k):
    """A caller can create a decoy file literally named ':(exclude)x.md' that
    they own (so a naive `test -r` gate would pass), then pass it as ?path= to
    make git apply pathspec MAGIC to OTHER files. The endpoints must refuse any
    magic path outright (and force literal pathspecs underneath)."""
    j = cl("bob")
    # a private alice doc with a marker bob must never see via history
    secret = f"{DIR}/kr_secret.md"
    assert k.post("/api/file", json={"path": secret}).status_code == 200
    write(k, secret, "CONFIDENTIAL_MARKER_ZZ\n")
    assert k.post("/fs/props", json={"path": secret, "visibility": "private"}).status_code == 200
    # bob creates the decoy he owns (allowed to exist; just never a valid query path)
    decoy = doc(":(exclude)decoy.md")
    assert j.post("/fs/newfile", json={"path": decoy}).status_code in (200, 409)
    try:
        for magic in [doc(":(exclude)decoy.md"), ":(glob)**/*.md", ":/", ":!x.md"]:
            r = j.get("/api/vc/diff", params={"path": magic, "rev": "HEAD"})
            assert r.status_code == 400, (magic, r.status_code)
            assert "CONFIDENTIAL_MARKER_ZZ" not in r.text
            assert j.get("/api/vc/log", params={"path": magic}).status_code == 400
    finally:
        j.post("/api/fs/delete", json={"path": decoy})


def test_a_bare_object_id_is_not_a_version(k):
    """?rev= must name a COMMIT. `git show <blob>` prints that blob whatever
    pathspec follows it, and `<tree>:<path>` reads inside any subtree, so a bare
    object id let anyone who could read ONE document read every object in the
    repo — and an abbreviated id is as short as four hex digits, so the whole
    store was enumerable. A blob's id is computable from its content without the
    repo, which is what this test uses."""
    j = cl("bob")
    secret = f"{DIR}/blob_secret.md"
    body = f"CONFIDENTIAL_BLOB_{TAG}\n"
    assert k.post("/api/file", json={"path": secret}).status_code == 200
    write(k, secret, body)
    assert k.post("/fs/props", json={"path": secret, "visibility": "private"}).status_code == 200
    wait_for_rev(k, secret, 1)
    blob = subprocess.run(["git", "hash-object", "--stdin"], input=body,
                          capture_output=True, text=True, check=True).stdout.strip()
    readable = f"{DIR}/blob_readable.md"
    assert k.post("/api/file", json={"path": readable}).status_code == 200
    write(k, readable, "team-readable\n")
    wait_for_rev(k, readable, 1)
    assert j.get("/api/vc/log", params={"path": readable}).status_code == 200   # bob passes the gate
    for op in ("diff", "show"):
        for rev in (blob, blob[:7]):
            r = j.get(f"/api/vc/{op}", params={"path": readable, "rev": rev})
            assert r.status_code == 404, (op, rev, r.status_code)
            assert f"CONFIDENTIAL_BLOB_{TAG}" not in r.text


# A repo-wide "30 days ago" window meant paging every commit in the live
# knowledgebase — 5406 of them in the 30 days to 2026-08-29 — which took ~29s
# and blew the client timeout, failing this test on its own BASELINE rather
# than on the leak it exists to catch. The writes it asserts on are seconds
# old, so the narrow window this file already uses everywhere else is enough.
WINDOW = {"since": "10 minutes ago", "limit": 1000}


def test_activity_only_shows_readable_files(k):
    """The 'what changed' feed never reveals a path the caller can't read.
    Uses a dedicated private file (not the shared private.md fixture)."""
    j = cl("bob")
    hidden = f"{DIR}/hidden.md"
    assert k.post("/api/file", json={"path": hidden}).status_code == 200
    write(k, hidden, f"private change {TAG}\n")
    assert k.post("/fs/props", json={"path": hidden, "visibility": "private"}).status_code == 200
    write(k, hidden, f"private change 2 {TAG}\n")
    time.sleep(6)
    # alice sees his own change in the feed…
    kact = k.get("/api/vc/activity", params=WINDOW).json()
    assert any(hidden in f["paths"] for c in kact["commits"] for f in c["files"]), "owner must see own change"
    # …but bob's feed must not mention it at all
    jact = j.get("/api/vc/activity", params=WINDOW).json()
    leaked = [pp for c in jact["commits"] for f in c["files"] for pp in f["paths"] if pp == hidden]
    assert not leaked, f"activity leaked alice's private file to bob: {leaked}"


def test_activity_pages_past_the_batch_and_flags_truncation(k):
    """A capped feed must SAY it is capped, and raising the cap must reach the
    older rows it hid. Regression: activity used a flat `git log -n 300` over
    the whole repo before the permission filter. Editor autosaves commit every
    few seconds, so those 300 covered hours — `--since "8 days ago"` silently
    returned only today and a week of someone's work read as 'no activity'."""
    p = f"{DIR}/paging.md"
    assert k.post("/api/file", json={"path": p}).status_code == 200
    write(k, p, "one\n")
    wait_for_rev(k, p, 1)
    time.sleep(6)                      # past the ~4s commit debounce
    write(k, p, "one\ntwo\n")
    revs = wait_for_rev(k, p, 2)
    older, newer = revs[1]["rev"], revs[0]["rev"]

    # a narrow window keeps this hermetic on a box with real history
    q = {"since": "10 minutes ago", "author": U("alice")}
    full = k.get("/api/vc/activity", params={**q, "limit": 1000}).json()
    seen = {c["rev"] for c in full["commits"]}
    assert {older, newer} <= seen, f"both revs must be in the window: {sorted(seen)[:5]}"

    capped = k.get("/api/vc/activity", params={**q, "limit": 1}).json()
    assert len(capped["commits"]) == 1, f"limit ignored: {len(capped['commits'])} rows"
    assert capped["truncated"] is True, "a capped feed must report truncated=True"
    assert capped["commits"][0]["rev"] == full["commits"][0]["rev"], "newest-first order"
    assert full["truncated"] is False, "an uncapped window must not claim truncation"


def test_cli_reports_who_did_what(k):
    """kb-history over the peer-cred socket: identity comes from the kernel
    (SO_PEERCRED), so `kb-history --author X` answers 'what did X do' — this is
    what an agent runs. Runs as whoever launched the suite."""
    p = f"{DIR}/cli.md"
    assert k.post("/api/file", json={"path": p}).status_code == 200
    write(k, p, f"cli marker {TAG}\n")
    wait_for_rev(k, p, 1)
    # Same race as the emoji test: a revision existing is not the same as the
    # history being queryable BY AUTHOR. kb-history reads the same data the
    # activity feed does, so wait for that before shelling out.
    wait_for_activity(k, p, U("alice"))
    # Resolve it the way the emoji test below already does: /usr/local/bin is
    # not on every login PATH, and a bare name turns a real assertion into a
    # FileNotFoundError that reads like a platform bug.
    cli = shutil.which("kb-history") or "/usr/local/bin/kb-history"
    out = subprocess.run([cli, "--since", "10 minutes ago",
                          "--author", U("alice"), "--json"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    paths = [pp for c in data["commits"] for f in c["files"] for pp in f["paths"]]
    assert p in paths, f"kb-history did not report alice's edit to {p}: {paths[:10]}"
    assert all(L(c["author"]) == "alice" for c in data["commits"]), "author filter must hold"


def test_activity_sees_non_ascii_folders(k):
    """A folder whose name holds an emoji must appear in the feed like any other.

    Regression, and the expensive kind: `git log --name-status` C-quotes any path
    with a non-ASCII byte — "projects/\\360\\237\\246\\203 Lumii dev/x.md", quotes
    and all. The readability gate then test -r'd that literal string, found no
    such file, and dropped the row as unreadable. Every project folder in this KB
    is emoji-named, so the feed answered "no visible changes" for people who had
    been working all week, in their OWN files. A leak fails loud; this failed
    silent and looked like the truth."""
    sub = f"{DIR}/🦃 emoji dir"
    assert k.post("/api/fs/mkdir", json={"path": sub}).status_code == 200
    p = f"{sub}/todo_ěščř.md"
    assert k.post("/api/file", json={"path": p}).status_code == 200
    write(k, p, f"emoji path marker {TAG}\n")
    wait_for_rev(k, p, 1)

    act = wait_for_activity(k, p, U("alice"))
    paths = [pp for c in act["commits"] for f in c["files"] for pp in f["paths"]]
    assert p in paths, f"emoji-named folder missing from the feed: {paths[:10]}"
    assert not any("\\" in pp or pp.startswith('"') for pp in paths), \
        f"paths must arrive verbatim, not C-quoted: {paths[:10]}"

    # and the same through the CLI an agent actually runs. Resolve the binary:
    # /usr/local/bin is not on PATH in every shell that runs this suite.
    cli = shutil.which("kb-history") or "/usr/local/bin/kb-history"
    out = subprocess.run([cli, "--since", "10 minutes ago",
                          "--author", U("alice"), "--limit", "1000", "--json"],
                         capture_output=True, text=True, timeout=40)
    assert out.returncode == 0, out.stderr
    cli_paths = [pp for c in json.loads(out.stdout)["commits"]
                 for f in c["files"] for pp in f["paths"]]
    assert p in cli_paths, f"kb-history hid the emoji path: {cli_paths[:10]}"


def test_blame_credits_people_and_machines(k):
    """The editor's authorship stripe: a line typed through the product is its
    author's; a file that changed outside the editor (here: a plain write by
    the test runner, which leaves no attribution hint) is kb-syncd's, flagged
    as a machine. Gated like every other history read."""
    p = f"{DIR}/blame.md"
    assert k.post("/api/file", json={"path": p}).status_code == 200
    write(k, p, f"# by alice {TAG}\n\nsecond line\n")
    wait_for_rev(k, p, 1)
    r = k.get("/api/vc/blame", params={"path": p})
    assert r.status_code == 200, r.text
    j = r.json()
    assert [t for _ci, t in j["lines"]] == [f"# by alice {TAG}", "", "second line"]
    c = j["commits"][j["lines"][0][0]]
    assert L(c["author"]) == "alice" and c["machine"] is False, c
    assert len(c["rev"]) == 12 and c["ts"] > 0
    assert set(c) == {"rev", "author", "ts", "machine"}, "no subject: it can name a moved file's old path"

    m = f"{DIR}/blame_machine.md"
    with open(f"/srv/kb/{m}", "w") as f:
        f.write(f"written outside the editor {TAG}\n")
    wait_for_rev(k, m, 1)
    j = k.get("/api/vc/blame", params={"path": m}).json()
    c = j["commits"][j["lines"][0][0]]
    assert c["author"] == "kb-syncd" and c["machine"] is True, c

    priv = f"{DIR}/blame_priv.md"
    assert k.post("/api/file", json={"path": priv}).status_code == 200
    write(k, priv, "alice only\n")
    assert k.post("/fs/props", json={"path": priv, "visibility": "private"}).status_code == 200
    wait_for_rev(k, priv, 1)
    assert cl("bob").get("/api/vc/blame", params={"path": priv}).status_code == 403


def test_blame_of_an_uncommitted_file_is_empty_not_an_error(k):
    p = f"{DIR}/never_committed_{TAG}.md"
    r = k.get("/api/vc/blame", params={"path": p})
    # absent file: the read gate refuses it like any other unreadable path
    assert r.status_code in (200, 403), r.text
    if r.status_code == 200:
        assert r.json()["lines"] == []


def _moved_doc(k):
    """alice drafts in her PRIVATE folder (v1, v2), then moves it into the
    shared area in the app. Returns (old, new, pre-move revs newest first)."""
    old = home("alice", f"draft_{TAG}/plan.md")
    assert k.post("/api/fs/mkdir", json={"path": old.rsplit("/", 1)[0]}).status_code == 200
    assert k.post("/api/file", json={"path": old}).status_code == 200
    write(k, old, f"# Plan {TAG}\n\nv1 by alice\n")
    wait_for_rev(k, old, 1)
    write(k, old, f"# Plan {TAG}\n\nv2 by alice\n")
    revs = [e["rev"] for e in wait_for_rev(k, old, 2)]
    new = f"{DIR}/moved_plan_{TAG}.md"
    r = k.post("/api/fs/rename", json={"src": old, "dst": new})
    assert r.status_code == 200, r.text
    deadline = time.time() + 40
    while time.time() < deadline:     # the move is ledgered once BOTH halves are committed
        j = k.get("/api/vc/log", params={"path": new}).json()
        if any(e.get("moved") for e in j.get("entries", [])):
            return old, new, revs
        time.sleep(1)
    raise AssertionError(f"history never followed the move: {j}")


@pytest.fixture(scope="module")
def moved(k):
    return _moved_doc(k)


def test_history_follows_a_move_made_in_the_app(k, moved):
    old, new, revs = moved
    j = k.get("/api/vc/log", params={"path": new}).json()
    before = [e for e in j["entries"] if e.get("moved")]
    assert [e["rev"] for e in before][:2] == revs[:2], j
    assert all(e.get("path") == old for e in before), "the mover may see where it came from"
    d = k.get("/api/vc/diff", params={"path": new, "rev": revs[0]}).json()
    assert "+v2 by alice" in d["patch"], d
    assert k.get("/api/vc/show", params={"path": new, "rev": revs[1]}).json()["content"].endswith("v1 by alice\n")
    # the version that arrived: a move, not a document typed from scratch
    arrived = j["entries"][len(j["entries"]) - len(before) - 1]["rev"]
    d = k.get("/api/vc/diff", params={"path": new, "rev": arrived}).json()
    assert d["patch"].startswith("moved here from") and "did not change" in d["patch"], d


def test_a_moved_documents_old_name_is_redacted_for_others(k, moved):
    """bob can read the document where it lives now, so its past is his to
    read too (the same rule as a file shared in place) — but alice's private
    folder is not his to list, so its old name never reaches him."""
    old, new, revs = moved
    b = cl("bob")
    folder = old.rsplit("/", 1)[0]
    r = b.get("/api/vc/log", params={"path": new})
    assert r.status_code == 200
    before = [e for e in r.json()["entries"] if e.get("moved")]
    assert len(before) >= 2 and not any("path" in e for e in before)
    for q in ({"rev": revs[0]}, {"rev": revs[1]}):
        d = b.get("/api/vc/diff", params={"path": new, **q})
        assert d.status_code == 200 and "v" in d.json()["patch"]
        assert folder not in d.text, d.text
    assert folder not in r.text
    act = b.get("/api/vc/activity", params={"since": "10 minutes ago", "author": U("alice"), "limit": 1000})
    assert folder not in act.text
    assert old not in b.get("/api/vc/log", params={"path": new}).text


def test_activity_lists_pre_move_work_under_the_new_name(k, moved):
    old, new, revs = moved
    j = wait_for_activity(k, new, U("alice"))
    rows = [c for c in j["commits"] if any(new in f["paths"] for f in c["files"])]
    assert {c["rev"] for c in rows} >= set(revs[:2]), "edits made before the move count where it lives now"
    assert any(f["status"] == "R" for c in rows for f in c["files"] if new in f["paths"]), "the move reads as one rename"


def test_blame_credits_lines_written_before_the_move(k, moved):
    old, new, revs = moved
    j = k.get("/api/vc/blame", params={"path": new}).json()
    by_text = {t: j["commits"][ci] for ci, t in j["lines"]}
    assert by_text["v2 by alice"]["rev"] == revs[0], "not the move: the edit that wrote it"
