#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Ollsoft Company OS installer.
#
# Stands the platform up on a fresh Ubuntu 24.04 host: system packages, the
# service account, the repo skeleton with kernel-enforced permissions, the
# Postgres cluster objects, the Python venv, the frontend bundle, and the
# systemd units. Creates ONE admin account — yours. No demo content; run
# scripts/seed-demo.sh separately if you want the sample company.
#
# Idempotent: safe to re-run to upgrade an existing install.
#
# Usage:
#   sudo bash scripts/install.sh --admin <username> [options]
#
# Options:
#   --admin <user>      Admin account to create (or adopt, if it exists).
#   --admin-pass <pw>   Password for it. Default: generated and printed once.
#   --repo <path>       Knowledgebase location.        Default /srv/kb
#   --prefix <path>     Where code is deployed.        Default /opt/kb-platform
#   --port <n>          Hub listen port on 127.0.0.1.  Default 8300
#   --admin-group <g>   OS group granting admin rights. Default sudo
#   --no-packages       Skip apt-get (deps already installed).
#   --no-start          Install but don't enable/start the services.
# ---------------------------------------------------------------------------
set -euo pipefail

ADMIN_USER=""; ADMIN_PASS=""; REPO=/srv/kb; PREFIX=/opt/kb-platform
PORT=8300; ADMIN_GROUP=sudo; DO_PACKAGES=1; DO_START=1
VENV="${PREFIX%/*}/kb-venv"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Re-running an existing install must not silently move the knowledgebase or
# change the port back to defaults. Adopt whatever the last install chose;
# explicit flags below still win.
if [ -f /etc/kb/kb.env ]; then
  # shellcheck disable=SC1091
  . /etc/kb/kb.env
  REPO="${KB_REPO:-$REPO}"
  PORT="${KB_HUB_PORT:-$PORT}"
  PREFIX="${KB_PLATFORM_ROOT:-$PREFIX}"
  ADMIN_GROUP="${KB_ADMIN_GROUP:-$ADMIN_GROUP}"
  VENV="$(dirname "$(dirname "${KB_VENV_PY:-$VENV/bin/python}")")"
  PRIOR_PROTECTED="${KB_PROTECTED_USERS:-}"
  # Monitoring config is operator-chosen, not derivable — carry it across a
  # re-install. Regenerating kb.env used to silently blank KB_NTFY_TOPIC, which
  # turns alerting off in the least visible way possible.
  PRIOR_NTFY_TOPIC="${KB_NTFY_TOPIC:-}"
  PRIOR_ALERT_PUSH="${KB_ALERT_PUSH:-}"
  PRIOR_EMBED_PROVIDER="${KB_EMBED_PROVIDER:-}"
  PRIOR_RERANK_PROVIDER="${KB_RERANK_PROVIDER:-}"
  PRIOR_ENV="$(cat /etc/kb/kb.env)"
fi
PRIOR_ENV="${PRIOR_ENV:-}"
PRIOR_PROTECTED="${PRIOR_PROTECTED:-}"
PRIOR_NTFY_TOPIC="${PRIOR_NTFY_TOPIC:-}"
PRIOR_ALERT_PUSH="${PRIOR_ALERT_PUSH:-0}"

while [ $# -gt 0 ]; do
  case "$1" in
    --admin)        ADMIN_USER="${2:?}"; shift 2 ;;
    --admin-pass)   ADMIN_PASS="${2:?}"; shift 2 ;;
    --repo)         REPO="${2:?}"; shift 2 ;;
    --prefix)       PREFIX="${2:?}"; VENV="${PREFIX%/*}/kb-venv"; shift 2 ;;
    --port)         PORT="${2:?}"; shift 2 ;;
    --admin-group)  ADMIN_GROUP="${2:?}"; shift 2 ;;
    --no-packages)  DO_PACKAGES=0; shift ;;
    --no-start)     DO_START=0; shift ;;
    -h|--help)      sed -n '2,/^# ---/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash scripts/install.sh ...)"
[ -n "$ADMIN_USER" ] || die "--admin <username> is required"
[[ "$ADMIN_USER" =~ ^[a-z][a-z0-9_]{1,30}$ ]] || die "invalid admin username '$ADMIN_USER'"
case "$ADMIN_USER" in
  root|postgres|kbindexer|nobody|daemon|bin|sys)
    die "'$ADMIN_USER' is a system account — pick a human username" ;;
esac
if id "$ADMIN_USER" &>/dev/null && [ "$(id -u "$ADMIN_USER")" -lt 1000 ]; then
  die "'$ADMIN_USER' is a system account (uid < 1000) — pick a human username"
fi

# ---------------------------------------------------------------------------
say "preflight"
# ---------------------------------------------------------------------------
. /etc/os-release 2>/dev/null || true
[ "${ID:-}" = "ubuntu" ] || echo "  WARNING: tested on Ubuntu 24.04; found '${PRETTY_NAME:-unknown}'"
[ -d /run/systemd/system ] || die "systemd is not running. This platform needs real systemd, PAM,
       and per-user processes — it cannot run in an unprivileged container.
       Use a VM or bare metal."
command -v runuser >/dev/null || die "runuser not found (package: util-linux)"

# ---------------------------------------------------------------------------
if [ "$DO_PACKAGES" -eq 1 ]; then
say "system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  postgresql postgresql-contrib \
  python3-pip python3-venv python3-dev \
  acl inotify-tools build-essential libpam0g-dev \
  nodejs npm git curl ca-certificates rsync
# pgvector package name tracks the server major version
PGMAJ="$(psql --version | grep -oE '[0-9]+' | head -1)"
apt-get install -y -qq "postgresql-${PGMAJ}-pgvector" \
  || die "no pgvector package for PostgreSQL ${PGMAJ}. Install it manually, then re-run with --no-packages."
fi

command -v psql >/dev/null || die "postgres client not found"
systemctl is-active --quiet postgresql || systemctl start postgresql

# ---------------------------------------------------------------------------
say "groups and service account"
# ---------------------------------------------------------------------------
groupadd -f kb-users
if ! id kbindexer &>/dev/null; then
  useradd -r -m -d /var/lib/kbindexer -s /usr/sbin/nologin kbindexer
fi
usermod -aG kb-users kbindexer
getent group "$ADMIN_GROUP" >/dev/null || die "admin group '$ADMIN_GROUP' does not exist"

# ---------------------------------------------------------------------------
say "admin account: $ADMIN_USER"
# ---------------------------------------------------------------------------
CREDS=/root/ollsoft-company-os-admin.txt
if id "$ADMIN_USER" &>/dev/null; then
  echo "  account exists — adopting it (password unchanged)"
else
  useradd -m -s /bin/bash "$ADMIN_USER"
  [ -n "$ADMIN_PASS" ] || { ADMIN_PASS="$(openssl rand -base64 12)"; }
  echo "$ADMIN_USER:$ADMIN_PASS" | chpasswd
  ( umask 077; printf '%s %s\n' "$ADMIN_USER" "$ADMIN_PASS" > "$CREDS" ); chmod 600 "$CREDS"
  echo "  created; password written to $CREDS"
fi
usermod -aG kb-users,"$ADMIN_GROUP" "$ADMIN_USER"

# ---------------------------------------------------------------------------
say "knowledgebase repo: $REPO"
# ---------------------------------------------------------------------------
mkdir -p "$REPO"
if ! git -C "$REPO" rev-parse --git-dir &>/dev/null; then
  git -C "$REPO" init -q
  git -C "$REPO" config user.name  kb-syncd
  git -C "$REPO" config user.email kb-syncd@localhost
fi
# SECURITY: .git is root-only, always — its objects hold every committed version
# of every file, so group access would bypass file permissions (and Postgres RLS)
# for all private content ever committed. Re-asserted here on every run because
# an earlier install may have left it group-readable.
git -C "$REPO" config --unset core.sharedRepository 2>/dev/null || true
chmod 700 "$REPO/.git"

if [ ! -f "$REPO/.gitignore" ]; then
  cat > "$REPO/.gitignore" <<'IGN'
# History covers what people write: documents (.md), artifacts (.html), and
# the platform's auditable config (.os/*.json). Everything else — uploads,
# binaries, machinery — stays out of version history.
*
!*/
!*.md
!*.html
!.gitignore
!.os/*.json
# secrets NEVER enter history (belt; the wall is in root-owned syncd code)
**/_secrets/**
# kb-convert's derived sidecars (.name.docx.md …) are regenerable machinery
.*.md
IGN
  chown root:kb-users "$REPO/.gitignore"; chmod 644 "$REPO/.gitignore"
fi
# An install from before 2026-09 un-ignored .claude/*.json instead; the config
# now lives in .os/ (the hub moves it on its next start), so make sure git
# keeps snapshotting it from there. Idempotent.
grep -qxF '!.os/*.json' "$REPO/.gitignore" || \
  printf '\n# platform config (launchers, egress, settings) lives in .os/\n!.os/*.json\n' >> "$REPO/.gitignore"

mkdir -p "$REPO/company" "$REPO/projects" "$REPO/users"
# Shared containers: setgid so children inherit the group.
chgrp kb-users "$REPO"          ; chmod 2775 "$REPO"
chgrp kb-users "$REPO/projects" ; chmod 2775 "$REPO/projects"
chgrp kb-users "$REPO/users"    ; chmod 2775 "$REPO/users"
# company/: everyone in kb-users reads+writes. The default ACL makes new files
# group-writable regardless of the writer's umask, so vim, agents and the web
# app all converge on the same permissions.
chgrp kb-users "$REPO/company"  ; chmod 2775 "$REPO/company"
setfacl -k "$REPO/company" 2>/dev/null || true
setfacl -d -m u::rwx,g::rwx,o::rx "$REPO/company"
# Sticky bit: group members create freely but may only rename/delete what they
# OWN. Needed on the containers that hold OTHER PEOPLE'S DIRECTORIES, because
# without it any member could rename another user's home aside and put their own
# directory in its place — users/<admin>/.claude/skills/ is loaded by that
# admin's agent, so that was a path from "ordinary KB account" to "code runs as
# the admin". The same primitive hijacked any projects/<name>.
#
# NOT on company/. That directory holds shared DOCUMENTS at its top level, and
# with fs.protected_regular=2 (default since Linux 4.19) a sticky, group-writable
# directory also blocks O_CREAT opens of files you do not own — which is what
# `open(path,"w")` and a shell `>` both issue. Setting +t there silently made
# every top-level company document read-only to everyone except its author,
# while access(2) still reported it writable. company/ is a deliberate
# free-for-all; protect the subdirectories that are not, like this:
chmod +t "$REPO" "$REPO/users" "$REPO/projects"

install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "$REPO/users/$ADMIN_USER"

# --- agent context, platform config, skills --------------------------------
# .claude/ is Claude Code's discovery path and holds ONLY agent context:
# CLAUDE.md (below) and skills/ (further down). Root-owned, world-readable.
install -d -m 0755 -o root -g kb-users "$REPO/.claude"
# never clobber a live system's edits
[ -e "$REPO/.claude/CLAUDE.md" ] || \
  install -m 0644 -o root -g kb-users "$SRC/defaults/CLAUDE.md" "$REPO/.claude/CLAUDE.md"

# .os/ is the platform's own config dir (launcher buttons, the artifact egress
# allowlist, settings): everyone reads it, root writes it. The hub reaches it
# through a dir_fd, so it must exist before the platform starts. The repo root
# is group-writable, so a member could have pre-planted an entry of that name:
# refuse anything that is not a root-owned directory rather than chown through
# it (the sticky bit stops a member swapping it out afterwards).
if [ -L "$REPO/.os" ]; then
  echo "refusing: $REPO/.os is a symlink — remove it and re-run" >&2; exit 1
fi
[ -d "$REPO/.os" ] || mkdir -m 0755 "$REPO/.os"
if [ "$(stat -c %u "$REPO/.os")" != 0 ]; then
  echo "refusing: $REPO/.os is not root-owned — remove it and re-run" >&2; exit 1
fi
chown root:kb-users "$REPO/.os"; chmod 2755 "$REPO/.os"
for f in egress.json launchers.json; do
  # never clobber a live system's edits — and never shadow a pre-2026-09 copy
  # still in .claude/, which the hub moves into .os/ on its next start (a
  # default installed now would silently replace the operator's list)
  [ -e "$REPO/.os/$f" ] || [ -e "$REPO/.claude/$f" ] || \
    install -m 0644 -o root -g kb-users "$SRC/defaults/$f" "$REPO/.os/$f"
done
# Codex discovers AGENTS.md from the working directory upward. Keep one source
# of truth by pointing it at the same governed context Claude Code reads.
if [[ ! -e "$REPO/AGENTS.md" && ! -L "$REPO/AGENTS.md" ]]; then
  ln -s .claude/CLAUDE.md "$REPO/AGENTS.md"
  chown -h root:kb-users "$REPO/AGENTS.md"
fi
# The To-dos aggregator is a platform default, not demo content: the docs
# present it as a shipped feature, so install it if the operator has not
# replaced it with their own.
if [ ! -e "$REPO/company/todos.html" ]; then
  install -m 0664 -o root -g kb-users "$SRC/defaults/artifacts/todos.html" \
          "$REPO/company/todos.html"
fi

# Skills DO get refreshed every run — they document the platform and must track
# the code, not drift from it.
install -d -m 0755 -o root -g kb-users "$REPO/.claude/skills"
for d in "$SRC"/company-skills/*/; do
  name="$(basename "$d")"
  install -d -m 0755 -o root -g kb-users "$REPO/.claude/skills/$name"
  install -m 0644 -o root -g kb-users "$d/SKILL.md" "$REPO/.claude/skills/$name/SKILL.md"
done

# ---------------------------------------------------------------------------
say "python venv"
# ---------------------------------------------------------------------------
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade -q pip
"$VENV/bin/pip" install -q -r "$SRC/requirements.txt"

# kb-convert gets its own venv: heavy document parsers, zero overlap with the
# platform's dependency set (see requirements-convert.txt).
CVENV="${VENV%/*}/kb-convert-venv"
if [ -f "$SRC/requirements-convert.txt" ]; then
  [ -x "$CVENV/bin/python" ] || python3 -m venv "$CVENV"
  "$CVENV/bin/python" -m pip install --upgrade -q pip
  "$CVENV/bin/pip" install -q -r "$SRC/requirements-convert.txt"
fi

# ---------------------------------------------------------------------------
say "frontend bundle"
# ---------------------------------------------------------------------------
if command -v npm >/dev/null && [ -d "$SRC/frontend/src" ]; then
  # Always rebuild when we can: on an upgrade the checked-in bundle (if any) is
  # by definition older than the source that was just pulled.
  ( cd "$SRC/frontend" && npm install --silent && node build.mjs )
elif [ -n "$(ls -A "$SRC/frontend/static" 2>/dev/null)" ]; then
  echo "  npm unavailable — using the pre-built bundle in frontend/static"
else
  die "npm not found and no pre-built bundle present; install nodejs/npm and re-run"
fi
[ -f "$SRC/frontend/static/app.html" ] || die "frontend build produced no app.html — check frontend/assets/"

# ---------------------------------------------------------------------------
say "deploy code to $PREFIX"
# ---------------------------------------------------------------------------
mkdir -p "$PREFIX/frontend"
rsync -a --delete "$SRC/kb_platform" "$PREFIX/"
rsync -a --delete "$SRC/scripts"     "$PREFIX/"
rsync -a --delete "$SRC/frontend/static" "$PREFIX/frontend/"
cp "$SRC/requirements.txt" "$PREFIX/" 2>/dev/null || true
# World-readable + executable: per-user backends run this code as their own uid.
chown -R root:root "$PREFIX" "$VENV"
chmod -R a+rX "$PREFIX" "$VENV"
if [ -d "$CVENV" ]; then
  chown -R root:root "$CVENV"
  chmod -R a+rX "$CVENV"
fi

# ---------------------------------------------------------------------------
say "postgres"
# ---------------------------------------------------------------------------
runuser -u postgres -- psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  -- Group role mirroring the kb-users OS group. Every human joins it, so a
  -- table shared with "the whole company" needs ONE grant, not one per person
  -- (and new hires inherit it). The hub adds new accounts on creation.
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='kb_users')  THEN CREATE ROLE kb_users NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='kbindexer') THEN CREATE ROLE kbindexer LOGIN;  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$ADMIN_USER') THEN CREATE ROLE "$ADMIN_USER" LOGIN; END IF;
  -- kbindexer is deliberately NOT a member: it indexes markdown and has no
  -- business reading users' application data.
  GRANT kb_users TO "$ADMIN_USER";
END \$\$;
SQL
runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='kb'" | grep -q 1 \
  || runuser -u postgres -- createdb -O kbindexer kb
runuser -u postgres -- psql -d kb -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;"
# Postgres ships with a 128 MB buffer cache; every search reads all of
# kb.blocks (RLS: no index is reachable, see schema.sql) plus the vectors, and
# on a real knowledgebase that is more than 128 MB — each search then evicts
# the last one's pages. A quarter of RAM, at most 2 GB, keeps it resident.
PGCONF_DIR="$(dirname "$(runuser -u postgres -- psql -tAc 'SHOW config_file')")/conf.d"
if [ -d "$PGCONF_DIR" ]; then
  mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  sb_mb=$(( mem_mb / 4 )); [ "$sb_mb" -gt 2048 ] && sb_mb=2048; [ "$sb_mb" -lt 128 ] && sb_mb=128
  want="# Written by Ollsoft Company OS scripts/install.sh
shared_buffers = ${sb_mb}MB"
  if [ "$(cat "$PGCONF_DIR/kb.conf" 2>/dev/null)" != "$want" ]; then
    printf '%s\n' "$want" > "$PGCONF_DIR/kb.conf"
    chmod 644 "$PGCONF_DIR/kb.conf"
    systemctl restart postgresql     # shared_buffers only changes on a restart
  fi
fi
runuser -u kbindexer -- psql -d kb -v ON_ERROR_STOP=1 < "$SRC/scripts/schema.sql"
# Personal schema + search_path for the admin (the hub does this for later users).
runuser -u postgres -- psql -d kb -v ON_ERROR_STOP=1 <<SQL
CREATE SCHEMA IF NOT EXISTS "u_$ADMIN_USER" AUTHORIZATION "$ADMIN_USER";
ALTER ROLE "$ADMIN_USER" SET search_path = "u_$ADMIN_USER", kb, public;
SQL

# ---------------------------------------------------------------------------
say "kb-history and kb-search CLIs"
# ---------------------------------------------------------------------------
# The documented way for a user to read version history: the socket checks the
# caller's identity via SO_PEERCRED, so this needs no privileges of its own.
cat > /usr/local/bin/kb-history <<WRAP
#!/bin/sh
# Ollsoft Company OS version-history CLI. Installed by scripts/install.sh.
PYTHONPATH="$PREFIX" exec "$VENV/bin/python" -m kb_platform.vc_cli "\$@"
WRAP
chmod 0755 /usr/local/bin/kb-history
# kb-search: hybrid search as the caller (docs/semantic-search.md).
cat > /usr/local/bin/kb-search <<WRAP
#!/bin/sh
# Ollsoft Company OS search CLI. Installed by scripts/install.sh.
PYTHONPATH="$PREFIX" exec "$VENV/bin/python" -m kb_platform.search_cli "\$@"
WRAP
chmod 0755 /usr/local/bin/kb-search

# ---------------------------------------------------------------------------
say "runtime config and directories"
# ---------------------------------------------------------------------------
install -d -m 0755 /run/kb /run/kb/users /etc/kb
if [ ! -f /etc/kb/session.key ]; then
  head -c 48 /dev/urandom | base64 | tr -d '\n' > /etc/kb/session.key
  chmod 600 /etc/kb/session.key
fi
# Preserve any protected users a previous install (or the operator) added.
PROTECTED_LIST="$ADMIN_USER"
for u in ${PRIOR_PROTECTED//,/ }; do
  case ",$PROTECTED_LIST," in *",$u,"*) ;; *) PROTECTED_LIST="$PROTECTED_LIST,$u" ;; esac
done

cat > /etc/kb/kb.env <<ENV
# Ollsoft Company OS runtime configuration. Read by the systemd units.
# Changing anything here requires: systemctl restart kb-hub kb-syncd kb-indexer kb-embedd kb-convert
KB_REPO=$REPO
KB_RUN=/run/kb
KB_ETC=/etc/kb
KB_HUB_PORT=$PORT
KB_PG_DB=kb
KB_VENV_PY=$VENV/bin/python
KB_PLATFORM_ROOT=$PREFIX
PYTHONPATH=$PREFIX
# OS group whose members are platform admins (sudo on Debian/Ubuntu, wheel on RHEL).
KB_ADMIN_GROUP=$ADMIN_GROUP
# Accounts the admin UI refuses to modify or delete, comma-separated. The
# founding admin is listed so a second admin cannot lock them out.
KB_PROTECTED_USERS=$PROTECTED_LIST
# --- monitoring -------------------------------------------------------------
# Alerts are ALWAYS appended to /var/log/kb/alerts.log. These two keys only
# control whether an alert is *also* pushed to a phone, which is off by
# default: kb-maintenance.service triages the log on a timer and is the thing
# that decides something is worth interrupting a human for.
#
# ntfy topic for pushes (kb-heartbeat + OnFailure hooks). Empty = no pushes are
# possible at all. Topics are public — pick an unguessable name and subscribe to
# it in the ntfy app.
KB_NTFY_TOPIC=$PRIOR_NTFY_TOPIC
# 1 = also push every alert to ntfy as it happens. Leave at 0 unless you want
# per-occurrence notifications: a flapping service will send one per flap.
KB_ALERT_PUSH=$PRIOR_ALERT_PUSH
# Seconds an identical alert title stays muted for pushes (log is unaffected).
KB_ALERT_DEDUP=21600
# --- semantic search (docs/semantic-search.md) --------------------------------
# Provider wiring; the keys are separate files, /etc/kb/embed.key and
# /etc/kb/rerank.key, written by scripts/install-search-keys.sh. none = full-text
# search only, nothing is ever sent anywhere.
KB_EMBED_PROVIDER=${PRIOR_EMBED_PROVIDER:-none}
KB_RERANK_PROVIDER=${PRIOR_RERANK_PROVIDER:-none}
ENV
# Everything else an operator (or a setup script) added to the previous kb.env
# — KB_SHARE_BASE, KB_EMBED_URL, KB_STT_MODEL, ... — is carried over verbatim.
# Regenerating this file used to drop such lines without a word.
if [ -n "$PRIOR_ENV" ]; then
  carried=""
  while IFS= read -r line; do
    key="${line%%=*}"
    grep -q "^$key=" /etc/kb/kb.env || carried="$carried$line"$'\n'
  done < <(printf '%s\n' "$PRIOR_ENV" | grep -E '^KB_[A-Z0-9_]+=' || true)
  if [ -n "$carried" ]; then
    printf '# --- carried over from the previous kb.env -------------------------------\n%s' \
      "$carried" >> /etc/kb/kb.env
  fi
fi
chmod 644 /etc/kb/kb.env

# ---------------------------------------------------------------------------
say "systemd units"
# ---------------------------------------------------------------------------
cp "$SRC/systemd/kb.conf" /etc/tmpfiles.d/kb.conf
cp "$SRC/systemd/kb-logrotate.conf" /etc/logrotate.d/kb
install -d -m 750 -o root -g root /var/log/kb
for u in "$SRC"/systemd/kb-*.service; do
  # Point ExecStart/venv at the chosen prefix.
  # Tokenise all defaults BEFORE expanding any, or a --prefix that contains
  # another default gets substituted twice (e.g. /opt/kb-platform/kb-venv).
  # The convert venv goes first: longest path, must not be chewed by the others.
  sed -e "s|/opt/kb-convert-venv|@@CVENV@@|g" \
      -e "s|/opt/kb-venv|@@VENV@@|g"   -e "s|/opt/kb-platform|@@PREFIX@@|g" \
      -e "s|@@CVENV@@|$CVENV|g" \
      -e "s|@@VENV@@|$VENV|g"          -e "s|@@PREFIX@@|$PREFIX|g" \
      "$u" > "/etc/systemd/system/$(basename "$u")"
done
for u in "$SRC"/systemd/kb-*.timer; do
  [ -e "$u" ] || continue
  cp "$u" "/etc/systemd/system/$(basename "$u")"
done
systemd-tmpfiles --create /etc/tmpfiles.d/kb.conf
systemctl daemon-reload

FAILED=0
# kb-embedd always runs: without keys it only reports `unconfigured` and sends nothing.
UNITS="kb-syncd kb-hub kb-indexer kb-embedd"
[ -x "$CVENV/bin/python" ] && UNITS="$UNITS kb-convert"
if [ "$DO_START" -eq 1 ]; then
  # shellcheck disable=SC2086  # UNITS is a deliberate word list
  systemctl enable $UNITS >/dev/null 2>&1
  systemctl enable --now kb-heartbeat.timer >/dev/null 2>&1 || true
  # restart, not just start: on an upgrade the units are already running and
  # would otherwise keep executing the previous code and environment.
  # shellcheck disable=SC2086
  systemctl restart $UNITS
  sleep 3
  for unit in $UNITS; do
    st="$(systemctl is-active "$unit")"
    printf '  %-12s %s\n' "$unit" "$st"
    [ "$st" = "active" ] || FAILED=1
  done
  if [ "$FAILED" -ne 0 ]; then
    echo
    echo "ERROR: a service failed to start. Diagnose with:" >&2
    echo "  journalctl -u kb-hub -u kb-syncd -u kb-indexer -u kb-embedd -u kb-convert -n 50 --no-pager" >&2
    exit 1
  fi
else
  echo "  installed but not started (--no-start)"
fi

# ---------------------------------------------------------------------------
say "done"
# ---------------------------------------------------------------------------
cat <<DONE
Ollsoft Company OS is installed.

  Web UI      http://127.0.0.1:$PORT   (localhost only — see docs/remote-access.md
                                        before exposing it to a network)
  Log in as   $ADMIN_USER
$( [ -f "$CREDS" ] && echo "  Password    in $CREDS (delete it once you've logged in)" )
  Repo        $REPO
  Config      /etc/kb/kb.env
  Logs        journalctl -u kb-hub -u kb-syncd -u kb-indexer -u kb-embedd -u kb-convert -f

Optional: populate a sample company to explore the permission model —
  sudo bash scripts/seed-demo.sh
Optional: semantic search (vectors + reranking, your own provider keys) —
  sudo bash scripts/install-search-keys.sh --help      (docs/semantic-search.md)
DONE
