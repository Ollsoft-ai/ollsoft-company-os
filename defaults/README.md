# Shipped defaults

Copied into the knowledgebase at `<repo>/.claude/` by `scripts/install.sh`, but
only if the file is not already there — your edits on a live system are never
overwritten by a re-run or an upgrade.

| File | Purpose |
|---|---|
| `CLAUDE.md` | Agent context: what this repo is and how to behave in it. |
| `egress.json` | Per-artifact network allow-list. Empty (deny-all) by default; the only way an artifact reaches the network. |
| `launchers.json` | Buttons in the UI's launcher bar. Default: one terminal button that runs `claude`. |

The agent skills in `company-skills/` are installed alongside these, at
`<repo>/.claude/skills/`. Those *are* refreshed on every install, since they
document the platform and should track the code.
