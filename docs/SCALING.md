# Scaling: what breaks, in what order, and what was already done

Assessment date 2026-07-29, on the production box (4 vCPU / 7.8 GB, ~540 docs /
~93k indexed blocks / 14 human accounts). Numbers below are measured, not
guessed; re-measure before trusting them at a different scale.

## Ceilings, ranked by which is hit first

1. ~~**Hub executor starvation at ~8 live backends.**~~ **Fixed 2026-07-29.**
   Every backend spawn parked `run_in_executor(None, proc.wait)` — one
   default-executor thread (pool = `min(32, cores+4)` = 8 on 4 cores) per
   backend, forever, in the same pool the hub's blocking work uses. Now a
   single dedicated reaper thread polls all wrappers (`hub.py`,
   `_reap_later`).
2. **Backends are never reaped or capped** (~50 MB + 3.8 MB wrapper per user
   *ever logged in* since the last hub restart). 20 users ≈ 1.1 GB,
   50 ≈ 2.7 GB, 140 ≈ 7 GB = OOM. Reaping is not a one-liner: a backend may
   hold live PTY shells (someone's tmux-equivalent), so an idle-reaper must
   check for child shells before killing — the `bounce_backends.py` rule,
   hub-side. **Open.**
3. **Tree polling is O(users × full repo walk).** Every client triggers a full
   server-side `os.scandir` walk every 4 s (~150 ms CPU / 205 KB JSON at
   1,119 nodes, no cache, no ETag). ~25 users saturate one core of four.
   Fix shape: short-TTL per-backend cache, or an mtime-keyed ETag so the poll
   is usually 304. **Open.**
4. ~~**RLS flat tax grows with file count.**~~ **Fixed 2026-08-07.** Was
   `kb.can_read()` over all of `kb.files` once per statement — measured
   **0.34 s at 655 files**, linear, paid by every search keystroke, task list
   and toggle gate. Now both policies gate on `kb.visible_files`, a
   materialized `(usr, path)` set the indexer diff-syncs on its 1 s sweep
   (`indexer.py`, `compute_visibility` / `refresh_visibility`). Measured:
   `/api/tasks` **356 ms → 20 ms**, search SQL **685 ms → 130 ms**.
   `kb.can_read()` stays as the parity oracle — it and `compute_visibility()`
   must agree pair-for-pair, asserted per user in
   `tests/cli/test_visible_files.py`.
5. **Indexer restart = full resweep** (~10 min at 93k blocks, watches arm only
   after). Don't restart it casually with deploys; see `/kb-deploy` skill.
   Fix shape: persist sweep signatures so a restart resumes instead of
   rebuilding. **Open.**
6. ~~**`kb.user_groups` TRUNCATE every 5 s**~~ **Fixed 2026-07-29** — was
   `TRUNCATE` + full reinsert under `ACCESS EXCLUSIVE` (which every RLS check
   reads through) on every tick, 3.8 M lifetime inserts for 69 rows; now a
   pure in-memory comparison with a row-diff write only when membership
   changes (`indexer.py`, `refresh_groups`).
7. ~~**Hub `NOFILE` soft limit 1024**~~ **Fixed 2026-07-29** — capped the
   whole platform at ~500 open docs/terminals (2 fds per proxied WS). Now
   `LimitNOFILE=65536` in `kb-hub.service`.
8. **Postgres `max_connections=100` + a connection per API request**
   (`user_server._adb`). Fine today; at ~50 active users add a pool (psycopg
   `AsyncConnectionPool`) or raise the limit. **Open.**
9. **Large-body buffering:** hub `proxy_http` and `fs_upload` buffer entire
   request/response bodies in RAM (`client_max_size` is 2 GiB). A few big
   attachment downloads = transient hub RSS spikes. Fix shape: stream.
   **Open.**
10. **syncd never evicts rooms** (`auto_clean_rooms=False`): every doc ever
    opened stays in RAM and its full text is materialized 4×/s by the flush
    loop, forever. Flipping the flag needs care (flush-before-evict), so it
    was deliberately NOT flipped in the quick pass. **Open.** Related:
    `/var/lib/kb-syncd` state files are never compacted or pruned.
11. **Content search is a sequential scan, and always will be under RLS.**
    `tsv @@` and `ILIKE` are not leakproof, so Postgres refuses to evaluate
    them below a policy qual — no index on `kb.blocks` is reachable for a
    user's own query, whatever indexes exist. So search latency scales with
    **heap width × row count**, not with indexing. That is why the dead
    `embedding` column and three zero-scan indexes were dropped 2026-08-07
    (`kb.blocks` 186 MB → 50 MB; search SQL 330 ms → 130 ms at constant row
    count). Do NOT "fix" this with a `SECURITY DEFINER` wrapper to reach the
    GIN index — that was tried and reverted the same day: with RLS off inside
    the definer, attacker-controlled non-leakproof predicates run against rows
    the caller cannot see, and a LIKE pattern ending in a lone backslash
    errors only when some row matches, i.e. a one-bit oracle over the whole
    corpus via `/api/artifact/query`. See the note in `scripts/schema.sql`.
    Next levers, in order: keep the heap narrow; `shared_buffers` is **128 MB**
    on a 23 GB box, so raise it (needs a restart) to keep the table resident.
    **Open (bounded).**

## Future: semantic search

Deliberately NOT built (2026-08-07). The old `embedding` column was a 64-dim
MD5 signed-hash placeholder — never a model, so never semantically useful, and
nothing ever read it; it was dropped rather than kept as dead heap weight.
If it comes back, the shape that works here:

- **Chunk-level, not block-level.** A "block" is one *line* (avg 99 chars, 32%
  under 40) — embedding those is noise. Chunk by section/paragraph: better
  retrieval and ~20× fewer vectors.
- **Its own table**, never a column on `kb.blocks`. 1536 dims × 4 B × 93k rows
  ≈ 570 MB; inline that would be 7× the current heap and wreck keyword search
  (see item 11 — every search reads the heap).
- **A separate worker**, like `kb-convert`. The indexer is local, synchronous
  and fail-closed; a network call in `reindex_file` would wreck that.
- **Mind the same RLS trap**: `<=>` is not leakproof either, so HNSW is
  unreachable under a policy — pre-filter to the caller's `visible_files`, and
  note pgvector's iterative index scans need ≥ 0.8. The box runs **PostgreSQL
  18.4 with pgvector 0.8.6** (upgraded 2026-08-07 from 16.14 / 0.6.0 via
  `pg_upgradecluster`, PGDG repo). The old 16/main cluster is still on disk,
  port 5433, `start.conf = manual` — the rollback path until someone runs
  `pg_dropcluster 16 main`.
- Cost is a non-issue: the whole corpus is ~9 MB of text ≈ 2.3 M tokens, well
  under $0.10 to embed with a current small model.
- Expose it to **agents via a skill** first (they can afford a ~200 ms embed
  round trip); the app's search box debounces at 180 ms and cannot.

## Growth hygiene (fixed 2026-07-29)

- `egress.log` / `stt.log` were append-only forever → now in
  `kb-logrotate.conf` (monthly, 12 kept, 16 M max).
- `/srv/kb/.git` grew unbounded (one commit per changed file per 4 s window;
  108 MB / 27k objects in a month) → weekly `kb-gitgc.timer` runs
  `git gc --auto`.
- syncd and indexer now carry `MemoryHigh=1G` (throttle, deliberately not
  `MemoryMax` — an OOM kill would eat unflushed CRDT edits / mid-transaction
  index writes). The hub deliberately has NO memory cap: its cgroup contains
  every user backend and web-terminal shell.

## Hardware guidance

The box (4 vCPU / 8 GB) is comfortable to ~10 active users. For a company
rollout, 8 vCPU / 16 GB removes RAM as the first wall and halves resweep and
suite times. Hardware does NOT fix items 2–3 above — they are algorithmic and
arrive with user count and corpus size regardless of cores. (Item 4 was the
third of that set and is now fixed; item 11 is bounded by heap size, which
*is* partly a hardware/config lever — see `shared_buffers`.)

## Drift watchlist (things that rot silently)

- **Schema vs live DB**: migrations are `sudo -u kbindexer psql -f
  scripts/schema.sql` by hand; nothing verifies at startup. After any schema
  change, check `pg_get_expr(polqual, polrelid)` matches the file.
- **Installed units vs `systemd/`**: box-local units referencing undeployed
  scripts crash-loop invisibly (the `kb-randoms` incident: 10k restarts).
  `diff /etc/systemd/system/kb-*.service systemd/` when in doubt.
- **Backend version skew**: backends run old code until bounced; only
  `scripts/bounce_backends.py` checks the `v` marker.
- **CI covers `tests/cli` on a pristine VM only** — e2e regressions and
  box-drift are invisible to it.
- **In-memory hub state** (STT quotas, spawn locks) resets on every restart.
