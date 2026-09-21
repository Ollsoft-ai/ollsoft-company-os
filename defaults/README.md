# Shipped defaults

Installed into the knowledgebase by `scripts/install.sh`, but **only if the
file is not already there** — your edits on a live system are never
overwritten by a re-run or an upgrade.

| File | Installed at | Purpose |
|---|---|---|
| `CLAUDE.md` | `<repo>/.claude/CLAUDE.md` | Agent context: what this repo is and how to behave in it. `.claude/` is Claude Code's discovery path and holds agent context only. |
| `egress.json` | `<repo>/.os/egress.json` | Per-artifact network allow-list. Empty (deny-all) by default; the only way an artifact reaches the network. |
| `launchers.json` | `<repo>/.os/launchers.json` | The company's pinned items, listed in every sidebar's Pinned section. Default: one terminal pin that runs `claude`. |

`<repo>/.os/` is the platform's own config directory (root:kb-users, everyone
reads, root writes): launcher buttons, the egress allow-list and settings live
there at company level, and each person has a private `users/<name>/.os/` for
their own. See [docs/settings.md](../docs/settings.md).

Installs from before 2026-09 kept the two JSON files in `.claude/`; the hub
moves them into `.os/` on its next start (a rename — history, and the egress
file's delegation ACL, come along).

`artifacts/todos.html` is installed too, but to `company/todos.html` — the
To-dos view is a shipped feature, not agent config. The other files under
`artifacts/` are demo and test fixtures; `scripts/seed-demo.sh` places those, and
a plain install does not.

The agent skills in `company-skills/` are installed alongside these, at
`<repo>/.claude/skills/`. Those *are* refreshed on every install, since they
document the platform and should track the code.

The installer also creates `<repo>/AGENTS.md` as a protected symlink to
`.claude/CLAUDE.md`, so Codex and Claude Code receive the same maintained
instructions without a second copy drifting.
