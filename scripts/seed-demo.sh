#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Optional demo content for Ollsoft Company OS.
#
# Creates three sample employees, a restricted project, and some seed documents,
# so you can log in as different people and watch the permission model work
# without inventing a company first. Run AFTER scripts/install.sh.
#
# It is also the test suite's fixture. For a test run, pass --namespace so that
# nothing shares a name with real content:
#
#   sudo bash scripts/seed-demo.sh --namespace ab12cd
#
# which seeds company/kbtest-ab12cd/, projects/kbtest-ab12cd-acme/ and the
# accounts kbt_ab12cd_{alice,bob,carol}, and writes the logical-name mapping to
# /tmp/kb-test-creds-ab12cd.json. tests/kbenv.py is the only reader of that file,
# so the suite needs no edit to follow a namespace. tests/conftest.py does this
# automatically in pytest_configure and tears it down again afterwards.
#
# Without --namespace it seeds the human-facing demo, as before:
#
#   sudo bash scripts/seed-demo.sh [--repo /srv/kb]
#
# To remove either again (removes ONLY what that run recorded creating):
#   sudo bash scripts/seed-demo.sh [--namespace <id>] --undo
# ---------------------------------------------------------------------------
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO=/srv/kb
UNDO=0
NS=""                            # --namespace <id>: isolate a test run
# The demo users are declared at their add_user calls below, each with its own
# group list. Teardown no longer needs a list here: it reads the manifest, so it
# also removes users a PARTIAL run created, which a fixed list never did.
PROJECT=acme                     # restricted project; alice+bob only

# Everything this script creates is RECORDED here, and --undo removes exactly
# what is recorded and nothing else.
#
# It used to remove a hardcoded list of paths instead — including
# company/overview.md and company/onboarding.md. On a box where those documents
# already existed and were written by a human, the seeder correctly SKIPPED them
# (see seed(), which refuses to overwrite) and then --undo deleted them anyway.
# It even printed "Documents you created yourself under company/ were left
# alone" while doing it. A manifest makes that class of mistake impossible:
# if we did not create it, we cannot remove it.
MANIFEST_DIR=/var/lib/kb-seed
MANIFEST="$MANIFEST_DIR/${NS:-demo}.manifest"

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="${2:?}"; shift 2 ;;
    --namespace) NS="${2:?}"; shift 2 ;;
    --undo) UNDO=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# NS is known only after argument parsing.
MANIFEST="$MANIFEST_DIR/${NS:-demo}.manifest"

# record <kind> <value>   kind: path | user | group | extfile
record() { mkdir -p "$MANIFEST_DIR"; chmod 700 "$MANIFEST_DIR"; printf '%s\t%s\n' "$1" "$2" >> "$MANIFEST"; }

# --- namespacing -----------------------------------------------------------
# With --namespace <id> NOTHING lands on a name a human would ever pick: the
# documents go under company/kbtest-<id>/, the project is its own directory and
# group, and the accounts are prefixed and thrown away afterwards. A test run
# then cannot collide with — or delete — real content, because it never shares a
# path or a username with any.
#
# The namespace is NOT a dot-directory on purpose: indexer.py prunes those
# (`dirnames[:] = [d for d in dirnames if not d.startswith(".")]`), so a hidden
# area would be invisible to search, to the To-dos panel and to RLS — silently
# turning most of the suite into a no-op instead of a failure.
if [ -n "$NS" ]; then
  case "$NS" in
    *[!a-z0-9]*|"") echo "--namespace must be lowercase alphanumeric" >&2; exit 2 ;;
  esac
  AREA="company/kbtest-$NS"           # the run's own "company"
  PROJ_REL="projects/kbtest-$NS-$PROJECT"
  # STABLE across runs, deliberately not namespaced. kbindexer's supplementary
  # groups are fixed at exec, so it must be a member to index the 2770 project —
  # and creating/deleting a per-run group would mean restarting kb-indexer twice
  # per test run, each costing ~10 minutes of index staleness while it resweeps
  # every block before its inotify watches arm. A single long-lived group that
  # kbindexer joins once removes that entirely. Between runs it holds only
  # kbindexer; the ephemeral accounts join and leave with their run.
  PROJ_GRP="kbt-$PROJECT"
  USER_PREFIX="kbt_${NS}_"
  CREDS=/root/kb-seed-$NS.txt
  TEST_CREDS=/tmp/kb-test-creds-$NS.json
else
  AREA="company"
  PROJ_REL="projects/$PROJECT"
  PROJ_GRP="proj-$PROJECT"
  USER_PREFIX=""
  CREDS=/root/ollsoft-company-os-demo.txt
  TEST_CREDS=/tmp/kb-test-creds.json
fi
# logical name (what the tests call someone) -> real account on this box
nsuser() { printf '%s%s' "$USER_PREFIX" "$1"; }
A_ALICE=$(nsuser alice); A_BOB=$(nsuser bob); A_CAROL=$(nsuser carol)

# Read what the installer chose. Needed before the --undo branch, which also
# talks to Postgres. Platform admins are members of the configured admin group:
# the test suite drives /admin/* as alice, so she must be one.
ADMIN_GROUP=sudo
PGDB=kb
if [ -f /etc/kb/kb.env ]; then
  ADMIN_GROUP="$(. /etc/kb/kb.env; echo "${KB_ADMIN_GROUP:-sudo}")"
  PGDB="$(. /etc/kb/kb.env; echo "${KB_PG_DB:-kb}")"
fi

# ---------------------------------------------------------------------------
if [ "$UNDO" -eq 1 ]; then
  say "removing seeded content (${NS:-demo})"
  if [ ! -f "$MANIFEST" ]; then
    cat >&2 <<EOF
No manifest at $MANIFEST — refusing to guess what to delete.

This is deliberate. Earlier versions removed a fixed list of paths, which meant
--undo could delete a document a human had written that merely shared a name
with a seed file. If this demo was seeded by an older version, remove it by
hand; from now on every seeded item is recorded and only recorded items go.
EOF
    exit 1
  fi
  # Reverse order: files before the directories that contain them.
  tac "$MANIFEST" | while IFS=$'\t' read -r kind val; do
    [ -n "${kind:-}" ] || continue
    case "$kind" in
      path)
        # Refuse anything outside the repo, however the manifest got that way.
        case "$val" in
          "$REPO"/*) ;;
          *) echo "  SKIP (outside repo): $val" >&2; continue ;;
        esac
        if [ -d "$val" ] && [ ! -L "$val" ]; then rmdir "$val" 2>/dev/null && echo "  rmdir ${val#"$REPO"/}"
        elif [ -e "$val" ] || [ -L "$val" ]; then rm -f "$val" && echo "  rm    ${val#"$REPO"/}"
        fi
        # Teardown must be idempotent: a manifest can name the same item twice
        # (a re-seed appends), and an already-gone item makes the && chain above
        # return 1. Under `set -e` that killed the whole loop mid-way, leaving
        # the rest of the manifest unprocessed and the manifest file behind.
        : ;;
      user)
        id "$val" &>/dev/null || continue
        pkill -KILL -u "$val" 2>/dev/null || true
        if runuser -u postgres -- psql -d "$PGDB" -tAc \
             "SELECT 1 FROM pg_roles WHERE rolname='$val'" | grep -q 1; then
          runuser -u postgres -- psql -d "$PGDB" -qc "DROP OWNED BY \"$val\" CASCADE;" >/dev/null
          runuser -u postgres -- psql -d "$PGDB" -qc "DROP ROLE IF EXISTS \"$val\";" >/dev/null
        fi
        userdel -r "$val" 2>/dev/null || true
        echo "  user  $val" ;;
      group) groupdel "$val" 2>/dev/null && { echo "  group $val"; : > "$MANIFEST.groups-went"; }
        : ;;
      # A namespaced area belongs entirely to its run, so it comes out whole.
      # `path` uses rmdir, which fails the moment a test leaves a scratch file
      # behind — that is how ten abandoned kbtest-* directories accumulated in
      # the real company/ folder. Guarded twice: inside the repo, and the name
      # must actually carry the kbtest- marker.
      tree)
        case "$val" in
          "$REPO"/*kbtest-*) rm -rf "$val" && echo "  rm -r ${val#"$REPO"/}" ;;
          *) echo "  SKIP (not a namespaced tree): $val" >&2 ;;
        esac
        : ;;
      # Credential files, outside the repo. Recorded rather than hardcoded for
      # the same reason as everything else: only what we wrote gets removed.
      extfile) [ -e "$val" ] && { rm -f "$val"; echo "  rm    $val"; }
        : ;;
    esac
  done
  rm -f "$MANIFEST"
  # Only when a group actually went: kbindexer's supplementary groups are fixed
  # at exec, so it must be restarted to STOP seeing a removed project. Restarting
  # it costs ~10 minutes of index staleness (the resweep re-inserts every block
  # before inotify watches arm), so it is not something to do unconditionally.
  if [ -e "$MANIFEST.groups-went" ]; then
    rm -f "$MANIFEST.groups-went"
    systemctl is-active --quiet kb-indexer && systemctl restart kb-indexer && \
      echo "  kb-indexer restarted (group membership changed)"
  fi
  echo
  echo "  Removed exactly what the manifest recorded. Anything you wrote yourself"
  echo "  was never recorded, so it was never a candidate."
  exit 0
fi

[ -d "$REPO" ] || { echo "repo $REPO not found — run scripts/install.sh first" >&2; exit 1; }

# ---------------------------------------------------------------------------
say "demo users"
# ---------------------------------------------------------------------------
# CREDS / TEST_CREDS are namespace-derived (see the namespacing block above).
# The test suite reads its logins from TEST_CREDS; every tests/cli module and the
# e2e conftest load it at import time, so without it pytest fails during
# collection — which is why the root conftest seeds in pytest_configure.
umask 077
: > "$CREDS"
record extfile "$CREDS"
GROUP_IS_NEW=0
if ! getent group "$PROJ_GRP" >/dev/null; then
  groupadd "$PROJ_GRP"
  GROUP_IS_NEW=1
  # Only the demo's group is torn down. A namespaced run's group is shared
  # infrastructure (see above), so it is deliberately NOT recorded.
  [ -n "$NS" ] || record group "$PROJ_GRP"
fi
if ! id -nG kbindexer | tr ' ' '\n' | grep -qx "$PROJ_GRP"; then
  usermod -aG "$PROJ_GRP" kbindexer
  GROUP_IS_NEW=1
fi


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
  record user "$u"
  install -d -m 700 -o "$u" -g "$u" "$REPO/users/$u"
  record path "$REPO/users/$u"
  runuser -u postgres -- psql -d "$PGDB" -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$u') THEN CREATE ROLE "$u" LOGIN; END IF;
END \$\$;
GRANT kb_users TO "$u";
CREATE SCHEMA IF NOT EXISTS "u_$u" AUTHORIZATION "$u";
ALTER ROLE "$u" SET search_path = "u_$u", kb, public;
SQL
  echo "  $u"
}
# kb-users is the REAL group on purpose: these accounts must have exactly the
# authority a real employee has, or the tests stop testing the production model.
# The manifest teardown removes the accounts, and with them the membership.
add_user "$A_ALICE" kb-users "$PROJ_GRP" "$ADMIN_GROUP"   # the demo/test admin
add_user "$A_BOB"   kb-users "$PROJ_GRP"
add_user "$A_CAROL" kb-users                # deliberately NOT on the project

chmod 600 "$CREDS"
# Tests address people by LOGICAL name ("alice"); on a namespaced run the real
# account is kbt_<ns>_alice. This file carries the mapping and the run's paths,
# and tests/kbenv.py is its only reader — so namespacing needs no edit anywhere
# else in the suite.
# Remove first, do not truncate in place. /tmp is sticky and world-writable, and
# with fs.protected_regular=2 the kernel refuses an O_CREAT open of a file whose
# owner differs from both the caller and the directory owner — and that check has
# no CAP_FOWNER exemption, so on a re-seed even root is denied writing the copy a
# previous run chowned to the developer.
rm -f "$TEST_CREDS"
cat > "$TEST_CREDS" <<JSON
{
  "ns": "${NS}",
  "repo": "${REPO}",
  "area": "${AREA}",
  "project": "${PROJ_REL}",
  "project_group": "${PROJ_GRP}",
  "admin_group": "${ADMIN_GROUP}",
  "users": {
    "alice": {"name": "${A_ALICE}", "password": "${PW[$A_ALICE]}"},
    "bob":   {"name": "${A_BOB}",   "password": "${PW[$A_BOB]}"},
    "carol": {"name": "${A_CAROL}", "password": "${PW[$A_CAROL]}"}
  }
}
JSON
# 0600, NOT 0644: this file holds three working passwords, and one of them
# (alice) is in the admin group — world-readable put them in reach of every
# local account on the box.
#
# It must still be READABLE BY WHOEVER RUNS THE SUITE, and that is not always
# the person who seeded: CI seeds with sudo (SUDO_USER=runner) but runs pytest
# as alice, so guessing wrong here breaks collection in every module that loads
# this file at import time. Set KB_TEST_USER to name that account explicitly.
TEST_CREDS_OWNER="${KB_TEST_USER:-${SUDO_USER:-root}}"
chmod 600 "$TEST_CREDS"
if ! chown "$TEST_CREDS_OWNER" "$TEST_CREDS" 2>/dev/null; then
  echo "  WARNING: could not chown $TEST_CREDS to $TEST_CREDS_OWNER" >&2
fi
record extfile "$TEST_CREDS"
echo "  test credentials -> $TEST_CREDS"

# ---------------------------------------------------------------------------
say "restricted project: $PROJ_REL"
# ---------------------------------------------------------------------------
# 2770 + setgid: only proj-<name> members can even list it. This is the folder
# that demonstrates the whole model — carol cannot see it in the file tree, and
# a raw SQL query as carol returns none of its rows either.
if [ ! -d "$REPO/$PROJ_REL" ]; then
  mkdir -p "$REPO/$PROJ_REL"
  # namespaced runs own their whole project dir; the demo shares projects/acme
  [ -n "$NS" ] && record tree "$REPO/$PROJ_REL" || record path "$REPO/$PROJ_REL"
fi
chgrp "$PROJ_GRP" "$REPO/$PROJ_REL"
chmod 2770 "$REPO/$PROJ_REL"
setfacl -d -m u::rwx,g::rwx,o::- "$REPO/$PROJ_REL"

# The run's own company area. Same mode and group as company/ itself, so the
# documents inside behave exactly like real shared documents do.
if [ "$AREA" != "company" ] && [ ! -d "$REPO/$AREA" ]; then
  install -d -m 2775 -g kb-users "$REPO/$AREA"
  # The default ACL is not optional. This script runs under `umask 077`, so
  # without it every seeded document lands 0600 — unreadable by the kbindexer
  # service account, which means it never enters the index and every search,
  # To-dos and RLS test silently sees an empty corpus instead of failing.
  # install.sh sets the same default ACL on company/ itself; setting it here too
  # means the area does not depend on that having survived on this box.
  setfacl -d -m u::rwx,g::rwx,o::rx "$REPO/$AREA"
  record tree "$REPO/$AREA"
fi

# ---------------------------------------------------------------------------
say "seed documents"
# ---------------------------------------------------------------------------
seed() {  # seed <owner> <relpath>  <<<content   (only if missing)
  local owner=$1 rel=$2 full="$REPO/$2"
  # An existing file is somebody else's document. Skipping it is why --undo must
  # be manifest-driven: a skipped file is never recorded, so it can never be
  # deleted by the teardown.
  [ -e "$full" ] && { echo "  skip $rel (exists, left alone)"; cat >/dev/null; return 0; }
  cat > "$full"
  chown "$owner" "$full"
  record path "$full"
  echo "  $rel"
}


# The distinctive nouns below (zebrafish, walrus, aardvark, pangolin) are not
# whimsy — the test suite greps for them to prove that a document is visible to
# one user and genuinely invisible to another. Changing them breaks tests/cli.

seed "$A_ALICE" "$AREA/overview.md" <<'MD'
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

seed "$A_BOB" "$AREA/onboarding.md" <<'MD'
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

seed "$A_ALICE" "$PROJ_REL/plan.md" <<'MD'
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
seed "$A_ALICE" "$PROJ_REL/leak.md" <<'MD'
# Traversal fixture

This file is mode 0644 — world-readable on its own terms. It is still invisible
to anyone outside `proj-acme`, because they cannot traverse the directory that
contains it. Codename walrus.
MD
chmod 644 "$REPO/$PROJ_REL/leak.md" 2>/dev/null || true

seed "$A_ALICE" "users/$A_ALICE/private.md" <<'MD'
# Alice's private notes

`users/<name>/` is mode 0700. Nobody else can read this, including other
admins, without changing the permissions on disk first.

Secret keyword: aardvark.
MD

seed "$A_BOB" "users/$A_BOB/private.md" <<'MD'
# Bob's private notes

Same as Alice's — 0700, owner-only.

Secret keyword: pangolin.
MD

seed "$A_CAROL" "users/$A_CAROL/private.md" <<'MD'
# Carol's private notes

Same again — 0700, owner-only.
MD

for u in "$A_ALICE" "$A_BOB" "$A_CAROL"; do
  chmod 600 "$REPO/users/$u/private.md" 2>/dev/null || true
done

# ---------------------------------------------------------------------------
say "demo artifacts"
# ---------------------------------------------------------------------------
# Sandboxed HTML dashboards that query the database as the viewer. These are
# also fixtures for tests/e2e — see defaults/artifacts/README.md.
if [ ! -d "$REPO/$AREA/dashboards" ]; then
  install -d -m 2775 -o "$A_ALICE" -g kb-users "$REPO/$AREA/dashboards"
  record path "$REPO/$AREA/dashboards"
fi
# A non-markdown document (the index only ingests .md) and a _secrets folder:
# both exist in company/ on a real install — todos.html from install.sh, _secrets
# by convention — and the suite asserts behaviour that depends on them (filename
# search reaching a file Postgres never sees; secrets never being searchable).
# A namespaced area has to provide its own, or those tests silently fall back to
# the real company/ and stop testing this run at all.
if [ ! -e "$REPO/$AREA/todos.html" ]; then
  install -m 0664 -o "$A_ALICE" -g kb-users "$SRC/defaults/artifacts/todos.html" \
          "$REPO/$AREA/todos.html"
  record path "$REPO/$AREA/todos.html"
  echo "  $AREA/todos.html"
fi
if [ ! -d "$REPO/$AREA/_secrets" ]; then
  install -d -m 2770 -o "$A_ALICE" -g kb-users "$REPO/$AREA/_secrets"
  record path "$REPO/$AREA/_secrets"
  echo "  $AREA/_secrets/"
fi

for a in randoms iotest scopetest xsstest; do
  if [ ! -e "$REPO/$AREA/dashboards/$a.html" ]; then
    install -m 664 -o "$A_ALICE" -g kb-users "$SRC/defaults/artifacts/$a.html" \
            "$REPO/$AREA/dashboards/$a.html"
    record path "$REPO/$AREA/dashboards/$a.html"
    echo "  $AREA/dashboards/$a.html"
  fi
done

# The readings table the randoms dashboard renders, shared with carol so the
# "artifact + grant reaches a specific colleague" path has something to show.
# Unquoted heredoc: the schema and role names are namespace-derived. (There are
# no $$ blocks in this statement, so nothing else needs escaping.)
runuser -u postgres -- psql -d "$PGDB" -v ON_ERROR_STOP=1 -q <<SQL
CREATE TABLE IF NOT EXISTS "u_$A_ALICE".readings (
    id bigserial PRIMARY KEY,
    value int NOT NULL,
    at timestamptz DEFAULT now()
);
INSERT INTO "u_$A_ALICE".readings (value)
SELECT (random() * 100)::int FROM generate_series(1, 20)
WHERE NOT EXISTS (SELECT 1 FROM "u_$A_ALICE".readings);
ALTER TABLE "u_$A_ALICE".readings OWNER TO "$A_ALICE";
ALTER SEQUENCE "u_$A_ALICE".readings_id_seq OWNER TO "$A_ALICE";
GRANT USAGE ON SCHEMA "u_$A_ALICE" TO "$A_CAROL";
GRANT SELECT, UPDATE, DELETE ON "u_$A_ALICE".readings TO "$A_CAROL";
SQL
echo "  u_$A_ALICE.readings (shared with $A_CAROL)"

# ---------------------------------------------------------------------------
say "refresh the indexer"
# ---------------------------------------------------------------------------
# kbindexer just joined $PROJ_GRP, but supplementary groups are fixed at
# exec — a already-running indexer still cannot traverse the restricted folder,
# so its content would never appear in search or the To-dos panel. Restart it.
if [ "$GROUP_IS_NEW" = 1 ] && systemctl is-active --quiet kb-indexer; then
  systemctl restart kb-indexer
  echo "  kb-indexer restarted (kbindexer joined $PROJ_GRP)"
else
  echo "  kb-indexer left alone (already a member of $PROJ_GRP)"
fi

cat <<DONE

== done ==
Demo users created. Passwords: $CREDS
Test credentials: $TEST_CREDS

  $A_ALICE   kb-users, $PROJ_GRP, $ADMIN_GROUP   (platform admin; has the project)
  $A_BOB     kb-users, $PROJ_GRP
  $A_CAROL   kb-users   (does NOT — log in as them to see the model work)

Documents:  $AREA/
Project:    $PROJ_REL/

The indexer picks the new files up within a few seconds.
Remove it all again with: sudo bash scripts/seed-demo.sh ${NS:+--namespace $NS }--undo
DONE
