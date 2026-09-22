"""What a section looks like when it is embedded — and what never goes with it.

Pure: no database, no provider. kb_platform/indexer.py, "chunks".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kb_platform.indexer import SHEET_PIECES, _breadcrumb, chunk_sections, clean_for_embedding  # noqa: E402

M = "test:model:1024"


def texts(doc, rel="company/doc.md"):
    return [c["text"] for c in chunk_sections(doc, rel, M)]


# ---- credentials never leave ------------------------------------------------------
def test_assigned_secrets_are_redacted_and_the_name_kept():
    for line, gone in [
        ('VERTEX_AI_CLIENT_SECRET="ZXhhbXBsZS1ub3QtYS1yZWFsLWtleS0xMjM0NTY3ODkw"', "ZXhhbXBsZS1ub3Q"),
        ("api_key = sk-proj-abcdefghijklmnopqrstuvwxyz0123456789", "abcdefghijklmnop"),
        ("password: Heslo2026!", "Heslo2026"),
        ('"client_secret": "GOCSPX-abcdefghijklmnop"', "GOCSPX"),
        ("DB_PASSWORD=hunter22hunter", "hunter22"),
    ]:
        out = clean_for_embedding(line)
        assert gone not in out and "[secret]" in out, (line, out)


def test_well_known_token_shapes_are_redacted_anywhere():
    for tok in ["ghp_" + "a1" * 18, "AKIA" + "ABCDEFGHIJKLMNOP", "xoxb-1234567890-abcdefghij",
                "AIza" + "B" * 35, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"]:
        assert tok not in clean_for_embedding(f"we use {tok} for it"), tok


def test_a_private_key_spanning_lines_is_blanked_and_line_numbers_survive():
    doc = ("# Setup\n\n## Key\n\n```\n-----BEGIN PRIVATE KEY-----\nMIIEexampleNOTAREALKEYexampleAAAA\n"
           "abcdefABCDEF0123456789abcdef\n-----END PRIVATE KEY-----\n```\nUse it for the service account.\n")
    [c] = chunk_sections(doc, "projects/tool/README.md", M)
    assert "MIIE" not in c["text"] and "abcdefABCDEF" not in c["text"] and "[private key]" in c["text"]
    assert "service account" in c["text"]
    assert (c["start_line"], c["end_line"]) == (5, 11)


def test_a_key_pasted_as_one_json_line_is_blanked_too():
    line = '  "private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEexampleNOT\\nxyEXAMPLEkey\\n-----END PRIVATE KEY-----\\n",'
    out = clean_for_embedding(line)
    assert "MIIE" not in out and "xyEX" not in out


def test_prose_about_passwords_is_not_mangled():
    s = "The token expires after an hour; the password must be long."
    assert clean_for_embedding(s) == s


# ---- noise that costs tokens and means nothing ------------------------------------
def test_blobs_ids_padding_and_table_rules_are_squeezed_out():
    assert clean_for_embedding("|---|:---:|---|") == ""
    assert clean_for_embedding("| a   |      b      |") == "| a | b |"
    assert "[id]" in clean_for_embedding("owner 11111111-2222-3333-4444-555555555555")
    assert clean_for_embedding("blob QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVoxMjM0NTY3ODkw end") == "blob […] end"
    assert clean_for_embedding("Hhhhhhhhhhhhhhhhhhhh!") == "Hhhh!"


def test_links_keep_their_words_not_their_tracking_parameters():
    out = clean_for_embedding("[Jane Doe](https://www.linkedin.com/sales/lead/ACwAAB1234567890abcdefXYZ,NAME) and "
                              "![CV.pdf](_files/CV.pdf) and https://example.com/a/b/c/d?x=1")
    assert "Jane Doe" in out and "ACwAAB" not in out and "[image: CV.pdf]" in out
    assert "example.com/a/b" in out and "x=1" not in out


def test_a_section_of_nothing_but_a_blob_is_not_embedded():
    doc = "# Keys\n\n## Blob\n\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVoxMjM0NTY3ODkw\n\n## Real\n\nThe rollout plan.\n"
    assert [t.split("\n")[0] for t in texts(doc)] == ["Keys › Real"]


# ---- context: every section knows where it lives -------------------------------------
def test_sections_carry_their_folder_and_readme_says_its_folder():
    assert _breadcrumb("projects/💱 Acme Billing/data.md") == ["💱 Acme Billing"]
    assert _breadcrumb("users/alice/Notes/_files/.x.pdf.md") == ["Notes"]
    assert _breadcrumb("company/onboarding.md") == []
    [t] = texts("Setup steps for the stack.\n", "projects/💱 Acme Billing/data.md")
    assert t.startswith("💱 Acme Billing › data\n")
    [t] = texts("What lives here.\n", "projects/💱 Acme Billing/README.md")
    assert t.startswith("💱 Acme Billing\n")


def test_a_spreadsheet_sheet_is_its_header_and_first_rows():
    rows = "\n".join(f"| {i} | customer {i} | Prague | active |" for i in range(400))
    doc = "# Sheet1\n\n| id | name | city | state |\n|---|---|---|---|\n" + rows + "\n"
    out = chunk_sections(doc, "projects/x/_files/.crm.xlsx.md", M)
    assert len(out) == SHEET_PIECES and "| id | name | city | state |" in out[0]["text"]
    assert len(chunk_sections(doc, "projects/x/crm-notes.md", M)) > SHEET_PIECES   # a document is not capped


def test_the_same_file_always_yields_the_same_hashes():
    doc = "# Plan\n\nkey: sk-proj-abcdefghijklmnopqrstuvwxyz0123456789\n\n## Budget\n\nQ3 approved.\n"
    a = [c["hash"] for c in chunk_sections(doc, "company/plan.md", M)]
    assert a == [c["hash"] for c in chunk_sections(doc, "company/plan.md", M)]
    assert a != [c["hash"] for c in chunk_sections(doc, "company/plan.md", "other:model:1024")]
