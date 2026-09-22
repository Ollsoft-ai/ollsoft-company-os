#!/usr/bin/env bash
# Install the public-link service: the one part of Company OS on the open
# internet. Everything it can reach is created here; it gets nothing else.
#
#   sudo bash scripts/install-public-share.sh            # install / update
#   sudo bash scripts/install-public-share.sh --remove   # take it all away
#
# Safe to re-run. It does NOT touch Cloudflare: the last step is a hostname
# route you add in the dashboard (see docs/public-sharing.md).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${KB_SHARE_PORT:-8402}"
KB_SHARE_BRAND_DEFAULT="${KB_SHARE_BRAND:-Company OS}"
IMAGE="kb-share:1"
PUBROOT="${KB_PUBLIC_ROOT:-/srv/kb-public}"
REPO="${KB_REPO:-/srv/kb}"
STORE_DIR="/var/lib/kb-shares"
USER_NAME="kbshare"
NETWORK="kb-share-net"

[ "$(id -u)" -eq 0 ] || { echo "run me with sudo" >&2; exit 1; }
say() { printf '\n== %s ==\n' "$1"; }

if [ "${1:-}" = "--remove" ]; then
  say "stopping"
  systemctl disable --now kb-share.service kb-share-sweep.timer 2>/dev/null || true
  rm -f /etc/systemd/system/kb-share.service /etc/systemd/system/kb-share-sweep.service \
        /etc/systemd/system/kb-share-sweep.timer
  systemctl daemon-reload
  docker rm -f kb-share >/dev/null 2>&1 || true
  BRIDGE="br-$(docker network inspect -f '{{.Id}}' "$NETWORK" 2>/dev/null | cut -c1-12)"
  if [ "$BRIDGE" != "br-" ]; then
    iptables -D DOCKER-USER -i "$BRIDGE" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
    iptables -D DOCKER-USER -i "$BRIDGE" -j DROP 2>/dev/null || true
  fi
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  rm -f /etc/kb/kb-share-firewall.sh
  say "unmounting every share"
  python3 - <<'PY'
import sys
sys.path.insert(0, "/opt/kb-platform")
from kb_platform import publicshare
for row in publicshare.listing():
    publicshare.revoke(row["id"])
    print("  revoked", row["id"])
PY
  echo "left in place: $PUBROOT, $STORE_DIR and the $USER_NAME account (remove by hand if you mean it)"
  exit 0
fi

say "the account the container runs as"
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  # A system account with no shell, no home and no groups: the only thing it
  # will ever be able to touch is what a share's ACL lets it.
  adduser --system --no-create-home --group --shell /usr/sbin/nologin "$USER_NAME"
fi
SHARE_UID="$(id -u "$USER_NAME")"
SHARE_GID="$(id -g "$USER_NAME")"
echo "  $USER_NAME = $SHARE_UID:$SHARE_GID"

say "directories"
install -d -m 0755 -o root -g root "$PUBROOT"
install -d -m 0755 -o root -g root "$PUBROOT/data"
install -d -m 0750 -o root -g "$USER_NAME" "$PUBROOT/conf"
install -d -m 0700 -o root -g root "$STORE_DIR"
echo "  $PUBROOT/{data,conf}, $STORE_DIR"

say "image"
docker build -q -t "$IMAGE" "$SRC/public" | sed 's/^/  /'

say "network"
# Its own bridge, and two rules in DOCKER-USER (the chain Docker leaves for
# exactly this): answers to requests are allowed, anything the container
# STARTS is dropped. It needs no outbound at all — not to the internet, not
# to the host's own services — and a compromise that cannot dial out is a
# compromise that cannot take anything with it.
docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
BRIDGE="br-$(docker network inspect -f '{{.Id}}' "$NETWORK" | cut -c1-12)"
iptables -D DOCKER-USER -i "$BRIDGE" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
iptables -D DOCKER-USER -i "$BRIDGE" -j DROP 2>/dev/null || true
iptables -I DOCKER-USER 1 -i "$BRIDGE" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -I DOCKER-USER 2 -i "$BRIDGE" -j DROP
install -d -m 0755 /etc/kb
cat > /etc/kb/kb-share-firewall.sh <<FW
#!/bin/sh
# Re-applied at every start: iptables rules do not survive a reboot.
BRIDGE="\$1"
iptables -D DOCKER-USER -i "\$BRIDGE" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
iptables -D DOCKER-USER -i "\$BRIDGE" -j DROP 2>/dev/null || true
iptables -I DOCKER-USER 1 -i "\$BRIDGE" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -I DOCKER-USER 2 -i "\$BRIDGE" -j DROP
FW
chmod 0755 /etc/kb/kb-share-firewall.sh
echo "  $NETWORK ($BRIDGE): inbound answers only, no egress"

say "units"
cat > /etc/systemd/system/kb-share.service <<UNIT
[Unit]
Description=kb-share — public links, in a container with nothing else in it
After=docker.service network-online.target
Requires=docker.service

[Service]
Restart=always
RestartSec=3
# systemd does not expand shell defaults, and "Company OS" has a space in it:
# set it here and pass the NAME to docker, which reads the value from us.
Environment="KB_SHARE_BRAND=${KB_SHARE_BRAND_DEFAULT}"
ExecStartPre=-/usr/bin/docker rm -f kb-share
ExecStartPre=/bin/sh /etc/kb/kb-share-firewall.sh ${BRIDGE}
# Everything after --rm is a lock: no root, no capabilities, no writable
# filesystem, no new privileges, no route to the host's services, and only
# the two directories the platform fills. /data is rw so an "edit" share can
# be written; each individual share is bind-mounted read-only unless it may
# be edited, and the file's ACL is the second lock.
ExecStart=/usr/bin/docker run --rm --name kb-share \\
  --user ${SHARE_UID}:${SHARE_GID} \\
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \\
  --cap-drop ALL --security-opt no-new-privileges \\
  --pids-limit 200 --memory 256m --cpus 0.5 \\
  --network ${NETWORK} \\
  --publish 127.0.0.1:${PORT}:8080 \\
  --mount type=bind,source=${PUBROOT}/conf,target=/conf,readonly \\
  --mount type=bind,source=${PUBROOT}/data,target=/data,bind-propagation=rslave \\
  --env KB_SHARE_BRAND \\
  ${IMAGE}
ExecStop=/usr/bin/docker stop kb-share

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/kb-share-sweep.service <<UNIT
[Unit]
Description=Take down expired public links, put back the ones a reboot dropped

[Service]
Type=oneshot
# The same environment every other kb unit carries. Without PYTHONPATH this
# is a ModuleNotFoundError every fifteen minutes, which is exactly how it
# shipped (caught by the maintenance report the next morning, 2026-09-22).
EnvironmentFile=-/etc/kb/kb.env
Environment=PYTHONPATH=/opt/kb-platform
Environment=KB_REPO=${REPO:-/srv/kb}
Environment=KB_RUN=/run/kb
Environment=KB_ETC=/etc/kb
ExecStart=/opt/kb-venv/bin/python -m kb_platform.publicshare
UNIT

cat > /etc/systemd/system/kb-share-sweep.timer <<UNIT
[Unit]
Description=Sweep public links every 15 minutes

[Timer]
OnBootSec=30s
OnUnitActiveSec=15min
AccuracySec=1min

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now kb-share.service kb-share-sweep.timer
sleep 2
systemctl is-active kb-share.service >/dev/null && echo "  kb-share is up on 127.0.0.1:${PORT}"

say "what is left for you"
cat <<NEXT
  1. In Cloudflare Zero Trust → Networks → Tunnels → this tunnel → Public
     hostnames, add:  share.<your domain>  →  http://127.0.0.1:${PORT}
  2. Leave it OUT of any Access policy: the people who use these links have
     no account here. Keep WAF and rate limiting ON for that hostname.
  3. Tell the platform the address so the links it hands out are complete:
       echo 'KB_SHARE_BASE=https://share.<your domain>' >> /etc/kb/kb.env
       systemctl restart kb-hub
NEXT
