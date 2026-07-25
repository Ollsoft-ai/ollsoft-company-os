#!/usr/bin/env bash
# Send an alert to the operator's phone via ntfy.
#
#   kb-alert.sh <title> <message> [priority] [tags]
#
# The topic comes from KB_NTFY_TOPIC in /etc/kb/kb.env. No topic configured =
# silent no-op (exit 0), so every caller — heartbeat, OnFailure hooks, cron —
# can fire unconditionally and boxes without monitoring configured (CI, fresh
# installs) stay quiet instead of erroring.
#
# Topic hygiene: ntfy.sh topics are public write/read to anyone who knows the
# name. Use an unguessable one (e.g. companyos-x7k2m9), treat it like a
# password, and rotate it by editing kb.env + resubscribing on the phone.
set -u

TITLE="${1:?usage: kb-alert.sh <title> <message> [priority] [tags]}"
MSG="${2:-}"
PRIO="${3:-high}"
TAGS="${4:-rotating_light}"

TOPIC=""
[ -f /etc/kb/kb.env ] && TOPIC="$(. /etc/kb/kb.env 2>/dev/null; echo "${KB_NTFY_TOPIC:-}")"
[ -n "$TOPIC" ] || exit 0

SERVER="${KB_NTFY_SERVER:-https://ntfy.sh}"
[ -f /etc/kb/kb.env ] && SERVER="$(. /etc/kb/kb.env 2>/dev/null; echo "${KB_NTFY_SERVER:-https://ntfy.sh}")"

# 3 attempts: an alert that matters is worth retrying, but a downed network
# must not wedge the caller (heartbeat runs under a timer with its own budget).
for i in 1 2 3; do
  curl -m 10 -s -o /dev/null \
       -H "Title: $TITLE" -H "Priority: $PRIO" -H "Tags: $TAGS" \
       -d "$MSG" "$SERVER/$TOPIC" && exit 0
  sleep 5
done
echo "kb-alert: could not reach $SERVER after 3 attempts" >&2
exit 1
