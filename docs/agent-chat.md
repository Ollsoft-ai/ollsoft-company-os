# Agent chat — AI coding agents in the app, over ACP

**New** in the sidebar's Chats section (the compose button, Alt+C, the
top-right button on a phone, or "New agent chat" in the palette) opens a
conversation with an AI agent in a column beside your document. The agent
is a real coding agent — Claude Code, Codex, Gemini CLI, Copilot CLI, Grok
Build, Qwen Code, OpenCode, Hermes, DeepSeek Harness — running **as you, in the
knowledgebase**, with the same experience its terminal UI gives: streamed
answers, visible tool calls, diffs, permission prompts, plans, slash
commands, modes.

It speaks the [Agent Client Protocol](https://agentclientprotocol.com)
(ACP, version 1): the open JSON-RPC protocol Zed, JetBrains and others use to
drive agents. Anything in the ACP registry can be added to the catalogue.

## What it looks like

- **First open**: a picker with every agent the server has installed, each
  with its status — signed in as whom, "needs sign-in", "not installed" — and
  the recent chats to resume. Pick one, and a session starts.
- **A chat**, in Claude Code's own idiom: your messages on the right as
  bubbles, the agent's answers as `⏺` lines of rendered markdown (code with
  syntax colouring and a copy button, tables, lists, links), its thinking a
  dim `✻ Thought for 4s` that opens on click, every tool call a line —
  `⏺ Bash(ls -la)`, `⏺ Read(company/notes.md)`, `⏺ Update(path)`,
  `⏺ Search(pattern)` — with a `⎿` result beneath (a summary, the first
  lines of output with `… +N lines` to unfold, for edits a coloured diff
  whose path opens the document); the dot is muted while it runs, green
  when done, red when it failed. A permission request is an accent-bordered
  box under its tool line with numbered options (`1. Allow once`,
  `2. Reject`…) that the keys 1–9 answer while nothing is typed. While a
  turn runs a `✻ Working… (esc to interrupt · 12s · context 42%)` line sits
  above the composer. A plan sits under the transcript as a `☒ ◐ ☐`
  checklist. `/` at the start of a message lists the agent's slash commands.
  The composer is one rounded box — ＋ adds context or attaches an image (a
  paste works too), the mic dictates, the round arrow sends (a stop square
  while the agent works); Enter sends, Shift+Enter is a new line, Escape
  stops. On a finger it is a column (context chips, attachments, the text at
  full width, the controls beneath); a short landscape chat keeps the one-row
  form, where the two chip rows take a wrapped line of their own.
- **Recent chats** (⋯ → Recent chats, or the picker) list every chat by the
  title the agent gave it or, failing that, your first message; opening one
  replays it from the agent's own transcript (Claude Code, Codex and Gemini
  keep one; an agent that does not says so). A chat's mode and the answers
  you gave to permission prompts come back after a reload, because the
  backend remembers them with the session.
- **It survives everything a terminal does**: the agent process lives in your
  backend, not in the page. Reload, close the tab, come back in an hour — the
  conversation replays and a turn that was running kept running. A
  permission asked while you were away is waiting when you return (for up to
  twenty minutes; then the agent is told "cancelled" so it can finish).
- **Where it goes**: a chat is a tab like any other in the unified layout
  ([unified-views.md](unified-views.md)); it opens in a right-hand column at
  a third of the width and can be dragged anywhere, stacked under a document,
  maximized. On a phone it opens in the main group.

## What the agent is pointed at (2026-09-21)

A chat knows what you are looking at. Above the message box sits a row of
chips, and every prompt carries them to the agent as `resource_link` blocks —
the path, not the text: the agent runs as you, in the same knowledgebase, so
it opens the file itself.

- **What you have open** rides along automatically: the visible document of
  every group, the focused one first, up to four, as dashed chips. They
  follow the window — open another document and the chip changes with it.
  The × on a chip drops that one for this chat; ＋ → "Including what I have
  open" turns the whole habit off (and back on).
- **＋ → Add a file…** opens a path picker over the tree (the palette's
  shape, but it hands the path back instead of opening it; dot-files stay
  hidden unless the sidebar's `.*` is on). Those chips are solid and stay
  until you remove them.
- **＋ → Work in a folder…** picks the folder the agent stands in. ACP takes
  the working directory in `session/new`, so it is fixed for the life of a
  session: a chat that has already spoken opens a *new* chat in the folder
  instead of pretending to move, and a chat that has said nothing simply
  starts its session again there (the unused one is forgotten). The backend
  remembers the folder with the chat (`cwd` in `users/<you>/.os/chats.json`),
  so resuming a chat resumes it in the same place and the chip says so.
  `acp.py` falls back to the knowledgebase root for anything that is not an
  absolute path to a directory.
- **Every installed adapter understands the block**: Claude's bridge renders
  a link as `[@name](file:///srv/kb/…)` in the prompt text, Codex's and
  Gemini's take `resource_link` directly. An agent that ignored it would
  simply miss the hint — nothing breaks.
- **The message keeps its receipts**: the bubble you sent lists the chips
  that went with it, and clicking one opens that file.
- The context lives with the tab, so a reload brings it back. It is never
  sent anywhere but to the agent, which is already you.

## Signing in

Agents keep their own credentials in your home directory, exactly as their
CLIs do, so signing in once serves the terminal and the chat alike.

| Agent | How you sign in on this server | Where it lands |
|---|---|---|
| Claude Code | "Sign in" runs `claude-agent-acp --cli auth login --claudeai` in a terminal tab: open the printed link on any device; if the browser shows a code, paste it back. (Or paste an `ANTHROPIC_API_KEY`.) | `~/.claude/.credentials.json` |
| Codex | "Sign in" runs the device-code login (`codex-acp cli login --device-auth`): open the link anywhere, enter the code. Or paste an `OPENAI_API_KEY`. | `~/.codex/auth.json` |
| Gemini CLI | "Sign in" runs `gemini` with `NO_BROWSER`: choose Login with Google, open the link, paste the authorization code. Or paste a `GEMINI_API_KEY`. | `~/.gemini/gemini-credentials.json` (older builds: `oauth_creds.json`) |
| Copilot CLI | `copilot login` (device code). | `~/.copilot/config.json` |
| Grok Build | `grok login`, or an `XAI_API_KEY`. | `~/.grok/auth.json` |
| Qwen Code | an OpenAI-compatible key (plus `OPENAI_BASE_URL`/`OPENAI_MODEL` in `~/.qwen/.env`). | `~/.qwen/.env` |
| OpenCode | `opencode auth login`. | `~/.local/share/opencode/auth.json` |
| Hermes | "Sign in" runs `hermes acp --setup`: pick the provider and model it should use. | `~/.hermes/` (`auth.json`, `.env`, `config.yaml`) |
| DeepSeek Harness | a `DEEPSEEK_API_KEY`. | — |

A pasted API key is stored in **your** `users/<you>/.os/agent-keys.json`
(0600) and handed to that agent's process as its environment variable; it
never enters a company file, git, search or the sync relay. "Change API key"
with an empty value removes it.

The status pills are best effort: "credentials found" means one of the
agent's credential files exists — the catalogue lists several per agent,
because a CLI renames the file between versions (Gemini 0.60 writes
`gemini-credentials.json` where 0.4 wrote `oauth_creds.json`, and a stale
name made a signed-in agent read "needs sign-in"); "signed in as …" is what the agent itself reported
after starting. An agent that rejects a session with "authentication
required" sends you back to the picker.

## Installing agents (admins)

Most agents are Node programs installed once per server into
`/opt/kb-agents` (`KB_AGENTS_PREFIX`), shared by everyone and run by each
person as themselves. An admin installs one from the picker ("Install", which
runs `scripts/install-agents.sh <package>` as root, logged, one at a time —
`GET /admin/agents` shows the job) or by hand:

```bash
sudo bash scripts/install-agents.sh          # Node 22 + Claude, Codex, Gemini
sudo bash scripts/install-agents.sh @xai-official/grok@1.0.38
```

**Hermes is the exception**: it is a Python agent that installs into the
person's own home (`~/.hermes`, launcher in `~/.local/bin`), so there is
nothing for an admin to install and the picker says so. Install it as its
own site describes, then add the ACP adapter — `cd ~/.hermes/hermes-agent &&
uv pip install -e '.[acp]'` — and check it with `hermes acp --check`. The
platform looks for agents in `~/.local/bin` as well as `/opt/kb-agents`, so
an agent you install for yourself shows as installed for you and for nobody
else.

The Claude adapter needs Node 22 and Ubuntu ships 18, so the script first
puts a private Node LTS under `/opt/kb-agents/node` (downloaded from
nodejs.org and checked against its published SHA-256 sums) and the platform
puts its `bin` first on every agent's PATH — the agent process and the
sign-in command alike. The system's Node is not touched. Nothing else
changes on the box: the platform works without any agent installed — the
picker just says so.

## How it works

```
browser ── /acp?agent=claude (WebSocket, through the hub) ──► your backend
                                                            (user_server, as you)
                                                              │ stdio, one JSON per line
                                                              ▼
                                                     claude-agent-acp (as you, cwd = the knowledgebase)
```

- `kb_platform/acp.py` is the ACP **client**. Your backend spawns one agent
  process per agent id, `initialize`s it once, and multiplexes every chat tab
  of that agent over it: the tab's requests get their ids re-mapped, the
  agent's `session/update`s are logged per session and relayed, the agent's
  own requests (`session/request_permission`, `elicitation/create`) go to the
  tab that owns the session — or wait for one. The process is reaped after
  thirty idle minutes; closing the last tab does not kill a running turn.
- **Capabilities advertised**: terminal auth (the login-in-a-terminal flow),
  elicitation (forms and URLs, which is how Codex's device login and Claude's
  "ask the user" tool work), boolean config options. **Not advertised**: the
  file-system and terminal methods — the agent runs as you and does its own
  reading, writing and running, which the kernel and the ACLs police exactly
  as they police a terminal; there is no second permission model to get wrong.
- The chat index (`users/<you>/.os/chats.json`) remembers every session's
  id, agent, title and time so it can be resumed from the picker; the agent
  keeps the transcript itself (`session/load` replays it).
- The frontend, `frontend/src/chat.js`, is a lazily loaded chunk registered
  as the `chat` view kind (`views.js`): the shell knows only that a tab of
  kind `chat` exists, persists `{agent, sessionId, title}`, and restores it.
  Markdown is rendered by `marked`, sanitised by DOMPurify, code coloured by
  highlight.js (a dozen languages, registered explicitly).

Endpoints (all through the hub, cookie-authenticated):

| Method and path | Who | What |
|---|---|---|
| `GET /api/acp/agents` | you | the catalogue with per-agent status, your default (`ai.agent`), the shared prefix |
| `POST /api/acp/key` `{agent, key}` | you | store or (empty) remove your API key for that agent |
| `GET /api/acp/chats`, `POST /api/acp/forget` `{id}` | you | your chat index |
| `POST /api/acp/restart` `{agent}` | you | stop that agent's process (it restarts on the next message) |
| `GET /acp?agent=<id>` | you | the WebSocket (frames documented at the top of `acp.py`; the tab sends `{"kb":"title"}` to name a chat) |
| `GET /admin/agents`, `POST /admin/agents/install` `{id}` | admins | what is installed, install one |

Setting: `ai.agent` (company default, per-person override) picks the agent
a new chat opens with. See [settings.md](settings.md).

## Testing

`tests/cli/test_acp.py` drives the bridge with a fake agent script — the
multiplexing, the permission relay, the replay, the refusals — with no
network. `tests/e2e/test_agent_chat.py` runs the whole thing in a browser
against `scripts/acp-echo-agent.py`, a tiny ACP agent shipped with the
platform that echoes, thinks, asks for permission, edits and plans; it is
hidden from the picker unless the page URL carries `#dev`.

## Adding an agent

One entry in `CATALOGUE` in `kb_platform/acp.py` (id, name, the npm package,
the command line that starts it in ACP mode, the env it needs on a headless
box, how one signs in), the matching option in `settings.py`'s `ai.agent`
entry, and a row in the two tables above and in the kb-settings skill.
`tests/cli/test_acp.py` checks the setting and the catalogue agree.

## The chrome around it (2026-09-21)

The chat is a first-class part of the shell, not a button in a corner.

- **The sidebar owns the chats.** A **Chats** section sits above **Files**: a
  compose button ("New", `#chats-new`) and the eight most recent
  conversations as rows, each with the agent's coloured mark, its title and
  how long ago it spoke; the one in view is highlighted like the open file
  in the tree, and "All chats…" opens the full list. The desktop top bar is
  therefore just ☰ and the brand; a phone keeps the compose button top
  right, and the collapsed-panel corner keeps ☰ + compose.
- **The agent lives in the composer**, where claude.ai keeps its model: a
  chip at the box's foot showing the agent's mark, its name and the current
  mode. Its menu lists every installed agent with its sign-in state, the
  modes, the agent's own switches (Codex's reasoning effort and the like)
  and "Manage agents & sign-in…". The chip is there at every size — in a
  third-width column, in the panel, on a phone (the mark alone) — which the
  old head pill was not.
- **Agent marks** (`agentAvatar` in app.js) are tinted squares with a glyph:
  Claude's spark, a letter for the rest, one colour per agent in every
  theme. They appear in the sidebar rows, the chip, the menu and the empty
  screen.
- **The empty screen** greets the person by name, says what the agent can
  reach, and offers three suggestion chips that fill the box.
- **Tool lines carry an icon for their kind** (a file for Read, a prompt for
  Bash, a pencil for Update…) and answers carry a copy button on hover.

## What the chat guarantees (hardened 2026-09-21)

A report-only tester drove the chat through every state it could think of
(81 screenshots, echo agent plus synthetic updates); these are the rules the
code now keeps, with the reasons.

- **A conversation survives its process.** The backend replays what its log
  holds on `attach`; when the agent process is new (an idle stop, a restart
  after a sign-in, `POST /api/acp/restart`) the log is empty, and a restored
  tab then asks the agent for the conversation itself (`session/load`, the
  same call Recent chats makes) instead of showing a blank "new chat". A
  chat born in this tab never asks. The echo test agent persists its
  sessions in `~/.cache/acp-echo/sessions.json` so this is testable.
- **Nothing you send is lost.** With the socket down the bubble is drawn,
  marked "Not sent · Retry", and sent by itself once the connection is back
  (`outbox` in chat.js). A socket that drops *while* a prompt is in flight
  leaves the outcome unknown, so that bubble waits for a hand: "The
  connection dropped while sending · Retry".
- **A reload mid-turn keeps the order.** Between our `attach` and the
  backend's `attached`, frames wait in a buffer and are applied in log order,
  once each — a live chunk used to be drawn above the whole replay.
- **Keys answer the prompt asked first.** With two open permission prompts,
  1–9 go to the oldest; the newer says "answer the one above first".
- **A stop the agent ignores still ends the turn** after 5 s ("Interrupted"),
  and a tool the agent left "running" at the end of a turn is marked done.
- **A chat alone takes the screen.** With nothing open the chat takes the
  empty group; the first document then opens in a new column on the other
  side, and the chat keeps the third it would have asked for.
- **The chat has no title bar of its own**: the tab strip above it is the
  title bar (the window system's), so a long title truncates there and
  nothing is printed twice. What is left of the old head is the ⋯ menu,
  which sits in the composer beside the agent chip; on a phone and in a
  short chat, where that button has no room, its items (New chat, Rename,
  Recent chats, Agent log) fold into the agent menu, so one control holds
  everything.
- **The agent menu is a page-level layer**, positioned against the chip and
  clamped to the window. Inside the pane it was clipped by the chat's own
  overflow — in the terminal panel the first agents were simply unreachable.
- **One row on a touch screen, and wherever height is scarce.** On a
  phone or tablet the composer is always ＋ · text · mic · send on one
  line, the text growing upward beside the buttons (ChatGPT's phone
  composer); a desktop chat switches to the same row when its own height
  drops under 460px (a landscape window, the panel), measured by the view's
  own ResizeObserver (a *height* container query on this element crashes
  Chromium's renderer — see the note in the stylesheet),
  and the plan folds away there. A tall desktop chat keeps claude.ai's
  two-level box.
- Transcript ergonomics: a "↓ New replies" pill when you scrolled up while
  it streams; a 52rem reading measure centred in wide groups; `dir="auto"`
  on bubbles; task-list checkboxes kept (disabled); tables never break a
  word (they scroll); folded tool output keeps one line per line and scrolls
  sideways; long titles truncate; toasts sit top-centre on a desktop so they
  never cover a group's actions.
- **A question sits under what it is about**: a permission prompt renders
  after the tool's diff or output, not above it.
- Not done: a raw-markdown user bubble is by design (Claude Code shows the
  prompt as typed); a Back button in a brand-new tab's picker (the tab's ×
  is the way out).
