# Setup

The short path is one command — see the [README](../README.md):

```bash
sudo bash scripts/install.sh --admin <your-username>
```

This document explains what that does, how to do it by hand if you want to
understand or adapt each step, and what to check when something goes wrong.

---

## Prerequisites

A fresh **Ubuntu 24.04** VM or bare-metal host. Not a container: the platform
creates real Linux accounts, authenticates against PAM, spawns processes as
individual users, and needs systemd and Postgres peer auth.

The installer refuses to run without systemd for exactly this reason.

---

## What `install.sh` does, step by step

### 1. System packages

```bash
sudo apt-get install -y \
  postgresql postgresql-contrib postgresql-<major>-pgvector \
  python3-pip python3-venv python3-dev \
  acl inotify-tools build-essential libpam0g-dev \
  nodejs npm git curl ca-certificates rsync
```

`acl` (setfacl/getfacl) and `inotify-tools` are load-bearing — sharing and the
file daemon depend on them. The pgvector package name tracks your Postgres major
version; the installer detects it. Semantic search needs **pgvector ≥ 0.7**
(`halfvec`); where the distribution ships older (Ubuntu 24.04 has 0.6), the
installer adds the PostgreSQL project's apt repository and installs a current
pgvector for the same server version. Without it everything else — full-text
search included — works, and semantic search reports `unsupported`. Confirm the cluster is up and uses peer auth:

```bash
pg_lsclusters                                        # should show <major>/main online
grep '^local' /etc/postgresql/<major>/main/pg_hba.conf   # want: local all all peer
```

Peer auth is not optional — it is what makes `SELECT current_user` in the
database equal your OS identity, with no password anywhere.

### 2. Groups and the service account

- `kb-users` — every human account joins it; it group-owns the shared repo.
- `kbindexer` — a `nologin` system account that owns the `kb` schema and is the
  only writer to it. It joins `kb-users` and every project group, so it can index
  restricted folders. What it is deliberately *not* a member of is the `kb_users`
  **Postgres** role — so it cannot read users' application data. `kb-convert`
  runs as the same account for the same reason: what can be indexed is exactly
  what can be converted (see
  [converted-documents.md](converted-documents.md)).

### 3. The admin account

One account, named by `--admin`. It is added to `kb-users` and to the admin group
(`sudo` by default, `--admin-group` to change). If the account already exists it
is adopted, and its password is left alone.

Cloud images often provide an existing admin account that is SSH-key-only and
password-locked. Check an adopted account with `passwd -S <user>`; if its status
is `L` or `NP`, set a password with `sudo passwd <user>` so PAM can authenticate
the Company OS web login. This does not enable SSH password authentication.

The generated password is written to `/root/ollsoft-company-os-admin.txt` — delete that
file after your first login.

### 4. The repo skeleton

```
/srv/kb              3775 root:kb-users, sticky   the knowledgebase (a git repo)
├── .git             0700 root                     history — root-only, always
├── .gitignore       0644 root:kb-users            .md, .html and .os/*.json only
├── .claude/         0755 root:kb-users            agent context (CLAUDE.md) and skills/
├── .os/             2755 root:kb-users            platform config: launchers, egress, settings
├── AGENTS.md        -> .claude/CLAUDE.md           Codex discovers the same context
├── company/         2775 root:kb-users + default ACL   everyone reads and writes
├── projects/        3775 root:kb-users, sticky    restricted folders go here
└── users/           3775 root:kb-users, sticky    one 0700 <name>:<name> dir per person
```

Three details that matter more than they look:

- **setgid (`2775`) plus a default ACL** on `company/` means new files are
  group-writable no matter what umask the writer had. Without it, `vim`, the web
  app and an agent would each produce different permissions for the same folder.
- **`.git` is mode 0700, root-only.** Its objects contain every committed version
  of every file. Group access there would let anyone read the history of content
  they cannot read on disk — bypassing both file permissions and RLS. `syncd`
  re-asserts this on start; the installer re-asserts it on every run.
- **Sticky (`+t`) on the repo root, `projects/` and `users/` — deliberately not on
  `company/`.** Sticky is what stops one member renaming another's directory
  aside; on `company/`, whose top level holds shared documents, it would also
  block `O_CREAT` on files you do not own. See [SECURITY.md](SECURITY.md).

### 5. Python venv and frontend bundle

The venv is created at `/opt/kb-venv`. The frontend is bundled with esbuild:
`frontend/src/app.js` is compiled and the hand-authored shell in
`frontend/assets/` (app.html, login.html, style.css, the brand marks) is copied
alongside it into `frontend/static/`, which is entirely generated and gitignored.
The installer rebuilds whenever npm is available, so an upgrade never ships a
stale bundle; if npm is missing it falls back to a pre-built `static/` and fails
loudly if there is none.


### 6. Postgres

Creates the `kb_users` group role, the `kbindexer` role, your admin role, the
`kb` database owned by `kbindexer`, the `vector` extension, the schema and RLS
policies from `scripts/schema.sql`, and your personal `u_<you>` schema.

`kb_users` is the database-side mirror of the `kb-users` OS group. Grant a table
to it and every employee — including people hired later — can use it. Granting to
a list of names instead is the mistake it exists to prevent.

### 7. Configuration, units, start

Writes `/etc/kb/kb.env` (read by all units via `EnvironmentFile`), creates
`/etc/kb/session.key` (0600, root) if absent, installs the tmpfiles config and the
systemd units with paths rewritten for your `--prefix`, then enables and starts
`kb-syncd`, `kb-hub`, `kb-indexer`, `kb-embedd` (semantic search; idle until
provider keys are installed — [semantic-search.md](semantic-search.md)) and
`kb-convert` (the last one only when its
venv was provisioned from `requirements-convert.txt` — the document parsers live
in a separate `kb-convert-venv` next to the platform venv).

---

## First login

The app binds to `127.0.0.1` only. From your laptop:

```bash
ssh -L 8300:127.0.0.1:8300 you@box
# then open http://localhost:8300
```

Log in with your admin account. To see the permission model rather than an empty
knowledgebase, seed the demo company first:

```bash
sudo bash scripts/seed-demo.sh
```

Then verify the core promises:

- Open the same document in two browsers as two users — edits merge live. `vim`
  the file over SSH and it appears in both.
- Log in as `carol`: `projects/acme/` is absent from her file tree, `/api/file`
  on it returns 403, and `SELECT * FROM kb.blocks` returns none of its rows. All
  three are the kernel and the RLS policy, not application code.
- The in-browser terminal runs `whoami` as the logged-in user.
- From that terminal, start Claude Code or Codex in `/srv/kb`. Claude reads
  `.claude/CLAUDE.md`; Codex follows the root `AGENTS.md` symlink to the same
  context. Both can use the platform skills in `.claude/skills/`; see
  [agent-cli.md](agent-cli.md) for installation, first prompts and guardrails.

For a presentation-ready fictional company instead of the compact test fixture,
seed the English-language German GmbH showcase:

```bash
sudo bash scripts/seed-showcase.sh --admin <your-username>
# after pulling newer showcase content:
sudo bash scripts/seed-showcase.sh --admin <your-username> --refresh
# optionally grant an EXISTING full demo user access to the public project:
sudo bash scripts/seed-showcase.sh --admin <your-username> --member peter
```

It installs ISO-aligned example processes, projects, interactive artifacts and a
web-only `demo` account. Its deliberately memorable demo password is written to
`/root/ollsoft-company-os-showcase.txt`; do not use that account or password on
a non-demo installation. `--undo` removes only the showcase's named trees and a
web-only account that the script itself created. This account can edit shared
content; web-only does not mean read-only. `--refresh` replaces seeded documents
and artifact JSON with the scenario baseline, so do not use it to deploy only
screenshots or preserve visitor edits. It does not remove unrelated files.

The repeatable `--member` option adds existing users to `kb-users` and
`proj-polaris`, not the confidential Helios project. Existing backend processes
must be restarted for changed Linux group membership to take effect; use the
administration interface for a live account, or stop its backend after checking
that no terminal work is running. The seeder does not install AI CLIs or create
full employee accounts.

On a dedicated showcase host, an optional exact-root redirect in the HTTPS nginx
server makes a fresh login open the tour instead of an empty workspace:

```nginx
location = / {
    return 302 /company/00%20START%20HERE.md;
}
```

Keep the normal proxy for other paths, `/login`, API and WebSockets. The hosted
showcase is public at the nginx layer; Company OS still presents its own OS-user
sign-in. Validate with `nginx -t` before reloading. This is a demo-host choice,
not a platform-wide change. See [showcase maintenance](../showcase/README.md)
for scenario boundaries and checks.

---

## Upgrading

Pull and re-run — `install.sh` is idempotent:

```bash
git pull
sudo bash scripts/install.sh --admin <your-username>
```

Existing accounts, repo content, the agent context in `.claude/` and the
platform config in `.os/` are left alone. Agent skills *are* refreshed, since
they document the platform.

**Upgrading from before 2026-09:** the launcher list and the egress allow-list
used to live in `.claude/`. The hub moves `.claude/{launchers,egress}.json` into
`.os/` on its next start and un-ignores `.os/*.json` in the repo's `.gitignore`
(watch for `config migration:` lines in `journalctl -u kb-hub`). Each person's
`users/<name>/.launchers.json` moves into `users/<name>/.os/` the first time
their backend reads it. To roll back to older code, move the two files back
first: `sudo mv /srv/kb/.os/{egress,launchers}.json /srv/kb/.claude/`.

For a code-only redeploy during development, `sudo bash scripts/deploy.sh` is
faster — it reads `/etc/kb/kb.env` for your paths.

---

## Troubleshooting

**`systemctl status kb-hub` shows a Python traceback about `pam`**
The venv is missing `six`, which `python-pam` imports but does not declare.
`sudo /opt/kb-venv/bin/python -m pip install six`.

**Login fails for a user who definitely has the right password**
The hub authenticates against PAM as root. Check `journalctl -u kb-hub`, and
confirm the account is not locked (`passwd -S <user>`) and has a real shell if it
is meant to be a full account rather than a viewer.

**The editor loads but never syncs**
`kb-syncd` is down, or a reverse proxy in front is not forwarding WebSocket
upgrades. See [remote-access.md](remote-access.md).

**Search and to-dos are empty**
`kb-indexer` is down, or it was restarted and is still part-way through its
resweep. The index is disposable — `sudo systemctl restart kb-indexer` rebuilds
it from the markdown, at the cost of a full pass (see [SCALING.md](SCALING.md)).

**An office file or PDF has no text in search**
`kb-convert` is down, or the conversion failed — check
`journalctl -u kb-convert` and the `status:` line in the hidden `.name.ext.md`
sidecar next to the file. Sidecars are as disposable as the index:
`sudo systemctl restart kb-convert` resweeps everything.

**A user was added to a group but still cannot see the folder**
Group membership is cached per process. The hub kills the user's backend on
membership change; if you changed the group by hand with `usermod`, kill it
yourself: `sudo pkill -u <user> -f kb_platform.user_server`.

**`permission denied for schema u_<someone>` inside an artifact**
The artifact reads a table in another person's schema and that person has not
granted it. See the `kb-database` skill — the fix is a `GRANT ... TO kb_users`.

---

## Configuration reference

`/etc/kb/kb.env`, read by the systemd units:

| Var | Default | Meaning |
|-----|---------|---------|
| `KB_REPO` | `/srv/kb` | the knowledgebase git repo |
| `KB_RUN` | `/run/kb` | runtime sockets dir |
| `KB_ETC` | `/etc/kb` | session key + profiles |
| `KB_PLATFORM_ROOT` | `/opt/kb-platform` | deployed code |
| `KB_VENV_PY` | `/opt/kb-venv/bin/python` | interpreter for spawned backends |
| `KB_HUB_PORT` | `8300` | hub listen port (127.0.0.1) |
| `KB_PG_DB` | `kb` | Postgres database |
| `KB_ADMIN_GROUP` | `sudo` | OS group granting platform-admin rights |
| `KB_PROTECTED_USERS` | founding admin | accounts the admin UI won't modify or delete |

Restart the services after editing.

---

## Exposing it

Do **not** point a network at `:8300`. There is no TLS, and the login throttle is
a backstop rather than a front door. Read [remote-access.md](remote-access.md)
and [SECURITY.md](SECURITY.md) first, and rotate any passwords generated during
install.

## Semantic search (optional)

Search by meaning, in any language, with your own embedding and rerank
provider (Azure AI Foundry, Azure OpenAI, OpenAI + Cohere). Without keys,
search is full-text and nothing is sent anywhere.

```bash
sudo bash scripts/install-search-keys.sh --from-azure <account> <resource-group>
sudo systemctl restart kb-embedd kb-indexer
kb-search --status
```

Costs, budgets (Settings → Company → Search & AI), what is and is not sent,
and how to measure quality: [semantic-search.md](semantic-search.md).

## Agent chat (optional)

The chat button drives AI coding agents over ACP ([agent-chat.md](agent-chat.md)).
They are Node programs installed once per server, shared by everyone:

```bash
sudo bash scripts/install-agents.sh    # a private Node 22 + Claude, Codex, Gemini into /opt/kb-agents
```

The system's Node (Ubuntu's 18) is left alone; the agents get their own.
An admin can also install them from the chat's picker.
Without any of them the platform runs unchanged; the picker says what is
missing. Each person signs in to an agent themselves, from the picker.
