# Agentic access from the CLI

Company OS supports Claude Code and OpenAI Codex as per-user clients. Start the
client in `/srv/kb`; it receives the same Linux permissions as the person who
launched it, so project boundaries apply to agent reads, search, SQL and writes.

A web-only viewer with `/usr/sbin/nologin` cannot start an agent. Give every
agent operator a named full account; never run a client as root.

## Install as the user

Codex on macOS or Linux:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Then run `codex` and choose **Sign in with ChatGPT** or another offered method.
See the official [Codex CLI guide](https://developers.openai.com/codex/cli/).

Claude Code on macOS, Linux or WSL:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

Then run `claude` and follow the browser sign-in flow. See the official
[Claude Code quickstart](https://code.claude.com/docs/en/quickstart).

Subscriptions and API usage belong to the user or company AI account; Company
OS does not proxy model credentials. Never place API keys in `/srv/kb`, prompts
or shell history.

## Start in the knowledgebase

```bash
cd /srv/kb
codex   # or: claude
```

- Codex discovers `AGENTS.md` and `.agents/skills/` — the context and the
  skills, one copy each.
- Claude Code discovers the same two through root-owned links: `CLAUDE.md` ->
  `AGENTS.md` and `.claude/skills` -> `.agents/skills`.
- Hermes reads skills from its own folder; add the company's with
  `skills.external_dirs: [/srv/kb/.agents/skills]` in `~/.hermes/config.yaml`.
- Anyone can add a skill: a folder with a `SKILL.md` in `.agents/skills/`, no
  sudo. The `kb-*` skills are the platform's — root-owned and read-only.
- The installer never replaces an existing `AGENTS.md`; operator customization
  remains intact.
- Both are told to search with **`kb-search "question"`** (meaning + words,
  as the caller, `path:line` results) before reaching for `rg`; Hermes and any
  other agent with a shell can call it the same way. `kb-search --json` is the
  machine-readable form ([semantic-search.md](semantic-search.md)).

A safe first prompt:

```text
Read the Company OS instructions and relevant skills. Do not change anything.
Map only the company and project information I can access, then show my open
tasks with links to the source files.
```

## Guardrails

- Ask for sources and a plan before broad changes.
- Draft in `users/<you>/`; promote reviewed work to shared folders.
- Review commands and diffs. The client can change everything its user can.
- Keep secrets out of Markdown, Git, artifacts and prompts.
- Preserve approval and evidence status; an agent cannot certify or approve on
  behalf of management.
