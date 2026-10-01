# Phase 6 — AI agents

Two different things — explain the difference once:

- **Agent chat** (Alt+C in the browser) — adapters installed **once per server** into `/opt/kb-agents`, run by each person as themselves.
- **Terminal CLIs** (`claude`, `codex`, `hermes` in a web terminal) — installed **per person** into their home.

Either way **each person signs in with their own AI account** in the app. Company OS never holds a shared model key, and you cannot sign in for them.

## Ask

"Which AI agents should people have? Claude Code, OpenAI Codex and Gemini are the usual three; Copilot, Grok, Qwen, OpenCode and DeepSeek are also available. Which one should a new chat open with? Should I also pre-install the terminal versions of Claude Code and Codex, and for whom?" Hermes is its own question, below.

## Agent chat adapters (server-wide)

```bash
ssh companyos 'cd ~/ollsoft-company-os && sudo cos-run agents bash scripts/install-agents.sh'   # Node 22 + Claude, Codex, Gemini
ssh companyos 'sudo cos-run agents'                                                              # poll
```

- Others one at a time: `admin POST /admin/agents/install '{"id":"copilot"}'` (ids: `copilot grok qwen opencode deepseek`), poll `admin GET /admin/agents`.
- Default agent: `admin POST /admin/settings '{"set":{"ai.agent":"claude"}}'`.
- The pinned **launcher** button runs `claude` in a terminal. If Claude is not installed, change it: `admin POST /admin/launchers` with `{"buttons":[{"label":"codex","kind":"term","target":"codex"}]}`.
- Verify: `admin GET /admin/agents` lists them installed; the human opens a chat (Alt+C), picks the agent and sees its sign-in prompt.

## Terminal CLIs (per person, full accounts only)

Run as each chosen person — after phase 7 creates them, or now for the admin:

```bash
ssh companyos 'sudo -iu <user> bash -c "curl -fsSL https://claude.ai/install.sh | bash"'              # Claude Code
ssh companyos 'sudo -iu <user> bash -c "curl -fsSL https://chatgpt.com/codex/install.sh | sh"'         # Codex
```

- Long installs: wrap in `sudo cos-run cli-<user> …` and poll.
- Viewer accounts (`nologin`) cannot run agents. Never run an agent as root.
- Verify: `ssh companyos 'sudo -iu <user> bash -lc "claude --version; codex --version" 2>&1'`.
- Sign-in is theirs: the first `claude` / `codex` in a web terminal opens a browser login.

## Hermes Agent — the one that works while nobody is looking

**Pitch it, in these words or close:** "Hermes is an agent that lives on the server, runs on a schedule and messages you on Telegram, Discord, Slack or email. Company OS is already built for it:

- **Daily company brief** — what each person created or changed yesterday (the knowledgebase is versioned, so this comes from its history, not guesswork), what they opened or downloaded, open and overdue to-dos.
- **Daily security audit** — failed sign-ins, sharing and permission changes, public links, new accounts, SSH and sudo use, and the host audit trail (auditd) from phase 3: someone poking at folders they cannot open, or touching another person's private files.
- **Anything recurring you describe** — a weekly project status, a Monday digest of what changed in the handbook, a reminder when a contract document gets edited."

**Ask:** "Do you want Hermes, and who should get it — usually just you?"

```bash
ssh companyos 'sudo -iu <user> bash -c "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --non-interactive"'
ssh companyos 'sudo -iu <user> bash -c "cd ~/.hermes/hermes-agent && VIRTUAL_ENV=\$PWD/venv ~/.hermes/bin/uv pip install -e \".[acp]\" && ~/.local/bin/hermes acp --check"'
ssh companyos 'sudo loginctl enable-linger <user>'      # its scheduler keeps running after they log out
```

- **Per person by design** (`~/.hermes`); once installed it also appears in that person's agent-chat picker.
- **Model and messaging are theirs to set**, in a web terminal: `hermes setup` (model provider), then `hermes gateway setup` (Telegram bot or other channel) and `hermes gateway install`.
- Verify: `ssh companyos 'sudo -iu <user> bash -lc "hermes --version; hermes gateway status"'`.
- The scheduled jobs themselves are created in **phase 8**, once people and sensitive folders exist.

## The maintenance agent

Phase 8 can run a daily AI health check as one admin, using **their** signed-in Claude Code. If they want it, make sure that admin has Claude Code here.
