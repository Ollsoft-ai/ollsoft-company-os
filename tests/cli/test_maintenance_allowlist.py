"""The unattended maintenance agent reads attacker-writable text and runs as an
admin, so its tool allowlist is the whole boundary between a poisoned journal
line and root. These checks read the rules out of the script; no agent runs.

A Claude Code rule is a PREFIX: `Bash(x:*)` allows anything starting with `x`,
and a `*` in the middle matches whole argument lists. So every wildcard must be
trailing, every sudo rule must be exact, and tools that can write files or run
commands from their arguments stay out."""
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "kb-maintenance.sh"
TEXT = SCRIPT.read_text()


def _allowed_rules():
    """Every quoted Bash(...) rule in the two allowlists (TOOLS and the dry-run
    ALLOWED), plus the LOG_READS template — not the BANNED list."""
    tools = TEXT[TEXT.index("TOOLS=("):TEXT.index("BANNED=(")]
    dry = TEXT[TEXT.index('  ALLOWED=("Read"'):TEXT.index('  ALLOWED=("${TOOLS[@]}")')]
    reads = TEXT[TEXT.index("LOG_READS=()"):TEXT.index("TOOLS=(")]
    rules = []
    for chunk in (tools, dry, reads):
        code = "\n".join(line for line in chunk.splitlines() if not line.lstrip().startswith("#"))
        rules += re.findall(r'"(Bash\([^"]*\))"', code)
    assert len(rules) > 10, rules
    return rules


def test_every_wildcard_is_trailing():
    for rule in _allowed_rules():
        body = rule[len("Bash("):-1]
        assert "*" not in body.removesuffix(":*"), rule


def test_sudo_rules_are_exact():
    for rule in _allowed_rules():
        if rule.startswith("Bash(sudo "):
            assert "*" not in rule, rule


def test_no_tool_that_writes_files_or_runs_commands():
    banned = ("curl", "wget", "git ", "find", "grep", "sed", "awk", "xargs", "env",
              "python", "bash", "sh ", "tar", "sudo journalctl", "sudo cat /var/log/kb/*")
    for rule in _allowed_rules():
        body = rule[len("Bash("):-1]
        for b in banned:
            assert not body.startswith(b), rule
