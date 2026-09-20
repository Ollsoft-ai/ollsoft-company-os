"""The platform's own config lives in <REPO>/.os/ (company) and
users/<name>/.os/ (personal) — not in .claude/, which is Claude Code's
directory and holds agent context only. Pure unit tests over the helpers in
common.py: no server, no fixtures, no root, so CI runs them on every push.

The migration runs as root at hub start on a live box; here it runs as the
test user over a tmp_path, which exercises every branch except the chown."""
import os
import stat

import pytest

from kb_platform import common

GITIGNORE = "*\n!*/\n!*.md\n!*.html\n!.gitignore\n!.claude/*.json\n**/_secrets/**\n.*.md\n"


def _mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A pre-2026-09 layout: config JSON in .claude/ next to the agent context."""
    monkeypatch.setattr(common, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(common, "COMPANY_CONFIG_DIR", tmp_path / common.CONFIG_DIRNAME)
    (tmp_path / ".claude" / "skills" / "x").mkdir(parents=True)
    (tmp_path / ".claude" / "CLAUDE.md").write_text("context\n")
    (tmp_path / ".claude" / "skills" / "x" / "SKILL.md").write_text("skill\n")
    (tmp_path / ".claude" / "launchers.json").write_text('{"buttons": []}\n')
    (tmp_path / ".claude" / "egress.json").write_text('{"a.html": {"domains": ["x.com"]}}\n')
    (tmp_path / ".gitignore").write_text(GITIGNORE)
    (tmp_path / "users" / "me").mkdir(parents=True)
    return tmp_path


def test_company_migration_moves_json_keeps_inode_and_leaves_agent_context(repo):
    ino = {n: os.stat(repo / ".claude" / n).st_ino for n in common.MIGRATED_CONFIG_FILES}
    done = common.migrate_company_config()
    assert any("moved .claude/egress.json" in d for d in done), done
    for n in common.MIGRATED_CONFIG_FILES:
        assert os.stat(repo / ".os" / n).st_ino == ino[n]       # a rename, not a copy
        assert not (repo / ".claude" / n).exists()
    assert (repo / ".claude" / "CLAUDE.md").read_text() == "context\n"
    assert (repo / ".claude" / "skills" / "x" / "SKILL.md").exists()
    lines = (repo / ".gitignore").read_text().splitlines()
    assert lines.count(common.GITIGNORE_CONFIG_RULE) == 1
    assert "!.claude/*.json" in lines                          # left alone: harmless
    # idempotent: a second run has nothing to do and touches nothing
    assert common.migrate_company_config() == []
    assert (repo / ".gitignore").read_text().splitlines().count(common.GITIGNORE_CONFIG_RULE) == 1


def test_company_migration_never_overwrites_an_existing_new_file(repo):
    (repo / ".os").mkdir()
    (repo / ".os" / "launchers.json").write_text('{"buttons": [{"label": "keep", "kind": "term", "target": "x"}]}\n')
    done = common.migrate_company_config()
    assert (repo / ".os" / "launchers.json").read_text().count("keep") == 1
    assert (repo / ".claude" / "launchers.json").exists()        # reported, not deleted
    assert any("both .claude/launchers.json and .os/launchers.json" in d for d in done), done
    assert not (repo / ".claude" / "egress.json").exists()       # the other one still moved


def test_company_migration_refuses_a_planted_symlink(repo):
    victim = repo / "victim"
    victim.mkdir()
    os.symlink(victim, repo / ".os")
    done = common.migrate_company_config()
    assert any(d.startswith("refusing:") for d in done), done
    assert not list(victim.iterdir())                            # nothing landed there
    for n in common.MIGRATED_CONFIG_FILES:
        assert (repo / ".claude" / n).exists()
    assert common.GITIGNORE_CONFIG_RULE not in (repo / ".gitignore").read_text()


def test_fresh_install_has_nothing_to_migrate(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(common, "COMPANY_CONFIG_DIR", tmp_path / common.CONFIG_DIRNAME)
    (tmp_path / ".gitignore").write_text("*\n!.os/*.json\n")
    done = common.migrate_company_config()
    assert (tmp_path / ".os").is_dir()
    assert not any("moved" in d for d in done), done
    assert (tmp_path / ".gitignore").read_text() == "*\n!.os/*.json\n"   # rule present: untouched


def test_write_company_config_in_place_keeps_the_inode(repo):
    common.migrate_company_config()
    p = repo / ".os" / "egress.json"
    ino = os.stat(p).st_ino
    common.write_company_config("egress.json", b'{"b.html": {"domains": ["y.com"]}}\n', in_place=True)
    assert os.stat(p).st_ino == ino                              # ACLs would have survived
    assert p.read_text() == '{"b.html": {"domains": ["y.com"]}}\n'


def test_write_company_config_atomic_replaces_the_inode_and_leaves_no_tmp(repo):
    common.migrate_company_config()
    p = repo / ".os" / "launchers.json"
    ino = os.stat(p).st_ino
    common.write_company_config("launchers.json", b'{"buttons": []}\n', in_place=False)
    assert os.stat(p).st_ino != ino
    assert _mode(p) == 0o644
    assert sorted(x.name for x in (repo / ".os").iterdir()) == ["egress.json", "launchers.json"]


@pytest.mark.parametrize("name", ["../x", "a/b", ".hidden", ""])
def test_config_file_names_are_plain(repo, name):
    with pytest.raises(ValueError):
        common.write_company_config(name, b"{}", in_place=True)
    with pytest.raises(ValueError):
        common.write_user_config("me", name, b"{}")


def test_write_user_config_is_private_and_atomic(repo):
    common.write_user_config("me", "launchers.json", b'{"buttons": []}\n')
    d = repo / "users" / "me" / ".os"
    assert _mode(d) == 0o700
    assert _mode(d / "launchers.json") == 0o600
    assert [x.name for x in d.iterdir()] == ["launchers.json"]   # no .kbtmp left behind
    common.write_user_config("me", "launchers.json", b'{"buttons": [1]}\n')
    assert (d / "launchers.json").read_text() == '{"buttons": [1]}\n'


def test_migrate_user_config_moves_the_legacy_file_once(repo):
    legacy = repo / "users" / "me" / ".launchers.json"
    legacy.write_text('{"buttons": []}\n')
    ino = os.stat(legacy).st_ino
    assert common.migrate_user_config("me", "launchers.json", ".launchers.json") is True
    new = repo / "users" / "me" / ".os" / "launchers.json"
    assert os.stat(new).st_ino == ino and not legacy.exists()
    assert common.migrate_user_config("me", "launchers.json", ".launchers.json") is False
    # a legacy symlink is never followed into place
    os.symlink(repo / ".gitignore", legacy)
    new.unlink()
    assert common.migrate_user_config("me", "launchers.json", ".launchers.json") is False
    assert not new.exists()


def test_load_config_json_is_tolerant(repo):
    assert common.load_config_json(repo / "nope.json") is None
    (repo / "bad.json").write_text("{not json")
    assert common.load_config_json(repo / "bad.json") is None
    (repo / "ok.json").write_text('{"a": 1}')
    assert common.load_config_json(repo / "ok.json") == {"a": 1}
