# Security model

Read this before exposing the platform beyond a trusted single box.

## Trust boundaries — what enforces what

| Boundary | Enforced by | Notes |
|----------|-------------|-------|
| A user can only read/write their own files | **the Linux kernel** | the per-user backend *is* the user (`runuser`); every open/write is kernel-checked |
| Search / DB queries can't return files you can't read | **Postgres RLS** (`kb.visible_files`) | both policies gate on a materialized (usr, path) set the indexer keeps in sync; `kb.can_read` is the live-computed oracle the parity test checks it against, not the policy |
| A root daemon can't be tricked into writing the wrong file | **`openat`/`O_NOFOLLOW`** | all privileged writes refuse symlinks at every path component — the platform config dir `.os/` included: a pre-planted entry of that name is refused, never chowned through |
| An artifact can't touch the app or exfiltrate | **opaque-origin iframe + CSP** | `sandbox="allow-scripts"` (no same-origin) + `connect-src 'none'` — the artifact itself never reaches the network |
| An artifact can reach an approved API, and only that | **hub egress proxy** | deny-all by default; per-artifact domain allow-list in `.os/egress.json` (root:kb-users 0644 — an admin, or anyone granted write on that file, may change it). Requests are proxied by the hub, so the artifact never holds the credential |
| An artifact can't reach the viewer's other data | **folder-scoped bridge** | every file verb is limited to the artifact's own directory and checked in the backend, never `_secrets/`. `kb-read`/`kb-write`/`kb-read-bytes`/`kb-upload`/`kb-delete` act through an fd whose real path is checked after the open, so no link leads out. `kb-list`/`kb-mkdir` check the resolved path only: a colleague racing a middle folder for a link could make them list or create outside, with the viewer's permissions |
| An uploaded file can't run script on the app's origin | **CSP + `nosniff` on `/api/attachment`** | `script-src 'none'`, so a planted SVG previews but never executes; a `.html` is classified as an artifact and served sandboxed by `/api/artifact/raw` instead |
| A session cookie can't be forged | **HMAC-SHA256** with a root-only key | `/etc/kb/session.key`, 12h TTL |
| An uploaded logo can't run script on the app's origin | **CSP sandbox on `/brand/logo`** + a refusal at upload | admin-only upload; an SVG containing `<script`, `javascript:` or event handlers is refused, and the served file carries `default-src 'none'; sandbox` so even a hostile SVG opened directly is inert |
| A read-only viewer can't mutate a doc | **`kb-syncd` drops their CRDT writes** | they still see content + live updates |
| Only admins can manage users/groups | **admin-group check** on every `/admin/*` call (membership of `KB_ADMIN_GROUP`, default `sudo`) | |

The **guiding principle**: the safe outcome is the *default* outcome. Forgetting
to share a file yields a broken view, never a leak. Almost all authorization is
the kernel's; the two SQL/daemon mirrors exist only for surfaces the kernel can't
see (a shared index; a root daemon).

## The privileged surfaces (run as root)

Three things run as root and are therefore the audited core:

1. **`kb-hub`** — auth + reverse proxy + `/fs/*` + `/admin/*`. It never runs
   business logic as a user; it re-derives the caller from the signed cookie and
   checks Unix authorization before any privileged filesystem op.
2. **`kb-syncd`** — the CRDT/file daemon. Reads/writes docs as root, so its sole
   gate is `common.can()` (inode access with POSIX ACLs + execute on every
   ancestor), reached through the `fs_can` adapter — the same evaluator the hub
   uses, which is the point: the two root daemons used to disagree about who
   could read what. Plus symlink-safe writes.
3. **`/admin/*` in the hub** — user/group management. Admin-only; inputs are
   regex-validated so they can't inject into `useradd`/`psql`/`setfacl`.

### The symlink-safe write pattern (follow it in all root code)

Privileged file operations must never trust a path string a user can influence,
because the user can swap a component for a symlink (`company/x → /etc/cron.d/x`)
and redirect a root write. The platform's rule, in `common.opendir_beneath()`:

- Reach a directory via `openat` + `O_NOFOLLOW` at **every** component, rooted at
  `/srv/kb` (refuses both final- and intermediate-component symlinks).
- Create/write the final file `O_NOFOLLOW` (`O_EXCL` for new files) under that
  dir fd; do ownership via `fchown` on the fd, ACLs via `setxattr` on the fd.
- `kb-syncd`'s atomic write uses an **unpredictable** temp name + `O_EXCL|O_NOFOLLOW`
  + `renameat`, so a pre-planted temp symlink is refused, not followed.

If you add any root code that writes files, use these helpers — do **not** use
path-string `open`/`chown`/`shutil`.

## Adversarial audit — findings and fixes

The platform was reviewed by an 8-dimension adversarial security sweep (each
finding independently verified). **13 confirmed findings were all fixed**, with
regression tests. The themes and remediations:

| Finding (severity) | Fix |
|--------------------|-----|
| Backend-socket squatting → cross-user account takeover (**critical**) | per-user `0700` socket dirs; hub verifies socket `st_uid==target`; `/run/kb/users` is `0755`, not world-writable |
| `fs_upload` / `syncd` / `fs_props` symlink → arbitrary **root** write (**critical/high**) | `opendir_beneath` + `O_NOFOLLOW` + fd-based `fchown`/`setxattr`; random `O_EXCL` temp in the daemon |
| A chunked upload's spool sits in a folder the uploader can also write, for as long as the upload runs | the spool is opened **once** (`_open_inheriting`, `O_NOFOLLOW` under a verified dir fd) and every chunk is `pwrite`n to that **fd**, never re-opened by name; the inode is re-verified against the fd immediately before the publishing rename, the session id is unguessable and bound to its owner (another account appending or finishing gets 404), and write access to the folder is re-checked at finish because a session can outlive the share that authorised it |
| RLS ignored ancestor-dir traversal → world-readable file inside a `0700` dir leaked via search (**high**) | `kb.can_read` and daemon `fs_can` now require traverse on every ancestor |
| RLS blind to POSIX ACL mask → over-shared a locked file to its whole group (**high**) | indexer de-masks group bits + records named ACL grants; RLS honors them |
| `can_read` was an arbitrary-user oracle (**low**) | single-arg, uses `session_user` |
| Artifact file-action confused-deputy exfiltration or destruction (**high**) | bridge scoped to the artifact's own folder; `kb-mkdir`/`kb-delete` scoped again in the backend, and an artifact may delete neither its own folder nor itself |
| `kb-toggle` could flip any checkbox-looking line in any writable file (**low**) | requires `.md` + a genuinely indexed task line (RLS-scoped) |
| Indexer recorded a symlink's metadata over its target's; startup-crash DoS (**medium/low**) | indexer skips symlinks; guarded `rel()` |
| Orphaned ACLs survive user deletion + uid recycling (**low**) | delete strips the user's ACL grants recursively |

Two bugs surfaced *while remediating* (also fixed): the perms reconcile loop ran
`getfacl` on every file every second (CPU storm → flaky) — now a ctime/mode
signature cache skips unchanged files; and auto-granting `u:X:--x` on a dir the
user already reached via group **downgraded** them (a named ACL entry overrides
the group entry) — sharing now skips dirs the grantee can already traverse.

## Second adversarial audit — 2026-08-24

A second sweep (10 parallel finders, every finding independently verified by a
refuter and an exploitability check, plus a completeness critic). Threat model:
an authenticated **non-admin** KB user, or an AI agent running as one. 42
candidates, 35 upheld. The ones that mattered and what closed them:

| Finding (severity) | Fix |
|--------------------|-----|
| No sticky bit on `users/`, `company/`, `projects/` → any member could rename another user's home aside and put their own in its place. That directory is where the platform loads that user's agent skills from, so it was a path from ordinary account to **code running as an admin** (**critical**) | `+t` on `users/` and `projects/` (see the note below on why NOT `company/`) |
| `fs_props_set` gated `chgrp` on inode ownership, and `_share_apply` then rewrote the owning group's ENTIRE roster → three requests let a non-member join any `proj-*`/`team-*` group and evict everyone else (**critical**) | authorization for both is group **membership**, not inode ownership |
| `convert.py` wrote its sidecar to a predictable temp path with `O_CREAT\|O_TRUNC` and no `O_NOFOLLOW` → planted symlink = arbitrary overwrite as the indexer service account, which belongs to every project group (**critical**) | pinned dir fd, random name, `O_EXCL\|O_NOFOLLOW`, ACL applied via `/proc/self/fd`, `renameat` |
| The unattended maintenance agent's tool allowlist contained `Bash(find:*)` — `find -exec` is general command execution — while its context is fed attacker-writable text (**critical**) | `find`/`grep` removed from both the normal and dry-run allowlists; work dir `0750`, report `0600` |
| Caddy's admin API listened unauthenticated on `127.0.0.1:2019`; any local account could rewrite the config of the process terminating public TLS (**high**) | `admin off`, with a systemd drop-in so `reload` still works |
| `artifact_query` ran artifact-authored SQL with the VIEWER's database authority, so a shared dashboard could `CREATE TABLE … ; GRANT … TO <author>` (**high**) | privilege/role/ownership statements refused; `statement_timeout`. Honest limit: a lexical check, not a parser — the structural fix is a consent gate before opening someone else's artifact |
| A file's owner could `chown` it to root or another user while keeping write access — provenance forgery (**medium**) | reassignment requires admin, and never to a system account |
| The admin override reached `_secrets/` files (**medium**) | admins can no longer widen a `_secrets/` file they do not own; the owner still can, which is the documented design |
| Sharing a folder must not hand out the credentials inside it | `_walk_repo` never descends into a `_secrets/` folder — sharing a project stops at it. Sharing the `_secrets` folder *itself* is the one case that does reach its contents, and only its owner may ask for that (`/fs/share` and `/fs/props` both refuse an admin who is not the owner). The indexer is never added to a secret's group or ACL, and it prunes `_secrets` before it walks, so a shared secret is still never indexed |
| `x-kb-user` from the client survived into the proxied request (**low**, latent) | asserted headers stripped on the way in |
| The Olingo CRM and timesheet tables were granted `INSERT/UPDATE/DELETE` to every employee, and anyone could make themselves a timesheet admin (**low**) | revoked; the games and lunch votes stay shared on purpose |
| `seed-demo.sh` wrote `/tmp/kb-test-creds.json` `0644` with three working passwords, one of them an admin's (**low**) | `0600`, owned by whoever will run the suite |

**Why `company/` is deliberately NOT sticky.** Setting `+t` there looked correct
and broke shared editing, because the kernel does more than the classic
rename/delete restriction: with `fs.protected_regular=2` (default since Linux
4.19) a sticky, group-writable directory also refuses `O_CREAT` opens of files
you do not own — and both `open(path,"w")` and a shell `>` issue `O_CREAT`. Every
top-level company document silently became read-only to everyone but its author,
while `access(2)` kept reporting it writable. `company/` is a deliberate
free-for-all; the subdirectory that is not — `company/.infrastructure/`, the
maintenance agent's input — is protected directly by dropping group write.

## Third adversarial audit — 2026-10-03

Ten area reviewers, every finding independently verified (two verifiers for high and critical). 46 confirmed: 2 critical, 6 high, 13 medium, 19 low, 6 info. The critical and high ones are fixed, except two the owner accepted for a trusted team:

| Finding (severity) | Fix |
|--------------------|-----|
| `/vc/diff` took any hex object id as `rev`, and `git show <blob>` ignores the pathspec, so anyone who could read one document could read every object in history, an abbreviated id being four hex digits; `/vc/show` read inside any subtree the same way (**critical**) | `rev` is peeled to a commit (`rev-parse --verify <rev>^{commit}`) before either is shown |
| The agent chat's Claude adapter loads project and local settings (hooks, `.mcp.json`, env, `apiKeyHelper`) from the folder it starts in, with no trust prompt in ACP mode; the KB root and `company/` are writable by everyone, so a planted file ran as whoever opened a chat there (**critical**) | **accepted risk** (owner's decision, 2026-10-03): the chat must behave like `claude` started in `/srv/kb` in a terminal — company skills, MCP servers and project settings all load. Colleagues are trusted; see residual risks |
| A public FOLDER link granted `kbshare` recursively, `_secrets/` and `.trash/` included, even other people's keys (**high**) | an fd-pinned walk that skips both; the sweep strips any `_secrets` made later; the container refuses those paths itself |
| Public link creation re-resolved the path after the owner check, so a component swapped for a link could publish someone else's folder (**high**) | the target is opened once without following links; owner check, ACLs and the bind mount (`/proc/<pid>/fd/N`, verified by inode) all act on that fd |
| `artifact_query`'s keyword filter did not stop exfiltration: an INSERT into a table the author shares, `pg_notify`, or a file in the artifact's folder all move the viewer's data without a privilege keyword (**high**) | **accepted risk** (owner's decision, 2026-10-03): artifacts stay author-trusted, with no prompt before someone else's page runs; the keyword filter stays as defence in depth |
| The bridge's folder scope was a string check in the browser; `kb-read`/`kb-write`/`kb-read-bytes`/`kb-upload` followed symlinks (**high**) | scope enforced in the backend on the opened fd; uploads never write through a planted `_files` link |
| `javascript:` links in a document ran on the app origin through `window.open` (**high**) | body links take the same `safeHref` allowlist as table cells |
| The maintenance agent's allowlist held `curl … -w *` (curl's `-w` writes files), a prefix `sudo cat /var/log/kb/*` (`../` reaches any file), `sudo journalctl` (`--vacuum` erases the audit trail) and `git diff/log` (`--output=`) — while it runs as an admin with passwordless sudo (**high**) | all four gone; exact per-file log reads; the bundle carries the hub's HTTP status; the agent reads the journal through `systemd-journal`, given to its process only; `tests/cli/test_maintenance_allowlist.py` fails on any non-trailing `*` or inexact sudo rule |

Still open from this audit, by severity: no Origin/CSRF check on state-changing routes and websockets (sibling `*.ollsoft.org` hosts are same-site), egress grants keyed to group-writable paths and `secret:` refs not scoped to the artifact's folder, an artifact frame may navigate itself (an exfiltration channel once allowed), rerank sends unredacted text, remote images auto-load in chat, and the post-login redirect. The full list is in the review report, kept off the knowledgebase until fixed.

## Audit trail — who changed access, and who opened what

The hub records six **mutation** events — logins, the sharing and account
changes that decide who can reach what, and company-wide settings — as one line
each in journald:

```bash
journalctl -u kb-hub -g AUDIT --since yesterday
```

`login` (both outcomes), `share.set`, `props.set`, `group.member`,
`user.create`, `settings.company`. Readable only by root and `sudo`/`adm`/`systemd-journal`;
journald shows every other account nothing but its own messages.

### Access events — who opened what

Four further events record access being *used*, not granted. They exist
because the likeliest incident here is not an intruder but a colleague who
already holds the permission, and until 2026-08-29 that left no trace at all.

```bash
journalctl -u kb-hub -g 'AUDIT (document.open|file.preview|file.download|folder.download)' --since yesterday
```

| Event | Means exactly | Does **not** mean |
|---|---|---|
| `document.open` | a collaborative session for that document was joined through `/ws/doc/*` and syncd accepted it | that anything was read, or that the tab stayed open |
| `file.preview` | the server served the attachment inline (`Content-Disposition: inline`) | that the browser rendered it or a person looked |
| `file.download` | the server served it with `Content-Disposition: attachment` — a download was *requested and served* | that the file reached the user's disk |
| `folder.download` | the server built and served a whole folder as one zip (`GET /api/folder-zip`) | that every file *in* the folder was in it — the archive holds only what the kernel let that user read |

The zip's `?probe=1` preflight builds nothing and serves no bytes, so it is
deliberately silent: it is the UI asking whether a download is possible, and
logging it would record an intention as an act.

Each line carries only the event, the authenticated actor, `result=ok`, the
normalized repo-relative `path`, the byte count served (the three byte-serving
events; a `document.open` has none), and — for a request that came through the
Cloudflare tunnel — the same trusted `source` the `login` event records. Never content, headers, cookies, query strings, user agents, or
a path the caller spelled but the server did not serve.

They are emitted only *after* the downstream service has already applied the
kernel's decision, so a 401, 403, 404 or a stale-lineage 409 can never appear as
a successful read. Deliberately not recorded: tree listings, search, presence
polling, `/api/vc/*`, autosaves, CRDT frames, and every other `/api/*` route.
An audit trail that logged those would be a surveillance stream, and the signal
would be unfindable inside it.

What it does NOT cover, and should not be relied on for:

- **Not every privileged action.** Only the six mutation events above.
  Deleting a user, switching an account between full and viewer, creating or
  deleting a group, rewriting the launcher list and editing
  `.os/egress.json` all run in the hub as root and write no AUDIT line —
  the raw hub log shows the `POST /admin/...`, but not who sent it.
- **Access events prove a service call, not a reading.** A `file.download` is
  the server's word that it sent the bytes. It is not proof of attention,
  comprehension, or a completed client-side save — and its absence is not proof
  nobody looked, since anyone with a shell reads `/srv/kb` directly.
- **Reads outside those paths are still invisible.** SSH, the mounted
  drive, `cat`, the search index, and the secrets viewer (`GET /api/file`, the
  only document read that never joins a live session) write no access line.
- **Root bypasses it.** An administrator running `setfacl` over SSH writes no
  audit line, and anyone with `sudo` can edit the journal. This is evidence
  about users, not about administrators.
- **It begins 2026-08-25**, and the three access events only on **2026-08-29**.
  There is nothing before those dates; the feature cannot reconstruct history.
- **journald rotates.** Retention is journald's, unchanged by this feature.
  Queries beyond the retention window return less, not an error.

Content history is a separate mechanism: `kb-history` reads the git trail of
what documents said and who wrote them, gated per request against the kernel so
it can only show what the caller could already open. A document's history
follows it across moves: whoever can open it now sees its earlier versions,
including from a folder they could not read (as with a file shared in place);
the earlier NAME is shown only to readers who can list the folder it was in.

## Semantic search — what leaves the box

Only with provider keys installed ([semantic-search.md](semantic-search.md)).

- **Sent:** section text to the embedding provider; a question plus up to 40
  candidate sections to the reranker. Never `_secrets/`, private files, hidden
  paths, `.noembed` subtrees, or `users/` when the company scope says so.
  Credentials pasted into ordinary documents are redacted first (private keys,
  `secret=`/`password:` assignments, `sk-`/`ghp_`/`AKIA`/JWT shapes).
- **Keys:** `/etc/kb/embed.key`, `/etc/kb/rerank.key`, 0640 root:kbindexer —
  readable by the worker, by no person and no agent.
- **The socket** `/run/kb/search/api.sock` sits in a 0750 kbindexer:kb-users
  directory: members (and their agents) connect, `kbshare` and the share
  container cannot. The worker checks the peer's uid (`SO_PEERCRED`) and group
  again, and holds each person to their own caps.
- **`kb.search_vec` is SECURITY DEFINER** because only `kbindexer` may read
  vectors. Its owner bypasses RLS, so it filters by `kb.visible_files` for
  `session_user` explicitly, returns paths and distances only, and never runs
  full-text (`@@` is not leakproof). Section text is read afterwards as the
  caller, under RLS. Accepted: `<=>` timing inside the definer is not
  content-dependent in any way we could exploit, and no error path depends on
  content.
- **Spend** is bounded in the worker, not by trust in callers: reservations,
  an in-process brake, budgets, per-person caps. A person or agent in a loop
  hits their cap, not the company's wallet.

## Residual risks / known limitations

- **Deferred from the 2026-08-24 audit**, each for a stated reason:
  `kb.can_read` is a timing/existence oracle (it is `SECURITY DEFINER` and
  granted to `PUBLIC`, and does measurably different work for "no such path"
  versus "denied") — revoking it breaks the parity test that guards the RLS
  invariant, so the test has to be reworked first. The index can also
  **over-grant versus the kernel** when a named ACL entry grants LESS than the
  class it overrides: POSIX says a matching named entry is authoritative, while
  `compute_visibility()` and `kb._has` treat named entries as additive only.
  Nothing on a normal box triggers it, because the share panel only ever grants
  — but the documented "a raw query can never return a row the caller couldn't
  read on disk" is, strictly, not yet true. The session cookie also has no
  `Secure` flag, the site sends no HSTS, and the app shell still ships without a
  CSP (`Hub.SHELL_CSP` is written but not applied — enabling it broke the
  artifact iframes, and `tests/cli/test_security_headers.py` keeps the gap
  visible as an `xfail`).
- **Platform admin is the OS `sudo` group** by default (`KB_ADMIN_GROUP`).
  That makes any web-admin grant also an OS-root-capable one, and it is why a
  seeded demo account was briefly a full platform admin. Decoupling it into a
  dedicated non-privileged group is the obvious hardening.
- **Single-box only.** OS users + inotify don't span machines; there is no
  off-box replication yet (git history + a manual `_files/` rsync are your DR).
- **Localhost-bound.** No TLS front door is wired in. Put Caddy/nginx in front
  before exposing it; the app assumes it isn't directly internet-facing.
- **Stateless logout.** The session cookie is valid until its TTL; logout clears
  the browser cookie but can't revoke a captured token early. Add a server-side
  session store / token version if you need instant revocation.
- **Artifacts are author-trusted.** Contained against the system and other users,
  but an artifact you open runs code with *your* authority (read/write within your
  own permissions, folder-scoped). Only open artifacts from people you'd trust
  with your own access — same as running a shared script. Its SQL can copy what
  you can see into a table its author reads; no keyword filter stops that, and a
  consent prompt was tried and declined (2026-10-03) as wrong for a trusted team.
- **Agent chat loads the folder's own agent config.** Like `claude` in a
  terminal, a chat reads `.claude/settings.json`, `.mcp.json` and `CLAUDE.md`
  from the folder it starts in and above, and ACP mode has no trust prompt. The
  KB root and `company/` are writable by every employee, so a colleague can put
  hooks or an MCP server there that runs as whoever opens a chat — root, for an
  admin with passwordless sudo. Accepted for a trusted team (2026-10-03).
- **Private-dir files aren't globally indexed.** `kbindexer` can't read a `0700`
  `users/<u>/` dir, so those files aren't searchable (by design; a per-user
  indexer would be needed).
- **`_secrets/` is kernel-protected, not encrypted.** A `_secrets/` folder holds
  creator-owned files that `kb-syncd` refuses to sync and `.gitignore` keeps out
  of history, and the egress proxy can inject them server-side so an artifact
  uses a credential without ever seeing it. They are born `0600` — and stay that
  way, whatever the folder around them says, until the owner of the `_secrets`
  folder shares it deliberately (the share records that decision as a
  `user.kb_secrets_shared` xattr on the folder, which is the only thing that
  lets a later key inherit an audience). A share can then hand a credential to
  named people, to a project group, or to the folder above — the containment
  guarantees do not depend on who may open it. What it is *not* is encryption at
  rest: root, and anyone who can read the file, can read the secret. Full-disk encryption and a real encrypted store (see the roadmap in
  [DEVELOPING.md](DEVELOPING.md)) are still worth having.
- **The trash never widens an audience, and never holds a secret.** A delete
  is a rename into a `.trash/` in the same folder, so the thing does not move
  in any sense that matters: the new directory inherits that folder's group,
  setgid bit and default ACL, and the rename carries the file's own owner,
  mode and ACLs. Nobody sees anything they could not see a moment earlier,
  and the people who could delete it are exactly the people who can restore
  it. The listing is a walk done AS the caller, so a `.trash` inside a folder
  they cannot enter is not in their list.
  A `_secrets/` file is deleted outright instead: a readable copy sitting in
  a trash indefinitely is precisely the exposure `_secrets/` exists to
  prevent — and nothing in the trash expires on its own, so "indefinitely" is
  the literal word.
- **Public links live in a container that can see nothing else.** A link
  hands a stranger one file or one folder, so the thing that serves them is
  not this platform: it is `kb-share`, a separate container with no database,
  no session key, no `/srv/kb` and — verified from inside — no way to open a
  connection to anything, not the internet and not the host. It sees only
  per-share bind mounts (read-only in the kernel unless the link may edit)
  and a config file per share that never contains the real path. A
  single-file link mounts the file's FOLDER — binding the file breaks the
  moment anything replaces it — and the folder is opened to `kbshare` with
  search only, so the kernel refuses to list it or read a sibling. Revoking is
  an unmount, so a leaked link dies in a second; expiry is enforced by a
  host timer AND by the container. A `_secrets/` path can never be published,
  and a shared `.html` artifact is served as source rather than run. The one
  thing it may read besides a share is the platform's built frontend, mounted
  read-only at `/assets` so a shared document opens in the real editor —
  the same JavaScript and stylesheet every browser on the app downloads,
  served by suffix allowlist, with no path into `/srv/kb`.
  Full threat model: [public-sharing.md](public-sharing.md).
- **A document's audience is its ACL, so a save has to carry it.** `kb-syncd`
  writes a document by creating a temp file and renaming it into place, and
  until 2026-09-22 it restored only owner, group and mode. The first
  keystroke after a share therefore erased the named people and the indexer
  from the file — and, because the stored mode is `0660` against a group of
  `kb-users`, handed the whole company read and write in their place. The
  flush now copies `system.posix_acl_access` onto the replacement (through
  the fd, after `fchmod`, which rewrites the mask), and
  `tests/e2e/test_external_merge.py::test_a_flush_keeps_the_files_audience`
  types a sentence into a shared document and checks the entries are still
  there.
- **`kb-convert` parses untrusted binaries.** Anything a user uploads (docx,
  pptx, xlsx, pdf) is fed to third-party parsers. It runs as the non-root
  `kbindexer` in its own venv with a memory cap, so a parser exploit is
  contained to what that account can do: read shared content it already indexes
  and write the disposable index/sidecars — no root, no user application data
  (`kbindexer` is not in the `kb_users` Postgres role). Sandboxing it further
  (seccomp, a throwaway namespace) is a reasonable hardening step.
- **`kb-hub`/`kb-syncd` run as root.** Acceptable for a localhost single box given
  how small/audited they are; a hardening pass (dropping capabilities, seccomp)
  is a reasonable next step for higher-assurance deployments.

## Reproducing the audit

The confirmed findings are pinned by regression tests:
`tests/cli/test_security_fixes.py`, `test_security_remediation.py`,
`test_share_reachable.py`, and `tests/e2e/test_artifact_scope.py` /
`test_artifact_xss.py`. Run `.venv/bin/python -m pytest tests/ -q`.

## Agents in the chat

The agent chat ([agent-chat.md](agent-chat.md)) spawns AI coding agents as
subprocesses of the person's own backend — as their OS account, in the
knowledgebase — and bridges their stdio to the browser. Consequences:

- **No second permission model.** The client does not advertise ACP's
  file-system or terminal capabilities; the agent reads, writes and runs
  things itself, and the kernel and the ACLs police it exactly as they police
  the person's terminal. Nothing the agent does can exceed what the person
  can do.
- **Credentials stay the person's.** Sign-in runs the agent's own login in a
  terminal tab and lands in their home directory (`~/.claude`, `~/.codex`,
  `~/.gemini`, …). A pasted API key is written to `users/<them>/.os/agent-keys.json`
  (0600, never synced, indexed or versioned) and handed to that agent's
  process as an environment variable. There is no company-wide key.
- **The agent binaries are shared, root-owned code** in `/opt/kb-agents`,
  installed by an admin (`POST /admin/agents/install` runs `npm install` as
  root, one job at a time, audited as `agents.install`) or by hand. Installing
  an agent is installing third-party code that every person may then run as
  themselves — treat the catalogue as you would any package list.
- **Process hygiene**: one process per person and agent, reaped after thirty
  idle minutes, killed with the backend (the hub's cgroup). A permission the
  agent asks for while nobody is attached waits at most twenty minutes and is
  then answered "cancelled".
