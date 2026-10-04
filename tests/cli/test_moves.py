"""kb_platform/moves.py against scratch git repos: which deletes and adds count
as a move, and how a document's history is followed back through them. No
server; commits are made the way kb-syncd makes them (one file per commit for
attributed edits, a sweep for the rest), with controlled timestamps."""
import os
import subprocess

import pytest

from kb_platform import moves

T0 = 1_790_000_000


class Repo:
    def __init__(self, root):
        self.root = root
        self.t = T0
        self.run("init", "-q")

    def run(self, *args, env=None):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True,
                              env={**os.environ, **(env or {})}, check=True).stdout

    def git(self, args):
        r = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else ""

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, *paths, author="alice", dt=1):
        """Stage `paths` (all changes when none) and commit at T0 + elapsed."""
        self.t += dt
        self.run("add", "-A", "--", *(paths or ["."]))
        date = f"@{self.t} +0000"
        self.run("-c", f"user.name={author}", "-c", f"user.email={author}@x", "commit", "-q",
                 "--allow-empty", "-m", f"edit: {paths[0] if paths else 'sweep'}",
                 env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        return self.run("rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def md(p):
    return p.endswith(".md")


def find(repo):
    return moves.find_moves(repo.git, md)


def test_a_move_split_over_two_commits_is_one_move(repo):
    repo.write("drafts/plan.md", "# Plan\n\nwritten by alice\n")
    repo.commit("drafts/plan.md")
    os.rename(repo.root / "drafts/plan.md", repo.root / "plan.md")
    add = repo.commit("plan.md", author="bob")             # the mover's attributed add …
    dele = repo.commit(author="kb-syncd")                   # … and the sweep's delete
    [m] = find(repo)
    assert (m["old"], m["new"], m["add"], m["del"], m["via"]) == ("drafts/plan.md", "plan.md", add, dele, "pair")


def test_a_rename_inside_one_commit_is_one_move(repo):
    repo.write("a.md", "one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n")
    repo.commit("a.md")
    os.rename(repo.root / "a.md", repo.root / "b.md")
    c = repo.commit(author="kb-syncd")
    [m] = find(repo)
    assert (m["old"], m["new"], m["add"], m["del"], m["via"]) == ("a.md", "b.md", c, c, "commit")


def test_copy_then_delete_later_is_a_fresh_start(repo):
    repo.write("a.md", "keep my past\n")
    repo.commit("a.md")
    repo.write("b.md", "keep my past\n")
    repo.commit("b.md")
    os.remove(repo.root / "a.md")
    repo.commit("a.md", dt=moves.PAIR_WINDOW + 50)
    assert find(repo) == []


def test_identical_twins_are_ambiguous_so_nothing_is_linked(repo):
    for p in ("x/a.md", "y/a.md"):
        repo.write(p, "same template\n")
    repo.commit()
    os.rename(repo.root / "x/a.md", repo.root / "x/b.md")
    os.remove(repo.root / "y/a.md")
    repo.commit("x/b.md")
    repo.commit()
    assert find(repo) == []


def test_empty_files_and_unversioned_paths_never_pair(repo):
    repo.write("a.md", "")
    repo.write("a.txt", "content\n")
    repo.commit()
    os.rename(repo.root / "a.md", repo.root / "b.md")
    os.rename(repo.root / "a.txt", repo.root / "b.txt")
    repo.commit("b.md")
    repo.commit()
    assert find(repo) == []


def test_lineage_follows_moves_and_stops_at_the_files_own_creation(repo, tmp_path_factory):
    # an UNRELATED earlier file once lived at old.md: deleted long before
    repo.write("old.md", "someone else's document\n")
    repo.commit("old.md", author="carol")
    os.remove(repo.root / "old.md")
    repo.commit("old.md", author="carol")
    # the real document: created at old.md, edited, moved twice, edited
    repo.write("old.md", "v1 by alice\n")
    born = repo.commit("old.md", dt=100)
    repo.write("old.md", "v2 by alice\n")
    v2 = repo.commit("old.md")
    os.rename(repo.root / "old.md", repo.root / "mid.md")
    repo.commit("mid.md", author="bob", dt=100)
    repo.commit(author="kb-syncd")
    os.rename(repo.root / "mid.md", repo.root / "now.md")
    repo.commit(author="kb-syncd", dt=100)                  # caught whole by the sweep
    repo.write("now.md", "v3 by bob\n")
    repo.commit("now.md", author="bob")
    led = moves.Ledger(tmp_path_factory.mktemp("l") / "moves.jsonl")
    assert led.add(find(repo)) == 2
    segs = moves.lineage(repo.git, led, "now.md")
    assert [s["path"] for s in segs] == ["now.md", "mid.md", "old.md"]
    authors = [e["author"] for s in segs for e in s["entries"]]
    assert "carol" not in authors, "an unrelated file that once had the name is not this one's past"
    assert moves.path_at(segs, v2) == "old.md" and moves.path_at(segs, born) == "old.md"
    # the version list asks for a few; the walk stops early
    assert sum(len(s["entries"]) for s in moves.lineage(repo.git, led, "now.md", limit=2)) == 2


def test_ledger_persists_and_dedupes(tmp_path):
    p = tmp_path / "moves.jsonl"
    link = {"old": "a.md", "new": "b.md", "add": "1" * 40, "del": "2" * 40, "ts": 1, "via": "pair"}
    led = moves.Ledger(p)
    assert led.add([link, dict(link)]) == 1
    assert led.add([link]) == 0
    again = moves.Ledger(p)
    assert len(again) == 1 and again.by_new["b.md"][0]["old"] == "a.md"
    assert oct(p.stat().st_mode & 0o777) == "0o600"
