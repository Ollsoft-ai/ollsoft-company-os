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
version; the installer detects it. Confirm the cluster is up and uses peer auth:

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

The generated password is written to `/root/ollsoft-company-os-admin.txt` — delete that
file after your first login.

### 4. The repo skeleton

```
/srv/kb              2775 root:kb-users, sticky   the knowledgebase (a git repo)
├── .git             0700 root                     history — root-only, always
├── .gitignore       0644 root:kb-users            only .md/.html enter history
├── .claude/         0755 root:kb-users            agent context, skills, egress rules
├── company/         2775 root:kb-users + default ACL   everyone reads and writes
├── projects/        2775 root:kb-users            restricted folders go here
└── users/<name>/    0700 <name>:<name>            private per person
```

Two details that matter more than they look:

- **setgid (`2775`) plus a default ACL** on `company/` means new files are
  group-writable no matter what umask the writer had. Without it, `vim`, the web
  app and an agent would each produce different permissions for the same folder.
- **`.git` is mode 0700, root-only.** Its objects contain every committed version
  of every file. Group access there would let anyone read the history of content
  they cannot read on disk — bypassing both file permissions and RLS. `syncd`
  re-asserts this on start; the installer re-asserts it on every run.

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
`kb-syncd`, `kb-hub`, `kb-indexer` and `kb-convert` (the last one only when its
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

---

## Upgrading

Pull and re-run — `install.sh` is idempotent:

```bash
git pull
sudo bash scripts/install.sh --admin <your-username>
```

Existing accounts, repo content, and `.claude/` config files are left alone.
Agent skills *are* refreshed, since they document the platform.

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
`kb-indexer` is down, or the `vector` extension is missing. The index is
disposable — `sudo systemctl restart kb-indexer` rebuilds it from the markdown.

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

Do **not** point a network at `:8300`. There is no TLS and no login rate limiting.
Read [remote-access.md](remote-access.md) and [SECURITY.md](SECURITY.md) first,
and rotate any passwords generated during install.
