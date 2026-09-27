# Architecture

How the KB platform actually works, component by component.

## Core thesis

- **Markdown is truth.** `/srv/kb` is a git repo of `.md` files (+ attachments).
  Every feature reads/writes those files. The database holds only *derived* data.
- **The database is disposable.** Drop the `kb` schema, restart the indexer, and
  you get a byte-identical index rebuilt from the files. Nothing authoritative
  lives only in Postgres (except per-user scratch schemas — see below).
- **The kernel is the authorization engine.** A request that touches user data is
  served by a process running *as that OS user*. The kernel enforces reads/writes.
  There is deliberately almost no application-level permission code — the two
  places that exist (RLS in SQL, and `common.can()` in Python) exist only to
  *mirror* the kernel for surfaces the kernel can't reach (a shared DB index; the
  two root daemons). `common.can()` is THE evaluator: inode access including
  POSIX ACLs, plus execute on every ancestor up to the repo root.
  `syncd.fs_can` and `hub._fs_can` are one-line adapters over it — they used to
  be separate checks, and the hub's was missing the ancestor walk.

## The processes

| Service      | Runs as     | Bound to            | Job |
|--------------|-------------|---------------------|-----|
| `kb-hub`     | **root**    | `127.0.0.1:8300`    | PAM login, session cookies, spawns & reverse-proxies per-user backends, privileged `/fs/*` + `/admin/*` |
| user backend | **the user**| `/run/kb/users/<u>/backend.sock` | files, terminal, search, tasks, artifact bridge — *all as the user* |
| `kb-syncd`   | **root**    | `/run/kb/syncd.sock`| y-websocket CRDT relay + filesystem merge daemon |
| `kb-indexer` | `kbindexer` | (no socket)         | parses markdown → Postgres; refreshes group membership + ACLs |
| `kb-embedd`  | `kbindexer` | `/run/kb/search/api.sock` (kb-users) | semantic search: embeds changed sections, holds the provider keys and the one spend ledger, serves query embeddings and reranking under per-person caps (see [semantic-search.md](semantic-search.md)) |
| `kb-convert` | `kbindexer` | (no socket)         | office/PDF binaries → hidden read-only `.md` sidecars; each parse in a throwaway child (see [converted-documents.md](converted-documents.md)) |
| PostgreSQL   | `postgres`  | local unix socket   | disposable index (`kb` schema, RLS) + per-user schemas (`u_<user>`) |
| `kb-heartbeat.timer` | **root** | (timer, 5 min) | functional health check — outcomes, not processes; logs, never pushes (see [monitoring.md](monitoring.md)) |
| `kb-maintenance.timer` | **root** | (timer, daily) | triages the logs via a headless agent with a harness-enforced tool allowlist; the only on-box thing that may notify |
| `kb-gitgc.timer` | **root** | (timer, weekly) | `git gc --auto` on `/srv/kb` — syncd commits every edit and nothing else ever repacks the audit history |

Only `kb-hub` and `kb-syncd` run as root, and they do only auth/proxy/merge —
the minimal audited surface.

## 1. `kb-hub` — the front door (root)

`kb_platform/hub.py`. Listens on `127.0.0.1:8300`.

- **Login** (`POST /login`): authenticates against PAM (`pam_auth.py`, uid
  1000-64999 only, so no service account can ever sign in; a **nologin shell is
  deliberately allowed** — viewer accounts use the web app with no terminal, and
  pty/cron are gated on the shell instead), then sets a signed session cookie
  `kb_session` (HMAC-SHA256 over `{user, exp}`, httponly, 12h TTL). The hub is the
  *only* component that ever sees a password.
- **Spawner** (`ensure_backend`): on demand, starts the user's backend with
  `runuser -u <user> -- <venv> -m kb_platform.user_server --uds <sock>`, so it
  runs as that OS user. The socket lives in a **per-user `0700` dir**
  (`/run/kb/users/<u>/`) the hub creates as root; `_backend_alive` verifies the
  socket is owned by the target uid before trusting it (prevents socket squatting).
- **Reverse proxy**: authenticated `/api/*` and `/pty` are proxied to that
  backend (with an `X-KB-User` header); `/ws/doc/*` is proxied to `kb-syncd` with
  a short-lived HMAC token carrying the caller's `uid` + supplementary `gids`.
- **Privileged filesystem admin** (`/fs/*`) and **user/group admin** (`/admin/*`)
  run *in the hub as root* (creating files owned by a folder's owner, or creating
  OS users, needs root). These are the exception to the run-as-user model, so they
  are the most carefully hardened — see [SECURITY.md](SECURITY.md).

## 2. The per-user backend — everything as you

`kb_platform/user_server.py`, spawned per logged-in user, runs as that user.
Because the *process is the user*, kernel permissions apply to everything it does;
there is no permission code here to get wrong.

- `GET /api/whoami` — kernel-truth identity (`geteuid`), used to prove the model.
- `GET /api/tree` — walks `/srv/kb`, returning only entries the user can access
  (`os.access` as the user), with per-node `access:{read,write}`. Folders come
  first, ordered by name; files follow newest-first and carry an `mtime` (epoch
  seconds) that the sidebar prints as a subtle last-modified stamp. Answers with
  an `ETag` and honours `If-None-Match` → 304; `?fresh=1` skips the signature
  hold (see *The tree is cheap to ask for again*, §8).
- `GET /api/events` — one **server-sent event stream** per tab, piped through
  the hub: `hello` (the catch-up: current tree ETag, presence) on every
  connect, `tree` (a delta — `changed` file mtimes to patch in place, or
  `full` to refetch with the ETag), `presence` when the set changes, `config`
  when launchers or settings changed on disk, `ping` every 20 s. Nothing in the
  browser polls any more (see *Nothing polls*, §8).
- `GET/POST /api/file` — read / create a document (as the user).
- `POST /api/fs/mkdir`, `POST /api/fs/delete` — create a folder / delete a file
  or folder (recursive), **as the user** — same authority as `mkdir`/`rm -r` in
  their terminal. Delete refuses the top-level areas (`company`/`projects`/
  `users`); a blocked ancestor is a clean 403, not a 500.
- `POST /api/fs/rename`, `POST /api/fs/copy` — move/rename and copy **as the
  user** (`mv` / `cp -r`). Both re-home the result to the **destination folder's**
  audience (`audience: "keep"` opts out of a rename), because a plain `mv` carries
  the old owner/group/ACLs into a folder with a different readership — a private
  note dragged into a team folder would stay unreadable to that team, and to the
  indexer. `POST /api/fs/move-preview` answers "would this change who can open
  it?" before the move, so the app can ask.
- `POST /api/upload`, `GET /api/attachment` — attachments in a `_files/` sibling.
  Attachments are served with `script-src 'none'` + `nosniff`, so an uploaded SVG
  previews without executing.
- `POST /api/upload/{begin,chunk,finish,abort}` — the same attachment, **in
  chunks** (see *Uploads of any size* below). The single-shot route above stays
  for scripts and the artifact `kb-upload` bridge.
- `GET /pty` — a **real login shell** in a PTY (`pty.fork` + the account's own
  login shell, `-l`), starting in `/srv/kb` (the knowledgebase root), bridged to
  xterm.js in the browser. A viewer account (nologin shell) gets 403. Shells are
  **persistent sessions** (`?session=<sid>&have=<bytes>`): they outlive the
  websocket, buffer 256 KB of output, and a reconnect replays only what the
  client missed (or sends `{"reset":true,"base":N}` + the full buffer when the
  gap outgrew the ring). Shell death is announced with `{"exit":true}` — a bare
  close always means "connection lost", and the client quietly reattaches. One
  client per session: a new attach sends the old one `{"detached":true}`. The
  client pings (`{"ping":1}`, swallowed server-side) so idle-timeouting proxies
  keep the socket. At most 12 sessions per user; only detached ones are ever
  evicted, else the new attach is refused with `{"error":…}`. Sessions still
  die with the backend (deploy restarts).
- `GET /api/tasks`, `POST /api/tasks/toggle` — task aggregation + write-back.
- `GET /api/search` — full-text search over the RLS index (as the user).
- `GET /api/settings`, `POST /api/settings` — the user's **settings**: the
  registry's schema, the company layer (read from `.os/settings.json`), their
  own layer (`users/<me>/.os/settings.json`, written as them) and the per-key
  resolution. See [settings.md](settings.md).
- `GET /api/cron`, `POST /api/cron/{add,remove,toggle}` — the user's **own
  crontab**, via `crontab(1)` run as them. List/add/delete/pause (pause = a
  `#kb:paused ` comment prefix, so cron skips it but the entry survives). Add is
  validated (5-field or `@keyword` schedule; command must be a single line —
  including exotic separators — so one UI action is exactly one crontab line, and
  a bare `%` is refused since cron would silently truncate there) and
  `crontab(1)` itself re-validates;
  remove/toggle must echo the current raw line back and get a 409 if the crontab
  changed underneath them. **Full accounts only**: a viewer (nologin shell) gets
  403 here, and the hub lists it in `/etc/cron.deny` so the OS refuses it too.
  Jobs run with the user's kernel + Postgres identity — scheduling is just *the
  user acting later*, no privilege to escalate.
- **Artifact bridge**: `POST /api/artifact/{query,read,write}`, `GET
  /api/artifact/raw`, `POST /api/tasks/toggle` — see §6.
- Connects to Postgres with `psycopg.connect("dbname=kb")` over the unix socket →
  **peer auth** → the PG role *is* the OS user → RLS applies automatically.

## 3. `kb-syncd` — multiplayer on the files (root)

`kb_platform/syncd.py`. The one component that makes the `.md` file a live CRDT peer.

- **CRDT relay**: a `pycrdt.websocket` server. The browser's `y-websocket` client
  connects (proxied through the hub) to `/ws/doc/<path>`; the server holds a
  `pycrdt.Doc` per room and speaks the standard Yjs sync protocol, so browsers
  co-edit Google-Docs style.
- **File daemon**: for each open doc it (a) **flushes** the CRDT text to disk on a
  250ms debounce, preserving the file's owner/group/mode; and (b) **watches** the
  filesystem (`watchfiles`/inotify) and merges *external* edits (vim, an agent,
  `git`) into the live doc via a **line-level three-way merge (diff3)** against
  the last-agreed shadow — never a character diff and never fuzzy patching, both
  of which spliced fragments into lookalike lines and lost concurrent keystrokes
  (`tests/e2e/test_external_merge.py`). So five writers — browser, vim, agent,
  git, script — converge through one CRDT.
- **git auto-commit**: debounced snapshots into `/srv/kb/.git` (history/rollback).
- **Authorization**: the hub-signed token carries the caller's uid/gids; `fs_can`
  re-checks Unix **read to join** and **write to edit**, including **traverse (x)
  on every ancestor directory** (root bypasses the kernel, so the daemon must
  model full path resolution itself). A **read-only** viewer is admitted but the
  daemon **drops their mutating messages**, so they see content + live updates but
  can't change a file they lack write on.
- All file writes use symlink-safe `openat`/`O_NOFOLLOW` — see [SECURITY.md](SECURITY.md).

## 4. `kb-indexer` — the disposable index

`kb_platform/indexer.py`, runs as `kbindexer` (a member of every content group).

- Parses every `.md` file into `kb.blocks` (one row per task/heading/line, with a
  `tsvector` for FTS) and denormalises each file's owner/group/mode into
  `kb.files`.
- Cuts each file into **sections** for semantic search (`kb.chunks`, same
  transaction as the blocks, inside a savepoint so a bad section never costs
  full-text): heading trail and folder prefix, credentials redacted, blobs
  squeezed, keyed by `sha256(model ‖ text)`. It makes no network call and holds
  no key — `kb-embedd` turns sections into vectors ([semantic-search.md](semantic-search.md)).
- Extracts `@assignees` and `#tags` from tasks into array columns.
- **ACL-aware**: reads real POSIX ACLs via `getfacl`, *de-masks* the group bits
  (so a file locked to owner and shared with one user via ACL doesn't look
  group-readable), and records named read/traverse grants in
  `acl_users`/`acl_groups`/`acl_x_users`/`acl_x_groups`.
- Skips symlinks and dot-dirs (`.git`, `.claude`, `.os`) — config is not knowledge.
- Refreshes `kb.user_groups` from `getent` every 5s (so new users / group changes
  reach RLS), and reconciles perms with a cheap ctime/mode signature cache (only
  re-reads ACLs for files that actually changed).

## 5. Postgres + Row-Level Security

`scripts/schema.sql`. One database `kb`, local peer auth (PG role == OS user).

- `kb.files`, `kb.blocks` have **RLS enabled**. Users get `SELECT` only.
- **Both policies gate on `kb.visible_files`**, a materialized (usr, path) table:
  `files_read` on `path IN (…)`, `blocks_read` on `file_path IN (…)`, each
  filtered by `usr = session_user`. One hashed subplan per statement (~1 ms).
  kb.blocks does **not** delegate to kb.files.
- `kb.visible_files` is written by the indexer's `compute_visibility()`, diff-
  synced on the same ~1 s sweep that refreshes kb.files' permission columns —
  so revocation latency is the sweep, not a restart.
- `kb.can_read(path)` is a `SECURITY DEFINER` function that replicates **full
  Unix path resolution** using `session_user`: read on the file (bits, OR a
  named-user ACL, OR a named-group ACL) **AND traverse (x) on every ancestor
  directory**. **The policies no longer call it.** It is the live-computed
  *oracle*: `compute_visibility()` must agree with it pair-for-pair, asserted
  per user in `tests/cli/test_visible_files.py`. Change the two together.
- This is why a raw query can never return a row you couldn't `cat`, and why a
  file made world-readable *inside* a `0700` dir stays hidden. An empty
  `visible_files` means everyone sees nothing through the index — the safe
  direction; the filesystem, tree and open documents are unaffected.
- **Per-user schemas** `u_<user>`: each user owns a private, default-deny schema
  for structured scratch data (an agent's scraped feed, a computed rollup). Nobody
  else has access until the owner `GRANT`s it. This is the *one* place primary
  data can live only in Postgres — back it up separately, or treat it as scratch.

## 6. Artifacts — sandboxed, viewer-scoped dashboards

An artifact is a self-contained `.html` file in the repo. When opened it renders
in an `<iframe sandbox="allow-scripts">` (no `allow-same-origin` → **opaque
origin**: no access to the app's DOM/cookies/session) served with a strict **CSP**
(`connect-src 'none'`, `img/font data:` only → **no network exfiltration**). Its
only channel is `postMessage` to the parent, which forwards a few narrow actions,
each executed **as the viewer**:

| Bridge action | Forwarded to        | Effect |
|---------------|---------------------|--------|
| `kb-query`    | `/api/artifact/query` | arbitrary SQL as the viewer (RLS applies; SELECT-only on `kb`) |
| `kb-read`     | `/api/artifact/read`  | read a file as the viewer — **scoped to the artifact's own folder** |
| `kb-read-bytes` | `/api/attachment`   | the file's **bytes** as a Blob (video/image/PDF next to it), same folder scope, 512 MB cap. blob: is a local handle — bytes display without becoming sendable |
| `kb-write`    | `/api/artifact/write` | write a file as the viewer — **scoped to the artifact's own folder** |
| `kb-list`     | `/api/artifact/list`  | list a folder at/under its own (name, size, mtime, per-entry read/write), depth-limited — same folder scope |
| `kb-mkdir`    | `/api/artifact/mkdir` | create a subfolder (missing parents included, each inheriting its parent's audience) — same folder scope |
| `kb-delete`   | `/api/artifact/delete`| delete a file or subfolder as the viewer — same folder scope, and never the artifact's own folder or the artifact itself; a non-empty folder needs `recursive` |
| `kb-toggle`   | `/api/tasks/toggle`   | flip a checkbox — only on a genuinely indexed task line |
| `kb-fetch`    | `/egress` (hub)       | HTTPS to an **allowlisted domain only**, per artifact, with `secret:` refs injected server-side so the artifact never holds the credential |
| `kb-upload`   | `/api/upload`         | binary into the artifact's own `_files/`, as the viewer |
| `kb-save-as`  | `/api/file` + `/api/artifact/write` | writes **outside** the folder scope — allowed only because the destination is chosen by the user in trusted chrome, not by the artifact |
| `kb-clipboard`| (host page)           | copy text to the viewer's clipboard |

The default **To-dos** view (`company/todos.html`) and the demo dashboards are
themselves artifacts — the platform dogfoods its own runtime. Artifacts are
*author-trusted, viewer-scoped*: contained against the system and other users, but
an artifact you open runs code with your authority (like a shared spreadsheet
macro), which is why the file actions are folder-scoped.

The folder scope is checked by the host page for every file action (against the
tab the message came from, not the focused one). `kb-list`/`kb-mkdir`/`kb-delete`
send the artifact's path along and the backend checks it **again**: those verbs
create and destroy, so their containment must not rest on one caller remembering
to check.

## 7. Sharing & the permission UI

The ⚙ panel speaks **people**, not octal. `/fs/share` takes a scope — *same as the
folder it's in* / *everyone at Ollsoft* / *specific people* / *only me* — and
hub.py picks the mechanism per object, so the everyday action (adding or removing
one person) is a `gpasswd` on the owning group and touches no files at all. The
O(files) ACL walk happens once, when a viewer group is first bound to a folder.

`/fs/props` is still there, one disclosure down in **Advanced**: raw owner, group,
mode preset and named ACL entries, for the cases the people list deliberately
cannot express. Same authorization either way — only the owner or an admin may
change who can open something, and `chgrp` additionally requires **membership** of
the target group (owning the inode is not enough).

Sharing a **file** with someone who can't reach it would be a dead grant (Unix
needs traverse on every ancestor dir), so a read grant **auto-grants traverse-only
(`x`, no listing)** on the ancestors the grantee can't already traverse — skipping
any they can, since a named `--x` entry would *downgrade* them. The response
reports which directories got one. The indexer records those grants so search/RLS
agree with the kernel.

**Credentials (`_secrets/`) share like anything else — deliberately.** Sharing a
project stops at the `_secrets/` folder inside it (`_walk_repo` never descends
into one), and every key is born `0600` no matter how open the folder around it
happens to be: on a real box a `_secrets/` folder made in a terminal inherits the
project's group and default ACL, and that is not a decision anyone made about
credentials. Sharing the `_secrets` **folder itself** is that decision — the one
case where the walk does reach the contents, so the keys already in it follow,
and the folder is marked (`user.kb_secrets_shared`) so the keys added afterwards
follow too. Only the owner may widen one, not an admin, and the indexer is never
put in a secret's group or ACL. What `_secrets/` guarantees is unchanged and is
not about who may open it: never in git, never in the index, never a live CRDT
session. The panel says exactly that above the people list.

## 8. The UI shell — tabs, terminals, cron panel

VS-Code-shaped chrome over the same primitives (vanilla JS, `frontend/src/app.js`;
the writing surface itself is `frontend/src/richview.js`, which knows nothing
about the app — see §8.1):

- **File tree actions** (on hover): `＋` new file, `⊞` new folder, `⇪` upload files
  (all three only on folders you can write), `⚙` permissions, `✕` delete (only
  where you can write the parent). Deleting a file/folder retires any tabs
  showing it.
- **Deleting is moving.** `✕` renames the thing into a `.trash/` in its own
  folder: `company/plans/x.md` becomes `company/plans/.trash/x.md`. There is
  no id, no manifest and no metadata, because the filesystem already knows
  everything — where it came from is the folder the `.trash` sits in, when it
  went is the inode's ctime (a rename updates it), whose it is the file's own
  owner. **The permissions therefore take care of themselves**: the new
  directory inherits the folder's group, setgid bit and default ACL, the
  rename carries the file's own owner, mode and ACLs untouched, and nothing
  crosses an audience boundary because nothing leaves the folder. Restoring
  is the move back up one level (and an empty `.trash` is tidied away). The
  toast that follows a delete carries an Undo, and the person's ⋯ menu holds
  **Trash** (with a count) whose view puts things back or deletes for good.
  **Nothing is swept on a timer**: a knowledgebase that quietly eats what you
  deleted a month ago is worse than a folder that grows. `.trash/` is a
  dot-directory, so the tree hides it, the indexer skips it and a deleted
  document leaves search — while an agent with a shell reads it like any
  other folder. Two things are still deleted outright, with the old red
  question: a secret (a readable copy waiting in a trash is what `_secrets/`
  exists to prevent) and something already in a trash.
  `POST /api/fs/delete` (`permanent` to skip the trash), `GET /api/fs/trash`,
  `POST /api/fs/restore`, `POST /api/fs/trash-purge` — the last three take
  the path of the thing inside the `.trash`. The context menu (right-click, or `⋯` on touch) adds the rest,
  including **Upload folder** and **Download as ZIP**. Two inputs that used to
  be typed are now chosen: **Move to…** opens the folder picker (`pickPath`,
  the same one the chat's ＋ uses — filter, ⏎, done; a folder is never offered
  itself or its own children), and a **new file's name needs no extension** —
  a plain name becomes a `.md` document (`withDefaultExt`), while anything
  with a dot is taken exactly as typed, `.html` included. Moving or renaming
  something carries its pins with it (`repointPins`), and deleting it takes
  them away — your own list always, the company list when you may write it.
- **Download as ZIP** (`GET /api/folder-zip`, `user_server.folder_zip`): the same
  right-click gesture a single file already had, on a folder. The archive is
  built by the per-user backend, so the kernel decides what goes in: symlinks are
  never followed, `.git` and `.kbtmp` spools are skipped exactly as the tree skips
  them, and a file the user cannot read is left out rather than failing the whole
  download. Empty subfolders are carried as explicit entries so the folder
  unpacks the shape it had. Two details are load-bearing: the hub *buffers* every
  proxied `/api/*` response in memory as root, so the handler refuses a folder
  over `FOLDER_ZIP_MAX_BYTES` (1 GiB uncompressed) with a 413 instead of an
  unbounded allocation; and `?probe=1` answers that same 403/404/413 *without*
  building anything, which is what the UI calls first — a browser pointed at a
  download that errors navigates away to show the JSON, throwing away the open
  tabs and terminals.
- **Uploads of any size** (`/fs/upload/*` on the hub, `/api/upload/*` on the
  backend, protocol in `kb_platform/uploads.py`): the browser slices the file
  and sends it a chunk at a time (8 MiB), each chunk carrying its byte offset.
  This is what removed the "larger than the server's upload limit" wall: that
  ceiling was never the platform's — `client_max_size` is 2 GiB — it was the
  edge in front of it, where Cloudflare refuses a request body over 100 MB. No
  single request is big now, so no hop has an opinion about the file's size, and
  the only limit left is free disk, which `begin` checks up front and reports as
  a real sentence. The bytes stream to a spool file **in the destination folder**
  (`.kbup-<rand>.kbtmp` — born with that folder's owner/mode/ACL, and invisible
  to the tree, indexer, kb-convert and syncd, which all skip `.kbtmp`) and the
  finish call renames it into place: one atomic syscall, no second write, and a
  half-arrived file is never visible under its real name. Offsets make a retry
  free (`pwrite` puts a re-sent chunk exactly where it was), so a dropped
  connection costs one chunk; the client retries with backoff and resumes from
  the offset the server reports. Progress is what the user sees: a **ghost row**
  in the target folder for tree uploads, and for a drop or paste inside a
  document the **upload tray** (bottom-right, one live bar and percentage per
  file, each with a ✕ that aborts the session server-side too).
- **Folder uploads**: a whole tree can come in two ways — dropped from the
  desktop onto a tree row, or picked via *Upload folder*. Neither is a new
  endpoint: the client turns the tree into paths relative to the target
  (`sub/deep/a.png`), creates every folder on them first (`/api/fs/mkdir`, as the
  user, 409 = already there so a re-drop merges) and then POSTs each file to
  `/fs/upload?dir=<target>/<sub>`, so permissions and inheritance are exactly the
  single-file rules. `dataTransfer.files` cannot describe a directory at all, so a
  drop walks the `webkitGetAsEntry()` tree (paging `readEntries` to the end, which
  also carries folders holding no files); the picker uses `webkitdirectory` and
  `webkitRelativePath`. `.git` (it would become a gitlink in the audit repo),
  `.DS_Store` and `Thumbs.db` are skipped, deep/huge drops are bounded
  (2000 files, 24 levels), and a drop asks for confirmation with the file count —
  the browser prompts for the picker but not for a drop.
- **Live presence & cursors**: the doc header shows an avatar per person with the
  file open (initials on a per-user color, yourself ringed), and each
  collaborator's caret + selection render inline with their name — in both the
  rich and source views. Both ride the Yjs awareness relayed by kb-syncd; a late
  joiner re-announces the people already present so nobody waits for the ~30s
  awareness heartbeat. Colors are a stable hash of the username, so a person is
  the same color everywhere (avatar and cursor).
- **Rich ⇄ Source editing**: every markdown doc opens in a **rendered-but-editable**
  view (default) — a CodeMirror decoration layer over the *same* Y.Text, Obsidian
  style: headings/bold/links/images/quotes/tasks render in place and each
  construct's raw syntax reappears only while the cursor is inside it. The
  markdown source stays the single source of truth, so multiplayer, vim/agent
  merges, the indexer and todos are untouched (a rendered checkbox toggle rewrites
  `- [ ]`→`- [x]` in the source, same path as the todos artifact). A formatting
  **dock** (H1-3, bold/italic/strike/code, lists, task, quote, link, attach,
  hr, mic; Ctrl+B/I — one Attach button for every file type, since what a file
  becomes is decided by its type; phones add Photo, whose media-filtered picker
  is what opens the camera) floats at the bottom of the document, near the hand and off
  the eyeline: 38% opacity until hovered or focused, dimmer while you type,
  back on the next mouse move. On touch it is a keyboard accessory row docked
  to the bottom edge — the top of the keyboard while one is up
  (`interactive-widget=resizes-content`) — present only while the keyboard is
  up, measured from the visual viewport's height rather than from focus
  (Android's back button hides the keyboard and keeps the focus), and lifted
  above an overlaying keyboard on iOS by the same measure, so reading gets the
  whole screen; opaque, most-used first, ⋯ for the
  rest; a tap on it never takes focus, so the keyboard stays. Drag-drop and
  **screenshot-paste** insert media at the drop point —
  the bytes upload to a `_files/` sibling (as the user, chunked, reporting in the
  upload tray) and render inline; images land on their own block, other files as
  links. Dragging a row *out of the tree*
  into the text instead inserts a **link to that existing file** (`relLink` writes
  it relative to the document, which is what `resolveMediaUrl` needs to render an
  embed) — media embeds, anything else becomes a clickable link; the same drag
  dropped on a folder still moves the file. A **Source** toggle (persisted in
  `localStorage`) drops to raw markdown with line numbers. GFM task/strikethrough/
  table nodes come from `@lezer/markdown` extensions.
- **Tables are a grid you type into** (`TableWidget`, a block decoration in its
  own `StateField` — a view plugin may not replace line breaks). The GFM text
  stays the truth: a cell edit rewrites that ONE cell's range, so two people in
  different cells merge, and a structural change rewrites the block. A cell
  shows its markdown **rendered** (`renderInlineMd` — bold, italic, strike,
  code, links, images, `<br>`, built as DOM nodes out of escaped text, so a
  document can never inject markup and only http/mailto/tel ever becomes an
  href; a bare `https://…` is a link too, minus the punctuation that ends
  its sentence, and a link's own label is never linked again — it once was,
  recursed until the stack overflowed, and blanked every note below such a
  table) and hands back the raw text as an `<input>` while the cursor is in it.
  A table that still fails to build shows as its markdown rather than taking
  the rest of the note with it (`TableWidget.draw`).
  Keys typed in a cell belong to the table, except Ctrl+Z/Ctrl+Shift+Z, which
  are the document's: they flush the pending cell write and run the editor's
  own undo. Shift+Enter writes `<br>`, the one line break GFM allows inside a
  cell. The bar under the grid works on the END of the table (add/remove the
  last row or column); anywhere in the middle is a right-click on the cell you
  mean, which also deletes that row or column.
- **A table sizes itself to its content and never scrolls inside a cell.** The
  cell is a `<textarea>` in the same box as the rendered text — same type,
  same padding — that grows DOWNWARD as you type, and both states wrap. The
  table keeps `width: auto`, which is the rule already wanted ("as wide as the
  content needs, up to what is available"): a two-column table stays narrow, a
  heavy one fills the page, eight columns wrap rather than scroll. `width:
  max-content` looks equivalent and is not — CodeMirror's content element is a
  flex item that shrinks only to its min-content size, so a pinned max-content
  table drags the whole document sideways under one long sentence. While a
  cell is open the columns are frozen **as proportions** (the raw `**ship**`
  is wider than the `ship` it renders as, and a column that widens under the
  pointer is the jump krystof kept hitting), and every size change calls
  `view.requestMeasure()` so the editor's height map keeps up with the box.
- **The writing surface is a module of its own** (`richview.js`): widgets,
  live-preview decorations, tables, list metrics, @mentions, the markdown
  highlight style. It imports nothing from `app.js` — how to toast, ask, draw
  a menu, open a path, resolve a relative path to a URL and who may be
  @mentioned all arrive through one `init({…})` call. That is what lets the
  **public-link container mount the same editor** (`publicdoc.js`, see
  [public-sharing.md](public-sharing.md)): a stranger with a link gets the
  rendered markdown, the tables and the checkboxes a colleague gets, with a
  plain mtime-checked save instead of the CRDT. Both entry points are built
  by the same esbuild run and share the module through a split chunk, so a
  fix to the surface lands in both.
- **A touch screen draws the whole note while you read it** (`drawWhole` in
  `richview.js`, `syncWhole` in `app.js`). CodeMirror draws the lines on
  screen plus ~1000px either side and *guesses* the height of the rest, and
  on a phone both halves showed: a fling moves the page on the compositor
  faster than a phone's main thread draws the next lines, so blank patches
  (the whole screen at worst) slid into view, and the guess — its yardstick
  is whichever short line it meets first, often a heading — made a note up
  to 50% too long and corrected it under the thumb. Measured on a phone
  emulation with a 4–6× slowed CPU and a bad cellular link: 43 frames with a
  blank band over 180px in four flings down a 13 KB note before, none after,
  and one page height from the first frame. The switch is CodeMirror's own
  print mode (`viewState.printing`, internal — `tests/e2e/test_scroll_whole.py`
  fails if an upgrade moves it), with the parse forced to the end first so
  the lines below the fold are drawn rich the first time. It is off while the
  keyboard is up — a note drawn whole is re-laid-out on every keystroke
  (33→53 ms a key at 13 KB, 46→145 ms at 38 KB on the same slowed CPU) —
  and for notes past 64K characters (2.4% of a real knowledgebase), where the
  one-off drawing costs ~0.7 s. Desktops keep the window. The live-preview
  layer recomputes when the parse advances, not only when the viewport
  moves, or a note drawn whole would keep raw `##` at its tail; a table
  tells CodeMirror its height before it is drawn (34px a row + the bar); and
  the touch listener that stops a held tab from scrolling the page sits on
  the tab strips, not the document — a page-wide cancelable `touchmove`
  makes every scroll of every note wait for the main thread.
- **Reading affordances in the rendered view**: a `code span` carries the same
  one-click copy a fenced block has (`InlineCopyWidget`, faint until hovered —
  inline code is everywhere in these documents), and an **@mention of a real
  account** is coloured (`mentionHighlight()`, `.cm-mention`). Both are
  decorations over the unchanged markdown, so nothing reaches the file. The
  mention scan uses character-for-character the indexer's `ASSIGNEE_RE` and
  consults the syntax tree before marking, so an `@` inside code or a URL stays
  plain and a highlighted name is exactly one the to-do index will also pick up.
  **Your own** tag is the exception that gets its own treatment — a yellow glow
  (`.cm-mention-me`) rather than one more flat colour, because scanning a long
  document the eye finds a light before it reads a hue. The roster comes from
  `/api/principals` (the same list the `@` autocomplete uses) and the viewer's
  name from `/api/whoami`; both are boot fetches in no fixed order relative to
  the first document opening, so *either* landing calls `repaintMentions()` —
  whichever loses the race would otherwise leave the editor a mention short, or
  colour your own name as somebody else's.
- **Pasting a URL writes the link.** GFM autolinking is deliberately not among
  the loaded markdown extensions, so a bare address is not a link in this
  dialect — it used to paste as dead text that rendered as dead text.
  `pasteAsLink` (a `paste` handler beside the media one, so it works in rich and
  source alike) turns a pasted `http(s):`/`mailto:` URL into markdown: over a
  selection it becomes that selection's link, on its own it links to itself.
  It stands down wherever the URL is content rather than a link — inside a code
  span, a fenced block or an existing destination, asked of the same syntax tree
  the mention scan uses — and wherever the selected label carries a bracket or a
  newline, since writing markdown that cannot parse is worse than pasting
  plainly. `mdDestination` leaves balanced parentheses unwrapped (CommonMark
  allows them, and `…_(disambiguation)` reads better in source) and angle-wraps
  anything else.
- **Spaces in link targets.** CommonMark refuses a bare space in a link
  destination, so `[q3](_files/q3 final.xlsx)` is not a link at all — it renders
  as literal text and cannot be clicked. Both halves are handled: an upload now
  percent-encodes the target per segment (`uploadAndInsert`, matching `relLink`)
  and strips brackets out of the label, and a `SpacedLink` inline parser
  (`markdown({extensions: [...]})`) parses the raw-space form anyway, so links
  already written into documents keep working. It only claims what the built-in
  parser refuses — a destination with whitespace, no angle brackets, no quoted
  title — and emits the same `LinkMark`/`URL` children, so the live-preview layer
  needs no special case. Destinations are read from the syntax tree via
  `linkTarget()` (which also unwraps the `<…>` form), never by regex over the
  source: a regex stops at the space and yields a truncated path.
- **A chat is pointed at what you have open.** Chips above the message box
  name the visible document of every group (automatic, dashed), the files you
  added by hand, and the folder the session stands in; every prompt carries
  them as ACP `resource_link` blocks and the folder goes in `session/new`.
  The picker behind ＋ is `pickPath()` in `app.js` — the palette's shape, but
  it returns a path. A row **dragged out of the tree and dropped on the chat**
  makes the same chip (`wireDrop` in `chat.js`, taking only the tree's own
  `application/x-kb-path` payload — an OS file dropped there is a picture for
  the composer), with the same refusals: a folder, a `_secrets/` path, the
  cap. See [agent-chat.md](agent-chat.md).
- **A link for someone with no account.** The share panel's last section
  makes one: read or edit, an optional password, a deadline (14 days by
  default, 90 at most). The platform grants `kbshare` an ACL on that subtree,
  bind-mounts it into `/srv/kb-public/data/<id>` (read-only unless the link
  may edit) and writes a config file that never names the real path; the
  `kb-share` container serves it and nothing else. The URL exists exactly
  once, in the answer that created it — only hashes are stored. Revoking
  unmounts. `GET/POST /fs/public`, `POST /fs/public/revoke` (hub, root);
  `kb_platform/publicshare.py` is the host half, `public/serve.py` the
  container, and [public-sharing.md](public-sharing.md) the whole design.
- **The inbox: what happened while you were elsewhere.** One append-only file
  per person, `users/<u>/.os/inbox.jsonl`, written by whichever root process
  actually saw the event — syncd when a document you were newly `@named` in
  is committed (it diffs the names against `HEAD~1`, so a name that was
  already there is not an event and a restart changes nothing), the hub when
  something is shared with you by name (a group is a standing audience, not
  news). Both check first that you can READ the thing: an inbox line carries
  a path and a line of text, so telling you about a document you cannot open
  would be the leak, not the courtesy. A secret never notifies at all.
  The app only reads the file and marks lines read (`GET /api/inbox`,
  `POST /api/inbox/read`): a dot on the person, a count beside **Inbox** in
  their ⋯ menu, and a list whose rows open the document at the line. A file
  rather than a table because everything else here is a file — an agent can
  read it, the backup already covers it, nothing has to migrate.
- **A path is a link, however it is written.** `/company/notes.md` has always
  been a route; `/srv/kb/company/notes.md` — the path an agent prints, and
  what people paste after the host — now redirects onto it (`abs_deep_link`
  in the hub, registered from `common.REPO_ROOT`, and only for the three
  areas). In a chat transcript such a link opens the document in this window
  instead of a new tab: the markdown sanitiser turns any href inside the
  knowledgebase (a `file://` resource link, an absolute path, a same-origin
  route) into `data-open-path`.
- **Pinned things are rows, and you pin by right-clicking.** The sidebar's
  first section lists what the company pinned (its icon takes the accent, and
  its tooltip and menu say so — the row carried the word "company" until
  krystof called it a word too many) and what you pinned yourself, as rows
  shaped exactly like the tree's — a pin is a file to open, a folder to reveal, or a command to run in
  a fresh shell (`kind: file | folder | term`). They were pills in two colours
  above the tree until 2026-09-21; a pill in a list of rows reads as an alien,
  and the colours encoded something a word says better. A tree row's menu
  offers "Pin to the sidebar" (and the reverse once it is pinned), and a pin
  row's own menu offers Open, Rename and Unpin — and, for an admin, the move
  between the two lists ("Pin for everyone" / "Keep it just for me", which
  writes the destination list before clearing the source, so a failed write
  can duplicate a pin but never lose one). With nothing pinned the
  section is not drawn at all; the dialog that writes a pin by hand — the only
  way to pin a *command*, and where an admin edits the company list — is
  "Pinned items" in the user menu, beside Settings. Personal pins live
  in `users/<you>/.os/launchers.json` (private, written by your own backend),
  the company's in `.os/launchers.json` (written only by the hub, admin-only);
  the API keeps the older name `launchers`.

- **The person, bottom left.** The sidebar ends in the signed-in user; one
  click opens the user menu with everything that is not a file or the search:
  Settings, Pinned items, Admin (admins and network delegates), Cron,
  New terminal (always a fresh shell — Ctrl+` is the way back to one you
  have), Dictation history, Keyboard shortcuts, Sign out. The top bar keeps
  only the brand, the search and the mic. On a phone the same menu sits at the bottom of the drawer,
  under the same Pinned / Chats / Files list a desktop shows. New documents
  come from the tree's "New file here", Alt+N, or the palette — there is no
  button for it.
- **Themes are token blocks.** `frontend/assets/style.css` names no colour
  outside `:root` and the `:root[data-theme=…]` blocks; every rule uses a token
  or a `color-mix()` of one (washes, borders, shadows derive from ~40 base
  values). Type and space are tokens too — the base size every rem follows,
  the editor's size and line heights, the written column's inset and width,
  heading sizes, row and tab density, the sidebar's padding, two radii — so a
  theme is a feel, not only a palette: deep blue keeps its numbers, Dark and
  Light are Notion's, measured — the app shell Notion ships (page and sidebar
  colours, hairlines, the 240px sidebar, 1.5 lines, the system font stack),
  the notion-enhancer extraction of the live app (text, secondary, border,
  hover, overlay, accent and scrollbar values per mode) and its published
  palette; 16px at 1.5 with 3px blocks, a 708px column behind 96px gutters,
  bold 700 headings at 1.875/1.5/1.25em, 14px sidebar rows, 6px corners and
  10px popovers, sans-serif section labels instead of mono capitals, links
  in ink with an underline, popovers on their own surface (`--pop`). Surfaces,
  labels, links, quotes, the search box, heading rhythm and the scrollbar are
  tokens so a theme can differ from deep blue in each. `ui.theme.custom`
  overrides any of these per person or company. `ui.theme` sets `data-theme` on `<html>` from a cached value before
  first paint; CodeMirror's highlight style reads `--md-*` tokens through CSS
  variables so it retints live, and xterm is handed a theme built from the
  `--term-*` tokens when it opens and again on every theme change. The sign-in
  page has no theme yet and always wears deep blue, on purpose.
- **Settings** (in the user menu, or "Settings" in the palette) is a dialog
  generated from the registry the backend serves: one row
  per setting, the control from its type, a pill saying which layer the value
  came from, × to clear that layer; admins get a Company tab over the same rows.
  `frontend/src/settings.js` holds the resolved values, refetches at boot,
  after every save and when the tab becomes visible, and applies `ui.theme` as
  `data-theme` on `<html>` before the first paint from a cached copy. Nothing in
  the client knows a setting by name; see [settings.md](settings.md).
- **The palette (`Ctrl/Cmd+P`, `>` for commands) never moves.** It is the one
  place you search from — file names, commands and document contents in a single
  list — and it composes two sections that arrive at different times: filename
  matches are scored locally against the client's copy of the tree and are on
  screen within the keystroke, document matches come back from `/api/search` a
  few hundred ms later. Two things follow, and both were once wrong:
  *the card's height is fixed* (`height: 74vh`, not `max-height`), because a
  content-sized panel was small while the local matches were all it had and
  jumped to full size when the server answered — resizing under a pointer
  already travelling towards a row; and *the late section renders last*.
  Documents used to insert above the files, which shoved every row down at
  exactly that moment. Now files sit on top, documents append beneath them, and
  the "Searching documents…" spinner occupies precisely the slot they will fill,
  so the results replace it in place and nothing above it ever moves. That also
  removed the index arithmetic that used to re-anchor the selection: every index
  already on screen keeps its meaning, so Enter cannot change what it opens
  depending on whether the server has answered yet.
- **Editor tabs**: every opened document/artifact is a tab; each keeps its own
  live mount (CodeMirror + Yjs provider, or sandboxed iframe) in a hidden
  container, so switching is instant and **background artifacts keep running**
  (their bridge messages are routed by `ev.source` to the tab they came from, and
  the file actions stay scoped to *that* artifact's folder, not the focused one).
- **Boot paints what it already knows first.** `boot()` used to be a serial
  chain — a deep-link probe, whoami, the tree, `/admin/me`, and only then
  `restoreSession()` — so the tab bar and the terminal panel arrived ~2 s after
  the page looked ready, each one shoving the editor when it landed. Measured:
  the tree alone was 580 ms + 640 KB, and `restoreSession()` never reads it (it
  reopens tabs and terminals from `localStorage`). Now the tree fetch and the
  deep-link probe leave immediately, only whoami is awaited (the restore needs
  `canShell`, and whoami now carries the pty protocol `v` so a restored
  terminal never races the old `/api/cron` answer into a hard reset), the
  restore starts, and the tree paints whenever it lands — `restoreTreeState()`
  runs inside whoami, so folders are open/closed as you left them on the first
  paint. `/admin/me` is fire-and-forget; the probe is awaited only right before
  the deep link and `syncUrl` need it. The two orderings that were bugs before
  (read `location` before restoring; only ever `replaceState` while settling)
  are unchanged. `app.html` `modulepreload`s the bundle and preloads the three
  first-paint fonts (`crossorigin`, same `?v=` as `style.css`, or it is a
  second download) so text does not reflow when IBM Plex arrives.
  The restore itself is two phases because its halves wait on different
  things. `restoreTabs()` runs at t=0 from `localStorage` alone — before
  whoami: `openPath` registers and draws each tab synchronously, the saved
  active tab is activated at once so the first frame is right, and nothing a
  tab needs knows who you are (an expired session bounces on the first 401
  whichever request it is). The one thing that did depend on identity — the
  collaborator name announced to the CRDT session — is read live and
  re-announced by `loadWhoami` (`t.announce`), so a tab restored early never
  stays introduced as "user". `restoreRest()` runs after whoami and the
  terminal wiring: terminals immediately (they need `canShell` and the pty
  protocol `v`, not the documents' websockets they used to queue behind), then
  the tabs' contents and the tidy-up that depends on them. **xterm is its own
  chunk** (`src/term.js`, `static/chunks/term-<hash>.js`, esbuild `splitting`):
  a quarter of the bundle that a viewer never needs and nobody needs to read a
  document. `warmTerminal()` fetches it on the first terminal — or at t=0 when
  a restore already knows it will want one — and the hash lets the hub serve it
  immutable. The cost of the hash: `deploy.sh` rsyncs `--delete`, so a page
  from before a deploy asking for its *first* terminal finds nothing at the old
  name and is told to reload rather than shown a blank panel.
- **The tree is cheap to ask for again.** Every open tab used to poll
  `/api/tree` every 4 s, and it was a full permission-checked walk each time — 580 ms of
  which more than half was `pathlib.relative_to` building objects to compute a
  string `scandir` already provides — run *on* the per-user event loop, so a
  poll froze that user's terminal and every other request for its duration.
  The backend (`user_server.tree`) now splits the question: a **signature
  walk** (scandir + one `lstat` per entry, no ACL reads: name, mode, owner,
  size, mtime, ctime — `chmod`/`setfacl` bump ctime, so permissions are covered
  without an xattr) at ~37 ms decides whether anything moved, the real walk
  (~180 ms, Path-free, byte-identical output) runs only when it did, and the
  cached JSON's hash is the `ETag`. A poll with a matching `If-None-Match` is a
  304; the client sends it with `cache: "no-store"` so the browser's own cache
  does not hand back a 200 to re-parse. The signature is checked at most once
  per `TREE_SIG_TTL` (2 s) per backend — all of one user's tabs share the
  process, so N tabs cost one check — and any non-GET `/api/*` that succeeds
  clears the hold via middleware, so your own new file is in the next poll
  without enumerating every mutating handler. Everything blocking runs in the
  executor. Not inotify, deliberately: a recursive `watchfiles` watch over the
  repo as an unprivileged user stayed silent when tried, inotify instances are
  capped per uid (31 of 128 in use here), and inotify drops events under load;
  the signature never lies and needs no fallback. Since 2026-09-20 the poll
  itself is gone (next bullet); this path is the catch-up and the fallback,
  and stays exactly as cheap. Cost now scales with change
  rate, not users × tabs; the signature is still O(entries), which is the
  hand-off point to a lazy per-folder tree past ~20k files.
- **Nothing polls.** A tab holds one `GET /api/events` open
  (`frontend/src/events.js`) and the backend pushes. Server side one walker per
  backend, alive only while a stream is open: the signature check every 2 s
  (or at once after a write through this process), presence from syncd over
  its world-connectable socket every 3 s, fanned out to all of that user's
  tabs. The `tree` event carries a **delta** built from two path→row indexes:
  when only file mtimes moved — what typing produces — the client patches
  those rows and adopts the new ETag; anything structural (added, removed,
  permissions, audience, >200 rows) is `full`, and the client refetches with
  its ETag **after typing pauses**, never under a keystroke. That is what
  removed the 4 s stutter on phones: a 641 KB tree parsed and rebuilt on the
  main thread every poll while your own saves kept the signature moving.
  The wire is silent between changes except a `ping` every 20 s — a real
  event, because SSE comments never reach JavaScript, and short of
  Cloudflare's 100 s idle cut. The client owns every failure: it closes and
  reopens with backoff (3 s → 60 s) rather than letting the browser hammer,
  reopens after 60 s of silence (a half-open socket after a network switch),
  reopens or catches up on `visibilitychange`/`pageshow`/`online` (a frozen
  mobile tab's stream dies without an error), probes the session with one
  ordinary fetch after an error (a 401 is invisible to EventSource; the guard
  redirects), and after five failures in a row falls back to slow polling
  (15 s, visible only) while still retrying the stream every minute. Every
  reconnect's `hello` is the catch-up, so events missed while away never
  matter. The hub pipes the stream chunk by chunk with no client timeout
  (`proxy_http`); a reverse proxy in front needs buffering off.
- **The tab strip is Chrome's, for Chrome's reason.** Every tab in a strip is the
  same width (`flex: 1 1 0`, capped at `--tab-max` so two tabs do not stretch
  across the window, floored at `min-width` so they stop shrinking and the strip
  scrolls); the icon and the `×` never shrink, only the name ellipsises. That is
  not cosmetic: content-sized tabs put every `×` at its own unpredictable
  offset, and equal widths put them on a regular pitch. The pitch is then made
  *useful* by the **close-streak lock** — closing a tab with the pointer widens
  the survivors and slides the next `×` out from under the cursor, so
  `lockTabStrip` pins `--tab-max` to the width the tabs measured at the moment
  of the close. The strip keeps that width (leaving a gap at the right, exactly
  as Chrome does) until `pointerleave` on the strip, a new tab, or a window
  resize releases it, and the transition on `max-width` glides them back. Only a
  close the MOUSE performed freezes anything (`closeTab(t, fromPointer)`): a
  keyboard close, or a tab retired because its file was deleted, has no cursor
  to keep a `×` under and must re-flow at once. While locked, every `×` is shown
  rather than just the hovered one — the row under the pointer has just been
  rebuilt, and a browser need not re-evaluate `:hover` until the mouse next
  moves, which for a pointer deliberately holding still is never. Only the
  MOUSE leaving releases the lock: a touch pointer stops existing the instant
  the finger lifts, so `pointerleave` fires after every tap and releasing there
  would re-flow the strip between taps; for touch the streak ends at the next
  `pointerdown` outside a locked strip. `revealCurrentTab` keeps the tab you
  switched to on screen when the strip is scrolled (its own `scrollLeft`, not
  `scrollIntoView`, which would scroll ancestors too) and stands down while
  locked. Deliberately NOT copied from Chrome: its larger minimum width for the
  active tab, which would make tabs unequal exactly when the equal pitch is
  worth the most.
- **Deep links — the open document IS the URL**: `/company/notes.md` in the
  address bar opens that file (hub route `deep_link`, which serves the same
  `app.html` for every repo path and bounces an unauthenticated visitor through
  `/login?next=…`), and switching tabs keeps the URL in step, so a document's URL
  can be pasted straight to a colleague. Three orderings matter and each of them
  was a bug once:
  (a) boot reads `location` **before** `restoreSession()`, because restore
  activates every tab it reopens and `activateTab → syncUrl()` `replaceState`s
  the bar onto it — reading afterwards opened the restored document instead of
  the link someone sent;
  (b) `syncUrl` only ever *replaces* while `_restoring` or `_settling` (boot) is
  set, so arriving on a link adds no history entry to go Back from;
  (c) a session that expires mid-visit carries the current path into
  `/login?next=…` too, rather than dropping the reader on `/`.
  The whole mechanism is gated on `_deepLinksOk`, a `HEAD` probe at boot: against
  a hub too old to route repo paths, rewriting the URL would turn F5 into a 404.
  Regression cover lives in `tests/e2e/test_ui_ux_round.py` — note that a
  deep-link test in a *fresh* browser context exercises the one case that never
  broke, so the ordering test deliberately reuses a context that already has a
  saved session.
- **Groups, columns and the dock (one layout for everything)**: the editor
  area is a *workspace* of *columns*, each column a stack of *groups* — a tab
  strip, a host and one visible tab — plus the *dock*: the group that lives
  outside the workspace as the bottom panel (or a column on the right or left)
  and holds the terminals by default. A terminal is a tab like a document; so
  is an agent chat. Drag a tab onto a group's left or right edge for a new
  column, its top or bottom edge for a new group above or below, its middle to
  move it in; the dock takes drops but is never split. On a phone or tablet
  the same drag is a hold: a tab held still for a third of a second lifts
  (a ghost follows the finger, a swipe before that stays a scroll) and lands
  on a strip, in a group, above or below one, or in the panel. Alt+\ and Alt+Shift+\
  split the focused group's tab right and below. Every strip ends in the same
  actions — ⤢ maximize (Alt+Z, or a double-click on the strip's empty space;
  the other groups are hidden, not closed, and ⤡ restores) and ▾ fold — and
  each appears only when it would do something: maximize is absent while a
  group is the only one holding tabs, fold is absent on the last open group.
  (There is no per-strip ＋: a terminal comes from Ctrl+`, Ctrl+Shift+`, the
  person's menu or the palette.) The same rules hold everywhere: a folded group is a
  one-line handle naming its tabs (a column of folded groups a thin rail) that
  a click, a tab activation or a dropped tab opens; the last open group of
  the workspace never folds (the dock always can); a group whose last tab
  leaves goes, and the last group of all stays open; while a group is
  maximized a document opens into it, and activating a tab that lives in a
  hidden group brings the layout back — what you asked for is what you see.
  Sizes reach the stylesheet as shares of the *visible* groups only, so a
  fold, a maximize or a close never leaves a gap (a saved share is a fraction
  of 1, and flex would hand out only that fraction). The keyboard on a folded
  group's handle acts on that group (Alt+W closes the tab the handle names,
  as does a middle-click on it); Ctrl+` while a group is maximized restores
  the layout and goes to the terminal. Under the phone breakpoint groups
  only stack — a drop on a group's left or right edge is a drop above or
  below it — and a stack refuses a group it cannot give 110px, as a row
  refuses a column it cannot give 160px. The sheet's automatic "full" and
  its remembered half / full are a *phone's* (narrow and touched): a desktop
  window dragged narrow renders the phone layout but keeps its record. The
  keyboard never lands on `<body>` — after a close, a fold or a maximize the
  focused group's tab takes it, and "Focus the next group" visits folded
  groups by their handles. A strip narrower than 240px gives up ▾ before it
  truncates a name; a group narrower than 420px or shorter than
  240px hides the floating toolbar on a mouse screen. The palette moves
  tabs and picks the dock's side (not on a phone, where the dock is always
  the sheet). `tabs` stays the one flat list (a tab's `paneId` says which
  group), `panes` the workspace groups in reading order, `columns` their
  stacking; the dock is `dockPane`, the terminal panel's own markup adopted as
  a group so every id and test hook stays. Two notions stay apart: the
  **active document** (`active`, what the header, toolbar, badge and URL
  describe — a terminal never becomes it) and the **focused group** (where the
  keyboard is; Alt+] / Alt+[ / Alt+1…9 / Alt+W act there, so in the dock they
  cycle and kill terminals). The rules — what a valid layout is, how a
  pre-2026-09 session record maps onto it, how a phone renders it — are the
  DOM-free `frontend/src/layout.js` (`node --test tests/js`); the session
  record is v2 (`columns`, `dock`, `focused`, `maximized`) written beside
  every v1 field derived from it, so a browser on the previous bundle still
  restores its tabs. Group elements are never re-inserted (an artifact iframe
  would reload); a tab's element moves. The design and its reasoning:
  [unified-views.md](unified-views.md).
- **View kinds**: what a tab can show is a registration in
  `frontend/src/views.js` — label, icon, `open(t, spec)`, `serialize`,
  `restore`, default `placement` (the active group, the dock, a side column),
  whether it is a document the header describes. Documents, artifacts, secrets
  and terminals register from `app.js`; the agent chat from its own lazily
  loaded chunk. The shell knows no kind by name. Modules add palette commands
  with `registerCommand` and chrome buttons through a named slot.
- **The chrome**: on a desktop the brand row is ☰ · logo · the product's
  name (which gives way only under ~190px of panel); the ☰ is a borderless
  icon that takes a background on hover, while the corner's floating one
  keeps its edge because it sits over the document, and it is the head of
  the file panel and the editor column starts at the top of the window, so
  every top group's tab strip is the top of the screen; under the search sits
  **Pinned**, then **Chats** (a compose button, the five most recent
  conversations, and a ⋯ row that opens the rest in place) and **Files** —
  one scrolling list with sticky section headers, not a fixed block above a
  scrolling tree. ☰ (or Alt+B) collapses the panel to nothing, remembered
  per browser (`kbNavHidden`), and a corner control keeps ☰ and a compose
  button reachable. A tab strip is 38px in deep blue and 44px in the
  Notion-style themes, 44px on any touch screen, and 48px on a phone, where
  the tabs fill it exactly so the ☰ beside the first one lines up. The divider between two
  groups is a hairline with a 5px invisible grab zone around it (a painted
  5px handle read as a trough of page background between the panes), and it
  is the only line there — the panes draw no border of their own against it. On a phone there is no brand row
  at all: the first group's strip is the top of the screen, and the topbar
  shrinks to its ☰ — a small square fixed in the strip's corner (the strip
  leaves it `--nav-w` of padding; it is `--strip-h` tall and on the strip's
  background, so tabs scrolled left pass under it). The drawer opens beneath
  the strip with the ☰ above its scrim, so ☰ closes it again; a maximized
  group covers the ☰ like everything else. A new chat comes from the drawer's
  Chats section. The document bar (path, history,
  presence, Rich | Source, the pencil, the access badge — the pen / eye is a button that opens "who can open this") is one element that
  lives inside the active document's group — under its strip on a desktop, at
  the group's bottom on a phone, where it steps aside for the keyboard row.
  The formatting dock floats over the same group. Only a group's visible tab
  is highlighted; the document the header describes keeps its `.active`
  class for the tests and the URL, not a look.
- **The written column**: one inset (`--gutter`, computed per pane on
  `.cm-editor`) positions the text, and everything that sits with it —
  quotations, tables, list markers. A code block is a card aligned to that
  column in the Notion-style themes on a desktop, where the gutter is wide,
  and a full-bleed tint in deep blue and on any phone, where it is not
  (`--block-inset`). List lines hang: the marker sits in the margin and
  wrapped lines line up under the first word (`.cm-listline`, `--list-hang`
  measured from the marker actually there).
- **Zoom is not a keyboard**: a pinch (a phone's, or a trackpad's) shrinks
  `visualViewport` exactly as a soft keyboard does. Both handlers — the
  keyboard row's lift and the viewport pinning that keeps a terminal above
  the keyboard — check `visualViewport.scale` first and stand down while the
  page is zoomed, or the app is squeezed into the zoomed rectangle (the top
  cut off, the document bar looming) and the scroll handler fights every pan.
- **File tree width**: dragged on the gutter between tree and editor, clamped to
  [140px, 60% of the window], remembered in `localStorage` (`kbSidebarW`) and
  applied at module eval so the tree never snaps after boot. Truncated row names
  reveal themselves as a native tooltip, set on hover only when the label is
  actually cut off.
- **Terminals**: tabs of kind `term` — xterm + PTY websocket each — in the
  dock unless dragged elsewhere. `＋` spawns in the dock, `×` kills, shell
  `exit` retires its tab (the server's `{"exit":true}` frame). A tab only dies
  when the *shell* dies: a dropped connection dims the terminal, badges it
  "reconnecting…", and reattaches on its own (1–15 s backoff; instantly on
  network-online or tab-visible), replaying only the missed bytes. Reloading
  the page reattaches the same running shells wherever their tabs were
  (session ids + names persist with the layout, the processes in the
  backend); a restored terminal is a "⟳ name" placeholder in its group until
  whoami has answered. Ctrl+`` ` `` toggles the dock when it has tabs, goes
  to a terminal that lives elsewhere when it is empty, and spawns one only
  when there is none anywhere. **A phone has no panel at all**: the screen
  holds one group, so a terminal is simply a tab in it (`placePane` sends
  the dock's kinds to the active group, and a session built on a desktop
  has its panel tabs lifted into the workspace on arrival). The dock is in
  the page flow, not an overlay
  — folded (▾, Ctrl+`) it is a one-line handle at the bottom of the editor
  area ("▴ Terminal · bash 1"), so nothing open is ever invisible; its size
  persists. On a phone the dock is the terminal sheet and takes only
  terminals; a document dropped there is refused with a toast. The touch
  keybar is one fixed row under whichever group the keyboard is in, when
  that group shows a terminal.
- **Agent chat**: a tab of kind `chat` (top-right button, Alt+C) that drives
  an AI coding agent — Claude Code, Codex, Gemini CLI and the rest of the ACP
  registry — through the person's backend, which runs the agent as them in
  the knowledgebase and keeps it alive across reloads. Streamed markdown, tool
  calls as cards, diffs, permission prompts, plans, slash commands, modes,
  sign-in from the picker. [agent-chat.md](agent-chat.md).
- **Cron panel**: "Cron" in the user menu opens the user's crontab (§2 endpoints):
  list, add (with presets), pause/resume, delete.

## 9. Admin — user & group management

`/admin/*` (admin group only — `KB_ADMIN_GROUP`, default `sudo`). Creating a user does the full provisioning in one
call: `useradd` + password + add to `kb-users` + private `users/<u>/` dir +
Postgres `CREATE ROLE` + personal `u_<u>` schema + a profile (first/last/email in
`/etc/kb/profiles.json`). Deleting reverses it all (incl. dropping the PG schema
and stripping the user's ACL grants so a recycled uid can't inherit them). Guards
prevent removing protected accounts or granting privileged groups via the UI.
`POST /admin/settings` writes the company layer of the settings registry
(`.os/settings.json`, in place so a write ACL would survive) and is audited as
`settings.company`; `POST /admin/brand/logo` uploads or resets the company
logo (`.os/logo.svg|png`, SVG or PNG ≤ 512 KB, an SVG with script refused),
same audit event; `POST /admin/launchers` replaces the company launcher list.
`GET /brand/logo` is public — the sign-in page needs it — and serves the
uploaded file or the built-in mark with a no-script CSP; `GET /login` has the
company's `brand.name` substituted on the way out.

## 10. Agents

Agents run as the user (`claude` / any harness) — an SSH/PTY shell or a headless
`claude -p`. They edit files, which flow through `kb-syncd` into live browser
sessions; they query Postgres as themselves; they schedule work with their own
`crontab`. Company **skills** in `/srv/kb/.claude/skills/` (root-owned,
world-readable, admin-write-only) teach them the platform:
`kb-orientation`, `kb-database`, `kb-automation`, `kb-artifacts`, `kb-todos`,
`kb-history`, `kb-audit`, `kb-settings`.

## 11. The two trails

Two separate mechanisms, answering two questions people routinely confuse.

**Content history** — what a document said and who wrote it. `kb-syncd` commits
every flush to `/srv/kb/.git`, attributed via the attrib-hint drop-box
(`/run/kb/attrib`, mode 1733): the per-user backend writes a hint AS the user,
so the hint's `st_uid` is the kernel's word on who acted. Served by
`/api/vc/*` and the `kb-history` CLI over a `SO_PEERCRED` socket, gated per
request with a fresh `runuser -u <user> test -r` so revocation takes effect
immediately.

`.git` is `0700 root`, so history cannot be used to read around file
permissions.

**Privileged-action audit** — who changed who can see what. `kb-history` tracks
content and is silent on permissions, which is the question that matters after
an incident. The hub logs six events to journald as
`hub AUDIT <event> actor=… result=…`: `login` (both outcomes), `share.set`,
`props.set`, `group.member`, `user.create`, `settings.company`. Other privileged actions — user
deletion, the full↔viewer switch, group create/delete, the launcher list, the
egress allow-list — write no AUDIT line.

**Access audit** — who opened what. Four events on the same line format record
access being *used*: `document.open` (a `/ws/doc/*` session syncd accepted),
`file.preview` and `file.download` (`/api/attachment` served inline, or with
`dl=1` as an explicit download), and `folder.download` (`/api/folder-zip` served
a whole folder as one archive — the `?probe=1` preflight serves no bytes and is
deliberately not recorded). The hub is the only place that can emit them,
because it is the only component that holds both the authenticated identity and
the downstream service's answer — so an event exists only after the kernel has
already allowed the read, and a refusal can never look like one. Nothing else
under `/api/*` is recorded: tree, search, presence and CRDT traffic are the app
breathing, and logging them would bury the signal.

Readable only by root and `sudo`/`adm`/`systemd-journal` — journald shows every
other account nothing but its own messages, so the audited cannot read the
audit. Reads through SSH, the mounted drive or the secrets viewer are not
logged, and root-side changes bypass it entirely; see
[SECURITY.md](SECURITY.md) for the full limits.

## Data-flow example: toggling a checkbox in the To-dos view

1. The To-dos artifact renders tasks it read via `kb-query` (RLS-scoped).
2. You tick a box → `kb-toggle {path,line}` → the parent forwards to
   `/api/tasks/toggle` on your backend, which verifies the line is a real indexed
   task and flips `- [ ]`→`- [x]` in the source `.md` **as you**.
3. `kb-syncd` sees the file change, merges it into any live editor of that doc.
4. `kb-indexer` re-parses the file; the checkbox state updates in `kb.blocks`.
5. `kb-syncd` debounces a git commit. Every window on that line stays in sync —
   one is a view of the other, never a copy.
