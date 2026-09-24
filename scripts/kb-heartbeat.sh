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
for u in kb-hub kb-syncd kb-indexer postgresql; do   # kb-embedd: check 8, only where set up
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

# --- 6. disk and inodes -------------------------------------------------------
# Messages here are STABLE text — deliberately NO percentage. An alert fires
# whenever this line CHANGES, so "root disk at 86%" re-fires on every point the
# disk climbs and rewrites health.md (and a syncd commit) with it — the exact
# flapping the check-8 comment below warns about. Buckets, not numbers: the
# figure belongs in `df`, which the operator runs once an alert says to look.
#
# /boot is checked too, and separately: it is its own small filesystem (~880M)
# and fills from retained kernel packages long before / is anywhere near full,
# so a root-only check reports plenty of space right up until apt breaks.
disk_pct() { df --output=pcent "$1" 2>/dev/null | tail -1 | tr -dc '0-9'; }
for mp in / /boot; do
  mountpoint -q "$mp" 2>/dev/null || [ "$mp" = "/" ] || continue
  p=$(disk_pct "$mp"); p=${p:-0}
  if [ "$mp" = "/" ]; then label="root disk"; else label="$mp"; fi
  if   [ "$p" -ge 95 ]; then note "$label is CRITICALLY full (over 95%) — writes are about to fail"
  elif [ "$p" -ge 85 ]; then note "$label is over 85% full — act before syncd/postgres start failing writes"
  fi
done

# Inodes fail writes with "No space left on device" while df shows free GB —
# a failure mode that reads as a lie unless something names it explicitly.
# NB: `df --output=ipcent` without -i; `df -i --output=ipcent` prints nothing.
ipct=$(df --output=ipcent / 2>/dev/null | tail -1 | tr -dc '0-9')
[ "${ipct:-0}" -ge 85 ] && note "root filesystem is over 85% of its inodes — new files will fail even though df shows free space"

# --- 7. backups — REMOVED 2026-09-25 ------------------------------------------
# There used to be a check here that alerted when the newest verified snapshot
# under /home/krystof/backups aged past 48h. Removed at the operator's request:
# the machine is backed up EXTERNALLY, off-box, which is the better arrangement
# and the thing the old comment here was waiting for.
#
# What it measured was the age of the local `kb-backup` snapshots — which are
# ad-hoc, taken before risky changes, and deliberately NOT on a schedule. So
# once off-box backups existed, this check only ever reported "nobody has run a
# manual pre-change snapshot lately", which is not a fault and fired a standing
# alert that masked real ones.
#
# Do not re-add it as-is. A useful backup check here would have to verify the
# EXTERNAL backup actually ran and restores — this box cannot see that, so the
# check belongs wherever that backup runs, not in this script.

# --- 8. semantic search (kb-embedd) ------------------------------------------
# Only where it is set up. Messages are STABLE text — no amounts, no counts —
# because an alert fires whenever this line changes; "$4.97 of $10" would page
# on every poll. docs/semantic-search.md, "Monitoring".
SST=/run/kb/search/status.json
if systemctl is-enabled --quiet kb-embedd 2>/dev/null; then
  if ! systemctl is-active --quiet kb-embedd; then
    note "kb-embedd is not active (search is full-text only)"
  elif [ -f "$SST" ]; then
    age=$(( $(date +%s) - $(stat -c %Y "$SST") ))
    [ "$age" -gt 180 ] && note "kb-embedd status is ${age}s old — the worker is stuck"
    while IFS= read -r msg; do [ -n "$msg" ] && note "$msg"; done < <(python3 - "$SST" "$STATE_DIR/search-behind-since" <<'PY_EOF'
import json, sys, time, os
st = json.load(open(sys.argv[1]))
since_file = sys.argv[2]
if st.get("configured"):
    p = st.get("paused")
    if p in ("breaker", "dims mismatch", "error", "database"):
        print(f"semantic search paused: {p}")
    elif p == "budget":
        print("semantic search paused: embedding budget reached")
    for w in st.get("warnings") or []:
        if w.startswith("embedding spend today"): print("semantic search: over half of today's embedding budget used")
        elif w.startswith("embedding spend this month"): print("semantic search: over half of this month's embedding budget used")
        elif w.startswith("rerank spend"): print("semantic search: over half of today's rerank budget used")
    q = st.get("query") or {}
    if q.get("rerank_breaker"):
        print("semantic search: reranking provider failing")
    chunks, done = st.get("chunks") or 0, st.get("embedded") or 0
    behind = chunks and done / chunks < 0.95 and p != "disabled"
    if behind:
        if not os.path.exists(since_file):
            open(since_file, "w").write(str(int(time.time())))
        elif time.time() - int(open(since_file).read() or 0) > 6 * 3600:
            print("semantic search: under 95% of sections embedded for 6h")
    elif os.path.exists(since_file):
        os.unlink(since_file)
PY_EOF
)
  else
    note "kb-embedd has written no status file"
  fi
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
    # Describe what actually happens, by reading the flag — this footer claimed
    # "alerts via ntfy" for the whole period pushes were switched off, which is
    # how a reader concludes the monitoring is dead when it is merely quiet.
    if [ "$(. /etc/kb/kb.env 2>/dev/null; echo "${KB_ALERT_PUSH:-0}")" = "1" ]; then
      echo "_Written by kb-heartbeat (every 5 min, on state change). Alerts: /var/log/kb/alerts.log and pushed to ntfy._"
    else
      echo "_Written by kb-heartbeat (every 5 min, on state change). Alerts are recorded in /var/log/kb/alerts.log; push notifications are off._"
    fi
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
