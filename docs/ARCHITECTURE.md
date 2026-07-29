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
  places that exist (RLS in SQL, and the daemon's `fs_can`) exist only to *mirror*
  the kernel for surfaces the kernel can't reach (a shared DB index; a root daemon).

## The processes

| Service      | Runs as     | Bound to            | Job |
|--------------|-------------|---------------------|-----|
| `kb-hub`     | **root**    | `127.0.0.1:8300`    | PAM login, session cookies, spawns & reverse-proxies per-user backends, privileged `/fs/*` + `/admin/*` |
| user backend | **the user**| `/run/kb/users/<u>/backend.sock` | files, terminal, search, tasks, artifact bridge — *all as the user* |
| `kb-syncd`   | **root**    | `/run/kb/syncd.sock`| y-websocket CRDT relay + filesystem merge daemon |
| `kb-indexer` | `kbindexer` | (no socket)         | parses markdown → Postgres; refreshes group membership + ACLs |
| `kb-convert` | `kbindexer` | (no socket)         | office/PDF binaries → hidden read-only `.md` sidecars; each parse in a throwaway child (see [converted-documents.md](converted-documents.md)) |
| PostgreSQL   | `postgres`  | local unix socket   | disposable index (`kb` schema, RLS) + per-user schemas (`u_<user>`) |
| `kb-heartbeat.timer` | **root** | (timer, 5 min) | functional health check — outcomes, not processes; logs, never pushes (see [monitoring.md](monitoring.md)) |
| `kb-maintenance.timer` | **root** | (timer, daily) | triages the logs via a headless agent with a harness-enforced tool allowlist; the only on-box thing that may notify |
| `kb-gitgc.timer` | **root** | (timer, weekly) | `git gc --auto` on `/srv/kb` — syncd commits every edit and nothing else ever repacks the audit history |

Only `kb-hub` and `kb-syncd` run as root, and they do only auth/proxy/merge —
the minimal audited surface.

## 1. `kb-hub` — the front door (root)

`kb_platform/hub.py`. Listens on `127.0.0.1:8300`.

- **Login** (`POST /login`): authenticates against PAM (`pam_auth.py`, only real
  uid≥1000 accounts with a normal shell), then sets a signed session cookie
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
  (`os.access` as the user), with per-node `access:{read,write}`.
- `GET/POST /api/file` — read / create a document (as the user).
- `POST /api/fs/mkdir`, `POST /api/fs/delete` — create a folder / delete a file
  or folder (recursive), **as the user** — same authority as `mkdir`/`rm -r` in
  their terminal. Delete refuses the top-level areas (`company`/`projects`/
  `users`); a blocked ancestor is a clean 403, not a 500.
- `POST /api/upload`, `GET /api/attachment` — attachments in a `_files/` sibling.
- `GET /pty` — a **real login shell** in a PTY (via `pty.fork` + `bash -l`),
  starting in `/srv/kb/company`, bridged to xterm.js in the browser. Shells are
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
- `GET /api/cron`, `POST /api/cron/{add,remove,toggle}` — the user's **own
  crontab**, via `crontab(1)` run as them. List/add/delete/pause (pause = a
  `#kb:paused ` comment prefix, so cron skips it but the entry survives). Add is
  validated (5-field or `@keyword` schedule; command must be a single line —
  including exotic separators — so one UI action is exactly one crontab line, and
  a bare `%` is refused since cron would silently truncate there) and
  `crontab(1)` itself re-validates;
  remove/toggle must echo the current raw line back and get a 409 if the crontab
  changed underneath them. Every user has cron on this box (no
  `/etc/cron.allow`/`cron.deny`), and jobs run with the user's kernel + Postgres
  identity — scheduling is just *the user acting later*, no privilege to escalate.
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
  `git`) into the live doc via `diff-match-patch`. So five writers — browser, vim,
  agent, git, script — converge through one CRDT.
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

- Parses every `.md` file into `kb.blocks` (one row per task/heading/line, with
  `tsvector` for FTS and a 64-dim hash `vector` placeholder for pgvector) and
  denormalises each file's owner/group/mode into `kb.files`.
- Extracts `@assignees` and `#tags` from tasks into array columns (GIN-indexed).
- **ACL-aware**: reads real POSIX ACLs via `getfacl`, *de-masks* the group bits
  (so a file locked to owner and shared with one user via ACL doesn't look
  group-readable), and records named read/traverse grants in
  `acl_users`/`acl_groups`/`acl_x_users`/`acl_x_groups`.
- Skips symlinks and dot-dirs (`.git`, `.claude`) — config is not knowledge.
- Refreshes `kb.user_groups` from `getent` every 5s (so new users / group changes
  reach RLS), and reconciles perms with a cheap ctime/mode signature cache (only
  re-reads ACLs for files that actually changed).

## 5. Postgres + Row-Level Security

`scripts/schema.sql`. One database `kb`, local peer auth (PG role == OS user).

- `kb.files`, `kb.blocks` have **RLS enabled**. Users get `SELECT` only.
- `kb.can_read(path)` is a `SECURITY DEFINER` function used by the RLS policy. It
  replicates **full Unix path resolution** using `session_user`: the caller must
  be able to **read the file** (owner/group/other bits, OR a named-user ACL, OR a
  named-group ACL) **AND traverse (x) every ancestor directory** (bits or a named
  traverse ACL). This is why a raw query can never return a row you couldn't
  `cat`, and why a file made world-readable *inside* a `0700` dir stays hidden.
- `kb.blocks` delegates its policy to `kb.files` (`file_path IN (SELECT path
  FROM kb.files)`): the subquery runs under the files policy, so `can_read`
  still decides — but once per *file* (a single hashed subplan), not once per
  *block*. With ~100k blocks over ~700 files that is the difference between a
  15-second search and a fast one; the visible row set is identical, and a
  block cannot outlive its file row (FK `ON DELETE CASCADE`).
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
| `kb-write`    | `/api/artifact/write` | write a file as the viewer — **scoped to the artifact's own folder** |
| `kb-toggle`   | `/api/tasks/toggle`   | flip a checkbox — only on a genuinely indexed task line |

The default **To-dos** view (`company/todos.html`) and the demo dashboards are
themselves artifacts — the platform dogfoods its own runtime. Artifacts are
*author-trusted, viewer-scoped*: contained against the system and other users, but
an artifact you open runs code with your authority (like a shared spreadsheet
macro), which is why `kb-read`/`kb-write` are folder-scoped.

## 7. Sharing & the permission UI

The ⚙ permissions modal (`/fs/props`) lets an owner/admin change owner, group, and
POSIX ACLs on a file/folder. Sharing a **file** with a user who can't reach it
would be a dead grant (Unix needs traverse on every ancestor dir), so `/fs/props`
**auto-grants traverse-only (`x`, no listing)** on the ancestor directories the
grantee can't already traverse — the surgical "share just this one file". The
response reports which directories got a traverse grant. The indexer records those
grants so search/RLS agree with the kernel.

## 8. The UI shell — tabs, terminals, cron panel

VS-Code-shaped chrome over the same primitives (vanilla JS, `frontend/src/app.js`):

- **File tree actions** (on hover): `＋` new file, `⊞` new folder (both only on
  folders you can write), `⚙` permissions, `✕` delete (only where you can write
  the parent). Deleting a file/folder retires any tabs showing it.
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
  `- [ ]`→`- [x]` in the source, same path as the todos artifact). A per-doc
  toolbar (H1-3, bold/italic/strike/code, lists, task, quote, link, media, hr;
  Ctrl+B/I) and **drag-drop + screenshot-paste** insert media at the drop point —
  the bytes upload to a `_files/` sibling (as the user) and render inline; images
  land on their own block, other files as links. A **Source** toggle (persisted in
  `localStorage`) drops to raw markdown with line numbers. GFM task/strikethrough/
  table nodes come from `@lezer/markdown` extensions.
- **Editor tabs**: every opened document/artifact is a tab; each keeps its own
  live mount (CodeMirror + Yjs provider, or sandboxed iframe) in a hidden
  container, so switching is instant and **background artifacts keep running**
  (their bridge messages are routed by `ev.source` to the tab they came from, and
  kb-read/kb-write stays scoped to *that* artifact's folder, not the focused one).
- **Terminal panel**: a docked, resizable bottom panel with its own terminal
  tabs (`＋` spawns, `×` kills, shell `exit` retires its tab — announced by the
  server's `{"exit":true}` frame). A tab only dies when the *shell* dies: a
  dropped connection dims the terminal, badges it "reconnecting…", and
  reattaches on its own (1–15 s backoff; instantly on network-online or
  tab-visible), replaying only the missed bytes. Reloading the page — or coming
  back hours later — reattaches to the same running shells (session ids +
  names persist in `localStorage`, the processes in the backend). Hiding the
  panel (`▾` / Ctrl+`` ` ``) keeps shells running; killing the last terminal
  hides it too. The panel is in the page flow, not an overlay — closed means
  the editor gets the space back.
- **Cron panel**: the topbar's "Cron" opens the user's crontab (§2 endpoints):
  list, add (with presets), pause/resume, delete.

## 9. Admin — user & group management

`/admin/*` (admin group only — `KB_ADMIN_GROUP`, default `sudo`). Creating a user does the full provisioning in one
call: `useradd` + password + add to `kb-users` + private `users/<u>/` dir +
Postgres `CREATE ROLE` + personal `u_<u>` schema + a profile (first/last/email in
`/etc/kb/profiles.json`). Deleting reverses it all (incl. dropping the PG schema
and stripping the user's ACL grants so a recycled uid can't inherit them). Guards
prevent removing protected accounts or granting privileged groups via the UI.

## 10. Agents

Agents run as the user (`claude` / any harness) — an SSH/PTY shell or a headless
`claude -p`. They edit files, which flow through `kb-syncd` into live browser
sessions; they query Postgres as themselves; they schedule work with their own
`crontab`. Company **skills** in `/srv/kb/.claude/skills/` (root-owned,
world-readable, admin-write-only) teach them the platform:
`kb-orientation`, `kb-database`, `kb-automation`, `kb-artifacts`, `kb-todos`.

## Data-flow example: toggling a checkbox in the To-dos view

1. The To-dos artifact renders tasks it read via `kb-query` (RLS-scoped).
2. You tick a box → `kb-toggle {path,line}` → the parent forwards to
   `/api/tasks/toggle` on your backend, which verifies the line is a real indexed
   task and flips `- [ ]`→`- [x]` in the source `.md` **as you**.
3. `kb-syncd` sees the file change, merges it into any live editor of that doc.
4. `kb-indexer` re-parses the file; the checkbox state updates in `kb.blocks`.
5. `kb-syncd` debounces a git commit. Every window on that line stays in sync —
   one is a view of the other, never a copy.
