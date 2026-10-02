---
name: install-company-os
description: Install Ollsoft Company OS on a fresh cloud server, interactively, from the user's own computer — rent a VPS (Hetzner or Contabo), harden it, install the platform, put it behind Cloudflare on their domain, then ask one by one about branding, ElevenLabs dictation, semantic search, AI agents (Claude Code, Codex, Hermes), user accounts, projects, starter content, monitoring and backups. Use when someone says "install Company OS", "set up / self-host Company OS", "put Company OS on a server", or follows the README's install-with-your-agent step. Also for re-running one phase on an existing install (add Cloudflare later, add search keys, add people, set up backups).
---

# Install Company OS, guided

You run on the **human's own computer** and drive a brand-new server over SSH. They rent the server, click through two dashboards and answer questions; you do everything else. Any agent can follow this file; helpers are bash (Git Bash on Windows).

## Rules

- **One question at a time**, plain language, your recommendation first. "Skip" is always valid — say how to come back to it.
- **Name the cost before any paid service.** No prices from memory; they see them at checkout.
- **Verify every step** with its *Verify* / *Done when* line before moving on. Report each step in one line.
- **Never lock them out.** Keep the working SSH session until a fresh connection on the new settings succeeds.
- **Secrets don't pass through you unless they choose so.** Default to a *key drop*. Never echo a secret, never put one in the state file, the knowledgebase or a reply.
- **Long commands run detached** (`cos-run`, phase 2) and you poll.
- **State file** `~/company-os-install/<host>.md`, local: answers, choices, IDs, each phase done/skipped — no secrets. Read it first; if it exists, offer to resume.
- **Stop on surprises** — wrong OS, a 200 where a 302 belongs, a failed verify. Explain, fix, then continue.

## Before phase 1, ask

1. Nothing about their computer — detect it: `uname -s` gives `Darwin` (macOS), `Linux`, or `MINGW*`/`MSYS*`/`CYGWIN*` (Windows); PowerShell has `$env:OS` = `Windows_NT`. Say what you found; phase 1's SSH key and phase 8's network drive follow it.
2. Company name, and the **admin username** (lowercase, usually their first name) — their Linux account and web login.
3. Do they already have a fresh Ubuntu 24.04 server? Then skip to phase 1's *Get key-based root access*.

## Phases

Read each file when you reach it, not before.

| # | Phase | You ask | File |
|---|---|---|---|
| 1 | Rent the server | provider, IP, key or password | `reference/1-server.md` |
| 2 | Harden | timezone | `reference/2-harden.md` |
| 3 | Install + audit trail | — they set their own password | `reference/3-install.md` |
| 4 | Domain + Cloudflare | Cloudflare? domain, who may sign in, dashboard or token | `reference/4-edge.md` |
| 5 | Branding + providers | name, logo, ElevenLabs, search keys, budgets | `reference/5-providers.md` |
| 6 | AI agents | which agents, terminal CLIs for whom, Hermes | `reference/6-agents.md` |
| 7 | People + content | accounts, their terminal agents, admins, projects, sensitive folders, starter content | `reference/7-people.md` |
| 8 | Operations | AI health check, alerts, Hermes brief + security audit, uptime, backups, share links, network drive, personal OneDrive | `reference/8-ops.md` (drive: `reference/network-drive.md`) |
| 9 | Handover | — | below |

## Helpers

**SSH alias** — written after phase 2 moves SSH (phase 1 uses `companyos-root`):

```
Host companyos
  HostName <ip>
  User <admin>
  Port 2007
  IdentityFile ~/.ssh/companyos_ed25519
  IdentitiesOnly yes
  ServerAliveInterval 30
```

**Admin API** — needs the session from phase 3 §5. Answers are JSON; `{"error": …}` means it failed.

```bash
admin() { printf '%s' "${3:-}" | ssh companyos "curl -s -b ~/.cos-admin.jar -X $1 -H 'Content-Type: application/json' ${3:+--data-binary @-} http://127.0.0.1:8300$2"; echo; }
# admin GET /admin/me      admin POST /admin/settings '{"set":{"brand.name":"Acme"}}'
```

**kb.env** — keeps it 0640 (it holds the alert topic):

```bash
kbenv() { ssh companyos "sudo sed -i '/^$1=/d' /etc/kb/kb.env && echo '$1=$2' | sudo tee -a /etc/kb/kb.env >/dev/null && sudo chmod 640 /etc/kb/kb.env"; }
```

**Key drop** — the human runs this in their own terminal; the key goes straight to a root-only file:

```bash
ssh -t companyos 'read -rsp "<what> (hidden): " k; echo; printf %s "$k" | sudo sh -c "umask 077; cat > /root/.cos-<name>.key"'
# multi-line (env files):
ssh -t companyos 'echo "Paste, then Enter and Ctrl-D:"; sudo sh -c "umask 077; cat > /root/.cos-<name>.key"'
```

If they would rather paste it in the chat, write it the same way via stdin, and tell them once that it now sits in this conversation's history — rotate it later if that matters.

## Phase 9 — Handover

1. **Remove what only the install needed** (sudoers last — it ends your passwordless sudo):
   ```bash
   ssh companyos 'rm -f ~/.cos-admin.jar; sudo sh -c "rm -f /root/ollsoft-company-os-*.txt; shred -u /root/.cos-*.key 2>/dev/null"; sudo rm -f /etc/sudoers.d/90-company-os-installer'
   ```
   Verify: `ssh companyos sudo -n true` now fails with *a password is required*.
2. **Cloudflare API token** (token path): they delete it under My Profile → API Tokens.
3. **Final check from outside:** `https://<host>` → 302 to Access · they sign in · all `kb-*` services active · `systemctl list-timers 'kb-*'` shows heartbeat, gitgc and whatever phase 8 enabled · a backup snapshot exists.
4. **Server record** in their knowledgebase, private: `users/<admin>/company-os-server.md`, written as the admin — IP, SSH alias and port, what is installed, providers on, Cloudflare IDs, backup target, **where** each key lives (never a value), how to upgrade. House style: short, bullets, no hard wraps.
5. **Upgrading later** (they type their sudo password):
   `ssh -t companyos 'cd ~/ollsoft-company-os && git pull && sudo bash scripts/install.sh --admin $USER'`
6. **Tell them in ≤ 8 lines:** the URL, who has accounts, what is on, what was skipped and that "run install-company-os, phase N" adds it later.

## Re-running a phase on an existing install

Read the state file. Your passwordless sudo is gone, so the human first runs (typing their password) `ssh -t companyos 'echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/90-company-os-installer'`; recreate the admin session (phase 3 §5) if the phase calls the API; finish with phase 9 step 1.
