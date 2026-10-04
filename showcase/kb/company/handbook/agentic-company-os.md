# Work with Company OS through an AI agent

> **For named full accounts.** The public `demo` viewer is intentionally
> web-only. Ask an administrator to create your own Company OS account before
> using a terminal or agent.

Company OS is a normal file tree at `/srv/kb`. An agent started there can work
across the company knowledge you are allowed to access. The Linux account is the
permission boundary: hidden projects stay hidden, and changes carry your user as
their author.

## 1. Open your terminal

Use the terminal panel in Company OS or SSH into the box with your named account,
then confirm where and who you are:

```bash
whoami
cd /srv/kb
pwd
```

Never run an AI client as `root`. Its effective access should match yours.

## 2. Install one client

Choose either client. These commands install into your user environment.

On this hosted showcase, Claude Code is already installed for the named demo
accounts. Click **Claude** in the launcher bar and complete your own Anthropic
sign-in. Use the installation command below on a new Company OS deployment.

### OpenAI Codex CLI

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex
```

On first run, choose **Sign in with ChatGPT** or another offered authentication
method. Official guide: [Codex CLI](https://developers.openai.com/codex/cli/).

### Anthropic Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude
```

Follow the browser login prompt. Official guide:
[Claude Code quickstart](https://code.claude.com/docs/en/quickstart).

Your AI subscription or API usage is separate from Company OS. Do not paste API
keys into company documents, prompts or shell history.

## 3. Let Company OS orient the agent

- Claude Code reads `CLAUDE.md` and discovers company skills under
  `.claude/skills/` — links to `AGENTS.md` and `.agents/skills/`.
- Codex reads `AGENTS.md` and the skills in `.agents/skills/` directly — one
  copy of each, shared by both.
- Start the client from `/srv/kb`, not from your home directory, so it discovers
  these instructions.

Use a read-only first prompt:

```text
Read the Company OS instructions and relevant skills. Do not change anything.
Map the company and project information I can access, then show my incomplete
tasks with links to their source files.
```

Then try a small owned task:

```text
Draft a weekly operating brief in users/<my-user>/weekly-brief.md from the
current objectives, risks, sales pipeline and delivery board. Show the proposed
outline before writing.
```

Or create governed company evidence:

```text
Using the relevant Company OS skills, draft a customer-requirements review for
a German industrial customer. Align it with our ISO 9001 process, cite the
source documents, and save it in my private folder for review.
```

## Working safely

- **Inspect first.** Ask for a plan and sources before broad edits.
- **Use your private folder for drafts.** Move approved results into shared or
  project folders only when they are ready.
- **Review every command and diff.** An agent can change everything your Linux
  account can change.
- **Keep secrets in approved secret stores.** Never put credentials in Markdown,
  Git, artifacts or prompts.
- **Approve the provider before sharing data.** Linux access is not permission
  to send customer information to an external AI service. The Polaris example
  requires EEA processing; check the provider, contract and data location before
  using real customer records. This hosted demo contains fictional records.
- **Respect evidence status.** Label drafts, preserve source links and never let
  an agent claim ISO certification or management approval.
