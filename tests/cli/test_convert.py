"""kb-convert: sidecar naming, the indexer's one dot-file exception, table
capping, frontmatter idempotency, and ACL-spec construction. The end-to-end
conversion test runs only where markitdown is installed (the service's own
venv, or a dev box) — CI's test venv deliberately stays light."""
from pathlib import Path

import pytest

from kb_platform import common
from kb_platform import convert
from kbenv import doc


# ---- naming and the hidden/indexed split -----------------------------------

def test_sidecar_naming_round_trip():
    src = Path("/srv/kb/company/_files/Q3 Review.pptx")
    side = convert.sidecar_path(src)
    assert side.name == ".Q3 Review.pptx.md"
    assert side.parent == src.parent
    assert convert.source_name_of(side.name) == "Q3 Review.pptx"


@pytest.mark.parametrize("rel,expected", [
    (doc("_files/.report.docx.md"), True),
    (".Talk.pptx.md", True),
    ("a/b/.data.xlsx.md", True),
    ("a/.scan.pdf.md", True),
    ("a/.old.doc.md", True),
    (doc("report.docx.md"), False),     # no dot prefix: a normal doc
    (doc(".hidden.md"), False),         # dot-md without a source suffix
    (doc(".report.docx"), False),       # not markdown
    (doc("report.docx"), False),
    (".claude", False),
])
def test_is_derived_sidecar(rel, expected):
    assert common.is_derived_sidecar(rel) is expected


@pytest.mark.parametrize("rel,hidden", [
    (".claude/skills/x/SKILL.md", True),   # dot-dir trees stay machinery
    (".agents/skills/x/SKILL.md", True),
    (".git/config", True),
    (doc(".claude/x.md"), True),
    (doc(".notes.md"), True),           # plain dot-files stay hidden
    (".gitignore", True),
    (doc(".report.docx.md"), False),    # THE exception: derived sidecars index
    (doc("a.md"), False),
    (doc("sub/b.md"), False),
])
def test_is_hidden_rel(rel, hidden):
    assert common.is_hidden_rel(rel) is hidden


# ---- spreadsheet row cap -----------------------------------------------------

def test_cap_table_rows_truncates_and_resets():
    rows = ["| h1 | h2 |", "|----|----|"] + [f"| a{i} | b{i} |" for i in range(10)]
    text = "\n".join(rows) + "\n\nprose between tables\n\n" + "\n".join(rows)
    capped = convert.cap_table_rows(text, cap=5)
    lines = capped.splitlines()
    # each table independently capped: 5 kept + 1 marker, twice
    assert sum("omitted" in l for l in lines) == 2
    assert "| a2 | b2 |" in lines
    assert "| a9 | b9 |" not in lines
    assert "prose between tables" in lines


def test_cap_table_rows_untouched_below_cap():
    text = "| a |\n| b |\nplain"
    assert convert.cap_table_rows(text, cap=500) == text


# ---- frontmatter / idempotency ------------------------------------------------

def test_compose_and_parse_hash_round_trip():
    sha = "ab" * 32
    out = convert.compose_sidecar("Q3.pptx", sha, "ok", "body text")
    assert convert.parse_sidecar_hash(out) == sha
    assert "derived_from: Q3.pptx" in out
    assert "status: ok" in out
    assert out.rstrip().endswith("body text")


def test_parse_hash_rejects_documents_without_one():
    assert convert.parse_sidecar_hash("# just a doc\ntext\n") is None
    assert convert.parse_sidecar_hash("---\ntitle: x\n---\nbody\n") is None


# ---- ACL cloning ---------------------------------------------------------------

def test_acl_spec_shared_group_file():
    spec = convert.build_acl_spec("alice", "proj-acme", True, False, [], [])
    assert spec == "u::rw-,g::---,o::---,m::r--,u:alice:r--,g:proj-acme:r--"


def test_acl_spec_world_readable():
    spec = convert.build_acl_spec("bob", "kb-users", True, True, [], [])
    assert "o::r--" in spec and "g:kb-users:r--" in spec


def test_acl_spec_private_file_with_named_readers():
    # 0640-style file shared to carol + group dash via named ACLs
    spec = convert.build_acl_spec("alice", "alice", False, False, ["carol"], ["dash"])
    assert spec == "u::rw-,g::---,o::---,m::r--,u:alice:r--,u:carol:r--,g:dash:r--"
    # nobody gets write, the setgid dir's group gets nothing
    assert "w" not in spec.replace("u::rw-", "")


def test_parse_getfacl_reads_named_entries():
    out = "user:carol:r--\ngroup::rw-\ngroup:dash:r-x\nmask::r-x\nother::---\n"
    group_perm, mask, nu, ng = convert.parse_getfacl(out)
    assert group_perm == "rw-" and mask == "r-x"
    assert nu == {"carol": "r--"} and ng == {"dash": "r-x"}


# ---- litter --------------------------------------------------------------------

@pytest.mark.parametrize("name,litter", [
    ("~$report.docx", True),               # Office lock file
    (".~lock.report.docx#", True),         # LibreOffice lock
    ("draft.tmp", True),
    ("x.docx.kbtmp", True),
    ("report.docx", False),
    ("Q3 Review.pptx", False),
])
def test_is_litter(name, litter):
    assert convert.is_litter(name) is litter


# ---- end-to-end (needs markitdown + openpyxl: the convert venv or a dev box) ----

def test_convert_one_xlsx_end_to_end(tmp_path, monkeypatch):
    pytest.importorskip("markitdown")
    openpyxl = pytest.importorskip("openpyxl")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["quarter", "revenue"])
    ws.append(["Q3", 1234567])
    src = tmp_path / "numbers.xlsx"
    wb.save(src)

    conv = convert.Converter()
    monkeypatch.setattr(conv, "root", tmp_path)
    assert conv.convert_one(src) == "ok"

    side = tmp_path / ".numbers.xlsx.md"
    text = side.read_text()
    assert "1234567" in text and "status: ok" in text
    # second run: hash matches, nothing rewritten
    assert conv.convert_one(src) is None
    # sidecar is read-only for group/other (ACL may be a no-op on exotic fs,
    # so assert via the mode bits which setfacl/chmod both set)
    mode = side.stat().st_mode & 0o777
    assert mode & 0o022 == 0, f"sidecar is group/other-writable: {oct(mode)}"


# ---- the extraction child reads a PINNED fd, not a path --------------------
# convert._convert_locked opens the source once with O_NOFOLLOW and hands the
# child /proc/self/fd/N, so a user who owns the directory cannot swap a symlink
# in between the is-it-a-regular-file check and the parser's open. A pin has no
# extension, which is exactly what broke xlsx the first time: openpyxl picks
# its reader from the filename. These pin the plumbing that fixes that.

def _extract_via(argv_path, out, name=None):
    import os, subprocess, sys
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    argv = [sys.executable, "-m", "kb_platform.convert_extract", str(argv_path), str(out)]
    fds = ()
    if name is not None:
        argv.append(name)
        fds = (int(str(argv_path).rsplit("/", 1)[1]),)
    return subprocess.run(argv, capture_output=True, text=True, env=env,
                          timeout=120, pass_fds=fds)


@pytest.mark.parametrize("suffix", [".xlsx", ".docx"])
def test_pinned_extraction_matches_extraction_by_path(tmp_path, suffix):
    import os
    openpyxl = pytest.importorskip("openpyxl")
    if suffix == ".docx":
        pytest.importorskip("markitdown")
    src = tmp_path / f"book{suffix}"
    if suffix == ".xlsx":
        wb = openpyxl.Workbook(); wb.active["A1"] = "pinned-roundtrip"; wb.save(src)
    else:
        real = Path("/srv/kb")
        cand = [p for p in real.rglob("*.docx")][:1]
        if not cand:
            pytest.skip("no .docx on this box to compare against")
        src.write_bytes(cand[0].read_bytes())

    by_path, by_pin = tmp_path / "a.md", tmp_path / "b.md"
    assert _extract_via(src, by_path).returncode == 0, "extraction by path failed"

    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        r = _extract_via(f"/proc/self/fd/{fd}", by_pin, name=src.name)
    finally:
        os.close(fd)
    assert r.returncode == 0, f"extraction through a pinned fd failed: {r.stderr[:300]}"
    assert by_pin.read_text() == by_path.read_text(), \
        "a pinned fd produced different text than the path — format dispatch is broken"


# ---- a sidecar's audience follows its source -------------------------------
# The staleness gate is a CONTENT hash and permissions are not content, so a
# permission-only change used to never reach the extracted text. The sidecar is
# what the index serves and what agents are told to read, so un-sharing a
# document left its full text searchable to the old audience, silently.

def test_tightening_a_source_reaches_its_sidecar(tmp_path):
    import os
    src = tmp_path / "doc.docx"
    side = tmp_path / ".doc.docx.md"
    src.write_bytes(b"pk-not-really")
    side.write_text("---\nsource_sha256: deadbeef\n---\n\nextracted text\n")
    os.chmod(src, 0o644)
    os.chmod(side, 0o644)

    c = convert.Converter()
    c._refresh_sidecar_acl(src, side)          # mirrors the wide source
    world, _team, _named = common.read_audience(*common.stat_and_acl(side))
    assert world, "precondition: the sidecar should start world-readable"

    os.chmod(src, 0o600)                        # the document goes private
    c._refresh_sidecar_acl(src, side)
    world, team, named = common.read_audience(*common.stat_and_acl(side))
    assert not world, "sidecar stayed world-readable after its source was tightened"
    assert not team, "sidecar still grants the owning group"


def test_refresh_is_a_no_op_when_the_audience_has_not_moved(tmp_path):
    """It runs on every sweep for every convertible file — it must not fork
    setfacl each time."""
    import os
    src = tmp_path / "doc.docx"
    side = tmp_path / ".doc.docx.md"
    src.write_bytes(b"x")
    side.write_text("---\nsource_sha256: deadbeef\n---\n")
    os.chmod(src, 0o644)
    c = convert.Converter()
    c._refresh_sidecar_acl(src, side)
    before = dict(c._aud)
    assert before, "the first call should record the audience"
    c._refresh_sidecar_acl(src, side)
    assert c._aud == before, "a second call with no change should do nothing"


# ---- a service account is not an audience ---------------------------------
# hub._grant_indexer puts a named ACL entry for kbindexer on every file the
# share panel touches, PRIVATE ones included, because search has to read them.
# Counting that as a reader made "private" report as "people" and fired the
# move warning on every drag. Caught by test_sharing, pinned here.

def test_the_indexer_grant_is_not_an_audience(tmp_path):
    import os, subprocess
    f = tmp_path / "note.md"
    f.write_text("x")
    os.chmod(f, 0o600)
    if subprocess.run(["setfacl", "-m", "u:kbindexer:r--", str(f)],
                      capture_output=True).returncode != 0:
        import pytest as _p
        _p.skip("no kbindexer account on this box")
    st, entries = common.stat_and_acl(f)
    _world, _team, named = common.read_audience(st, entries)
    assert named, "precondition: the indexer grant should be visible in the raw audience"
    assert common.human_readers(named) == [], \
        "the indexer counted as a person — every private file reads as shared"


def test_a_real_person_still_counts(tmp_path):
    import os, subprocess, getpass
    f = tmp_path / "note.md"
    f.write_text("x")
    os.chmod(f, 0o600)
    me = getpass.getuser()
    if subprocess.run(["setfacl", "-m", f"u:{me}:r--", str(f)],
                      capture_output=True).returncode != 0:
        import pytest as _p
        _p.skip("cannot set an ACL here")
    st, entries = common.stat_and_acl(f)
    _world, _team, named = common.read_audience(st, entries)
    assert common.human_readers(named), "a real person was filtered out as a service account"
