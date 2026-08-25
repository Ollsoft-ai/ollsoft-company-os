#!/usr/bin/env bash
# Functional health check for the platform — runs every 5 minutes from
# kb-heartbeat.timer, as root. Checks OUTCOMES, not processes: a service can
# be `active` while serving garbage (the 2026-07-25 indexer bug served a stale
# search index for 90 minutes while systemd showed green).
#
# Alerts ONLY on state change: one notification when something breaks, one when
# it recovers. Repeating a known problem every 5 minutes teaches the operator
# to ignore the channel, which is how monitoring dies.
set -u

REPO=/srv/kb; PGDB=kb
[ -f /etc/kb/kb.env ] && { REPO="$(. /etc/kb/kb.env; echo "${KB_REPO:-/srv/kb}")";
                            PGDB="$(. /etc/kb/kb.env; echo "${KB_PG_DB:-kb}")"; }
STATE_DIR="${STATE_DIRECTORY:-/var/lib/kb-monitor}"
mkdir -p "$STATE_DIR"
ALERT="$(dirname "$(readlink -f "$0")")/kb-alert.sh"

FAILS=()
note() { FAILS+=("$1"); }

# --- 1. services are active --------------------------------------------------
for u in kb-hub kb-syncd kb-indexer postgresql; do
  systemctl is-active --quiet "$u" || note "$u is not active"
done

# --- 2. the hub answers HTTP -------------------------------------------------
code=$(curl -m 8 -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8300/ || echo 000)
case "$code" in 200|302) ;; *) note "hub answered HTTP $code (want 302)";; esac

# --- 3. postgres accepts connections ------------------------------------------
runuser -u postgres -- pg_isready -q 2>/dev/null || note "postgres not accepting connections"

# --- 4. the search index is FRESH — the check that catches the silent bug ----
# Compare the newest markdown on disk (shared areas only: the indexer cannot
# read users' 0700 dirs, so they must not count) against what kb.files recorded
# for it. Disk newer than the index by minutes = the indexer is running blind.
# %p (full path) then strip the repo prefix — %P is relative to the FIND ROOT
# (company/ or projects/), which produced paths kb.files has never heard of and
# a false STALE alert on the very first quiet afternoon.
# The prune list must mirror the indexer's own (indexer.py: dirnames pruned on
# startswith(".") and == "_secrets"). Comparing a file the indexer deliberately
# never indexes against kb.files yields idx_t=0 and a PERMANENT false STALE —
# which is exactly what this check's own output file does, now that it lives in
# company/.infrastructure/. Dot-FILES are not pruned: kb-convert sidecars are
# indexed on purpose and should still count.
newest=$(find "$REPO/company" "$REPO/projects" \
           -type d \( -name '.*' -o -name '_secrets' \) -prune -o \
           -name '*.md' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1)
if [ -n "$newest" ]; then
  disk_t=${newest%% *}; disk_t=${disk_t%.*}
  rel=${newest#* }; rel=${rel#"$REPO"/}
  age=$(( $(date +%s) - disk_t ))
  if [ "$age" -gt 120 ]; then          # grace: give the indexer 2 min to catch up
    # :'rel' is psql's own literal quoting. $rel is a FILENAME off the disk and
    # company/ is group-writable, so anyone could name a file
    #   notes'; SELECT ...;--.md
    # and have psql -c run it as the postgres superuser, unattended, every five
    # minutes from a root timer. Never interpolate a path into SQL text here.
    # The query comes in on STDIN, not -c: psql performs :'rel' variable
    # quoting only on input it lexes itself, and -c strings bypass that.
    idx_t=$(runuser -u postgres -- psql -d "$PGDB" -tA -v rel="$rel" \
      <<<"SELECT COALESCE(floor(mtime),0)::bigint FROM kb.files WHERE path=:'rel'" \
      2>/dev/null || echo 0)
    idx_t=${idx_t:-0}
    if [ $(( disk_t - idx_t )) -gt 180 ]; then
      note "search index is STALE: $rel changed $(( age / 60 ))m ago, index still has the old version"
    fi
  fi
fi

# --- 5. syncd is committing --------------------------------------------------
# Only meaningful when there was something to commit: newest md vs last commit.
if [ -n "${newest:-}" ] && [ "$age" -gt 900 ]; then
  last_commit=$(git -C "$REPO" log -1 --format=%ct 2>/dev/null || echo 0)
  if [ $(( disk_t - last_commit )) -gt 900 ]; then
    note "kb-syncd stopped committing: newest edit is $(( age / 60 ))m old, last commit is older"
  fi
fi

# --- 6. disk ------------------------------------------------------------------
pct=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "${pct:-0}" -ge 85 ] && note "root disk at ${pct}% — act before syncd/postgres start failing writes"

# --- 7. backups exist and are recent ------------------------------------------
# Until off-box backups are automated this WILL alert once when snapshots age
# past 48h — that is the check working, not the check being wrong.
newest_bk=$(find /home/krystof/backups -maxdepth 2 -name SHA256SUMS -printf '%T@\n' 2>/dev/null | sort -rn | head -1)
if [ -z "$newest_bk" ]; then
  note "no verified backups found at all"
elif [ $(( $(date +%s) - ${newest_bk%.*} )) -gt 172800 ]; then
  note "newest backup is older than 48h"
fi

# --- state transition + alerting ----------------------------------------------
STATE_FILE="$STATE_DIR/status"
prev=$(cat "$STATE_FILE" 2>/dev/null || echo "OK")
if [ ${#FAILS[@]} -eq 0 ]; then cur="OK"; else cur=$(printf '%s; ' "${FAILS[@]}"); fi

H="$REPO/company/.infrastructure/health.md"
if [ "$cur" != "$prev" ] || [ ! -f "$H" ]; then
  if [ "$cur" = "OK" ]; then
    "$ALERT" "Company OS recovered" "All checks passing again." default white_check_mark || true
  else
    "$ALERT" "Company OS: $((${#FAILS[@]})) problem(s)" "$cur" high rotating_light || true
  fi
  printf '%s' "$cur" > "$STATE_FILE"

  # The OS reports on itself where everyone (agents included) can read it —
  # written only on CHANGE so syncd's auto-commit history stays quiet.
  # `.infrastructure` is a dot-dir: readable to kb-users on disk and versioned
  # by syncd, but pruned by the indexer, so it is NOT in kb.blocks and NOT in
  # the app's tree or search. Agents must read the file, not grep for it
  # (`rg` needs --hidden). Deliberate — see that folder's README.md.
  mkdir -p "$(dirname "$H")"
  {
    echo "# Platform health"
    echo
    echo "Last state change: $(date -Is)"
    echo
    if [ "$cur" = "OK" ]; then echo "**All checks passing.**"
    else echo "**Problems:**"; for f in "${FAILS[@]}"; do echo "- $f"; done; fi
    echo
    echo "_Written by kb-heartbeat (every 5 min, alerts on change via ntfy)._"
  } > "$H"
  chown root:kb-users "$H" 2>/dev/null; chmod 644 "$H" 2>/dev/null
fi

echo "heartbeat: $cur"

# Exit 0 whenever the check itself ran, even when it found problems.
#
# This used to be `[ "$cur" = "OK" ]`, which exited 1 on any finding — and for a
# Type=oneshot unit that means systemd marks kb-heartbeat.service *failed* and
# logs "Failed to start kb-heartbeat.service". So "the health check noticed
# something" and "the health check is broken" looked identical in the journal,
# 7 times in a single day. It cost the maintenance agent a whole report: it
# blamed the heartbeat for OOM kills that belonged to kb-convert.
#
# The findings are reported through the channels built for them — kb-alert.sh,
# health.md, and the line printed above. The exit status only answers "did the
# check complete?", which is what systemd actually asks.
exit 0
