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
4. **RLS flat tax grows with file count.** `blocks_read` evaluates
   `kb.can_read()` over all of `kb.files` once per statement (hashed subplan;
   see the comment block in `scripts/schema.sql`). ~0.2 s at 675 files,
   linear: ~2 s at 10× — paid by every search keystroke, task list, and
   toggle gate. Fix shape: materialized per-user visibility maintained by the
   indexer. **Open — the known big one.**
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
suite times. Hardware does NOT fix items 2–4 above — they are algorithmic and
arrive with user count and corpus size regardless of cores.

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
