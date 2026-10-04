# Shipped defaults

Installed into the knowledgebase by `scripts/install.sh`, but **only if the
file is not already there** — your edits on a live system are never
overwritten by a re-run or an upgrade.

| File | Installed at | Purpose |
|---|---|---|
| `AGENTS.md` | `<repo>/AGENTS.md` | Agent context: what this repo is and how to behave in it. Codex reads it; `<repo>/CLAUDE.md` is a root-owned link to it for Claude Code. |
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

`AGENTS.md`, the skills and `skills-README.md` are placed by
`scripts/deploy.sh`, which every install ends with, so an upgraded box gets the
same layout as a new one:

- `<repo>/.agents/skills/` holds every skill — Codex reads it, and
  `<repo>/.claude/skills` is a link to it for Claude Code. Members add and edit
  skills there; it is sticky, so a folder is renamed or deleted only by its
  creator.
- The agent skills in `company-skills/` are installed into it root-owned and
  read-only, and *are* refreshed on every deploy, since they document the
  platform and should track the code.
- `skills-README.md` becomes `<repo>/.agents/skills/README.md`, also refreshed.
- `AGENTS.md` is installed only if absent. A box from before 2026-10 kept this
  file at `.claude/CLAUDE.md` with `AGENTS.md` a link to it; deploy turns that
  file into `AGENTS.md` (edits kept) and moves `.claude/skills/*` across.
