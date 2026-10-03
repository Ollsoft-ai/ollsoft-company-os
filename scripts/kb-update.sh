#!/usr/bin/env bash
# Pull a new release and put it live — unattended, on a timer, with a way back.
#
#   sudo bash scripts/kb-update.sh [--now] [--channel stable|edge] [--dry-run]
#
# Reads /etc/kb/kb.env:
#   KB_SRC              the server's own clone of the public repo (/opt/kb-src)
#   KB_UPDATE_CHANNEL   stable (tags only) | edge (main) | off
#   KB_UPDATE_DEFERRALS how many hours to wait out live work before giving up
#
# WHY IT WAITS: restarting kb-hub kills every open web-terminal shell, and the
# per-user backends live in the hub's cgroup — so an update at the wrong moment
# throws away a colleague's unsaved work. Scheduling it for Sunday 02:00 is not
# enough on its own: somebody is always the exception. So the update refuses to
# restart while shells are alive, waits an hour, and tries again; after the
# allowance it gives up and reports, rather than forcing it.
#
# WHY IT CAN GO BACK: the commit that was live is recorded before anything
# moves. If the hub does not answer HTTP after the deploy, the previous commit
# is checked out and deployed again, and the operator is told. An install that
# updates itself has to be able to undo that.
set -uo pipefail

CHANNEL=""; NOW=0; DRY=0
for a in "$@"; do
  case "$a" in
    --now) NOW=1 ;;
    --dry-run) DRY=1 ;;
    --channel) shift; CHANNEL="${1:?}" ;;
    stable|edge|off) CHANNEL="$a" ;;
    *) ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

SRC=/opt/kb-src; PORT=8300; DEFERRALS=3
if [ -f /etc/kb/kb.env ]; then
  # shellcheck disable=SC1091
  . /etc/kb/kb.env
  SRC="${KB_SRC:-$SRC}"; PORT="${KB_HUB_PORT:-$PORT}"
  DEFERRALS="${KB_UPDATE_DEFERRALS:-$DEFERRALS}"
  [ -n "$CHANNEL" ] || CHANNEL="${KB_UPDATE_CHANNEL:-stable}"
fi
[ -n "$CHANNEL" ] || CHANNEL=stable

# Helpers come from the deployed platform, which always exists; the clone may
# not, and a missing clone must still be able to report itself.
HELPERS="${KB_PLATFORM_ROOT:-/opt/kb-platform}/scripts"
[ -x "$HELPERS/kb-telemetry" ] || HELPERS="$SRC/scripts"
TELEMETRY="$HELPERS/kb-telemetry"
ALERT="$HELPERS/kb-alert.sh"
note() { echo "kb-update: $*"; }
tell() { [ -x "$TELEMETRY" ] && "$TELEMETRY" event "$1" "${2:-}" >/dev/null 2>&1 || true; }
alert() { [ -x "$ALERT" ] && "$ALERT" "$1" "$2" "${3:-default}" update >/dev/null 2>&1 || true; }

if [ "$CHANNEL" = off ]; then note "channel is off — nothing to do"; exit 0; fi
[ -d "$SRC/.git" ] || { note "no clone at $SRC — see docs/updates.md"; exit 0; }

# --- what is available ------------------------------------------------------
git -C "$SRC" fetch --tags --prune --quiet origin || { note "fetch failed"; exit 0; }
case "$CHANNEL" in
  stable) TARGET="$(git -C "$SRC" tag --sort=-v:refname --merged origin/main | head -1)"
          [ -n "$TARGET" ] || { note "no release tag yet — staying put"; exit 0; } ;;
  edge)   TARGET="origin/main" ;;
  *)      note "unknown channel: $CHANNEL"; exit 2 ;;
esac

PREV="$(git -C "$SRC" rev-parse HEAD)"
WANT="$(git -C "$SRC" rev-parse "$TARGET^{commit}")"
[ "$PREV" = "$WANT" ] && { note "already on $TARGET"; exit 0; }

# Only ever move forward. A rewritten history upstream is a surprise, and a
# surprise is not something an unattended script should resolve by itself.
if ! git -C "$SRC" merge-base --is-ancestor "$PREV" "$WANT"; then
  note "$TARGET is not a descendant of the running commit — refusing"
  alert "Company OS update skipped" "Upstream history diverged; update by hand." high
  exit 1
fi

note "channel $CHANNEL: $(git -C "$SRC" describe --tags --always "$PREV") -> $TARGET"
[ "$DRY" -eq 1 ] && { note "dry run — stopping here"; exit 0; }

# --- wait for the shells to go home ----------------------------------------
live_shells() {
  local cg=/sys/fs/cgroup/system.slice/kb-hub.service/cgroup.procs n=0 p c
  [ -r "$cg" ] || { echo 0; return; }
  while read -r p; do
    c="$(cat "/proc/$p/comm" 2>/dev/null || true)"
    case "$c" in bash|sh|zsh|fish|tmux*|screen) n=$((n + 1)) ;; esac
  done < "$cg"
  echo "$n"
}
if [ "$NOW" -eq 0 ]; then
  tries=0
  while [ "$(live_shells)" -gt 0 ]; do
    if [ "$tries" -ge "$DEFERRALS" ]; then
      note "still $(live_shells) live shells after ${DEFERRALS}h — leaving it for next time"
      alert "Company OS update deferred" \
            "Someone was working in a terminal; $TARGET not installed yet." default
      tell update_deferred "$CHANNEL"
      exit 0
    fi
    tries=$((tries + 1))
    note "$(live_shells) live shell(s) — waiting an hour (${tries}/${DEFERRALS})"
    sleep 3600
  done
fi

# --- go ---------------------------------------------------------------------
tell update_started "$TARGET"
git -C "$SRC" checkout --quiet --detach "$WANT" || { note "checkout failed"; exit 1; }

deploy() {
  if [ -d "$SRC/frontend" ] && [ -f "$SRC/frontend/build.mjs" ]; then
    (cd "$SRC/frontend" && npm install --silent && node build.mjs) >/dev/null 2>&1 \
      || note "frontend build failed — deploying the rest"
  fi
  bash "$SRC/scripts/deploy.sh"
}
healthy() {
  sleep 5
  local code
  code="$(curl -m 10 -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || echo 000)"
  case "$code" in 200|302) return 0 ;; *) note "hub answered HTTP $code"; return 1 ;; esac
}

if deploy && healthy; then
  NEWV="$(. /etc/kb/kb.env; echo "${KB_VERSION:-?}")"
  note "updated to $NEWV"
  alert "Company OS updated" "Now running $NEWV." low
  tell update_ok "$NEWV"
  exit 0
fi

note "deploy or health check failed — rolling back"
git -C "$SRC" checkout --quiet --detach "$PREV"
if deploy && healthy; then
  alert "Company OS update rolled back" \
        "$TARGET failed its health check; the previous version is running again." high
  tell update_rollback "$TARGET"
  exit 1
fi
alert "Company OS update FAILED and rollback failed" \
      "Manual recovery needed on this host." high
tell update_broken "$TARGET"
exit 2
