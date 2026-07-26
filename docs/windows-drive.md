# The knowledgebase as a Windows drive

Explorer-native access to `/srv/kb`: browse the tree as a drive letter, open a
`.docx` in Word, hit save, done. The platform needs no cooperation from the
Windows side — everything on the box watches the filesystem, so a file saved
over a mount behaves exactly like one written in the web app or by an agent:

1. Word saves → the bytes land in `/srv/kb/...`
2. `kb-convert` waits for the write to settle, refreshes the hidden text
   sidecar ([converted-documents.md](converted-documents.md))
3. `kb-indexer` re-indexes → search, agents and to-dos see the new content
4. an edited `.md` merges into live editor sessions via `kb-syncd`

The mount authenticates as **your Unix account**, so the drive shows exactly
what the kernel lets you see and every file you create is owned by you. No
permission logic is re-implemented on the Windows side — this is the same
two-gate model as [remote-access.md](remote-access.md), with SSH as gate 1.

---

## Pick your architecture first

"Files in my Explorer" covers two different designs:

| | Model | Office feel | Offline | Server changes |
|---|---|---|---|---|
| SSHFS-Win | live drive over SSH | poor — Office's small reads + lock files cross the WAN on every open/save | ✗ | none |
| **rclone mount** | live drive + local VFS cache | good — download on first open, then local-disk speed; uploads on close | cached files only | none |
| Samba over a private overlay (Tailscale, WireGuard…) | true SMB drive | best — native locking, "file in use" dialogs | ✗ | new daemon; **never** expose 445 publicly |
| Syncthing | background sync (the OneDrive model) | perfect — files are local | ✓ | daemon on the box running as the user |

Recommendation: **rclone mount** for an individual — zero server changes and
the VFS cache is what makes Office tolerable over a WAN. Samba behind an
overlay network is the right answer when the whole *team* should get a drive
(per-user credentials, no SSH keys for non-technical colleagues). Syncthing if
you want offline work and accept a second copy of company data on the laptop —
its server-side file versioning also patches the platform's one history gap
(binaries aren't in git, only `.md`/`.html` are).

Skip plain SSHFS-Win: same transport and auth as rclone, no cache, and a
dropped connection can wedge Explorer.

---

## Setup: rclone mount (~10 minutes, all on the Windows PC)

Prerequisite: SSH key access to the box as your platform account. Password
auth should stay off (see [SECURITY.md](SECURITY.md)); if you don't have a key
yet, in PowerShell (OpenSSH ships with Windows 10/11):

```powershell
ssh-keygen -t ed25519
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
# append that line to ~/.ssh/authorized_keys on the box, then confirm:
ssh -p <port> <you>@<your-box> whoami
```

**1. Install WinFsp and rclone:**

```powershell
winget install WinFsp.WinFsp
winget install Rclone.Rclone
```

**2. Configure the remote** — `%APPDATA%\rclone\rclone.conf`:

```ini
[kb]
type = sftp
host = <your-box>
port = <port>
user = <you>
key_file = C:\Users\<YOU>\.ssh\id_ed25519
```

Test: `rclone lsd kb:/srv/kb/company` should list the shared folders.

**3. Mount:**

```powershell
rclone mount kb:/srv/kb/company K: --network-mode --vfs-cache-mode full `
  --vfs-cache-max-size 5G --dir-cache-time 15s `
  --log-file $env:USERPROFILE\rclone-kb.log --log-level INFO
```

`K:` appears in Explorer rooted at `company/`.

**4. Make it permanent** — Task Scheduler → Create Task:

- Trigger: *At log on*
- Action: `rclone` with the same `mount ...` arguments as above
- Enable *Run whether user is logged on or not* + *Hidden*

---

## What to expect

- **Open** a document: the first open downloads it (a beat on large files),
  after that it is cached and instant. **Save**: writes land locally and
  upload a few seconds after the application closes the file; the sidecar and
  index refresh a couple of seconds later. Ctrl+S to searchable ≈ 10 s.
- **Importing folders**: drag any folder tree onto the drive — the browser
  upload only takes individual files, but Explorer copies trees natively, and
  everything converts and indexes as it lands.
- Changes made elsewhere (web app, agents, colleagues) appear in Explorer
  within `--dir-cache-time` (~15 s). SFTP has no change notifications, so
  don't set it much higher.
- **No Office co-authoring.** Simultaneous editing of the same binary is a
  OneDrive/SharePoint protocol feature no mount can provide — the last save
  wins. Different files are fine; live multi-user editing is what the
  platform's own `.md` editor is for.
- Office's transient `~$name.docx` lock files are already ignored by the
  converter and the indexer, and disappear when the document closes.

## Scope and safety

- **Mount `company/` (and project folders), not the repo root.** Your private
  `users/<you>/` and any `_secrets/` then never get cached onto the laptop.
  The kernel would enforce access either way — this is about where plaintext
  copies of sensitive bytes end up.
- Remember binaries have no version history (`.gitignore` admits only
  `.md`/`.html`): an overwritten `.pptx` is only as recoverable as your
  backups.
- If you outgrow this and reach for Samba: bind it to the overlay interface
  only. SMB on a public address is one of the most-scanned attack surfaces on
  the internet.
