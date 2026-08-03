# Ollsoft Company OS

An **OS-native, AI-agent-native company knowledgebase**. Think "Obsidian, but
multiplayer, permissioned, and built for agents" — running on a single Linux box.

The design rests on three ideas:

1. **Markdown files are the source of truth.** Everything lives as plain `.md`
   files in a git repo at `/srv/kb`. The database is a *disposable* index you can
   drop and rebuild from the files at any time.
2. **One Linux user per human. The kernel enforces access.** Every web request is
   served by a process running *as that OS user* (`runuser`), so the kernel — not
   application code — decides what each person can read and write. Postgres
   Row-Level Security mirrors the same Unix permissions, so search and SQL can
   never return a file you couldn't `cat`.
3. **Everything composes on files + Unix + Postgres.** Multiplayer editing, task
   aggregation, live dashboards, agents, sharing — none of them need a bespoke
   permission system. They inherit the kernel's.

There is no permission table in this codebase. That is the whole point.

---

## Requirements

Ollsoft Company OS needs a **whole machine** — a VM or bare metal running **Ubuntu 24.04**.

It cannot run in an unprivileged container, and that is by design rather than an
oversight: it creates real Linux accounts, authenticates against PAM, spawns
processes as individual users, and relies on systemd and Postgres peer auth. A
container with fake users would run, but it would be a demo of the UI with the
security model removed — the part worth having.

Budget a small VM: 2 vCPU / 4 GB RAM / 20 GB disk is comfortable for a team.

---

## Install

```bash
git clone https://github.com/<you>/ollsoft-company-os.git
cd ollsoft-company-os
sudo bash scripts/install.sh --admin <your-username>
```

That single command installs system packages, creates the `kb-users` group and
the `kbindexer` service account, builds the frontend, lays out `/srv/kb` with the
right modes and ACLs, creates the Postgres cluster objects and RLS schema, writes
`/etc/kb/kb.env`, and enables the three systemd services. It is idempotent — re-run
it to upgrade.

It creates exactly one account: yours. A generated password is written to
`/root/ollsoft-company-os-admin.txt` (delete it after your first login), or pass your own
with `--admin-pass`.

Then open **http://127.0.0.1:8300**. It binds to localhost only. From your laptop:

```bash
ssh -L 8300:127.0.0.1:8300 you@your-box
```

See **[docs/remote-access.md](docs/remote-access.md)** before exposing it to a
network — it needs a TLS front door and an identity layer. To work on the
knowledgebase from Explorer — open and save Office files as if it were a
network share — see **[docs/windows-drive.md](docs/windows-drive.md)**.

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--admin <user>` | *required* | Admin account to create, or adopt if it exists |
| `--admin-pass <pw>` | generated | Password for a newly created admin |
| `--repo <path>` | `/srv/kb` | Where the knowledgebase lives |
| `--prefix <path>` | `/opt/kb-platform` | Where code is deployed |
| `--port <n>` | `8300` | Hub port on 127.0.0.1 |
| `--admin-group <g>` | `sudo` | OS group granting platform-admin rights |
| `--no-packages` | — | Skip `apt-get` (dependencies already present) |
| `--no-start` | — | Install without enabling the services |

### Try the permission model

```bash
sudo bash scripts/seed-demo.sh
```

Creates `alice`, `bob` and `carol`, plus a `projects/acme/` folder restricted to
the `proj-acme` group. Alice and Bob are members; Carol is not. Log in as Carol:
the folder is absent from her file tree, absent from search, and
`SELECT * FROM kb.blocks` returns none of its rows either — the RLS policy
re-checks the same Unix permission for every row. Undo with `--undo`.

---

## What it does

- **Multiplayer markdown editing** on the files themselves (browser + `vim` +
  agents all merge through one CRDT; the `.md` file *is* a CRDT peer). Every
  document save is versioned in a root-only git repo with the real author
  recorded; users read their permitted history via `kb-history` or the editor's
  history panel — never git directly.
- **Live presence & cursors**: see who has a doc open and each collaborator's
  named cursor moving in real time, in both rich and source views.
- **Rich or source editing**: a rendered-but-editable view (headings, bold, links,
  interactive checkboxes, images) over the same markdown — with a formatting
  toolbar and drag-drop / screenshot-paste that stores files and renders them
  inline — or a raw-source view with line numbers. One toggle, same document.
- **Link what is already there**: drag any file or folder from the tree into an
  open document and it becomes a link at the drop point — images and video embed,
  documents open as a tab when you click through.
- **Kernel-enforced permissions**, surfaced through a web file tree, editor, and a
  real in-browser terminal (each running as your OS user).
- **VS-Code-style shell**: documents and artifacts open as tabs (background
  artifacts stay live); terminals are tabbed in a docked, resizable bottom panel.
- **Cron panel**: every user has their own `crontab`; the UI lists, adds, pauses
  and deletes jobs, which run as the user even while logged out.
- **Read-only viewing** of files you can see but not edit (live, but the daemon
  refuses to persist your edits).
- **Dictation** (`F9`, hold-to-talk or tap-to-latch): speech-to-text that lands
  wherever you were already typing — a document, a **terminal**, the command
  palette, any field. The ElevenLabs key is a company credential at
  `/etc/kb/elevenlabs.key` (`0600 root:root`): every logged-in user may spend it
  through the hub, nobody may read it, and the caller never picks the upstream
  URL. See [docs/dictation.md](docs/dictation.md).
- **Full-text + task search** over a Postgres index, RLS-scoped per user.
- **To-dos**: `- [ ] task @assignee #tag` checkboxes aggregated across everything
  you can see, filterable, with write-back to the source file.
- **Sandboxed artifacts**: agent-written HTML dashboards that query the database
  and read/write files *as the viewer*, contained by an opaque-origin iframe + CSP.
- **File sharing**: per-file and per-folder ACLs via a permissions UI, including
  automatic traverse-grants so a share actually reaches the file.
- **Admin UI** (admin group only): create and remove users, create groups, assign
  membership — full provisioning of the OS account, home, private dir, Postgres
  role and personal schema.
- **AI agents** run as each user, with company **skills** in the repo teaching them
  the platform.

---

## Architecture at a glance

```
                          browser  (session cookie, https + websocket)
                              │
                     ┌────────▼─────────┐
                     │   kb-hub · ROOT  │   PAM login → signed cookie.
                     │  127.0.0.1:8300  │   Spawns & reverse-proxies per-user
                     └───┬──────────┬───┘   backends. Privileged /fs/* + /admin/*.
        runuser -u <you> │          │  /ws/doc  (+ signed uid/gids token)
      ┌──────────────────▼──┐   ┌───▼──────────────────┐
      │  user backend       │   │   kb-syncd · ROOT     │  y-websocket CRDT relay +
      │  runs AS <you>      │   │  file daemon; writes  │  filesystem merge. Preserves
      │  files·pty·sql·tasks│   │  .md, preserves owner │  owner/group/mode. Read-only
      │  artifact bridge    │   └───────────┬──────────┘   viewers can't mutate.
      └──────────┬──────────┘               │ inotify ⇄ Yjs
                 │ peer auth                 ▼
        ┌────────▼─────────┐    /srv/kb  ·  git-versioned markdown = TRUTH
        │   PostgreSQL     │◀── kb-indexer (user kbindexer): parses md → rows,
        │  RLS = Unix      │    refreshes group membership, honors POSIX ACLs.
        │  read + traverse │    Disposable · rebuildable · pgvector + FTS.
        └──────────────────┘
                                kb-convert (same user): office/PDF binaries →
                                hidden read-only .md sidecars, so their text is
                                searchable and agent-readable like any doc.
```

The two root services (`kb-hub`, `kb-syncd`) do only auth, proxying, and the
CRDT/file merge — the small, auditable surface. Everything touching a user's data
runs *as that user* (`runuser`, peer auth) or re-checks their Unix bits.

Full component tour: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.
Capacity ceilings, growth hygiene and the drift watchlist: **[docs/SCALING.md](docs/SCALING.md)**.

---

## Repository layout

```
ollsoft-company-os/
├── kb_platform/            the Python backend (one module per component)
│   ├── common.py           paths, config, HMAC session tokens, safe path helpers
│   ├── pam_auth.py         PAM login
│   ├── hub.py              ROOT: login, spawner, reverse proxy, /fs/* + /admin/*
│   ├── user_server.py      per-user backend (runs AS the user)
│   ├── syncd.py            ROOT: CRDT relay + filesystem daemon
│   └── indexer.py          markdown → Postgres index (RLS metadata, ACLs, tasks)
├── frontend/               vanilla-JS SPA (CodeMirror 6 + Yjs + xterm), esbuild
│   ├── src/app.js          the whole client
│   ├── src/dictation.js    microphone capture + push-to-talk (owns no routing)
│   ├── assets/             hand-authored shell: app.html, login.html, style.css, logos
│   ├── static/             build output (generated, gitignored)
│   └── build.mjs           esbuild bundler
├── scripts/
│   ├── install.sh          one-command install / upgrade  ← start here
│   ├── seed-demo.sh        optional sample company (alice/bob/carol + acme)
│   ├── deploy.sh           redeploy code after editing it (development)
│   ├── kb-heartbeat.sh     functional health check (kb-heartbeat.timer, 5 min)
│   ├── kb-alert.sh         append an alert to /var/log/kb/alerts.log (push is opt-in)
│   ├── kb-maintenance.sh   daily triage: bundle -> headless agent -> notify only if real
│   ├── kb-maintenance-policy.md  what counts as noise vs a real problem, and what the agent may do
│   ├── install-dictation-key.sh  validate + install the ElevenLabs key (root 0600)
│   ├── bounce_backends.py  restart per-user backends after a deploy
│   ├── schema.sql          Postgres schema, RLS functions, grants
│   └── demo_cron_pulse.py  example: a crontab feeding a live artifact
├── systemd/                kb-hub / kb-syncd / kb-indexer / kb-convert units, the
│                           kb-heartbeat + kb-maintenance + kb-gitgc timers, tmpfiles, logrotate
├── defaults/               shipped into <repo>/.claude/ and company/ on install
├── company-skills/         agent skills, deployed to /srv/kb/.claude/skills/
├── tests/                  pytest: cli/ (httpx) + e2e/ (Playwright) + torture/
└── docs/                   ARCHITECTURE · SECURITY · SETUP · DEVELOPING · monitoring · dictation · remote-access · converted-documents · windows-drive
```

**Created on the box by the installer** (not in this repo):

```
/opt/kb-platform      code, world-readable (so per-user backends can run it)
/opt/kb-venv          the Python venv, world-executable
/opt/kb-convert-venv  kb-convert's parser venv — heavy deps, kept separate on purpose
/srv/kb               the knowledgebase: git repo of markdown + attachments
/etc/kb/kb.env        runtime configuration read by the systemd units
/etc/kb/elevenlabs.key  dictation credential (root 0600) — the hub alone reads it
/etc/kb/session.key   HMAC key (root 0600)
/run/kb               unix sockets: syncd.sock (root), users/<u>/ (per-user 0700)
```

---

## Configuration

Everything the services need lives in `/etc/kb/kb.env`, written by the installer:

| Variable | Default | Meaning |
|---|---|---|
| `KB_REPO` | `/srv/kb` | Knowledgebase location |
| `KB_HUB_PORT` | `8300` | Hub listen port (127.0.0.1 only) |
| `KB_PG_DB` | `kb` | Postgres database name |
| `KB_ADMIN_GROUP` | `sudo` | OS group granting platform-admin rights |
| `KB_PROTECTED_USERS` | founding admin | Accounts the admin UI refuses to modify or delete |
| `KB_PLATFORM_ROOT` | `/opt/kb-platform` | Deployed code |
| `KB_VENV_PY` | `/opt/kb-venv/bin/python` | Interpreter for per-user backends |
| `KB_NTFY_TOPIC` | *(empty)* | ntfy topic for pushed alerts; empty = no pushes possible |
| `KB_ALERT_PUSH` | `0` | `1` pushes every alert as it happens. `0` = log only, triaged daily |
| `KB_ALERT_DEDUP` | `21600` | Seconds an identical alert title stays muted for pushes |

After editing: `sudo systemctl restart kb-hub kb-syncd kb-indexer kb-convert`.

---

## Operating it

```bash
# status and logs
systemctl status kb-hub kb-syncd kb-indexer kb-convert
journalctl -u kb-hub -u kb-syncd -u kb-indexer -u kb-convert -f

# redeploy after editing code (reads /etc/kb/kb.env for paths)
sudo bash scripts/deploy.sh

# rebuild the frontend after editing frontend/src
(cd frontend && node build.mjs) && sudo bash scripts/deploy.sh

# the index is disposable — rebuild it from the markdown at any time
sudo systemctl restart kb-indexer

# office/PDF → markdown sidecars are equally disposable — force a resweep
sudo systemctl restart kb-convert

# monitoring: alerts are logged, not pushed (see docs/monitoring.md)
sudo tail -5 /var/log/kb/alerts.log            # every alert ever raised
sudo tail -40 /var/log/kb/maintenance.log      # the daily triage verdicts
sudo /opt/kb-platform/scripts/kb-maintenance.sh --dry-run --stdout   # triage now
```

> `deploy.sh` restarts `kb-hub`, and its cgroup holds every open web terminal and
> per-user backend. Pass `--no-restart` if anyone might be mid-session, then
> restart only what your change touched.

### Running the tests

The suite assumes the demo company exists, because it tests the permission model
against real accounts. `seed-demo.sh` writes the logins the tests read from
`/tmp/kb-test-creds.json`, so run it first:

```bash
sudo bash scripts/seed-demo.sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium        # for tests/e2e
.venv/bin/python -m pytest tests/ -q
```

`tests/cli/` needs no browser and is the fast loop. See
**[docs/DEVELOPING.md](docs/DEVELOPING.md)**.

---

## Security posture

Read **[docs/SECURITY.md](docs/SECURITY.md)** before putting this anywhere.

The design is deliberate and has been hardened: the privileged root surfaces
(`/fs/*`, `/admin/*`, `kb-syncd`) use symlink-safe `openat`/`O_NOFOLLOW`
operations, `.git` is root-only so history can't bypass file permissions, and
artifacts run in an opaque-origin sandbox with no network. The security model is
tested, not just asserted — `tests/cli/test_rls.py`, `test_security_fixes.py`,
`test_visibility.py` and the e2e sandbox tests exercise it directly.

That said: **this has not been externally audited.** It binds to localhost by
design. Do not expose it to a network without a TLS front door and an identity
layer in front, and read the threat model first.

Found a security problem? Please report it privately rather than opening a public
issue — see [SECURITY.md](docs/SECURITY.md) for the contact.

---

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. The short version: there is no
permission code to add — if a feature seems to need one, it probably wants a Unix
mode, an ACL, or a Postgres grant instead.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Built at [Ollsoft](https://ollsoft.ai).
