"""git blame --porcelain -> the editor's authorship stripe, without a server."""
from kb_platform.syncd import SyncDaemon

A, B = "a" * 40, "b" * 40
PORCELAIN = f"""{A} 1 1 2
author alice
author-mail <alice@example>
author-time 1700000000
author-tz +0000
committer alice
summary edit: notes.md
filename notes.md
\t# Title
{A} 2 2
\t
{B} 3 3 1
author kb-syncd
author-time 1700000100
summary sync: auto-snapshot
previous {A} notes.md
filename notes.md
\t\tkeeps\ttabs
"""


def test_parse_keeps_each_commit_once_and_every_line():
    commits, lines = SyncDaemon._parse_blame(PORCELAIN)
    assert commits == [
        {"sha": A, "rev": A[:12], "author": "alice", "ts": 1700000000},
        {"sha": B, "rev": B[:12], "author": "kb-syncd", "ts": 1700000100},
    ]
    # [commit, text, line number in that commit's version]
    assert lines == [[0, "# Title", 1], [0, "", 2], [1, "\tkeeps\ttabs", 3]]


def test_no_path_ever_leaves_the_parser():
    """Across a move, summary/filename/previous name the OLD path; none of it
    may reach the response."""
    commits, _ = SyncDaemon._parse_blame(PORCELAIN)
    assert "notes.md" not in repr(commits) and "sync:" not in repr(commits)


def test_only_login_accounts_are_people():
    cache = {}
    assert SyncDaemon._is_person("kb-syncd", cache) is False   # a commit name, not an account
    assert SyncDaemon._is_person("root", cache) is False       # uid 0
    assert SyncDaemon._is_person("nobody", cache) is False     # outside the login range
