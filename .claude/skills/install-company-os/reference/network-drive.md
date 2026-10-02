# Network drive — Company OS in Explorer, Finder or the file manager

The whole `/srv/kb` as a drive on the person's own computer: open a `.docx` in real Word, save, and seconds later it is searchable in Company OS; drag folders in to import them. It is **rclone over SFTP**, using the SSH key from phase 1, so the drive shows exactly what their account may see.

**Ask:** "Do you want Company OS as a drive on this computer — open and save Word and Excel files straight into it, drag folders in?"

## Detect the computer — don't ask

You run on it: `uname -s` → `Linux`, `Darwin` (macOS), or `MINGW*`/`MSYS*`/`CYGWIN*` (Windows, Git Bash); in PowerShell `$env:OS` is `Windows_NT`. Tell the human what you found, then run the steps yourself with their OK. Installers that need admin (WinFsp, apt) show them a prompt to confirm.

**rclone, not the OS's own "map network drive" or SSHFS:** Office rewrites a file hundreds of times while you type; rclone caches locally and uploads once on close, so Word stays fast over the internet.

## The connection — every OS

rclone does not read `~/.ssh/config`, so host and port are repeated:

```bash
rclone config create CompanyOS sftp host <ip> port 2007 user <admin> key_file <home>/.ssh/companyos_ed25519 shell_type unix
rclone lsd CompanyOS:/srv/kb        # must list company, projects, users
```

## Windows — drive Z:

1. `winget install WinFsp.WinFsp` and `winget install Rclone.Rclone` (WinFsp asks for admin; reboot if Z: does not appear later). Then the connection above, `key_file "$env:USERPROFILE\.ssh\companyos_ed25519"`.
2. Make it survive reboots: save as `%TEMP%\companyos-drive.ps1` and run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\companyos-drive.ps1"`. Two details are load-bearing — the trailing **`True`** in `launch.vbs` (wait for the process, or Task Scheduler loses track of the mount loop) and the **repeating trigger** (a laptop that sleeps instead of rebooting gets no logon for weeks).

```powershell
$dir = "$env:LOCALAPPDATA\rclone"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Stop-ScheduledTask -TaskName "CompanyOS drive" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='wscript.exe'" |
  Where-Object { $_.CommandLine -like '*launch.vbs*' -and $_.ProcessId -ne $PID } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -like '*mount-companyos.ps1*' -and $_.ProcessId -ne $PID } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Stop-Process -Name rclone -Force -ErrorAction SilentlyContinue

@'
$dir = "$env:LOCALAPPDATA\rclone"
while ($true) {
    Get-Process rclone -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
    & rclone mount CompanyOS:/srv/kb Z: `
        --config "$env:APPDATA\rclone\rclone.conf" `
        --vfs-cache-mode full --vfs-write-back 5s --dir-cache-time 30s `
        --network-mode --no-console --volname CompanyOS --skip-links `
        --vfs-cache-max-age 168h --vfs-cache-max-size 20G `
        --timeout 30s --contimeout 15s --low-level-retries 20 `
        --log-file "$dir\mount.log" --log-level INFO
    Start-Sleep -Seconds 10
}
'@ | Set-Content -Path "$dir\mount-companyos.ps1" -Encoding UTF8

@"
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""$dir\mount-companyos.ps1""", 0, True
"@ | Set-Content -Path "$dir\launch.vbs" -Encoding ASCII

$act = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$dir\launch.vbs`""
$trgLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trgLogon.Delay = "PT20S"
$trgRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -DontStopOnIdleEnd -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "CompanyOS drive" -Action $act -Trigger @($trgLogon, $trgRepeat) `
  -Settings $set -RunLevel Limited -Force
Start-ScheduledTask -TaskName "CompanyOS drive"
```

3. Verify a minute later: `(Get-ScheduledTask -TaskName "CompanyOS drive").State` is **`Running`** (`Ready` = the orphan-loop bug), and Z: shows in Explorer.

## macOS — ~/CompanyOS in Finder

1. `brew install rclone`, then the connection above with `key_file ~/.ssh/companyos_ed25519`. **Homebrew's rclone has no `mount`** (it would need macFUSE); `rclone nfsmount` uses macOS's own NFS client — no kernel extension, no sudo.
2. Make it start at login — `~/Library/LaunchAgents/com.companyos.drive.plist`, with `$(brew --prefix)/bin/rclone` and their home filled in:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.companyos.drive</string>
  <key>ProgramArguments</key><array>
    <string>/opt/homebrew/bin/rclone</string><string>nfsmount</string>
    <string>CompanyOS:/srv/kb</string><string>/Users/ME/CompanyOS</string>
    <string>--vfs-cache-mode</string><string>full</string>
    <string>--vfs-write-back</string><string>5s</string>
    <string>--dir-cache-time</string><string>30s</string>
    <string>--log-file</string><string>/Users/ME/Library/Logs/companyos-drive.log</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

```bash
mkdir -p ~/CompanyOS && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.companyos.drive.plist
```

3. Verify: `ls ~/CompanyOS` lists `company projects users`. Tell them to drag the folder into Finder's sidebar. Stuck after a network change: `umount -f ~/CompanyOS` — launchd remounts it.

## Linux — ~/CompanyOS

1. `sudo apt install -y fuse3` and current rclone (`curl -fsSL https://rclone.org/install.sh | sudo bash` — distro packages lag), then the connection above.
2. `~/.config/systemd/user/companyos-drive.service`:

```ini
[Unit]
Description=Company OS drive (rclone)
Wants=network-online.target
After=network-online.target

[Service]
Type=notify
ExecStartPre=/usr/bin/mkdir -p %h/CompanyOS
ExecStart=/usr/bin/rclone mount CompanyOS:/srv/kb %h/CompanyOS --vfs-cache-mode full --vfs-write-back 5s --dir-cache-time 30s
ExecStop=/bin/fusermount3 -u %h/CompanyOS
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now companyos-drive
```

3. Verify: `ls ~/CompanyOS` lists `company projects users`.

## Good to know — tell them once

- **Last save wins** on the same Office file: no co-authoring over a drive. Live multi-person editing is what the web editor is for.
- Files they open are **cached unencrypted on the laptop**: BitLocker or FileVault should be on.
- Changes made elsewhere show up within ~30 s.

## Everyone else

Each colleague mounts their own drive as themselves, and needs **their own SSH key** first — the server accepts keys only. So the starter pack (phase 7) writes `company/onboarding.md` with, in order: make a key (`ssh-keygen -t ed25519 -f ~/.ssh/companyos`), paste the public key into `~/.ssh/authorized_keys` from the Company OS web terminal, add a `Host companyos` profile with this server's IP and port 2007, test `ssh companyos whoami` (no password prompt), then this file's steps for their OS. Web-only accounts have no shell, so they cannot mount a drive.
