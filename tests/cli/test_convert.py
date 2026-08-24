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
