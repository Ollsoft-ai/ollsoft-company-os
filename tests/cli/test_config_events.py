"""A setting saved twice within one second still reaches the open tabs.

The tree carries whole-second mtimes and no size, so a reset followed by a
change in the same second moved nothing it shows: no tree event, no config
event, and the tab kept the old value. The events loop now watches the config
files themselves (_config_sig). Pure: a scratch repo, no hub."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kb_platform import common, user_server as us  # noqa: E402


def test_a_same_second_rewrite_is_seen(tmp_path, monkeypatch):
    mine = tmp_path / "users" / "bob" / ".os"
    mine.mkdir(parents=True)
    (tmp_path / ".os").mkdir()
    monkeypatch.setattr(common, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(common, "COMPANY_CONFIG_DIR", tmp_path / ".os")
    monkeypatch.setattr(us, "ME", "bob")
    f = mine / "settings.json"

    def save(text):
        (mine / "settings.json.t").write_text(text)
        os.rename(mine / "settings.json.t", f)

    while time.time() % 1 > 0.3:            # both saves inside one wall-clock second
        time.sleep(0.05)
    save("{}")
    tree1 = us._tree_index(us._build_tree(str(tmp_path))["tree"], {})
    before = us._config_sig()
    save('{"ui.theme.custom": {"bg": "#123456"}}')
    tree2 = us._tree_index(us._build_tree(str(tmp_path))["tree"], {})
    assert int(f.stat().st_mtime) == tree1["users/bob/.os/settings.json"][1], "the test needs one second"
    assert us._tree_delta(tree1, tree2)["paths"] == [], "the tree alone cannot see it (why _config_sig exists)"
    assert us._config_sig() != before, "the config watch sees it"


def test_a_pages_memory_is_not_config(tmp_path, monkeypatch):
    mine = tmp_path / "users" / "bob" / ".os"
    mine.mkdir(parents=True)
    monkeypatch.setattr(common, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(common, "COMPANY_CONFIG_DIR", tmp_path / ".os")
    monkeypatch.setattr(us, "ME", "bob")
    before = us._config_sig()
    (mine / us.ARTIFACT_STATE_FILE).write_text("{}")
    assert us._config_sig() == before, "a page remembering its tab must not refetch every setting"
    assert not us._config_touched([f"users/bob/.os/{us.ARTIFACT_STATE_FILE}"])
    assert us._config_touched(["users/bob/.os/settings.json"]) and us._config_touched([".os/launchers.json"])
