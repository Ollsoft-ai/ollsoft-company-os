#!/usr/bin/env bash
# Seed the dedicated Company OS showcase with a fictional German company.
set -euo pipefail

REPO=/srv/kb
ADMIN=""
REFRESH=0
UNDO=0
MEMBERS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO=${2:?missing value for --repo}; shift 2 ;;
    --admin) ADMIN=${2:?missing value for --admin}; shift 2 ;;
    --member) MEMBERS+=("${2:?missing value for --member}"); shift 2 ;;
    --refresh) REFRESH=1; shift ;;
    --undo) UNDO=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -n "$ADMIN" ]] || { echo "--admin is required" >&2; exit 2; }
[[ "$REPO" == /* && "$REPO" != / && -d "$REPO/company" ]] || {
  echo "invalid Company OS repo: $REPO" >&2; exit 1; }
id "$ADMIN" >/dev/null 2>&1 || { echo "admin user not found: $ADMIN" >&2; exit 1; }
for member in "${MEMBERS[@]}"; do
  id "$member" >/dev/null 2>&1 || { echo "member not found: $member" >&2; exit 1; }
done

SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TEMPLATES="$SRC/showcase/kb"
STATE=/var/lib/company-os-showcase
CREDS=/root/ollsoft-company-os-showcase.txt
VIEWER=demo
PUBLIC_GROUP=proj-polaris
PRIVATE_GROUP=proj-helios

if [[ $UNDO -eq 1 ]]; then
  [[ -f "$STATE/installed" ]] || { echo "showcase is not recorded as installed" >&2; exit 1; }
  for path in \
    "$REPO/company/00 START HERE.md" "$REPO/company/README.md" \
    "$REPO/company/media" "$REPO/company/handbook" \
    "$REPO/company/quality" "$REPO/company/sales" \
    "$REPO/company/finance" "$REPO/company/dashboards" \
    "$REPO/projects/polaris-energy-gateway" \
    "$REPO/projects/helios-confidential"; do
    [[ "$path" == "$REPO/"* ]] || exit 1
    rm -rf -- "$path"
  done
  if [[ -f "$STATE/launchers.before.json" ]]; then
    install -o root -g kb-users -m 644 "$STATE/launchers.before.json" "$REPO/.os/launchers.json"
  fi
  if [[ -f "$STATE/viewer-created" ]] && id "$VIEWER" >/dev/null 2>&1; then
    pkill -KILL -u "$VIEWER" 2>/dev/null || true
    runuser -u postgres -- psql -d kb -v ON_ERROR_STOP=1 -q <<SQL
DROP OWNED BY "$VIEWER" CASCADE;
DROP ROLE IF EXISTS "$VIEWER";
SQL
    userdel -r "$VIEWER"
  fi
  rm -f -- "$CREDS"
  rm -rf -- "$STATE"
  systemctl restart kb-indexer kb-syncd
  echo "showcase removed"
  exit 0
fi

[[ -d "$TEMPLATES/company" && -d "$TEMPLATES/projects" ]] || {
  echo "showcase templates are missing: $TEMPLATES" >&2; exit 1; }

[[ -d "$REPO/.os" ]] || { echo "run scripts/install.sh first ($REPO/.os is missing)" >&2; exit 1; }
install -d -o root -g root -m 700 "$STATE"
if [[ ! -f "$STATE/installed" ]]; then
  install -o root -g root -m 600 "$REPO/.os/launchers.json" "$STATE/launchers.before.json"
fi

# v1 placed artifact state and screenshots in ordinary visible paths. Remove
# only those exact legacy paths on refresh; their replacements follow Company
# OS conventions below (`.state.json` and `_files/`).
if [[ $REFRESH -eq 1 ]]; then
  for legacy in \
    "$REPO/company/dashboards/delivery-board.json" \
    "$REPO/company/dashboards/.delivery-board.json" \
    "$REPO/company/finance/invoice-data.json" \
    "$REPO/company/quality/risk-data.json" \
    "$REPO/company/sales/pipeline-data.json"; do
    [[ "$legacy" == "$REPO/company/"* ]] || exit 1
    rm -f -- "$legacy"
  done
  if [[ -d "$REPO/company/media" ]]; then
    [[ "$REPO/company/media" == "$REPO/company/"* ]] || exit 1
    rm -rf -- "$REPO/company/media"
  fi
fi

groupadd -f "$PUBLIC_GROUP"
groupadd -f "$PRIVATE_GROUP"
usermod -aG kb-users,"$PUBLIC_GROUP","$PRIVATE_GROUP" "$ADMIN"
for member in "${MEMBERS[@]}"; do
  usermod -aG kb-users,"$PUBLIC_GROUP" "$member"
done
usermod -aG "$PUBLIC_GROUP","$PRIVATE_GROUP" kbindexer

if ! id "$VIEWER" >/dev/null 2>&1; then
  useradd -m -s /usr/sbin/nologin "$VIEWER"
  touch "$STATE/viewer-created"
fi
echo "$VIEWER:companyos" | chpasswd
usermod -aG kb-users,"$PUBLIC_GROUP" "$VIEWER"
install -d -o "$VIEWER" -g "$VIEWER" -m 700 "$REPO/users/$VIEWER"

runuser -u postgres -- psql -d kb -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$VIEWER') THEN CREATE ROLE "$VIEWER" LOGIN; END IF;
END \$\$;
GRANT kb_users TO "$VIEWER";
CREATE SCHEMA IF NOT EXISTS "u_$VIEWER" AUTHORIZATION "$VIEWER";
ALTER ROLE "$VIEWER" SET search_path = "u_$VIEWER", kb, public;
SQL

umask 077
printf 'COMPANY_OS_SHOWCASE_USERNAME=%s\nCOMPANY_OS_SHOWCASE_PASSWORD=companyos\n' "$VIEWER" > "$CREDS"
chmod 600 "$CREDS"

copy_tree() {
  local src=$1 dst=$2 owner=$3 group=$4 dmode=$5 fmode=$6
  [[ -d "$dst" ]] || install -d -o "$owner" -g "$group" -m "$dmode" "$dst"
  if [[ $REFRESH -eq 1 ]]; then
    rsync -a --chown="$owner:$group" --chmod="D$dmode,F$fmode" "$src/" "$dst/"
  else
    rsync -a --ignore-existing --chown="$owner:$group" --chmod="D$dmode,F$fmode" "$src/" "$dst/"
  fi
}

copy_tree "$TEMPLATES/company" "$REPO/company" "$ADMIN" kb-users 2775 0664
copy_tree "$TEMPLATES/projects/polaris-energy-gateway" "$REPO/projects/polaris-energy-gateway" "$ADMIN" "$PUBLIC_GROUP" 2770 0660
copy_tree "$TEMPLATES/projects/helios-confidential" "$REPO/projects/helios-confidential" "$ADMIN" "$PRIVATE_GROUP" 2770 0660
setfacl -d -m u::rwx,g::rwx,o::rx "$REPO/company"
setfacl -d -m u::rwx,g::rwx,o::- "$REPO/projects/polaris-energy-gateway"
setfacl -d -m u::rwx,g::rwx,o::- "$REPO/projects/helios-confidential"
install -o root -g kb-users -m 644 "$SRC/showcase/launchers.json" "$REPO/.os/launchers.json"

touch "$STATE/installed"
systemctl restart kb-indexer
echo "showcase installed; demo credentials: $CREDS"
