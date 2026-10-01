# Phase 1 — Rent the server, get root

## Recommend a provider

| | Hetzner Cloud | Contabo |
|---|---|---|
| **Pick it when** | default choice — fast, clean API, hourly billing | the most RAM and disk per euro matters more than polish |
| Setup time | ~1 minute | minutes to a few hours |
| SSH key at creation | yes | depends on the order form; otherwise a root password |
| Provider backups | one checkbox (paid extra) | paid add-on |

- **Size:** minimum **2 vCPU / 4 GB / 40 GB**. Recommend **4 vCPU / 8 GB / 80 GB+** if people will use AI agents in the browser — agent processes and document conversion are what eat RAM.
- **Image: Ubuntu 24.04 LTS.** Nothing else is supported (it needs real Linux users, PAM and systemd — no containers).
- **Location:** close to the team. **Keep IPv4 on** (Hetzner charges for it separately; some networks have no IPv6).
- Quote no prices from memory; the human sees them in the order form.

## Make the SSH key (you, locally)

```bash
ssh-keygen -t ed25519 -N "" -C "companyos" -f ~/.ssh/companyos_ed25519   # skip if it exists
cat ~/.ssh/companyos_ed25519.pub
```

- No passphrase, so you can drive SSH unattended. Tell the human they can add one later with `ssh-keygen -p` plus ssh-agent.
- Windows: same commands in Git Bash or PowerShell (`$env:USERPROFILE\.ssh\…` in PowerShell).
- If they install more than one box, suffix key and alias with the company slug.

## Ask the human to create the VM

Give them this, adapted to the provider they picked:

1. Create a server: **Ubuntu 24.04**, the size above, a nearby location.
2. **SSH key:** paste the public key you just printed. If the form only takes a root password, set a strong one — they will type it once.
3. Optional: tick **Backups** if offered (phase 8 covers backups properly).
4. Send back: **the IPv4 address**, and whether they added the key or set a password.

## Get key-based root access

- **Key added at creation:** `ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -i ~/.ssh/companyos_ed25519 root@<ip> true`
- **Password only:** you cannot type passwords. Have the human run, in their own terminal:
  - macOS/Linux: `ssh-copy-id -i ~/.ssh/companyos_ed25519.pub root@<ip>`
  - Windows PowerShell: `type $env:USERPROFILE\.ssh\companyos_ed25519.pub | ssh root@<ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"`
  - If the provider forces a root password change on first login, they do it, then rerun the command.

Then add the local alias you will use until hardening moves SSH:

```
Host companyos-root
  HostName <ip>
  User root
  IdentityFile ~/.ssh/companyos_ed25519
  IdentitiesOnly yes
  ServerAliveInterval 30
```

## Done when

`ssh companyos-root 'lsb_release -ds; nproc; free -g | awk "/Mem/{print \$2}"; df -h / | tail -1; test -d /run/systemd/system && echo systemd'` shows **Ubuntu 24.04**, the CPUs and RAM ordered, and `systemd`. Anything else: stop and have them reinstall the image.
