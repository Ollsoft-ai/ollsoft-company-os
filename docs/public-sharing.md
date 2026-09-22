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
3. `mkdir /srv/kb-public/data/<id>` and `mount --bind` onto it: a folder
   share binds the folder; a single-file share binds **the folder the file
   lives in**, and the conf names the one file inside it that may be served.
   Remounted `ro` unless the link may edit.

   *Why not bind the file itself?* Because a bind mount pins an **inode**,
   and everything careful on this box replaces a file rather than rewriting
   it — syncd flushes a document to a `.kbtmp` and renames it into place, as
   do git, vim and half the tools an agent runs. The first such write leaves
   the share holding an orphan: reads return the old text, writes land in a
   file with no name, and both sides report success. That is exactly what
   happened to a live link on 2026-09-22 (`findmnt` printed the source with
   `//deleted` on the end, which is the tell).

   A directory inode is not replaced, so the mount survives. The siblings are
   then kept out by the kernel rather than by the mount: `kbshare` gets
   **search only** (`--x`) on the folder and read/write on the one file, so
   it can open that name and cannot list the folder or open anything else.
   Routing enforces the same thing a second time (`only` in the conf), and
   the sweep checks that what is mounted is still the live inode and remounts
   when it is not.
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

- **A document opens in the platform's own editor.** Not a rendered copy and
  not a textarea: `frontend/src/publicdoc.js` mounts the same `richview.js`
  the app mounts — rendered markdown, tables you type into, checkboxes, the
  themed find panel, the same stylesheet. A stranger sees what a colleague
  sees. The bundle is bind-mounted into the container read-only from
  `/opt/kb-platform/frontend/static` and served under `/assets` with the
  build's stamp on every URL, so a frontend deploy updates the public page
  too, with no image rebuild. Nothing in it is secret — every browser on the
  app downloads the same files — and the container still cannot read one byte
  of `/srv/kb`.
- **Read**: the editor, read-only (no cell inputs, disabled checkboxes, no
  media actions). A folder is still a plain listing; images and other files
  are served as themselves.
- **Edit** (only for an edit share): the same editor, writable, saving 1.2 s
  after you stop typing (and on Ctrl+S) through `__save`, which refuses a
  write whose `mtime` is not the one the page loaded — so two strangers
  cannot silently overwrite each other. No CRDT, no websocket to syncd, no
  terminal, no agents, no uploads.
- **Somebody else's change arrives by polling.** Every five seconds the page
  asks `__stat` for the file's timestamp; when it moves and the visitor has
  nothing unsaved, it pulls `__raw` and swaps the text in. A reader sees an
  edit land within seconds; a writer with unsaved text is told rather than
  overwritten.
- **The reader's device picks the theme.** Nobody out here has an account, so
  there is no `ui.theme` to honour: a light screen gets the light theme, a
  dark one keeps the deep-blue chassis. Decided in the page's head, before the
  first paint, by `assets/share-theme.js` — a classic script, because a module
  is deferred and would flash the wrong one.
- **No JavaScript**: the server-rendered markdown (and, for an edit share,
  the old textarea form) is still there, inside `<noscript>` — with GFM tables
  turned on (CommonMark has none, so it used to print a paragraph of pipes)
  and `<br>` allowed back through the escaping, since it is the only line
  break a table cell can have. Raw HTML stays off otherwise.
- **Artifacts are not run.** A shared `.html` is shown as source, not
  executed. Running someone's JavaScript on a public origin that also serves
  other people's shares is a cross-share hole waiting to happen. If a client
  really must see a live dashboard, that is a second decision with its own
  origin per share, not a v1 default.

### Why not real multiplayer with anonymous visitors?

It is possible, and it is not free. Live cursors mean the container joining
the document's CRDT room, which means a websocket from the public container
to `kb-syncd` — the one door this design keeps shut. `kb-syncd` speaks for
every document on the box and authenticates by platform session; teaching it
"this connection may have exactly this one document, as nobody, because a
token says so" is a new authentication path in the most privileged daemon
here, reachable from the internet.

The cheap 90% is what is built: polling shows a reader other people's edits
within seconds and stops two writers clobbering each other. If live cursors
on a public link are actually wanted, the honest shape is a **relay endpoint
of its own** — a separate listener, share-scoped, that holds one Y.Doc per
live share, accepts only `id + token`, and syncs to the file rather than into
syncd's world. That is a project, not a flag, and it should be decided as
one.

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
| Data | only per-share bind mounts; `ro` in the kernel unless the link may edit. A folder share admits `kbshare` to that subtree; a file share mounts the folder and admits it to ONE file, with search-only on the folder itself — it cannot list the folder or read a sibling |
| Assets | `/opt/kb-platform/frontend/static` mounted read-only at `/assets`, served by suffix allowlist (`.js .css .woff2 .woff .svg`) — public files either way, and the only writable thing in the container is a share that may be edited |
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
   edited? (The inbox would carry it.)
4. Nanoseconds do not survive JSON in a browser (`Number.MAX_SAFE_INTEGER`
   is 9.0e15), so the page's version stamp is **microseconds**. Whole
   seconds, which is where this started, cannot tell "I saved that" from
   "somebody else saved in the same second".
5. A document open in the app while a stranger saves through a link goes
   down syncd's normal external-edit path (`apply_external`): a deterministic
   three-way merge, with a conflicting region resolved in favour of whoever
   has it open and the loss logged. The public page then polls, sees the
   merged file and shows it. Worth a test of its own — the case has never
   been exercised deliberately.
