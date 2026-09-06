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

- Claude Code discovers `.claude/CLAUDE.md` and `.claude/skills/`.
- Codex discovers root `AGENTS.md`, a protected symlink to the same context.
  Tell it to inspect `.claude/skills/` before database, artifact or automation
  work.
- The installer never replaces an existing `AGENTS.md`; operator customization
  remains intact.

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
