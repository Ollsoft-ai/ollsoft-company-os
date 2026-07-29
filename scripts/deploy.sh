#!/usr/bin/env bash
# Redeploy code to the install prefix after editing it. For development —
# scripts/install.sh does this (and everything else) on a fresh machine.
#
# Reads the paths chosen at install time from /etc/kb/kb.env, so it follows a
# non-default --prefix automatically. Run as root:
#
#   sudo bash scripts/deploy.sh [--no-restart]
#
# WARNING: restarting kb-hub kills every open web-terminal session. The units use
# systemd's default KillMode=control-group, and the per-user backends (plus their
# login shells) are spawned by the hub and therefore live in its cgroup. Check for
# live work first — `systemctl status kb-hub | sed -n '/CGroup/,$p'` — and pass
# --no-restart if someone is mid-session. kb-syncd, kb-indexer and kb-convert
# contain only themselves, so those restarts are safe; a kb-syncd restart can
# however lose CRDT edits that have not yet been flushed to disk.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

# Defaults match install.sh; /etc/kb/kb.env overrides them if it exists.
PREFIX=/opt/kb-platform
VENV=/opt/kb-venv
if [ -f /etc/kb/kb.env ]; then
  # shellcheck disable=SC1091
  . /etc/kb/kb.env
  PREFIX="${KB_PLATFORM_ROOT:-$PREFIX}"
  VENV="$(dirname "$(dirname "${KB_VENV_PY:-$VENV/bin/python}")")"
else
  echo "WARNING: /etc/kb/kb.env not found — is the platform installed? Using defaults."
fi

echo "== code -> $PREFIX =="
mkdir -p "$PREFIX/frontend"
rsync -a --delete "$SRC/kb_platform" "$PREFIX/"
rsync -a --delete "$SRC/scripts"     "$PREFIX/"
if [ -d "$SRC/frontend/static" ] && [ -n "$(ls -A "$SRC/frontend/static" 2>/dev/null)" ]; then
  rsync -a --delete "$SRC/frontend/static" "$PREFIX/frontend/"
else
  echo "  (no frontend/static — run 'cd frontend && npm install && node build.mjs' first)"
fi
cp "$SRC/requirements.txt" "$PREFIX/" 2>/dev/null || true

echo "== permissions (world-readable code; NOT the session key) =="
chown -R root:root "$PREFIX"
chmod -R a+rX "$PREFIX"

echo "== systemd units =="
CVENV="${VENV%/*}/kb-convert-venv"
cp "$SRC/systemd/kb.conf" /etc/tmpfiles.d/kb.conf
cp "$SRC/systemd/kb-logrotate.conf" /etc/logrotate.d/kb
install -d -m 750 -o root -g root /var/log/kb
for u in "$SRC"/systemd/kb-*.service "$SRC"/systemd/kb-*.timer; do
  [ -e "$u" ] || continue
  # convert-venv first: longest path, must not be chewed by the shorter rules
  sed -e "s|/opt/kb-convert-venv|$CVENV|g" \
      -e "s|/opt/kb-venv|$VENV|g" -e "s|/opt/kb-platform|$PREFIX|g" \
      "$u" > "/etc/systemd/system/$(basename "$u")"
done
systemd-tmpfiles --create /etc/tmpfiles.d/kb.conf
systemctl daemon-reload
# Monitoring, not workloads — always on, and never bounced by a code deploy.
systemctl enable --now kb-heartbeat.timer 2>/dev/null || true
systemctl enable --now kb-maintenance.timer 2>/dev/null || true
systemctl enable --now kb-gitgc.timer 2>/dev/null || true

if [ "$RESTART" -eq 1 ]; then
  echo "== restart =="
  UNITS="kb-syncd kb-hub kb-indexer"
  # kb-convert only exists where its venv was provisioned (scripts/install.sh)
  [ -x "$CVENV/bin/python" ] && UNITS="$UNITS kb-convert"
  # shellcheck disable=SC2086  # UNITS is a deliberate word list
  systemctl restart $UNITS
  sleep 2
  for s in $UNITS; do
    printf '  %-12s %s\n' "$s" "$(systemctl is-active "$s")"
  done
fi

echo "== deploy complete =="
