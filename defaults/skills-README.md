# Agent skills — shared by the whole company

**Every agent in Company OS loads the skills in this folder:** Claude Code (through `.claude/skills`, a link here), Codex, and Hermes once its `skills.external_dirs` points here.

## Add a skill

- Create a folder named like the skill: lowercase letters, digits and dashes (e.g. `video-recording`).
- Put a `SKILL.md` in it, starting with frontmatter: `name:` (same as the folder) and `description:` (when an agent should use it — this is what triggers it).
- Extra files (scripts, templates, examples) go next to it, e.g. in `reference/`.
- **One level only** — Claude Code reads skills at the top of this folder, never in a subfolder.
- Agents pick it up in their next session. No sudo, no link.

## Rules

- **No secrets** — no keys, passwords or tokens. Every `.md` here is in git and readable by everyone; point to where a secret lives instead.
- **`kb-*` skills are the platform's** — root-owned, read-only, refreshed on every deploy. Pick another name: a folder that takes a platform name first is never updated.
- **Anyone can edit a skill a member wrote.** Only its creator (or an admin) can rename or delete the folder itself.
- **A deleted skill goes to `.trash/` here.** Claude Code and Codex ignore it; Hermes may still load it until the trash is emptied.
- Write it short and scannable — bullets, one fact per line, conclusion first.
- This README is the platform's and is rewritten on every deploy.
