#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Optional demo content for Ollsoft Company OS.
#
# Creates three sample employees, a restricted project, and some seed documents,
# so you can log in as different people and watch the permission model work
# without inventing a company first. Run AFTER scripts/install.sh.
#
# This is also what the test suite expects: tests/cli and tests/e2e assume the
# users `alice`, `bob` and `carol` exist.
#
#   sudo bash scripts/seed-demo.sh [--repo /srv/kb]
#
# To remove it again:
#   sudo bash scripts/seed-demo.sh --undo
# ---------------------------------------------------------------------------
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO=/srv/kb
UNDO=0
DEMO_USERS=(alice bob carol)
PROJECT=acme                     # restricted project; alice+bob only

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:?}"; shift 2 ;;
    --undo) UNDO=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
if [ "$UNDO" -eq 1 ]; then
  say "removing demo content"
  for u in "${DEMO_USERS[@]}"; do
    id "$u" &>/dev/null || continue
    pkill -KILL -u "$u" 2>/dev/null || true
    runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$u'" | grep -q 1 && {
      runuser -u postgres -- psql -qc "DROP OWNED BY \"$u\" CASCADE;" >/dev/null
      runuser -u postgres -- psql -qc "DROP ROLE IF EXISTS \"$u\";" >/dev/null
    }
    userdel -r "$u" 2>/dev/null || true
    echo "  removed $u"
  done
  rm -rf "${REPO:?}/projects/$PROJECT"
  groupdel "proj-$PROJECT" 2>/dev/null || true
  echo "  removed projects/$PROJECT"
  for f in company/overview.md company/onboarding.md \
           company/dashboards/randoms.html company/dashboards/iotest.html \
           company/dashboards/scopetest.html company/dashboards/xsstest.html; do
    rm -f "${REPO:?}/$f" && echo "  removed $f"
  done
  rmdir "${REPO:?}/company/dashboards" 2>/dev/null || true
  rm -f /tmp/kb-test-creds.json /root/ollsoft-company-os-demo.txt
  systemctl is-active --quiet kb-indexer && systemctl restart kb-indexer
  echo "  demo removed. Documents you created yourself under company/ were left alone."
  exit 0
fi

[ -d "$REPO" ] || { echo "repo $REPO not found — run scripts/install.sh first" >&2; exit 1; }

# ---------------------------------------------------------------------------
say "demo users"
# ---------------------------------------------------------------------------
CREDS=/root/ollsoft-company-os-demo.txt
# The test suite reads its logins from here; every tests/cli module and the e2e
# conftest load it at import time, so without it pytest fails during collection.
TEST_CREDS=/tmp/kb-test-creds.json
umask 077
: > "$CREDS"
groupadd -f "proj-$PROJECT"
usermod -aG "proj-$PROJECT" kbindexer

# Platform admins are members of the configured admin group. The suite drives
# /admin/* as alice, so she must be one — read the group the installer chose.
ADMIN_GROUP=sudo
[ -f /etc/kb/kb.env ] && ADMIN_GROUP="$(. /etc/kb/kb.env; echo "${KB_ADMIN_GROUP:-sudo}")"

declare -A PW

add_user() {
  local u=$1; shift
  local pw
  if ! id "$u" &>/dev/null; then
    useradd -m -s /bin/bash "$u"
  fi
  # Always (re)set the password: on a re-run we must still know it, because the
  # test credentials file has to contain a password that actually works.
  pw=$(openssl rand -base64 12 | tr -d '/+=' )
  echo "$u:$pw" | chpasswd
  PW["$u"]="$pw"
  printf '%s %s\n' "$u" "$pw" >> "$CREDS"
  for g in "$@"; do usermod -aG "$g" "$u"; done
  install -d -m 700 -o "$u" -g "$u" "$REPO/users/$u"
  runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$u') THEN CREATE ROLE "$u" LOGIN; END IF;
END \$\$;
GRANT kb_users TO "$u";
CREATE SCHEMA IF NOT EXISTS "u_$u" AUTHORIZATION "$u";
ALTER ROLE "$u" SET search_path = "u_$u", kb, public;
SQL
  echo "  $u"
}
add_user alice kb-users "proj-$PROJECT" "$ADMIN_GROUP"   # alice is the demo admin
add_user bob   kb-users "proj-$PROJECT"
add_user carol kb-users                    # deliberately NOT on the project

chmod 600 "$CREDS"
printf '{"alice":"%s","bob":"%s","carol":"%s"}\n' \
  "${PW[alice]}" "${PW[bob]}" "${PW[carol]}" > "$TEST_CREDS"
chmod 644 "$TEST_CREDS"   # the suite runs as an unprivileged developer account
echo "  test credentials -> $TEST_CREDS"

# ---------------------------------------------------------------------------
say "restricted project: projects/$PROJECT"
# ---------------------------------------------------------------------------
# 2770 + setgid: only proj-<name> members can even list it. This is the folder
# that demonstrates the whole model — carol cannot see it in the file tree, and
# a raw SQL query as carol returns none of its rows either.
mkdir -p "$REPO/projects/$PROJECT"
chgrp "proj-$PROJECT" "$REPO/projects/$PROJECT"
chmod 2770 "$REPO/projects/$PROJECT"
setfacl -d -m u::rwx,g::rwx,o::- "$REPO/projects/$PROJECT"

# ---------------------------------------------------------------------------
say "seed documents"
# ---------------------------------------------------------------------------
seed() {  # seed <owner> <relpath>  <<<content   (only if missing)
  local owner=$1 rel=$2 full="$REPO/$2"
  [ -e "$full" ] && { echo "  skip $rel (exists)"; cat >/dev/null; return 0; }
  cat > "$full"
  chown "$owner" "$full"
  echo "  $rel"
}


# The distinctive nouns below (zebrafish, walrus, aardvark, pangolin) are not
# whimsy — the test suite greps for them to prove that a document is visible to
# one user and genuinely invisible to another. Changing them breaks tests/cli.

seed alice company/overview.md <<'MD'
# Company Overview

Welcome to the knowledgebase. Everything in `company/` is readable and writable
by every employee — the folder is group-owned by `kb-users`, so the kernel is
what enforces that, not application code.

## This quarter
- [ ] Ship the knowledgebase @alice #product
- [ ] Write the onboarding guide @bob #docs
- [x] Set up the sandbox VM

Try it: check a box above, then open the To-dos panel. Tasks are parsed out of
every file you can read and aggregated there.
MD

seed bob company/onboarding.md <<'MD'
# Onboarding

Things to do in your first week:

- [ ] Read the security policy @carol #onboarding
- [ ] Set up your development environment
- [ ] Say hello in the team channel

## How this knowledgebase works

Files are markdown on disk. Your login is a real Linux account, and every page
you load is served by a process running as *you* — so what you can see here is
exactly what you could `cat` in the terminal, no more.
MD

seed alice "projects/$PROJECT/plan.md" <<'MD'
# Project Acme (confidential)

This file lives in a `2770` directory owned by the `proj-acme` group. Alice and
Bob are members; Carol is not. Codename zebrafish.

Log in as carol and you will not find this file — not in the tree, not in
search, and not via `SELECT * FROM kb.blocks` either. The Postgres row-level
security policy re-checks the same Unix permission for every row.

## Milestones
- [ ] Finalize the hospital integration spec
- [ ] Compliance review with the zebrafish protocol
- [x] Kickoff meeting
MD

# Deliberately world-readable (0644) but inside the 2770 project directory.
# Proves the index enforces ancestor *traversal*, not just the file's own mode:
# carol can't reach it even though its own bits say anyone may read it.
seed alice "projects/$PROJECT/leak.md" <<'MD'
# Traversal fixture

This file is mode 0644 — world-readable on its own terms. It is still invisible
to anyone outside `proj-acme`, because they cannot traverse the directory that
contains it. Codename walrus.
MD
chmod 644 "$REPO/projects/$PROJECT/leak.md" 2>/dev/null || true

seed alice users/alice/private.md <<'MD'
# Alice's private notes

`users/<name>/` is mode 0700. Nobody else can read this, including other
admins, without changing the permissions on disk first.

Secret keyword: aardvark.
MD

seed bob users/bob/private.md <<'MD'
# Bob's private notes

Same as Alice's — 0700, owner-only.

Secret keyword: pangolin.
MD

seed carol users/carol/private.md <<'MD'
# Carol's private notes

Same again — 0700, owner-only.
MD

chmod 600 "$REPO"/users/*/private.md 2>/dev/null || true

# ---------------------------------------------------------------------------
say "demo artifacts"
# ---------------------------------------------------------------------------
# Sandboxed HTML dashboards that query the database as the viewer. These are
# also fixtures for tests/e2e — see defaults/artifacts/README.md.
install -d -m 2775 -o alice -g kb-users "$REPO/company/dashboards"
for a in randoms iotest scopetest xsstest; do
  if [ ! -e "$REPO/company/dashboards/$a.html" ]; then
    install -m 664 -o alice -g kb-users "$SRC/defaults/artifacts/$a.html" \
            "$REPO/company/dashboards/$a.html"
    echo "  company/dashboards/$a.html"
  fi
done

# The readings table the randoms dashboard renders, shared with carol so the
# "artifact + grant reaches a specific colleague" path has something to show.
runuser -u postgres -- psql -d kb -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE TABLE IF NOT EXISTS u_alice.readings (
    id bigserial PRIMARY KEY,
    value int NOT NULL,
    at timestamptz DEFAULT now()
);
INSERT INTO u_alice.readings (value)
SELECT (random() * 100)::int FROM generate_series(1, 20)
WHERE NOT EXISTS (SELECT 1 FROM u_alice.readings);
ALTER TABLE u_alice.readings OWNER TO alice;
ALTER SEQUENCE u_alice.readings_id_seq OWNER TO alice;
GRANT USAGE ON SCHEMA u_alice TO carol;
GRANT SELECT, UPDATE, DELETE ON u_alice.readings TO carol;
SQL
echo "  u_alice.readings (shared with carol)"

# ---------------------------------------------------------------------------
say "refresh the indexer"
# ---------------------------------------------------------------------------
# kbindexer just joined proj-$PROJECT, but supplementary groups are fixed at
# exec — a already-running indexer still cannot traverse the restricted folder,
# so its content would never appear in search or the To-dos panel. Restart it.
if systemctl is-active --quiet kb-indexer; then
  systemctl restart kb-indexer
  echo "  kb-indexer restarted (picked up proj-$PROJECT membership)"
fi

cat <<DONE

== done ==
Demo users created. Passwords: $CREDS
Test credentials: $TEST_CREDS

  alice   kb-users, proj-$PROJECT, $ADMIN_GROUP   (platform admin; has the project)
  bob     kb-users, proj-$PROJECT
  carol   kb-users                                (does NOT — log in as carol to see the model work)

The indexer picks the new files up within a few seconds.
Remove it all again with: sudo bash scripts/seed-demo.sh --undo
DONE
