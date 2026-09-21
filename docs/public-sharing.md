# Public sharing — a separate, isolated service

A folder or a file, handed to someone who has no account here: a client, a
lawyer, a candidate. They open a link, they see it, and — if the share says so
— they can edit it. No Cloudflare Access login, because they have nothing to
log in with.

**Built and running on this box** (`kb-share.service`); the last step is a
Cloudflare hostname, which is at the end of this document.

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
                    │  runs as kbshare, read-only rootfs│
                    │  cap-drop ALL, no-new-privileges  │
                    │  own bridge, NO egress at all     │
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

1. `id` = 10 random bytes as lowercase base32; `token` = 24 random bytes,
   url-safe. Only their SHA-256 hashes are kept.
2. One ACL entry lets `kbshare` in: `rX` for a read link, `rwX` (plus a
   default ACL) for an editable one, on exactly that subtree. Mode bits alone
   would not do — a `0640` document is unreadable to the container, and a
   public link must not depend on a file happening to be world-readable.
3. `mkdir /srv/kb-public/data/<id>` and `mount --bind` the target onto it —
   a FOLDER onto the directory itself, a FILE onto `data/<id>/<name>` so its
   siblings are never exposed — remounted `ro` unless the link may edit.
4. Write `/srv/kb-public/conf/<id>.json` (root:kbshare 0640): mode, expiry,
   title, the file's name, the scrypt password hash and the token hash.
   **Never the real path.**
5. Record it in `/var/lib/kb-shares/shares.json` (root 0600) and audit the
   line (`public.create`, `public.revoke`).

The container reads `/conf/<id>.json` per request. No restart, no privileged
call from inside, no way for it to create a share of its own. `kb_platform/
publicshare.py` is the whole host half; `public/serve.py` is the whole
container.

**Revocation is an unmount.** Deleting a share removes the bind mount, so a
leaked token reaches an empty directory a second later — no cache, no
"eventually consistent" window.

## The URL and the password

`https://share.<domain>/s/<id>/<token>`

- `Referrer-Policy: no-referrer`, so the token does not travel to sites the
  document links to.
- A password share shows a form first, then sets a cookie signed with a key
  generated at container start (in memory, gone on restart) and scoped to
  that share's path, for 24 hours. `hashlib.scrypt`, and a lockout per share
  AND per IP (10 attempts a minute), with Cloudflare rate limiting in front.
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
to no group, so `ls -l` tells you an edit came from outside. Git history does
not yet say so: syncd attributes a commit from the author hints the app
writes, and the container writes none, so an edit through a link lands in the
unattributed sweep commit. Naming it is one of the open questions below.

## Hardening (the checklist the container must pass)

| | |
|---|---|
| Process | runs as the host's `kbshare` (a system account with no shell, no home, no groups); `python:3.12-alpine`, two pure-Python packages, nothing else installed |
| Filesystem | `--read-only`, `--tmpfs /tmp:rw,noexec,nosuid,size=16m`, `/conf` read-only |
| Privileges | `--cap-drop ALL`, `--security-opt no-new-privileges`, Docker's default seccomp and AppArmor profiles |
| Network | its own bridge (`kb-share-net`), published only on `127.0.0.1`, and two `DOCKER-USER` rules: answers to requests are allowed, **anything the container starts is dropped** — no internet, no host services. Verified by trying, from inside |
| Limits | `--memory 256m --cpus 0.5 --pids-limit 200`, a 4 MB body cap, a 20 s socket timeout |
| Data | only per-share bind mounts; `ro` in the kernel unless the link may edit, and an ACL that admits `kbshare` only to that subtree |
| Secrets | none in the image or the environment; the cookie key is made at start and lives in memory |
| Logs | one line per request to stdout → journald |

Not done yet, and worth doing: pinning the base image by digest, a custom
seccomp profile, and a weekly rebuild for CVEs.

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

## Installing it

```
sudo bash scripts/install-public-share.sh          # user, dirs, image, units
sudo bash scripts/install-public-share.sh --remove # …and back out again
```

It makes the `kbshare` system account (no shell, no home, no groups), the two
directories, the image, `kb-share.service` (the container) and
`kb-share-sweep.timer` (expiry and re-mounting after a reboot, every 15
minutes).

Then the hostname, in Cloudflare Zero Trust → Networks → Tunnels → this
tunnel → **Published application routes** (NOT "Hostname routes", which is
WARP steering for your own people): subdomain `share`, your domain, type
HTTP, URL `127.0.0.1:8402`. It writes the DNS record itself. Attach **no
Access policy**: the people using these links have no account to log in with.

*Why the tunnel and not simply a proxied A record at the box?* An orange
cloud still needs the origin to accept inbound connections, which means a
port open on the machine and anyone who learns the address can walk past
Cloudflare straight to it (unless you also pin the firewall to Cloudflare's
ranges, for ever). The tunnel dials out, so there is no listening port at
all, `kb-share` stays bound to `127.0.0.1`, and the same ingress that already
carries the app carries this — one hostname to Access and the app, another
to no Access and the container.

Finally tell the platform its own address, so the links it hands out are
whole:

```
echo 'KB_SHARE_BASE=https://share.<domain>' >> /etc/kb/kb.env
systemctl restart kb-hub
```

## Open questions for the build

1. Attribution for an edit that came through a link. The cheapest honest
   version: syncd checks whether the path is inside a live public share and
   commits it as `kbshare (public link <id>)`. A free-text "who are you?"
   field on the edit page is cheap too, and unverifiable — worth it or noise?
2. Folder shares: recursive, or one level? Recursive is what people expect;
   it also means one wrong click shares a subtree.
3. Should a share notify its creator when it is first opened, and when it is
   edited? (The notification design below would carry it.)
