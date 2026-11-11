"""The unattended maintenance agent reads attacker-writable text and runs as an
admin, so its tool allowlist is the whole boundary between a poisoned journal
line and root. These checks read the rules out of the script; no agent runs.

A Claude Code rule is a PREFIX: `Bash(x:*)` allows anything starting with `x`,
and a `*` in the middle matches whole argument lists. So every wildcard must be
trailing, every sudo rule must be exact, and tools that can write files or run
commands from their arguments stay out."""
import os
import re
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "kb-maintenance.sh"
TEXT = SCRIPT.read_text()
DIGEST = "/usr/local/sbin/kb-audit-digest"
# The one sudo rule allowed a trailing `*`: the audit digest needs its window.
# Safe only while test_audit_digest_takes_only_its_window holds.
SUDO_WITH_ARGS = {f"Bash(sudo -n {DIGEST}:*)"}


def _code(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _allowed_rules():
    """Every quoted Bash(...) rule in the two allowlists (TOOLS and the dry-run
    ALLOWED), plus the LOG_READS template — not the BANNED list."""
    tools = TEXT[TEXT.index("TOOLS=("):TEXT.index("BANNED=(")]
    dry = TEXT[TEXT.index('  ALLOWED=("Read"'):TEXT.index('  ALLOWED=("${TOOLS[@]}")')]
    reads = TEXT[TEXT.index("LOG_READS=()"):TEXT.index("TOOLS=(")]
    rules = []
    for chunk in (tools, dry, reads):
        rules += re.findall(r'"(Bash\([^"]*\))"', _code(chunk))
    assert len(rules) > 10, rules
    return rules


def test_every_wildcard_is_trailing():
    for rule in _allowed_rules():
        body = rule[len("Bash("):-1]
        assert "*" not in body.removesuffix(":*"), rule


def test_sudo_rules_are_exact():
    for rule in _allowed_rules():
        if rule.startswith("Bash(sudo ") and rule not in SUDO_WITH_ARGS:
            assert "*" not in rule, rule


def test_no_tool_that_writes_files_or_runs_commands():
    banned = ("curl", "wget", "git ", "find", "grep", "sed", "awk", "xargs", "env",
              "python", "bash", "sh ", "tar", "sudo journalctl", "sudo cat /var/log/kb/*")
    for rule in _allowed_rules():
        body = rule[len("Bash("):-1]
        for b in banned:
            assert not body.startswith(b), rule


def test_audit_digest_is_allowed_with_and_without_its_window_in_both_runs():
    block = TEXT[TEXT.index("AUDIT_DIGEST=("):TEXT.index("TOOLS=(")]
    assert re.findall(r'"(Bash\([^"]*\))"', _code(block)) == [
        f"Bash(sudo -n {DIGEST})", f"Bash(sudo -n {DIGEST}:*)"]
    tools = TEXT[TEXT.index("TOOLS=("):TEXT.index("BANNED=(")]
    dry = TEXT[TEXT.index('  ALLOWED=("Read"'):TEXT.index('  ALLOWED=("${TOOLS[@]}")')]
    for chunk in (tools, dry):
        assert '"${AUDIT_DIGEST[@]}"' in _code(chunk)


def test_audit_digest_takes_only_its_window():
    """Its rule ends in `*`, so whatever the agent appends runs as root. That is
    harmless only while the program takes nothing but two datetimes: an option
    naming a file, a command or a pattern would turn the rule into a root
    primitive. Add such an option and the rule must become exact instead."""
    src = (SCRIPT.parent / "kb-audit-digest").read_text()
    assert sorted(re.findall(r'add_argument\(\s*"(-[^"]+)"', src)) == ["--since", "--until"]
    for escape in ("fromfile_prefix_chars", "parse_known_args", "shell=True", "os.system"):
        assert escape not in src, escape


def test_agent_starts_in_the_fixed_folder_with_its_old_reach():
    """Claude Code loads CLAUDE.md from its working directory, so the operator's
    instructions only arrive if the agent STARTS in the fixed folder. And it
    refuses an allowed `head`/`stat` outside its directories, so `/` is added:
    the reach runs had when they started from systemd's default, /."""
    assert 'AGENT_DIR="$STATE_DIR/agent"' in TEXT
    assert "mktemp -d /tmp" not in TEXT
    start = TEXT.index('( cd "$AGENT_DIR" && exec runuser -u "$OPERATOR"')
    launch = TEXT[start:TEXT.index(') > "$REPORT" 2> "$WORK/agent.err"', start)]
    assert '"$CLAUDE_BIN" -p "$PROMPT"' in launch
    assert re.search(r"--add-dir /\s*$", launch)
    assert "CLAUDE.md" not in _code(TEXT), "the script must never write the operator's CLAUDE.md"


def test_run_folder_comes_and_goes_and_the_operators_files_stay(tmp_path):
    """Run the script's own folder setup (no root needed: the chowns fail
    quietly) against a state dir that already holds an operator's CLAUDE.md,
    an old run folder a killed run left behind, and a fresh one of a run still
    going."""
    setup = TEXT[TEXT.index('AGENT_DIR="$STATE_DIR/agent"'):]
    setup = setup[:setup.index("trap 'rm -rf \"$WORK\"' EXIT") + len("trap 'rm -rf \"$WORK\"' EXIT")]
    agent = tmp_path / "agent"
    (agent / "notes").mkdir(parents=True)
    claude_md = agent / "CLAUDE.md"
    claude_md.write_text("operator notes\n")
    stale, fresh = agent / "run.stale1", agent / "run.fresh1"
    stale.mkdir()
    fresh.mkdir()
    old = time.time() - 3 * 86400
    for p in (claude_md, agent / "notes", stale):
        os.utime(p, (old, old))
    out = subprocess.run(
        ["bash", "-c", setup + '\nprintf "%s\\n" "$WORK"; stat -c %a "$AGENT_DIR" "$WORK"'],
        env={"PATH": os.environ["PATH"], "STATE_DIR": str(tmp_path),
             "OPERATOR": subprocess.run(["id", "-gn"], capture_output=True, text=True).stdout.strip()},
        capture_output=True, text=True, check=True).stdout.split()
    work, agent_mode, work_mode = Path(out[0]), out[1], out[2]
    assert work.parent == agent and work.name.startswith("run.")
    assert (agent_mode, work_mode) == ("750", "750")
    assert not work.exists(), "the run folder is removed when the script exits"
    assert not stale.exists(), "a day-old run folder is swept"
    assert fresh.exists(), "a run still going keeps its folder"
    assert claude_md.read_text() == "operator notes\n" and claude_md.stat().st_mtime == old
    assert (agent / "notes").exists(), "only run.* folders are ever swept"
