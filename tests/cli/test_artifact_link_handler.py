"""Where the platform puts the link handler (artifact_links.js) in an artifact
it serves: the first thing in the head, never inside the page's own script,
never before the doctype. Pure — the function, no hub."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kb_platform import user_server  # noqa: E402

MARK = "// Added by the platform to every artifact"
put = user_server.with_link_handler


def test_it_goes_first_in_the_head():
    out = put("<!doctype html>\n<html lang=en>\n<head>\n<meta charset=utf-8></head><body></body></html>", "company/d/p.html")
    assert out.startswith("<!doctype html>\n<html lang=en>\n<head><script>" + MARK)
    assert out.count(MARK) == 1


def test_without_a_head_it_goes_after_the_doctype_and_never_into_the_pages_script():
    out = put("<!DOCTYPE html><p>hi</p><script>var s = '<head>';</script>", "company/p.html")
    assert out.startswith("<!DOCTYPE html><script>" + MARK), "after the doctype, so no quirks mode"
    assert "var s = '<head>';" in out, "the page's own script is untouched"
    out = put("<script>document.write('<head>')</script><head></head>", "company/p.html")
    assert out.startswith("<script>" + MARK)


def test_it_knows_the_pages_folder_and_cannot_be_broken_out_of():
    assert 'var DIR = "projects/acme/dash";' in put("<head></head>", "projects/acme/dash/p.html")
    assert 'var DIR = "";' in put("", "p.html")
    out = put("", "company/a</script><b/p.html")
    assert "a<\\/script>" in out and out.count("</script>") == 1
