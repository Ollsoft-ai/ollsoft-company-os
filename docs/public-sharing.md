# Public sharing — a separate, isolated service (DESIGNED, NOT BUILT)

A folder or a file, handed to someone who has no account here: a client, a
lawyer, a candidate. They open a link, they see it, and — if the share says so
— they can edit it. No Cloudflare Access login, because they have nothing to
log in with.

That single sentence puts a door on the internet, so the design is mostly
about where the door leads. **The public service is a separate container that
can only ever see what has been placed in front of it.** It has no database,
no session key, no `/srv/kb`, no Docker socket and no route to anything else
on the box. A total compromise of it exposes the shares that were live at that
moment, and nothing else.

## The shape

```
                    Cloudflare (TLS, WAF, rate limits, NO Access policy)
                                     │  share.<domain>
                                     ▼
                    ┌───────────────────────────────────┐
                    │ container kb-share                │
                    │  user 10001, read-only rootfs     │
                    │  cap-drop ALL, no-new-privileges  │
                    │  /conf  (ro)  one json per share  │
                    │  /data  per-share bind mounts     │
                    └───────────────────────────────────┘
                                     ▲
             root, on the host: mounts, conf, expiry sweep
                                     │
/srv/kb/company/plans  ──bind ro──►  /srv/kb-public/data/<id>
```

Nothing inside the container knows the word `/srv/kb`. It resolves a share id
to `/data/<id>` and serves what is under it. Path traversal reaches a bind
mount's root and stops.

## Making a share

In the app: right-click a file or folder → **Share publicly…**

| Choice | Default |
|---|---|
| Who | anyone with the link · anyone with the link and the password |
| What | view · view and edit |
| For how long | **14 days** (max 90) |
| Title | the file's name |

The hub (root) then, in this order:

1. `id` = 16 random bytes base32; `token` = 32 random bytes.
2. `mkdir /srv/kb-public/data/<id>` and `mount --bind` the target onto it,
   remounted `ro` unless the share is editable.
3. Write `/srv/kb-public/conf/<id>.json` (root:kbshare 0640): mode, argon2id
   password hash, expiry, title, token hash. **Never the real path.**
4. Record the share in `/var/lib/kb-shares/shares.json` (root 0600): real
   path, creator, both hashes, expiry, audit line.
5. For an editable share, add one ACL entry granting the host user `kbshare`
   write on exactly that subtree; remove it when the share ends.

The container watches `/conf` with inotify. No restart, no privileged call
from inside, no way for it to create a share of its own.

**Revocation is an unmount.** Deleting a share removes the bind mount, so a
leaked token reaches an empty directory a second later — no cache, no
"eventually consistent" window.

## The URL and the password

`https://share.<domain>/s/<id>/<token>`

- `Referrer-Policy: no-referrer`, so the token does not travel to sites the
  document links to.
- A password share shows a form first, then sets a cookie signed with a key
  generated at container start (in tmpfs, gone on restart) and scoped to that
  share id. Argon2id, and a lockout that is per share AND per IP (10 attempts
  a minute, then exponential), with Cloudflare rate limiting in front of it.
- The token is checked in constant time. A wrong id and a wrong token give the
  same 404 after the same delay.

## What it serves

- **Read**: rendered Markdown (the same sanitiser rules as the chat), a folder
  listing, images inline, everything else as a download.
- **Edit** (only for an edit share): a plain textarea and a Save, with an
  mtime check so two strangers cannot silently overwrite each other. No CRDT,
  no websocket to syncd, no terminal, no agents, no search, no uploads.
- **Artifacts are not run.** A shared `.html` is shown as source, not
  executed. Running someone's JavaScript on a public origin that also serves
  other people's shares is a cross-share hole waiting to happen. If a client
  really must see a live dashboard, that is a second decision with its own
  origin per share, not a v1 default.

A write lands as the host user `kbshare`, which owns nothing else and belongs
to no group. Git history therefore shows the change as `kbshare` with the
share id in the commit trailer: you can always tell what came in from
outside.

## Hardening (the checklist the container must pass)

| | |
|---|---|
| Process | `USER 10001`, no shell in the image, distroless or Alpine pinned by digest |
| Filesystem | `--read-only`, `--tmpfs /tmp:rw,noexec,nosuid,size=64m`, `/conf` ro |
| Privileges | `--cap-drop ALL`, `--security-opt no-new-privileges`, seccomp default, AppArmor profile |
| Network | own bridge; ingress only from the Cloudflare tunnel; egress to RFC1918 and the host's loopback **dropped** |
| Limits | memory, CPU and PIDs capped; request body capped; a slow-loris timeout |
| Data | only per-share bind mounts; `ro` unless the share is editable |
| Secrets | none in the image or the environment; the cookie key is generated per boot into tmpfs |
| Updates | image rebuilt weekly for CVEs; the platform never trusts its output |
| Logs | structured to stdout → journald; the daily triage reads failed password attempts and 404 storms |

Expiry runs on the host: a timer every 15 minutes unmounts and removes what
has expired, and the container independently refuses a conf whose expiry has
passed. Two mechanisms, because the interesting failure is the one where the
timer did not run.

## What I would not do

- Serve public links from the platform process behind a path prefix. Then the
  internet is talking to the thing that holds the session key, the database
  credentials and every user's files.
- Mount `/srv/kb` into the container and resolve paths inside it. One bug in
  path handling is then the whole knowledgebase.
- Let a public visitor upload. That is free malware storage on your box.
- Reuse `os.<domain>`. A separate hostname means a cookie for the app is never
  sent to the public service, and the Access policy on the app stays absolute.

## Open questions for the build

1. Does an edit share need a name field ("who are you?") so history shows
   more than `kbshare`? A free-text name in the commit trailer is cheap and
   unverifiable — worth it or noise?
2. Folder shares: recursive, or one level? Recursive is what people expect;
   it also means one wrong click shares a subtree.
3. Should a share notify its creator when it is first opened, and when it is
   edited? (The notification design below would carry it.)
