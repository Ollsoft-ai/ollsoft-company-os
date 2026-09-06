# Connect Company OS as a network drive

> **For named full accounts.** The public `demo` viewer cannot use SSH. Ask an
> administrator for a personal account and register your public SSH key.

The drive uses **SFTP over the existing SSH service**. It is not a VPN or a
tunnel, and it opens no additional server port. Files saved through the drive
land directly in `/srv/kb`, so Company OS converts, indexes and versions them in
the same way as browser or agent changes.

## Windows 10 or 11

### 1. Create and register your SSH key

In PowerShell:

```powershell
ssh-keygen -t ed25519
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
```

Send only the `.pub` line to your Company OS administrator. After it is
registered, test your account:

```powershell
ssh -p 2007 <your-user>@demo.companyos.ollsoft.org whoami
```

Never send anyone the private `id_ed25519` file.

### 2. Install the drive tools

```powershell
winget install WinFsp.WinFsp
winget install Rclone.Rclone
```

WinFsp supplies the Windows filesystem layer used by `rclone mount`.

### 3. Configure Company OS

Create `%APPDATA%\rclone\rclone.conf`:

```ini
[companyos]
type = sftp
host = demo.companyos.ollsoft.org
port = 2007
user = <your-user>
key_file = C:\Users\<YOU>\.ssh\id_ed25519
```

If the key has a passphrase, load it into an SSH agent and configure rclone to
use the agent instead of putting the passphrase in this file. Test the remote:

```powershell
rclone lsd companyos:/srv/kb
```

You should see `company`, `projects` and `users`. Restricted folders are absent
or return access denied by design.

### 4. Mount it as `K:`

```powershell
rclone mount companyos:/srv/kb K: --network-mode --vfs-cache-mode full `
  --vfs-cache-max-size 5G --dir-cache-time 15s --volname "Company OS" `
  --log-file $env:USERPROFILE\rclone-companyos.log --log-level INFO
```

Keep that PowerShell window open while using the drive. For automatic mounting,
create a Windows Task Scheduler task **At log on** with the same `rclone mount`
arguments and enable **Hidden**.

Official references: [rclone SFTP](https://rclone.org/sftp/),
[rclone mount](https://rclone.org/commands/rclone_mount/) and
[WinFsp](https://winfsp.dev/).

## macOS or Linux

Install rclone plus the supported FUSE package for your OS, reuse the same SFTP
remote, create a local folder, and mount it:

```bash
mkdir -p "$HOME/Company OS"
rclone mount companyos:/srv/kb "$HOME/Company OS" \
  --vfs-cache-mode full --dir-cache-time 15s
```

## What to expect

- The drive shows exactly what your Linux user may read and write.
- The first open downloads a file; later reads use the local cache.
- Saves upload in the background and become searchable after indexing.
- Changes from colleagues or agents appear after the directory cache refreshes.
- Office binary files do not support live co-authoring here; simultaneous edits
  are last-save-wins. Use Company OS Markdown for live collaboration.
- The cache can contain company files and your private folder. Encrypt the laptop
  with BitLocker, FileVault or the Linux equivalent.
