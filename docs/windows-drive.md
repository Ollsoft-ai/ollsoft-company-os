# The knowledgebase as a Windows drive

Explorer-native access to the whole of `/srv/kb`: browse it as a drive letter,
open a `.docx` in Word, hit save, done. The platform needs no cooperation from
the Windows side — everything on the box watches the filesystem, so a file
saved over the mount behaves exactly like one written in the web app or by an
agent:

1. Word saves → the bytes land in `/srv/kb/...`
2. `kb-convert` waits for the write to settle, refreshes the hidden text
   sidecar ([converted-documents.md](converted-documents.md))
3. `kb-indexer` re-indexes → search, agents and to-dos see the new content
4. an edited `.md` merges into live editor sessions via `kb-syncd`

The tool for the job is **rclone** (with WinFsp): it speaks SFTP over the SSH
access you already have — no new daemon, no new port — and its local cache is
what makes Office fast over a WAN: files download on first open, then reads
and writes hit local disk, and saves upload in the background.

The mount authenticates as **your Unix account**, so the drive shows exactly
what the kernel lets you see — your private `users/<you>/`, the project
folders you belong to, everything shared — and every file you create is owned
by you. Nothing is re-implemented on the Windows side; this is the same
two-gate model as [remote-access.md](remote-access.md), with SSH as gate 1.

---

## Setup (~10 minutes, all on the Windows PC)

Prerequisite: SSH key access to the box as your platform account. Password
auth should stay off (see [SECURITY.md](SECURITY.md)); if you don't have a key
yet, in PowerShell (OpenSSH ships with Windows 10/11):

```powershell
ssh-keygen -t ed25519
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
# append that line to ~/.ssh/authorized_keys on the box, then confirm:
ssh -p <port> <you>@<your-box> whoami
```

A web-only showcase viewer deliberately has no shell and cannot mount a drive.
Use a named full Company OS account; its Linux permissions become the drive's
permissions.

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

Test: `rclone lsd kb:/srv/kb` should list `company`, `projects`, `users`.

**3. Mount the whole knowledgebase:**

```powershell
rclone mount kb:/srv/kb K: --network-mode --vfs-cache-mode full `
  --vfs-cache-max-size 5G --dir-cache-time 15s `
  --log-file $env:USERPROFILE\rclone-kb.log --log-level INFO
```

`K:` appears in Explorer with the full tree: `company\`, `projects\`,
`users\<you>\`, and the rest.

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
- **Importing folders**: drag any folder tree onto the drive — Explorer copies
  trees natively (the web app can also take a dropped or picked folder), and
  everything converts and indexes as it lands.
- Changes made elsewhere (web app, agents, colleagues) appear in Explorer
  within `--dir-cache-time` (~15 s). SFTP has no change notifications, so
  don't set it much higher.
- **Permissions just work.** Folders your account can't read (other people's
  `users/` dirs, projects you're not in, the root-only `.git`) either don't
  open or come back "access denied" — that's the kernel answering, same as
  everywhere else on the platform.
- **No Office co-authoring.** Simultaneous editing of the same binary is a
  OneDrive/SharePoint protocol feature no mount can provide — the last save
  wins. Different files are fine; live multi-user editing is what the
  platform's own `.md` editor is for.
- Office's transient `~$name.docx` lock files are already ignored by the
  converter and the indexer, and disappear when the document closes. The
  hidden `.name.docx.md` sidecars are visible in Explorer as ordinary files;
  they are read-only and harmless.

## Good to know

- Mounting the repo root means everything you can read — including any
  `_secrets/` you own — gets cached in plaintext on the laptop while in use.
  If the laptop isn't encrypted, consider turning on BitLocker.
- Binaries have no version history (`.gitignore` admits documents and artifacts,
  never binaries): an overwritten `.pptx` is only as recoverable as your backups.
