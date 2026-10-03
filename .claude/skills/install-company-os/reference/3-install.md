# Phase 3 — Install Company OS

## 1. Get the code onto the server

As `$ADMIN`, into `~/ollsoft-company-os`, from the same place the human's local clone came from (`git -C <local clone> remote get-url origin`; default `https://github.com/Ollsoft-ai/ollsoft-company-os.git`):

```bash
ssh companyos 'git clone <origin-url> ~/ollsoft-company-os'
```

**If it asks for credentials, the repo is private for this human.** Then:

- **They cloned it locally (that's how you got this skill):** ship a bundle.
  ```bash
  git -C <local clone> bundle create /tmp/companyos.bundle HEAD
  scp /tmp/companyos.bundle companyos:
  ssh companyos 'git clone ~/companyos.bundle ~/ollsoft-company-os && rm ~/companyos.bundle'
  ssh companyos git -C ollsoft-company-os remote set-url origin <origin-url>
  ```
  The `set-url` matters: a clone of a bundle points at the bundle, which is deleted, so the server could never pull an update.
- **Upgrades will need `git pull`**, so also offer a read-only deploy key: `ssh-keygen -t ed25519 -N "" -f ~/.ssh/companyos_deploy` on the server, an `~/.ssh/config` `Host github.com` entry using it, and the human (or whoever gave them access) adds the `.pub` under the repo's Settings → Deploy keys, read-only.

## 2. The admin password = their web login

Company OS signs people in with their **Linux password** (PAM). Have the human run this in their own terminal — the password never passes through you:

```bash
ssh -t companyos sudo passwd <admin>
```

- **At least 12 characters**; a password manager entry named after the domain.
- It does **not** enable SSH passwords — phase 2 turned those off for good.

## 3. Run the installer (5–15 min)

```bash
ssh companyos 'cd ~/ollsoft-company-os && sudo cos-run install bash scripts/install.sh --admin <admin>'
ssh companyos 'sudo cos-run install'        # poll until "exit 0"
```

- It asks nothing; it is idempotent — on failure, fix the cause and rerun the same line.
- It installs Postgres + pgvector, builds the frontend, lays out `/srv/kb`, writes `/etc/kb/kb.env` (0640), then runs `deploy.sh`: code, CLIs, skills, schema, services and the heartbeat, maintenance and git-gc timers. The daily maintenance runs as `<admin>`.
- `--port` if 8300 is taken; nothing else needs a flag.

## 4. Verify

```bash
ssh companyos 'for s in kb-syncd kb-hub kb-indexer kb-embedd kb-convert; do printf "%s " $s; systemctl is-active $s; done
  curl -s -o /dev/null -w "hub %{http_code}\n" http://127.0.0.1:8300/
  sudo passwd -S <admin> | cut -d" " -f2'
```

All `active`, hub `302` or `200`, password status `P`. Then the human signs in for real:

1. You open a tunnel in the background: `ssh -N -L 8300:127.0.0.1:8300 companyos` (use `18300:` if 8300 is busy locally).
2. They open **http://localhost:8300** and sign in as `<admin>`.
3. Ask: **"Can you see the app and your empty knowledgebase?"** Do not continue until yes.

## 5. Admin session for the later phases

Phases 5–9 call admin endpoints (settings, users, groups, agents). Have the human create a session **on the server** — the password goes from their keyboard to the server, never through you:

```
ssh -t companyos cos-login
```

- It prints `signed in`. You then call `ssh companyos 'curl -s -b ~/.cos-admin.jar …'`. Valid 12 h; have them run it again if a call returns `403 admin only`.
- `GET /admin/me` → `{"admin": true}` proves it.
- **Deleted at handover.**

## 6. Host audit trail — always, no question

```bash
ssh companyos 'cd ~/ollsoft-company-os && sudo bash scripts/install-audit.sh --reader $USER'
```

- auditd records, for **every person** — web terminal, agents and SSH alike, and anyone created later: refused file opens, opening someone else's private folder, direct writes to platform config, sudo/account/permission tools, changes to identity, sudoers, SSH and audit config.
- **No usernames in the rules**: admins are filtered when the digest reads the log, from the admin group as it is that day.
- `--reader` lets the admin run `sudo kb-audit-digest` unattended — what a scheduled security brief (phase 8) needs.
- Sensitive folders (HR, finance) get added in phase 7 with `--watch`.
- Verify: it prints `N rules loaded` and `digest answers`.
- Tell the human once: this, plus the web app's own audit log and the knowledgebase's git history (`kb-history`), is what daily reports and security audits are built from.
