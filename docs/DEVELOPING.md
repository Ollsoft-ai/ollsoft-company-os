# Developing & extending

For the next person working on this. Read [ARCHITECTURE.md](ARCHITECTURE.md) first.

## Repository layout

```
ollsoft-company-os/
├── kb_platform/            the Python backend (one module per component)
│   ├── common.py           paths, config, HMAC session tokens, safe path helpers
│   ├── pam_auth.py         PAM login
│   ├── hub.py              ROOT: login, spawner, reverse proxy, /fs/* + /admin/*
│   ├── user_server.py      per-user backend (runs AS the user)
│   ├── syncd.py            ROOT: CRDT relay + filesystem daemon
│   └── indexer.py          markdown → Postgres index (RLS metadata, ACLs, tasks)
├── frontend/               vanilla-JS SPA (CodeMirror 6 + Yjs + xterm), esbuild
│   ├── src/app.js          the whole client
│   ├── src/richview.js     the writing surface: widgets, live preview, tables, @mentions
│   │                       (no app inside it — the public-link page mounts the same module)
│   ├── src/publicdoc.js    that surface with no app behind it: one file, a plain save
│   ├── src/dictation.js    microphone capture + push-to-talk (owns no routing)
│   ├── assets/             hand-authored shell: app.html, login.html, style.css, logos
│   ├── static/             build output (generated, gitignored)
│   └── build.mjs           esbuild bundler
├── scripts/
│   ├── install.sh          one-command install / upgrade  ← start here
│   ├── seed-demo.sh        sample company, and the test suite's fixtures
│   ├── deploy.sh           code, CLIs, skills, schema, units, timers → live (install.sh ends with it)
│   ├── kb-heartbeat.sh     functional health check (kb-heartbeat.timer, 5 min)
│   ├── kb-alert.sh         append an alert to /var/log/kb/alerts.log (push is opt-in)
│   ├── kb-maintenance.sh   daily triage: bundle -> headless agent -> notify only if real
│   ├── kb-maintenance-policy.md  what counts as noise vs a real problem, and what the agent may do
│   ├── install-dictation-key.sh  validate + install the ElevenLabs key (root 0600)
│   ├── install-audit.sh    auditd + kb-audit.rules (no usernames) + kb-audit-digest (daily summary)
│   ├── bounce_backends.py  restart per-user backends after a deploy
│   ├── schema.sql          Postgres schema, RLS functions, grants
│   └── demo_cron_pulse.py  example: a crontab feeding a live artifact
├── systemd/                kb-hub / kb-syncd / kb-indexer / kb-embedd / kb-convert units, the
│                           kb-heartbeat + kb-maintenance + kb-gitgc timers, tmpfiles, logrotate
├── defaults/               shipped into <repo>/.os/ (config), <repo>/AGENTS.md (agent context), .agents/skills/ and company/
├── company-skills/         the platform's agent skills, deployed to /srv/kb/.agents/skills/
├── .claude/skills/install-company-os/  the agent-guided installer (run from your own computer)
├── tests/                  pytest: cli/ (httpx) + e2e/ (Playwright)
└── docs/                   ARCHITECTURE · SECURITY · SETUP · DEVELOPING · settings · unified-views · agent-chat · public-sharing · monitoring · dictation · remote-access · agent-cli · converted-documents · windows-drive
```

**Created on the box by the installer** (not in this repo):

```
/opt/kb-platform      code, world-readable (so per-user backends can run it)
/opt/kb-venv          the Python venv, world-executable
/opt/kb-convert-venv  kb-convert's parser venv — heavy deps, kept separate on purpose
/srv/kb               the knowledgebase: git repo of markdown + attachments
/srv/kb/.os/          platform config in the repo: launchers, egress allow-list, company settings
/srv/kb-public/       what the public-link container can see: per-share bind mounts + configs
**/.trash/            a deleted file waits in one of these, beside where it lived
/etc/kb/kb.env        runtime configuration read by the systemd units
/etc/kb/elevenlabs.key  dictation credential (root 0600) — the hub alone reads it
/etc/kb/session.key   HMAC key (root 0600)
/run/kb               unix sockets: syncd.sock (root), users/<u>/ (per-user 0700)
```

---

## Dev loop

Source is your clone of this repo; the running system runs from the install
prefix recorded in `/etc/kb/kb.env` (default `/opt/kb-platform`). To ship a
change:

```bash
# backend change: rsync to /opt (keeps the venvs), then restart kb-syncd, kb-hub,
# kb-indexer and kb-embedd — plus kb-convert where its venv exists.
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

## Operating it

```bash
# status and logs
systemctl status kb-hub kb-syncd kb-indexer kb-embedd kb-convert
journalctl -u kb-hub -u kb-syncd -u kb-indexer -u kb-embedd -u kb-convert -f

# redeploy after editing code (reads /etc/kb/kb.env for paths)
sudo bash scripts/deploy.sh

# rebuild the frontend after editing frontend/src
(cd frontend && node build.mjs) && sudo bash scripts/deploy.sh

# the index is disposable — rebuild it from the markdown at any time
sudo systemctl restart kb-indexer

# office/PDF → markdown sidecars are equally disposable — force a resweep
sudo systemctl restart kb-convert

# monitoring: alerts are logged, not pushed (see docs/monitoring.md)
sudo tail -5 /var/log/kb/alerts.log            # every alert ever raised
sudo tail -40 /var/log/kb/maintenance.log      # the daily triage verdicts
sudo /opt/kb-platform/scripts/kb-maintenance.sh --dry-run --stdout   # triage now
```

> `deploy.sh` restarts `kb-hub`, and its cgroup holds every open web terminal and
> per-user backend. Pass `--no-restart` if anyone might be mid-session, then
> restart only what your change touched.

## Testing

**There is nothing to set up first.** The suite seeds and removes its own
fixtures: each run makes a namespace of its own (`company/kbtest-<ns>/`,
`projects/kbtest-<ns>-acme/`, throwaway `kbt_<ns>_*` accounts) and removes all of
it afterwards, so a run cannot collide with — or delete — real content. It needs
passwordless sudo to create those accounts.

First time on a box:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install chromium        # for tests/e2e
# optional: firefox + webkit for tests/e2e/test_cross_browser.py (they skip if absent)
```

Set `KB_TEST_NS=<id>` to reuse a namespace you seeded yourself — conftest will
not tear down what it did not create, which is what CI does, because pytest runs
there as an account without sudo. `KB_TEST_NO_SEED=1` skips the lifecycle
entirely.

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
- **One file drives the other engines.** Everything else here is Chromium,
  including the phone tests — which emulate a phone's viewport and touch but
  not its ENGINE, while every iPhone runs WebKit.
  `tests/e2e/test_cross_browser.py` is a smoke pass (login, the rendered
  markdown, a table cell, typing, the mode switch, a quiet console) in
  Chromium, Firefox and WebKit; an engine that is not installed **skips**, so
  CI stays Chromium-only. To have them locally:
  `python -m playwright install firefox webkit` and, as root,
  `python -m playwright install-deps webkit` (GTK). Native HTML5 drag cannot
  be driven from a headless harness — the tab-drag tests synthesise the
  `DragEvent`s and their `DataTransfer`, which tests our handlers, not the
  browser's.
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
  frontmatter `name` + `description`, then markdown); `deploy.sh` installs it
  into `/srv/kb/.agents/skills/` (root-owned, 644), which Claude Code sees as
  `.claude/skills/`. Agents discover it automatically.
- **A semantic-search provider**: a class in `kb_platform/embedding.py` with
  `async embed(session, texts) -> EmbedResult(vectors, tokens)` (or `rerank(...)
  -> RerankResult(scores, units)`) that raises `ProviderError` with a `kind`,
  never the upstream body; add it to `make_embedder`/`make_reranker` and to
  `scripts/install-search-keys.sh`. Report the provider's own usage — the ledger
  never trusts an estimate. `KB_EMBED_PROVIDER=fake` (free, deterministic) is
  what CI and the invariant tests run; measure a real one with
  `scripts/search-eval.py` ([semantic-search.md](semantic-search.md)).
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

- **Where a piece of frontend belongs.** `richview.js` is the writing surface
  and knows nothing about this app: no tabs, no CRDT, no tree, no session, no
  `fetch` of a platform route. Anything it needs from a host — how to toast,
  ask, draw a menu, open a path, turn a relative path into a URL, list who
  may be @mentioned — arrives through `init()`, because the public-link
  container mounts the very same module (`publicdoc.js`) with a plain save
  behind it. Put app machinery (the upload tray, the file tree, deep links)
  in `app.js` and pass it in. `node --test tests/js/module-boundaries.test.mjs`
  fails when a module uses a name it neither declares nor imports: esbuild
  leaves such a reference as a global, and the page throws the first time
  that line runs — which is how the first attempt at this split shipped a
  blank app (`StateEffect`, 2026-09-22).
- **A named indexer grant on a FOLDER stops its sidecars.** POSIX ACL
  evaluation stops at the first matching named user entry, so a folder
  carrying `user:kbindexer:r-x` no longer lets `kb-convert` (which runs as
  `kbindexer`) create the `.name.docx.md` beside a document — the account's
  project-group write is never consulted. Found on 2026-09-22: three
  documents in one project had been unconvertible for weeks, and the folder
  looked perfectly group-writable. The fix applied there was
  `setfacl -m u:kbindexer:rwx` on the directories; the general fix is for the
  audience machinery to grant the indexer `rwx` on directories (it already
  grants `rx`) or for convert to write sidecars somewhere it always may.
  Until one of those lands, the symptom is "the sidecar never appears and the
  journal says Permission denied on a `.kbtmp`".
- **More kinds of notification.** The inbox is built (mentions and shares —
  see [ARCHITECTURE.md](ARCHITECTURE.md)); what is not written yet is a cron
  job that failed, an agent that finished while you were away, and a public
  share that was opened or edited. Each is one `common.add_inbox_event` call
  in the process that already knows.
- **Comments, as Markdown.** A comment is a callout block written into the
  document right after what it comments on — `> [!note] @krystof · 21 Sep` —
  rendered as a quiet card by the rich editor, with Reply (a nested callout)
  and Resolve (delete, or turn into `> [!done]`). It lives in the file, so
  agents read and answer it, git records the discussion, and the document
  stays readable in any editor; the cost is that anchoring is positional and
  a comment moves the text. The alternative, a sidecar keyed by `block_ref`
  (the column exists), keeps documents untouched and needs real anchor
  machinery — worth it only if comments must not change the file.
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

