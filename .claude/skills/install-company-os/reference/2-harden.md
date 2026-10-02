# Phase 2 — Harden the server

**Ollsoft's own VPS runbook**, the one every company box gets, made non-interactive and safe to re-run. Same outcome: key-only SSH on port 2007, no root login, UFW, fail2ban, unattended upgrades with a 03:00 reboot, Docker, 4 GB swap. Two fixes found on the live box are folded in (fail2ban journal, third-party updates).

You are `root` on port 22 with the key from phase 1. `$ADMIN` = the admin username, `$PORT` = 2007 unless the human chose another.

**Ask first:** "Which timezone should the server use? It decides when the nightly 03:00 security reboot and the 07:30 health check run." Default `UTC`.

## 0. The installer's helpers

Four small scripts from this skill's `server/` folder, copied as files — never typed through a quoted command:

| Helper | Does |
|---|---|
| `cos-run` | runs a long command detached, so an SSH drop cannot kill it |
| `cos-keydrop` | the human pastes a secret straight into a root-only file |
| `cos-login` | the human signs in to Company OS on the server (admin API session) |
| `cos-access` | the installer's temporary passwordless sudo, on and off |

```bash
scp <skill>/server/cos-* companyos-root:/usr/local/bin/
ssh companyos-root 'sed -i "s/\r$//" /usr/local/bin/cos-* && chmod 755 /usr/local/bin/cos-*'
```

The `sed` matters on Windows: a clone there can carry CRLF line endings, and bash refuses a script with them. They stay installed after handover, for re-running phases.

Long commands: start with `ssh <host> 'sudo cos-run upgrade apt-get ...'`, poll every 30–60 s with `ssh <host> 'sudo cos-run upgrade'` until it prints `exit 0`. Anything else: read the log, fix, re-run.

## 1. Update, timezone

```bash
timedatectl set-timezone "$TZ"
cos-run upgrade bash -c 'apt-get update && apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade'
```

## 2. Admin user + temporary passwordless sudo

```bash
id "$ADMIN" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$ADMIN"
usermod -aG sudo "$ADMIN"
install -d -m 700 -o "$ADMIN" -g "$ADMIN" /home/$ADMIN/.ssh
install -m 600 -o "$ADMIN" -g "$ADMIN" /root/.ssh/authorized_keys /home/$ADMIN/.ssh/authorized_keys
cos-access on "$ADMIN"     # lets YOU run sudo over non-interactive SSH; off again at handover
```

- **The password is set in phase 3** — it becomes the web login.
- **Verify from local before touching sshd:** `ssh -p 22 -i <key> $ADMIN@<ip> 'sudo -n true && echo ok'` prints `ok`.

## 3. SSH: key-only, port 2007, no root

```bash
cat > /etc/ssh/sshd_config.d/00-hardening.conf <<EOF
Port $PORT
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
Subsystem sftp /usr/lib/openssh/sftp-server -u 0002
EOF
sed -i 's/^PasswordAuthentication yes/#&/' /etc/ssh/sshd_config.d/*.conf
sshd -t && systemctl disable --now ssh.socket && systemctl mask ssh.socket \
  && systemctl enable ssh && systemctl restart ssh
sshd -T | grep -E '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication) '
```

- **`00-` on purpose:** sshd takes the first value it reads; Contabo's `50-cloud-init.conf` ships `PasswordAuthentication yes` and beats any `99-*.conf`.
- **ssh.socket masked:** Ubuntu 24.04 socket-activates SSH pinned to port 22, ignoring `Port`.
- **Keep the root session open.** In a NEW connection from local, all three must hold:
  - `ssh -p $PORT -i <key> $ADMIN@<ip> 'sudo -n true && echo ok'` → `ok`
  - `ssh -p $PORT -o BatchMode=yes -o PubkeyAuthentication=no $ADMIN@<ip>` → exactly `Permission denied (publickey).` — any `password` or `keyboard-interactive` in the list means not hardened
  - `ssh -p $PORT -i <key> root@<ip>` → denied
- If the provider has its own firewall (Hetzner Cloud Firewall), it must allow `$PORT` too.
- **From here on use the `companyos` alias** (admin, port `$PORT`) and `sudo`.

## 4. Firewall

```bash
apt-get install -y ufw
ufw default deny incoming && ufw default allow outgoing
ufw allow $PORT/tcp
ufw --force enable && ufw status verbose
```

- **No 80/443:** Cloudflare Tunnel dials out, so the web needs no open port. Phase 4 opens 80/443 only on the no-Cloudflare path.
- Verify: a fresh `ssh companyos true` still works.

## 5. fail2ban

```bash
apt-get install -y fail2ban
cat > /etc/fail2ban/jail.local <<EOF
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
port = $PORT
journalmatch = _SYSTEMD_UNIT=ssh.service + _COMM=sshd
EOF
systemctl enable --now fail2ban && systemctl restart fail2ban
fail2ban-client status sshd
```

- **`journalmatch` is a fix to the original runbook.** The stock filter watches `sshd.service`; Ubuntu 24.04 logs SSH as `ssh.service`, so without this line the jail counts 0 failures forever (found on the Ollsoft box, 2026-10-01).
- Verify: `Journal matches` shows `ssh.service`; after your password-refusal probe from step 3, `Total failed` is above 0.

## 6. Unattended upgrades, 03:00 reboot

```bash
apt-get install -y unattended-upgrades
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
cat > /etc/apt/apt.conf.d/52company-os-reboot <<'EOF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
// third-party repos the platform adds; Ubuntu-only by default, so these were never patched
Unattended-Upgrade::Origins-Pattern { "origin=cloudflared"; "origin=apt.postgresql.org"; };
EOF
unattended-upgrade --dry-run 2>&1 | tail -3
```

- Tell the human once: **the server may reboot at 03:00 after a kernel update**; open web terminals are lost, documents are not.
- Docker is deliberately not auto-upgraded: a daemon restart bounces the share container.

## 7. Docker, swap, small tools

```bash
apt-get install -y ripgrep jq tmux htop   # rg: the KB's agent instructions tell every agent to use it
rm -f /etc/cron.hourly/free               # Contabo image: drops page cache hourly — slows Postgres
command -v docker >/dev/null || cos-run docker bash -c 'curl -fsSL https://get.docker.com | sh'
usermod -aG docker "$ADMIN"
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf && sysctl --system >/dev/null
```

- Docker is needed for public share links (phase 8). **Never publish a container port on 0.0.0.0**: Docker's rules bypass UFW. Bind `127.0.0.1:`.

## Done when

`sshd -T` shows port `$PORT`, root `no`, password `no` · `ufw status` active with only `$PORT` · `fail2ban-client status sshd` lists the jail · `swapon --show` shows 4G · `docker version` answers.

Then **reboot once** (`ssh companyos 'sudo systemctl reboot'`), wait ~60 s, and `ssh companyos 'uptime; sudo ufw status | head -1'` must answer. It proves SSH, firewall and swap survive a reboot — and the box now runs the kernel the upgrade installed.
