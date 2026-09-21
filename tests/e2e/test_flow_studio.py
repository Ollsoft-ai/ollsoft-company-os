"""Flow Studio, driven through the real artifact bridge.

This is the worked example for a *tool-shaped* artifact — one that reads and
writes a whole workspace next to itself, rather than rendering a single view.
It uses `artifact_harness`, so it exercises the deployed CSP, sandbox and bridge
rather than a stand-in for them.

Skipped when the workspace is not on this box, so the suite stays green on a
machine that has never seen it.
"""
import json
from pathlib import Path

import pytest

from artifact_harness import open_artifact, place_artifact, watch_console
from conftest import login
from kbenv import full

STUDIO = Path("/srv/kb/users/krystof/ollsoftflow")

pytestmark = pytest.mark.skipif(
    not (STUDIO / "index.html").is_file(),
    reason="Flow Studio workspace is not present on this box",
)


@pytest.fixture(scope="module")
def studio(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    errors = watch_console(page)
    # the studio writes: hand it to the viewer, or every save is a 403
    path = place_artifact("flow/index.html", src_dir=STUDIO, viewer="alice")
    frame = open_artifact(page, path)
    frame.locator("#app.ready").wait_for(timeout=20000)
    yield page, frame, path, errors
    ctx.close()


def _text(loc):
    return loc.inner_text().strip()


def test_it_opens_in_its_own_folder_with_nothing_to_configure(studio):
    """The artifact's folder is the only workspace it can have, so it is never
    something to ask about — no boot screen, no root to type."""
    _page, f, path, _e = studio
    folder = path.rsplit("/", 1)[0]
    assert f.locator("#app.ready").count() == 1, "the app did not mount"
    assert not f.locator("#boot").is_visible(), "a setup screen appeared instead of just opening"
    assert folder in _text(f.locator("#status")), \
        f"the workspace root is not the artifact's own folder: {_text(f.locator('#status'))!r}"
    assert f.locator("#btnRoot").count() == 0, "there is still a workspace-root chooser"


def test_workspace_loads_without_the_database(studio):
    """Listing is a directory walk, so a cold index must not matter."""
    page, f, _path, _e = studio
    assert f.locator("#skillList .rail-item").count() >= 3
    for want in ("inbox-triage", "release-gate", "plain-notes"):
        assert f.locator(f'#skillList [data-skill$="{want}/SKILL.md"]').count() == 1, want
    assert f.locator('#runList [data-run$=".run.md"]').count() >= 2
    assert _text(f.locator("#adapter")).lower() == "bridge"


def test_graph_renders_and_edits_write_back(studio):
    page, f, path, _e = studio
    f.locator('#skillList [data-skill$="inbox-triage/SKILL.md"]').click()
    page.wait_for_timeout(300)
    assert f.locator("#gsvg [data-node]").count() == 5
    assert f.locator("#gsvg .edge-label").count() == 2

    f.locator('#gsvg [data-node="T2"]').click()
    assert f.locator("#fId").input_value() == "T2"
    f.locator("#fIntent").fill("Sort each message into urgent or routine, by sender.")
    assert "unsaved" in _text(f.locator("#gDirty"))

    f.locator("#gSave").click()
    f.locator(".sheet").wait_for(timeout=8000)
    assert f.locator(".sheet pre.diff .add").count() == 1, "a one-field edit should be a one-line diff"
    f.locator('.sheet [data-act="write"]').click()
    page.wait_for_timeout(800)

    on_disk = full(path.rsplit("/", 1)[0] + "/skills/inbox-triage/SKILL.md").read_text()
    assert "by sender." in on_disk, "the edit never reached disk"
    assert "Runs at 08:00" in on_disk, "prose outside the managed regions was lost"
    assert on_disk.count("```mermaid") == 1


def test_run_replay_and_conformance(studio):
    page, f, _path, _e = studio
    f.locator("#tab-runs").click()
    f.locator("#rsvg [data-node]").first.wait_for(timeout=8000)

    f.locator("#rStart").click()
    page.wait_for_timeout(300)
    assert f.locator("#rsvg .s-none").count() == 5, "t=0 should be all never-entered"

    f.locator("#rEnd").click()
    page.wait_for_timeout(300)
    assert f.locator("#rsvg .s-exec").count() == 3
    assert f.locator("#rsvg .s-skip").count() == 1, "the untaken branch is skipped, not never-entered"
    assert f.locator("#rsvg .s-viol").count() == 1, "only the disallowed tool is high severity"
    assert f.locator("#events .ev").count() == 15

    f.locator("#tab-conf").click()
    page.wait_for_timeout(300)
    counts = " ".join(f.locator("#confCounts .count-chip").all_inner_texts()).lower()
    assert "1\nhigh" in counts, counts
    rows = f.locator("#confTable tbody tr")
    assert rows.count() == 2
    rows.first.click()
    page.wait_for_timeout(400)
    assert f.locator("#v-runs").is_visible(), "clicking a violation should jump to the run"
    assert f.locator("#rsvg .node-sel").count() == 1
    cur = f.locator("#events .ev[aria-current=true]")
    assert cur.count() == 1 and "Bash" in cur.inner_text()


def test_malformed_run_lines_are_counted_not_fatal(studio):
    page, f, _path, _e = studio
    f.locator('#skillList [data-skill$="release-gate/SKILL.md"]').click()
    f.locator('#runList [data-run$="r_91c.run.md"]').click()
    page.wait_for_timeout(400)
    assert "3 malformed" in _text(f.locator("#rSkipped"))
    f.locator("#tab-conf").click()
    page.wait_for_timeout(300)
    counts = " ".join(f.locator("#confCounts .count-chip").all_inner_texts()).lower()
    for want in ("1\nhigh", "1\nmedium", "4\nlow"):
        assert want in counts, f"{want} missing from {counts!r}"


def test_a_run_never_replays_onto_the_wrong_skill(studio):
    """A run declares the skill it was recorded against; selecting a skill must
    not leave another skill's run loaded and playable."""
    page, f, _path, _e = studio
    f.locator('#skillList [data-skill$="inbox-triage/SKILL.md"]').click()
    page.wait_for_timeout(300)
    assert "r_88f" in f.locator("#runPick").input_value() or \
        "r_88f" in _text(f.locator("#runPick")), "inbox-triage did not get its own run"

    # cool-shit has no run at all: better to show none than someone else's
    if f.locator('#skillList [data-skill$="cool-shit/SKILL.md"]').count():
        f.locator('#skillList [data-skill$="cool-shit/SKILL.md"]').click()
        f.locator("#tab-runs").click()
        page.wait_for_timeout(400)
        assert f.locator("#rsvg [data-node]").count() == 0, "a foreign run was replayed"
        assert "no run recorded for cool-shit" in _text(f.locator("#rHint")), _text(f.locator("#rHint"))

    # picking a run follows it to the skill it belongs to
    f.locator('#runList [data-run$="r_91c.run.md"]').click()
    page.wait_for_timeout(400)
    assert _text(f.locator("#insp .insp-h h2")) == "release-gate" or \
        f.locator('#skillList [data-skill$="release-gate/SKILL.md"][aria-current=true]').count() == 1, \
        "selecting r_91c did not switch to release-gate"


def test_creating_and_deleting_a_skill_needs_no_prompt(studio):
    """kb-mkdir + kb-write, so a new skill is one click, not a save-as dialog."""
    page, f, path, _e = studio
    folder = path.rsplit("/", 1)[0]
    f.locator("#tab-graph").click()
    before = f.locator("#skillList .rail-item").count()

    f.locator("#btnNewSkill").click()
    f.locator("#nsName").fill("harness-made")
    f.locator("#nsDesc").fill("Created by the e2e harness to prove the create path.")
    f.locator('.sheet [data-act="make"]').click()
    page.wait_for_timeout(400)
    assert f.locator("#skillList .rail-item").count() == before + 1
    assert f.locator("#gsvg [data-node]").count() == 3

    f.locator("#gSave").click()
    f.locator(".sheet").wait_for(timeout=8000)
    f.locator('.sheet [data-act="write"]').click()
    page.wait_for_timeout(900)

    made = full(folder + "/skills/harness-made/SKILL.md")
    assert made.is_file(), "kb-mkdir + kb-write did not create the skill"
    body = made.read_text()
    assert "flow: true" in body and "flow:layout" in body and "## node: N1" in body
    assert "unsaved" not in _text(f.locator("#gDirty"))

    # and delete it again, through the bridge
    f.locator("#gsvg").click(position={"x": 6, "y": 6})
    page.wait_for_timeout(300)
    f.locator("#skDelete").click()
    f.locator("#delConfirm").fill("harness-made")
    f.locator('.sheet [data-act="del"]').click()
    page.wait_for_timeout(900)
    assert not made.parent.exists(), "the skill folder survived the delete"
    assert f.locator("#skillList .rail-item").count() == before


def test_this_folder_installs_itself_with_no_script(studio):
    """The studio's own .claude/ is inside the artifact's folder, so the button
    does the work; only other projects need the script."""
    page, f, path, _e = studio
    folder = path.rsplit("/", 1)[0]
    f.locator("#btnHooks").click()
    f.locator("#insHere").wait_for(timeout=8000)
    f.locator("#insHere").click()
    page.wait_for_timeout(1200)
    msg = _text(f.locator("#insHereMsg"))
    assert "skills installed" in msg and "hooks" in msg, msg

    installed = full(folder + "/.claude/skills/inbox-triage/SKILL.md")
    assert installed.is_file(), "the button did not write into .claude/skills"

    # without hooks nothing records a run, so the button installs them too
    settings = full(folder + "/.claude/settings.json")
    assert settings.is_file(), "no settings.json — nothing would log a run"
    cfg = json.loads(settings.read_text())
    for ev in ("SessionStart", "PostToolUse", "Stop"):
        assert any(h.get("command", "").endswith("log.sh --tool claude")
                   for g in cfg["hooks"][ev] for h in g.get("hooks", [])), ev
    assert any(g.get("matcher") == "*" for g in cfg["hooks"]["PostToolUse"])
    assert "flow: true" in installed.read_text()
    f.locator('.sheet [data-x="close"]').click()
    page.wait_for_timeout(200)

    # an installed copy must not show up as a second skill in the rail
    names = f.locator("#skillList .rail-item .nm").all_inner_texts()
    assert len(names) == len(set(names)), f"the installed copy is listed twice: {names}"

    # and it must track the source on save, or it silently goes stale
    f.locator("#tab-graph").click()
    f.locator('#skillList [data-skill$="inbox-triage/SKILL.md"]').click()
    page.wait_for_timeout(300)
    f.locator('#gsvg [data-node="E1"]').click()
    f.locator("#fIntent").fill("Pull unread mail from the last 24h, oldest first.")
    f.locator("#gSave").click()
    f.locator(".sheet").wait_for(timeout=8000)
    f.locator('.sheet [data-act="write"]').click()
    for _ in range(30):
        if "oldest first." in installed.read_text():
            break
        page.wait_for_timeout(250)
    assert "oldest first." in installed.read_text(), \
        "the installed copy did not follow the save — it would drift"


def test_installer_is_project_local_and_written_in_place(studio):
    page, f, path, _e = studio
    folder = path.rsplit("/", 1)[0]
    f.locator("#btnHooks").click()
    f.locator("#insScript").wait_for(timeout=8000)
    script = _text(f.locator("#insScript"))
    # only the executable lines matter: the header comment mentions $HOME
    # precisely to say that nothing is written there
    code = [l for l in script.split("\n") if not l.strip().startswith("#")]
    for line in code:
        assert "$HOME" not in line and "~/.claude" not in line and "~/.codex" not in line, \
            f"the installer reached into the home directory: {line}"
    assert 'PROJECT="${1:-$PWD}"' in script
    assert '"$PROJECT"/.claude/skills' in script
    assert "ln -s" in script and "NOT a symlink" in script

    f.locator('.sheet [data-act="write"]').click()
    page.wait_for_timeout(800)
    installer = full(folder + "/install.sh")
    assert installer.is_file(), "install.sh was not written into the artifact's own folder"
    assert installer.read_text().startswith("#!/usr/bin/env bash")
    json.loads(_text(f.locator("#hookCfg")))
    f.locator('.sheet [data-x="close"]').click()


@pytest.fixture(autouse=True)
def _close_any_open_sheet(studio):
    """A sheet left open by a failing test blocks every click after it, which
    turns one real failure into a screenful of misleading ones."""
    yield
    page, f, _path, _e = studio
    try:
        if f.locator('.sheet [data-x="close"]').count():
            f.locator('.sheet [data-x="close"]').first.click()
            page.wait_for_timeout(150)
    except Exception:
        pass


def test_static_report_is_emitted_with_no_script(studio):
    page, f, path, _e = studio
    folder = path.rsplit("/", 1)[0]
    # Say which run explicitly. This used to rely on whatever the previous test
    # left selected, which only worked while a run could leak across skills.
    f.locator('#skillList [data-skill$="release-gate/SKILL.md"]').click()
    page.wait_for_timeout(300)
    f.locator("#tab-runs").click()
    page.wait_for_timeout(300)
    f.locator("#rReport").click()
    # mkdir + write is two bridge round-trips; poll rather than guess a duration
    reports = []
    for _ in range(40):
        reports = list((full(folder) / "reports").glob("*.report.html"))
        if reports:
            break
        page.wait_for_timeout(250)
    assert reports, f"no report was written — toast said: {_text(f.locator('#toast'))!r}"
    html = reports[0].read_text()
    assert "<script" not in html.lower(), "the report must render under a script-free CSP"
    assert ":checked~.stage" in html, "step state is not encoded as CSS"


def test_nothing_was_blocked_and_no_console_errors(studio):
    """Runs last: `errors` fills as every test above drives the page."""
    _page, _f, _path, errors = studio
    assert not errors, errors[:5]
