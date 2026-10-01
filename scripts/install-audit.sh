#!/usr/bin/env bash
# Install the host audit trail: auditd, the Company OS rules
# (scripts/kb-audit.rules) and the daily digest (scripts/kb-audit-digest).
# The rules name no person, so everyone is covered — including accounts
# created after this runs — and nothing needs re-running when people change.
#
#   sudo bash scripts/install-audit.sh                      # install / update
#   sudo bash scripts/install-audit.sh --watch <folder>     # + a sensitive folder (repeatable)
#   sudo bash scripts/install-audit.sh --reader <user>      # <user> may run the digest via sudo
#   sudo bash scripts/install-audit.sh --remove
#
# Safe to re-run. --watch and --reader add to what is there; they never forget.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "run me with sudo" >&2; exit 1; }
[ -f /etc/kb/kb.env ] && . /etc/kb/kb.env
REPO="${KB_REPO:-/srv/kb}"
RULES=/etc/audit/rules.d/50-kb-security.rules
WATCH_LIST=/etc/kb/audit-watch.list
DIGEST=/usr/local/sbin/kb-audit-digest
SUDOERS=/etc/sudoers.d/kb-audit-digest
KEYS='denied_file_access|kb_cross_user_access|kb_os_config_change|kb_sensitive_access|privilege_tool|account_tool|permission_tool|identity_config|privilege_config|ssh_config|audit_config'
say() { printf '\n== %s ==\n' "$1"; }

WATCH=() READERS=() REMOVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --watch)   WATCH+=("${2:?--watch needs a folder}"); shift 2 ;;
    --reader)  READERS+=("${2:?--reader needs a user}"); shift 2 ;;
    --remove)  REMOVE=1; shift ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ "$REMOVE" = 1 ]; then
  rm -f "$RULES" "$DIGEST" "$SUDOERS"
  augenrules --load >/dev/null 2>&1 || true
  echo "removed the rules, the digest and its sudoers entry; auditd and $WATCH_LIST are left"
  exit 0
fi

say "auditd"
command -v auditctl >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y auditd
systemctl enable --now auditd >/dev/null

say "rules"
install -d -m 0755 /etc/kb
[ -f "$WATCH_LIST" ] || install -m 0640 /dev/null "$WATCH_LIST"
for p in "${WATCH[@]}"; do
  case "$p" in /*) ;; *) echo "--watch needs an absolute path: $p" >&2; exit 2 ;; esac
  [ -e "$p" ] || { echo "no such folder: $p" >&2; exit 2; }
  grep -qxF "$p" "$WATCH_LIST" || echo "$p" >> "$WATCH_LIST"
done
tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
sed "s|@REPO@|$REPO|g" "$SRC/scripts/kb-audit.rules" > "$tmp"
while IFS= read -r p; do
  case "$p" in /*) ;; *) continue ;; esac
  [ -e "$p" ] || { echo "  skipping $p — it no longer exists"; continue; }
  # -F dir= cannot carry a space; an escaped -w watch can.
  echo "-w ${p// /\\ } -p wa -k kb_sensitive_access" >> "$tmp"
  echo "  watching changes in $p"
done < "$WATCH_LIST"
if [ -f "$RULES" ] && ! grep -q "Installed by scripts/install-audit.sh" "$RULES"; then
  cp -p "$RULES" "/var/backups/50-kb-security.rules.$(date +%F-%H%M%S)"
  echo "  replaced a hand-written $RULES (the old one is in /var/backups)"
fi
install -m 0640 -o root -g root "$tmp" "$RULES"
augenrules --load >/dev/null
expected=$(grep -cE -- "-k ($KEYS)\$" "$RULES")
loaded=$(auditctl -l | grep -cE "key=($KEYS)|-k ($KEYS)" || true)
[ "$loaded" -eq "$expected" ] || {
  echo "!! $loaded of $expected rules loaded — run: auditctl -R $RULES" >&2; exit 1; }
echo "  $loaded rules loaded"

say "digest"
install -m 0755 -o root -g root "$SRC/scripts/kb-audit-digest" "$DIGEST"
if [ ${#READERS[@]} -gt 0 ]; then
  [ -f "$SUDOERS" ] && mapfile -t -O "${#READERS[@]}" READERS < <(awk '/NOPASSWD/ {print $1}' "$SUDOERS")
  {
    echo "# Installed by scripts/install-audit.sh: these people may run the audit digest"
    echo "# as root (a scheduled security brief) and nothing else."
    for u in $(printf '%s\n' "${READERS[@]}" | sort -u); do
      id "$u" >/dev/null 2>&1 || { echo "no such user: $u" >&2; exit 2; }
      echo "$u ALL=(root) NOPASSWD: $DIGEST"
    done
  } > "$tmp"
  visudo -cf "$tmp" >/dev/null
  install -m 0440 -o root -g root "$tmp" "$SUDOERS"
  echo "  readers: $(awk '/NOPASSWD/ {print $1}' "$SUDOERS" | paste -sd' ')"
fi
"$DIGEST" --since "$(date -d '-5 min' -Iseconds)" --until "$(date -Iseconds)" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["ok"], d; print("  digest answers")'

cat <<DONE

Host audit trail is on. It records, for every person (uid >= 1000), web and SSH alike:
  refused file opens · someone else's private users/ folder · direct writes to .os
  · sudo/su/passwd/account/permission tools · identity, sudoers, SSH and audit config
  · changes in the folders listed in $WATCH_LIST
Daily summary:  sudo $DIGEST --since <ISO> --until <ISO>
Raw record:     sudo ausearch -k <key> -i
DONE
