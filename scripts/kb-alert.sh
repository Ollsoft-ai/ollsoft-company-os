#!/usr/bin/env bash
# Record an operator-facing alert.
#
#   kb-alert.sh <title> <message> [priority] [tags]
#
# Every alert is APPENDED TO A LOG. Pushing it to a phone is a separate,
# off-by-default decision.
#
# Why: on 2026-07-26 one unconvertible spreadsheet OOM-killed kb-convert every
# 15 minutes, and each death fired this script. The phone received the same
# 1.5 KB journal dump over and over for hours. The information was correct and
# completely useless — the operator learned to swipe the channel away, which is
# how a monitoring channel dies. The bug was not that the alert was wrong; it
# was that a *repeating* condition got a *per-occurrence* interruption.
#
# So the model is now:
#   * the log is the record     — always written, never rate-limited, cheap,
#                                 and greppable long after the fact;
#   * a push is an interruption — needs KB_ALERT_PUSH=1 *and* has to survive
#                                 deduplication (below);
#   * triage is somebody's job  — kb-maintenance.sh reads this log on a timer,
#                                 decides what is noise and what is real, and is
#                                 normally the only thing that asks for the
#                                 operator's attention.
#
# Config, all from /etc/kb/kb.env:
#   KB_ALERT_PUSH    1 = also push to ntfy. Anything else (default) = log only.
#   KB_NTFY_TOPIC    ntfy topic; empty disables pushing regardless of the above.
#   KB_NTFY_SERVER   defaults to https://ntfy.sh
#   KB_ALERT_DEDUP   seconds an identical title stays muted (default 21600 = 6h).
#
# Topic hygiene: ntfy.sh topics are public write/read to anyone who knows the
# name. Use an unguessable one (e.g. companyos-x7k2m9), treat it like a
# password, and rotate it by editing kb.env + resubscribing on the phone.
set -u

TITLE="${1:?usage: kb-alert.sh <title> <message> [priority] [tags]}"
MSG="${2:-}"
PRIO="${3:-high}"
TAGS="${4:-rotating_light}"

LOG_DIR=/var/log/kb
LOG="$LOG_DIR/alerts.log"
STATE_DIR=/var/lib/kb-monitor/alert-dedup

cfg() {  # cfg NAME DEFAULT — read one key out of kb.env without leaking the rest
  # An explicitly exported value wins over kb.env. That is what lets
  # kb-maintenance.sh push its digest (KB_ALERT_PUSH=1 for that one call) while
  # per-occurrence pushes stay off for everybody else. Sourcing kb.env first
  # would clobber the caller's intent.
  if [ -n "${!1:-}" ]; then
    printf '%s' "${!1}"
  elif [ -f /etc/kb/kb.env ]; then
    (. /etc/kb/kb.env 2>/dev/null; eval "printf '%s' \"\${$1:-$2}\"")
  else
    printf '%s' "$2"
  fi
}

PUSH=$(cfg KB_ALERT_PUSH 0)
TOPIC=$(cfg KB_NTFY_TOPIC "")
SERVER=$(cfg KB_NTFY_SERVER https://ntfy.sh)
DEDUP=$(cfg KB_ALERT_DEDUP 21600)

# --- 1. the log: always, first, and never allowed to fail the caller ---------
# One JSON object per line. Machine-readable because its main consumer is an
# agent doing triage, and a log you have to regex loosely is a log that gets
# misread. `ts` is epoch seconds so "everything since my last run" is
# arithmetic rather than date parsing.
mkdir -p "$LOG_DIR" 2>/dev/null || true
if command -v python3 >/dev/null 2>&1; then
  TITLE="$TITLE" MSG="$MSG" PRIO="$PRIO" TAGS="$TAGS" python3 -c '
import json, os, sys, time
sys.stdout.write(json.dumps({
    "ts": int(time.time()),
    "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "title": os.environ["TITLE"],
    "message": os.environ["MSG"],
    "priority": os.environ["PRIO"],
    "tags": os.environ["TAGS"],
}, ensure_ascii=False) + "\n")' >> "$LOG" 2>/dev/null || true
else
  # No python3 (should not happen on this box) — a lossy line beats no record.
  printf '{"ts":%s,"title":"%s","message":"(python3 missing; see journal)"}\n' \
         "$(date +%s)" "$(printf '%s' "$TITLE" | tr -d '"\\')" >> "$LOG" 2>/dev/null || true
fi
chmod 640 "$LOG" 2>/dev/null || true

# Always leave a trace in the journal too, so `journalctl -u <unit>` tells the
# whole story without anyone needing to know this log exists.
echo "kb-alert: $TITLE" >&2

# --- 2. the push: opt-in, and deduplicated even then ------------------------
[ "$PUSH" = "1" ] || exit 0
[ -n "$TOPIC" ] || exit 0

# Dedup on the title, which is the *condition*. The message carries the
# per-occurrence detail (a journal tail, a timestamp) and would defeat any
# suppression if it were part of the key.
mkdir -p "$STATE_DIR" 2>/dev/null || true
KEY=$(printf '%s' "$TITLE" | sha256sum | cut -c1-32)
STAMP="$STATE_DIR/$KEY"
if [ -f "$STAMP" ]; then
  last=$(cat "$STAMP" 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - ${last:-0} )) -lt "$DEDUP" ]; then
    echo "kb-alert: push suppressed (same title within ${DEDUP}s): $TITLE" >&2
    exit 0
  fi
fi
date +%s > "$STAMP" 2>/dev/null || true

# 3 attempts: an alert that matters is worth retrying, but a downed network
# must not wedge the caller (heartbeat runs under a timer with its own budget).
for _attempt in 1 2 3; do
  curl -m 10 -s -o /dev/null \
       -H "Title: $TITLE" -H "Priority: $PRIO" -H "Tags: $TAGS" \
       -d "$MSG" "$SERVER/$TOPIC" && exit 0
  sleep 5
done
echo "kb-alert: could not reach $SERVER after 3 attempts" >&2
exit 1
