"""How full-text and semantic hits become one list — pure, no database.
kb_platform/hybrid.py; the live half is test_semantic_search.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kb_platform import hybrid  # noqa: E402


# ---- fusion (pure) ---------------------------------------------------------------------
def _vec(path, seq, start, end, text, d=0.3):
    return {"path": path, "seq": seq, "start": start, "end": end, "heading": "T", "text": text, "distance": d}


def _txt(path, line, text, rank=0.5):
    return {"path": path, "line": line, "kind": "text", "text": text, "rank": rank}


def test_a_word_hit_inside_a_matched_section_is_one_result_on_the_matching_line():
    sec = {"seq": 1, "start": 5, "end": 9, "text": "T › Money\nIntro\n\nThe budget is approved"}
    out = hybrid.fuse("budget", [_txt("a.md", 7, "The budget is approved"), _txt("a.md", 8, "budget again")],
                      [_vec("a.md", 1, 5, 9, "T › Money\nIntro\n\nThe budget is approved")],
                      {("a.md", 7): sec, ("a.md", 8): sec})
    assert len(out) == 1
    assert out[0]["why"] == "both" and out[0]["line"] == 7 and out[0]["text"] == "The budget is approved"


def test_both_beats_either_alone():
    out = hybrid.fuse("q", [_txt("b.md", 3, "words only"), _txt("a.md", 2, "in both")],
                      [_vec("c.md", 0, 1, 4, "T\nmeaning only"), _vec("a.md", 0, 1, 4, "T\nin both")])
    assert out[0]["path"] == "a.md" and out[0]["why"] == "both"
    assert {o["why"] for o in out[1:]} == {"text", "meaning"}


def test_the_same_boilerplate_in_ten_folders_is_listed_once():
    vec = [_vec(f"p{i}/README.md", 0, 1, 3, f"Project {i}\nAll materials for the project live here.") for i in range(10)]
    out = hybrid.fuse("where are the project materials", [], vec)
    assert len(out) == 1


def test_a_word_only_hit_is_reranked_on_its_whole_section():
    sec = {"seq": 2, "start": 10, "end": 14, "text": "Doc › Costs\nThe office rent is 600.\nPaid monthly."}
    [u] = hybrid.fuse("rent", [_txt("d.md", 11, "The office rent is 600.")], [], {("d.md", 11): sec})
    assert u["doc"] == sec["text"] and u["line"] == 11 and u["why"] == "text"


def test_snippets_match_inflected_words():
    s, _ = hybrid.snippet("T\nÚvod\nRozpočtu na Q3 jsme schválili.", "rozpočet")
    assert s.startswith("Rozpočtu")


def test_one_chatty_file_cannot_fill_the_list():
    text = [_txt("big.md", i, f"line {i}") for i in range(1, 20)] + [_txt("small.md", 1, "x")]
    out = hybrid.fuse("q", text, [])
    assert sum(1 for o in out if o["path"] == "big.md") == hybrid.PER_FILE
    assert any(o["path"] == "small.md" for o in out)


def test_the_snippet_is_the_line_that_shares_the_most_words():
    s, _ = hybrid.snippet("Doc › Plan\n- [ ] Kickoff meeting\n- [ ] Finalize the hospital integration spec",
                          "hospital spec")
    assert s == "Finalize the hospital integration spec"
