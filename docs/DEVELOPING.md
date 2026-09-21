# Developing & extending

For the next person working on this. Read [ARCHITECTURE.md](ARCHITECTURE.md) first.

## Dev loop

Source is your clone of this repo; the running system runs from the install
prefix recorded in `/etc/kb/kb.env` (default `/opt/kb-platform`). To ship a
change:

```bash
# backend change: rsync to /opt (keeps the venvs), then restart kb-syncd, kb-hub
# and kb-indexer — plus kb-convert where its venv exists.
sudo bash scripts/deploy.sh

# ...or, if anyone is mid-session: the hub's cgroup holds every web terminal, and
# an indexer restart costs a full resweep (SCALING.md). Skip the restarts, then
# bounce only what your change touched.
sudo bash scripts/deploy.sh --no-restart
sudo systemctl restart kb-syncd
# per-user backends keep serving old code until their process ends. A hub restart
# takes them with it; scripts/bounce_backends.py ends only the ones reporting a
# stale version marker, driving the product as each user rather than as root.

# frontend change:
(cd frontend && node build.mjs)
sudo bash scripts/deploy.sh

# database schema change: edit scripts/schema.sql AND write a migration you apply
# to the running DB as the schema owner:
sudo -u kbindexer psql -d kb -f your_migration.sql
```

## Testing

The layout model has JavaScript unit tests that need no browser and no box:

```bash
node --test tests/js/*.test.mjs      # layout.js: normalize, v1 migration, the v1 shadow, the phone plan, caps
```

CI runs them right after the installer. Everything else is Python, below.


```bash
.venv/bin/python -m pytest tests/ -q          # full suite
.venv/bin/python -m pytest tests/cli -q       # fast (httpx only)
.venv/bin/python -m pytest tests/e2e -q       # Playwright/chromium (slower)
```

- `tests/cli/` drive the HTTP API with `httpx` (permission matrix, RLS, admin,
  security regressions). `tests/e2e/` drive a real chromium via Playwright
  (multiplayer convergence, the editor, artifacts, the file manager).
- The root `conftest.py` seeds a namespace in `pytest_configure` and removes it
  in `pytest_unconfigure`. It has to be `configure`, not a fixture: ~20 modules
  read the credentials file at IMPORT time, so it must exist before collection.
- `tests/kbenv.py` maps logical names to what exists right now — `U("alice")`,
  `doc("x.md")`, `proj()`, `home("bob")`, and `L(real)` for names coming BACK
  from the API. Use it for every path and account; never a literal.
- `tests/e2e/conftest.py` has the `browser` fixture + `login()`/`open_doc()`
  helpers. It resolves `kbenv` LAZILY — it is an "initial" conftest whenever
  pytest is given `tests/e2e`, so pytest loads it before `pytest_configure` has
  seeded anything. A module-level import there breaks every e2e run at
  collection.
- **Tests that add ACLs must clean up** (including any auto-granted ancestor
  traverse) — otherwise they pollute state across runs now that shares are
  effective. See `test_share_reachable.py` for the pattern.
- **What CI gates:** the installer + `tests/cli`, on every push and PR. The
  browser suite is **opt-in**, because it costs ~12 minutes: run it from Actions
  → CI → *Run workflow* (or `gh workflow run CI --ref <branch> -f e2e=true`).
  Nothing runs it for you, so run it yourself before merging anything that
  touches the editor, the CRDT layer, the artifact bridge, or shared test
  plumbing (`tests/kbenv.py`, `tests/e2e/conftest.py`, fixture paths) — that is
  precisely the class of change that green cli tests cannot see.

## How to add things

- **A per-user API endpoint** (runs as the user): add a handler + route in
  `user_server.py`. The hub proxies all `/api/*` automatically. Kernel perms apply
  for free; use `common.resolve_repo_path()` to bound paths to the repo.
- **A privileged endpoint** (needs root): add it to `hub.py`. Re-derive the caller
  with `self.current_user(request)`, authorize explicitly, and if it writes files
  use the symlink-safe helpers (below). Register a non-proxied route (like
  `/fs/*`).
- **An artifact bridge action**: add a message type in the `window.addEventListener
  ("message", …)` handler in `app.js` and a backend endpoint it forwards to. Keep
  it narrow and viewer-scoped — every action an artifact can invoke is one a
  hostile author can invoke against the viewer.
- **A setting** (company default, personal override, or both): one entry in
  `REGISTRY` in `kb_platform/settings.py` — key, type, default, which layers may
  set it, label. The validation, the two files, `/api/settings`,
  `/admin/settings` and the dialog row follow from it; add a
  `settings.subscribe(key, fn)` in `app.js` if it has a live effect, and a row
  in `docs/settings.md` + the `kb-settings` skill (a test checks both).
- **A theme**: one `:root[data-theme="<name>"]` block in `frontend/assets/style.css`
  that sets every colour token `:root` defines (type and spacing tokens are
  optional — unset ones inherit deep blue's) (`tests/cli/test_theme_tokens.py`
  fails on a missing one — an unset token silently inherits deep blue), plus the
  name in `ui.theme`'s `options` in `kb_platform/settings.py`. Nothing else: no
  rule in the stylesheet names a colour, the editor's highlight style reads the
  `--md-*` tokens and the terminal reads `--term-*` when it opens or the theme
  changes. The same test refuses a new `#hex` or `rgba()` outside the token blocks.
- **A company skill**: add a folder under `company-skills/<name>/SKILL.md` (YAML
  frontmatter `name` + `description`, then markdown), then deploy it to
  `/srv/kb/.claude/skills/` (root-owned, 644). Agents discover it automatically.
- **An RLS-visible index field**: add a column in `scripts/schema.sql`, populate
  it in `indexer.py` (`stat_row`/`upsert_file` + `reconcile_perms`), and reference
  it in `kb.can_read` if it affects visibility. Migrate the running DB.

### A new kind of view (a tab that shows something new)

Register it once with `registerView(kind, {…})` from `frontend/src/views.js`
— label, icon, how to `open(t, spec)` into the tab's element, what to
`serialize` for the session record, how to `restore` a persisted spec, where
it opens by default (`placement`: the active group, the dock, or a side
column) and whether it is a document the header describes. The shell does
the rest: tab strips, drag and drop, splits, the dock, persistence, keyboard
focus, restore. `chat.js` is the worked example (a lazily loaded chunk);
documents, artifacts, secrets and terminals are registered the same way in
`app.js`. Commands and shortcuts a module adds go through `registerCommand`;
buttons in the chrome go into a named slot (`slot("topbar")`).

## Gotchas that will bite you

- **Install into the platform venv by interpreter.** `sudo /opt/kb-venv/bin/python
  -m pip install <pkg>`, never the venv's `pip` console script
  (its shebang points at the original venv path).
- **A long-lived shell has stale group membership.** If you add a user to a group,
  a shell that started *before* that doesn't have it. Backends spawned via
  `runuser` and fresh SSH logins get correct groups; for a stale shell use
  `sg <group> -c '…'`. RLS uses `kb.user_groups` (refreshed by the indexer), not
  your shell's groups.
- **`current_user` vs `session_user` in Postgres.** Inside a `SECURITY DEFINER`
  function, `current_user` is the *definer* (`kbindexer`). Use `session_user` to
  get the real connecting role. This exact bug once broke everyone's access.
- **A named POSIX ACL entry overrides the group entry.** Adding `u:bob:--x` to a
  dir Bob already reached via a group **downgrades** Bob to traverse-only. Only add
  a named ACL where the user doesn't already have the access (see
  `_grant_ancestor_traverse` / `_dir_traversable` in `hub.py`).
- **`kbindexer` can only index files it can read.** A `0600` file owned by a user
  never enters the index (so it's not searchable, and can't be over-shared through
  the index either).
- **Don't `getfacl` in a hot loop.** The reconcile loop learned this — it uses a
  cheap ctime/mode signature cache and only re-reads ACLs for changed files.
- **`sudo -S` + a heredoc fight over stdin.** When scripting root commands, pipe
  the password to `sudo -S` and put any heredoc/`< file` redirection *inside* an
  inner `bash -c`, or write the input to a file first.
- **Root file writes must be symlink-safe.** Use `common.opendir_beneath()` +
  `O_NOFOLLOW` + fd-based `fchown`/`setxattr` (see `hub._create_inheriting`,
  `hub.fs_props_set`, `syncd._atomic_write`). Never path-string `open`/`chown` in
  root code.

## Roadmap — designed but NOT built

These were designed during the build (some deliberately deferred) and are the
natural next steps.

- **Notifications.** An append-only inbox per person,
  `users/<u>/.os/inbox.jsonl`, written by the processes that already see the
  event: the indexer for a new `@mention` of a real account (it already scans
  for them with `ASSIGNEE_RE`), the backend for a share, cron for a job that
  failed, the ACP bridge for an agent that finished while you were away. A
  dot on the person's ⋯, an Inbox view, and the SSE channel that already
  exists to push them live. A file rather than a table because everything
  else here is a file: agents can read it, backup already covers it, and no
  schema has to migrate.
- **Comments, as Markdown.** A comment is a callout block written into the
  document right after what it comments on — `> [!note] @krystof · 21 Sep` —
  rendered as a quiet card by the rich editor, with Reply (a nested callout)
  and Resolve (delete, or turn into `> [!done]`). It lives in the file, so
  agents read and answer it, git records the discussion, and the document
  stays readable in any editor; the cost is that anchoring is positional and
  a comment moves the text. The alternative, a sidecar keyed by `block_ref`
  (the column exists), keeps documents untouched and needs real anchor
  machinery — worth it only if comments must not change the file.
- **Public sharing.** A folder or file handed to someone with no account,
  served by a separate container that can only see what was bind-mounted in
  front of it. Fully designed in [public-sharing.md](public-sharing.md).
- **Encrypted secrets store.** `_secrets/` already exists: creator-owned files,
  born `0600` and shareable from the permissions panel like anything else, that
  syncd refuses to sync, kept out of git history, with server-side injection
  through the egress proxy so an agent uses a credential without seeing it. What is *not* built is encryption at rest — a `passage`/age tree with
  recipients-per-folder matching the sharing tiers, `secret://` reveal links
  resolved per-viewer, and a rotation runbook.
- **True logout / session revocation.** A server-side session store or a
  per-user token version, so logout invalidates immediately instead of at TTL.
- **Real embeddings.** Search is FTS only. The `vector(64)` hash placeholder was
  dropped 2026-08-07 — never a model, never read, and pure heap weight on every
  (necessarily sequential) content search. The `vector` extension stays installed,
  so adding it back needs no superuser step; the shape that works here is in
  [SCALING.md](SCALING.md#future-semantic-search).
- **Per-user Postgres schema UI.** The `u_<user>` schemas exist and are default-deny;
  a UI to browse/grant them (and back them up) isn't built.
- **Live agent-in-the-doc.** Agents currently participate by editing files (which
  sync). A session-level websocket tool would give visible agent cursors + live
  typing, plus a "re-read and re-diff before writing" discipline for long-thinking
  agents.
- **Richer knowledge model.** Transclusion (`![[x]]`), backlinks, `[[wikilinks]]`,
  and durable `^block-id` references (the `block_ref` column exists but isn't
  auto-assigned); optionally a block-based (Notion-style) editor instead of
  CodeMirror-on-plaintext.
- **S3/blob attachments.** Currently local files only (deliberate for a single
  box); an S3 path with signed URLs gated by file permissions is the scale story.
- **Multi-machine.** Everything assumes one box (OS users, inotify, local files).
  Scaling out needs LDAP + a shared filesystem — a real rewrite, not a feature.
- **Root hardening.** `kb-hub`/`kb-syncd` run as root; dropping capabilities +
  seccomp/AppArmor scoping is a reasonable next step for higher assurance.
