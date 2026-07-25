# Developing & extending

For the next person working on this. Read [ARCHITECTURE.md](ARCHITECTURE.md) first.

## Dev loop

Source is your clone of this repo; the running system runs from the install
prefix recorded in `/etc/kb/kb.env` (default `/opt/kb-platform`). To ship a
change:

```bash
# backend change:
sudo bash scripts/deploy.sh                       # rsync code to /opt (keeps the venv)
sudo systemctl restart kb-hub kb-syncd kb-indexer # restart what you changed
sudo rm -f /run/kb/users/*/backend.sock           # drop cached per-user backends so they respawn with new code

# frontend change:
(cd frontend && node build.mjs)
sudo bash scripts/deploy.sh

# database schema change: edit scripts/schema.sql AND write a migration you apply
# to the running DB as the schema owner:
sudo -u kbindexer psql -d kb -f your_migration.sql
```

## Testing

```bash
.venv/bin/python -m pytest tests/ -q          # full suite
.venv/bin/python -m pytest tests/cli -q       # fast (httpx only)
.venv/bin/python -m pytest tests/e2e -q       # Playwright/chromium (slower)
```

- `tests/cli/` drive the HTTP API with `httpx` (permission matrix, RLS, admin,
  security regressions). `tests/e2e/` drive a real chromium via Playwright
  (multiplayer convergence, the editor, artifacts, the file manager).
- `tests/e2e/conftest.py` has the `browser` fixture + `login()`/`open_doc()`
  helpers. Test credentials come from `/tmp/kb-test-creds.json` (write it once:
  `{"alice": "...", "bob": "...", "carol": "..."}`).
- **Tests that add ACLs must clean up** (including any auto-granted ancestor
  traverse) — otherwise they pollute state across runs now that shares are
  effective. See `test_share_reachable.py` for the pattern.

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
- **A company skill**: add a folder under `company-skills/<name>/SKILL.md` (YAML
  frontmatter `name` + `description`, then markdown), then deploy it to
  `/srv/kb/.claude/skills/` (root-owned, 644). Agents discover it automatically.
- **An RLS-visible index field**: add a column in `scripts/schema.sql`, populate
  it in `indexer.py` (`stat_row`/`upsert_file` + `reconcile_perms`), and reference
  it in `kb.can_read` if it affects visibility. Migrate the running DB.

## Gotchas that will bite you

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

- **Encrypted secrets store.** `_secrets/` already exists: creator-owned `0600`
  files that syncd refuses to sync, kept out of git history, with server-side
  injection through the egress proxy so an agent uses a credential without seeing
  it. What is *not* built is encryption at rest — a `passage`/age tree with
  recipients-per-folder matching the sharing tiers, `secret://` reveal links
  resolved per-viewer, and a rotation runbook.
- **True logout / session revocation.** A server-side session store or a
  per-user token version, so logout invalidates immediately instead of at TTL.
- **Real embeddings.** Search currently uses FTS + a *deterministic hash*
  `vector(64)` placeholder. Swap `indexer.embed()` for a real embedding API and
  the pgvector search becomes semantic (add an embedding cache keyed by content
  hash for cheap rebuilds).
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
