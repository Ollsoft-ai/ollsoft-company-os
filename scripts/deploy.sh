#!/usr/bin/env bash
# Redeploy code to the install prefix after editing it. For development —
# scripts/install.sh does this (and everything else) on a fresh machine.
#
# Reads the paths chosen at install time from /etc/kb/kb.env, so it follows a
# non-default --prefix automatically. Run as root:
#
#   sudo bash scripts/deploy.sh [--no-restart]
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
cp "$SRC/systemd/kb.conf" /etc/tmpfiles.d/kb.conf
for u in "$SRC"/systemd/kb-*.service; do
  sed -e "s|/opt/kb-venv|$VENV|g" -e "s|/opt/kb-platform|$PREFIX|g" \
      "$u" > "/etc/systemd/system/$(basename "$u")"
done
systemd-tmpfiles --create /etc/tmpfiles.d/kb.conf
systemctl daemon-reload

if [ "$RESTART" -eq 1 ]; then
  echo "== restart =="
  systemctl restart kb-syncd kb-hub kb-indexer
  sleep 2
  for s in kb-syncd kb-hub kb-indexer; do
    printf '  %-12s %s\n' "$s" "$(systemctl is-active "$s")"
  done
fi

echo "== deploy complete =="
