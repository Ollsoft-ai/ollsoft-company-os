"""kb-embedd — turns chunks into vectors, and never spends more than it may.

The indexer writes kb.chunks (the text of every section, keyed by a hash of the
model and the text). This daemon finds chunks whose hash has no vector yet,
sends them to the configured embedding provider, and stores the vectors. It is
the ONLY process that holds the provider keys and the ONLY writer of the spend
ledger, so every limit below is enforced in one place.

What makes it safe to leave running (docs/semantic-search.md, "Invariants"):

1. Unchanged text is never re-billed: vectors are keyed by content hash, so
   the indexer rewriting every chunk row on a restart costs nothing.
2. Spend is RESERVED in the ledger (an estimate, committed) before a call is
   made, and reconciled to the provider's reported usage after. A crash
   between the two over-counts; nothing can under-count.
3. An in-process brake that does not depend on Postgres: at most
   BRAKE_PER_MIN calls a minute and BRAKE_PER_DAY a day, whatever the ledger
   says — a ledger bug cannot become a bill.
4. A chunk the provider rejects is retried on a widening schedule and parked
   for good after PARK_AFTER attempts. A batch rejected as a whole is bisected
   to find the culprit. A provider that is down, or a wrong key, trips a
   breaker for the WHOLE loop instead of failing chunk by chunk.
5. Budgets from company settings are checked before every call.
6. A document is only embedded once it has been quiet for
   `search.embed.quiet_seconds` (syncd writes a file four times a second
   while someone types), and a file may cost at most FILE_DAY_FACTOR× its
   chunk count per day.

It never crashes the unit over configuration: missing keys, a wrong vector
width or a missing table are STATES, written to the status file, not
exceptions.
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import logging
import math
import os
import time
from pathlib import Path

import psycopg

from . import common, embedding
from . import settings as kbsettings

log = logging.getLogger("kb-embedd")

TICK = 2.0                 # seconds between batches while there is work
IDLE_TICK = 10.0           # …and while there is none
BATCH = 64                 # chunks per provider call
MAX_BATCH_CHARS = 60_000   # …and never more text than this in one call
BRAKE_PER_MIN = 20         # provider calls per minute, index side
BRAKE_PER_DAY = 3_000      # provider calls per process per UTC day, index side
BACKOFF = (60, 600, 3600, 21600, 86400)   # seconds, per failed attempt of one chunk
PARK_AFTER = 6             # attempts before a chunk is parked for good
BREAKER_TRIP = 5           # consecutive provider-wide failures before the breaker opens
BREAKER_OPEN = 900         # seconds the breaker stays open
BISECT_EXTRA = 12          # extra calls allowed to find the chunk that spoiled a batch
FILE_DAY_FACTOR = 3        # a file may cost this many times its chunk count per day
GC_GRACE_DAYS = 7          # an orphaned vector survives this long (a restored file is free)
EST_CHARS_PER_TOKEN = 3.0  # the reservation estimate; reconciled to reported usage after
PAID_MEMORY = 3600         # seconds a paid-for hash is never paid for again, DB or not

STATUS_DIR = common.RUN_DIR / "search"
STATUS_FILE = STATUS_DIR / "status.json"
SOCK_FILE = STATUS_DIR / "api.sock"

# The query side (the socket): searches from people, their agents and cron.
Q_MAX_CHARS = 1000         # a query longer than this is cut, not refused
Q_EMBED_PER_MIN = 300      # query embeddings a minute, all callers together
Q_EMBED_PER_UID_HOUR = 1200  # …and per caller an hour (≈ a keystroke search every 3 s)
Q_RERANK_PER_MIN = 60      # reranks a minute, all callers together
Q_CACHE_SECONDS = 600      # a query's vector is reused for this long
Q_CACHE_SIZE = 4096
RERANK_MAX_DOCS = 100      # one Cohere search unit covers up to 100 documents…
RERANK_MAX_DOC_CHARS = 4000  # …of up to 500 tokens each; longer ones count double


def utc_today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds") if ts else None


class Brake:
    """Call counters that live in memory only — the last line of defence."""

    def __init__(self, per_min: int, per_day: int):
        self.per_min, self.per_day = per_min, per_day
        self.recent: collections.deque[float] = collections.deque()
        self.day, self.today = utc_today(), 0

    def allows(self, now: float | None = None) -> float:
        """0.0 if a call may go now, else seconds to wait."""
        now = time.monotonic() if now is None else now
        while self.recent and now - self.recent[0] >= 60:
            self.recent.popleft()
        if utc_today() != self.day:
            self.day, self.today = utc_today(), 0
        if self.today >= self.per_day:
            return 3600.0
        if len(self.recent) >= self.per_min:
            return max(0.5, 60 - (now - self.recent[0]))
        return 0.0

    def note(self, now: float | None = None) -> None:
        self.recent.append(time.monotonic() if now is None else now)
        self.today += 1


class Breaker:
    """Closed → (BREAKER_TRIP failures) → open for BREAKER_OPEN → half-open:
    one single-chunk probe; success closes it, failure re-opens it."""

    def __init__(self):
        self.failures = 0
        self.open_until = 0.0
        self.half_open = False
        self.reason: str | None = None

    def is_open(self, now: float) -> bool:
        if self.open_until and now >= self.open_until and not self.half_open:
            self.half_open = True               # the next call is a probe
        return now < self.open_until

    def success(self) -> None:
        self.failures, self.open_until, self.half_open, self.reason = 0, 0.0, False, None

    def failure(self, err: embedding.ProviderError, now: float) -> float:
        """Record a provider-wide failure; returns seconds to wait."""
        self.reason = str(err)
        if err.kind == "rate" and err.retry_after:
            return err.retry_after              # throttling with a clear answer: not a fault
        self.failures += 1
        if self.half_open or self.failures >= BREAKER_TRIP:
            self.open_until, self.half_open = now + BREAKER_OPEN, False
            return BREAKER_OPEN
        return min(30.0 * self.failures, 300.0)


class Embedder:
    def __init__(self, cfg: embedding.Config | None = None, provider=None):
        self.cfg = cfg or embedding.load_config()
        self.provider = provider if provider is not None else embedding.make_embedder(self.cfg)
        self.model = self.cfg.model_id
        self.brake = Brake(BRAKE_PER_MIN, BRAKE_PER_DAY)
        self.breaker = Breaker()
        self.settings = kbsettings.defaults()
        self.settings_at = 0.0
        self.conn: psycopg.AsyncConnection | None = None
        self.session = None
        self.paused: str | None = None
        self.paused_reason: str | None = None
        self.wait_until = 0.0
        self.db_backoff = 2.0
        self.paid: dict[bytes, float] = {}      # hash -> monotonic time we paid for it
        self.rate: collections.deque[tuple[float, int]] = collections.deque()
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.warnings: list[str] = []
        self.dims_ok: bool | None = None
        self.has_table = True
        self._noembed: dict[str, tuple[float, bool]] = {}
        self.key_reloaded = False
        self.service: Service | None = None
        self.last_status: dict | None = None

    # ---- plumbing ----------------------------------------------------------
    async def db(self) -> psycopg.AsyncConnection:
        if self.conn is None or self.conn.closed:
            self.conn = await psycopg.AsyncConnection.connect(f"dbname={common.PG_DB}", autocommit=True)
            self.dims_ok = None
        return self.conn

    def refresh_settings(self) -> None:
        now = time.monotonic()
        if now - self.settings_at < 60 and self.settings_at:
            return
        self.settings_at = now
        values = dict(kbsettings.defaults())
        try:
            layer = kbsettings.load_layer(kbsettings.company_file(), "company")
            values.update(layer["values"])
        except Exception as e:                  # noqa: BLE001 — defaults are a safe answer
            log.warning("could not read company settings: %s", e)
        self.settings = values

    def price(self, key: str) -> float:
        try:
            return max(0.0, float(self.settings.get(key) or 0))
        except (TypeError, ValueError):
            return 0.0

    def note_error(self, msg: str) -> None:
        self.last_error, self.last_error_at = msg[:200], time.time()

    def in_scope(self, path: str) -> bool:
        if self.settings.get("search.embed.scope") == "company+projects" and not (
                path.startswith("company/") or path.startswith("projects/")):
            return False
        return not self.noembed(path)

    def noembed(self, path: str) -> bool:
        """A `.noembed` file in a folder keeps everything under it on the box."""
        parts = path.split("/")[:-1]
        now = time.monotonic()
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            hit = self._noembed.get(d)
            if hit is None or now - hit[0] > 60:
                hit = (now, (common.REPO_ROOT / d / ".noembed").exists())
                self._noembed[d] = hit
            if hit[1]:
                return True
        return False

    # ---- the ledger --------------------------------------------------------
    async def spent(self, conn: psycopg.AsyncConnection | None = None) -> dict:
        """{today: {kind: usd}, month: {kind: usd}} from the ledger."""
        conn = conn or await self.db()
        today = utc_today()
        month0 = today.replace(day=1)
        out = {"today": {}, "month": {}}
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT kind, sum(usd) FILTER (WHERE day = %s), sum(usd) FROM kb.spend "
                "WHERE day >= %s GROUP BY kind", (today, month0))
            for kind, d, m in await cur.fetchall():
                out["today"][kind] = float(d or 0)
                out["month"][kind] = float(m or 0)
        return out

    async def charge(self, kind: str, calls: int, units: int, usd: float, *,
                     model: str | None = None, conn: psycopg.AsyncConnection | None = None) -> None:
        conn = conn or await self.db()
        await conn.execute(
            "INSERT INTO kb.spend(day, kind, model, calls, units, usd) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (day, kind, model) DO UPDATE SET calls = kb.spend.calls + EXCLUDED.calls, "
            "units = kb.spend.units + EXCLUDED.units, usd = kb.spend.usd + EXCLUDED.usd",
            (utc_today(), kind, model or self.model, calls, units, usd))

    async def flag_once(self, period: str, flag: str, conn: psycopg.AsyncConnection | None = None) -> bool:
        """True the first time (period, flag) is seen — for one-shot warnings."""
        conn = conn or await self.db()
        async with conn.cursor() as cur:
            await cur.execute("INSERT INTO kb.spend_flags(period, flag) VALUES (%s,%s) "
                              "ON CONFLICT DO NOTHING RETURNING 1", (period, flag))
            return (await cur.fetchone()) is not None

    async def budget_block(self, est_usd: float, conn: psycopg.AsyncConnection | None = None) -> str | None:
        """Why an embedding call costing est_usd may NOT be made now, or None.
        Index and query embeddings share these caps: a cap is a cap."""
        s = await self.spent(conn)
        day_cap = self.settings.get("search.budget.embed_day_usd", 10)
        month_cap = self.settings.get("search.budget.embed_month_usd", 100)
        day = sum(v for k, v in s["today"].items() if k.startswith("embed"))
        month = sum(v for k, v in s["month"].items() if k.startswith("embed"))
        self.warnings = []
        for used, cap, label, period in ((day, day_cap, "today", str(utc_today())),
                                         (month, month_cap, "this month", str(utc_today())[:7])):
            if cap and used >= cap / 2:
                self.warnings.append(f"embedding spend {label}: ${used:.2f} of ${cap}")
                if await self.flag_once(period, f"embed_50_{label}", conn):
                    log.warning("embedding budget half used %s: $%.2f of $%s", label, used, cap)
        if day + est_usd > day_cap:
            return f"daily embedding budget (${day:.2f} of ${day_cap})"
        if month + est_usd > month_cap:
            return f"monthly embedding budget (${month:.2f} of ${month_cap})"
        return None

    # ---- picking work ------------------------------------------------------
    async def pick(self, limit: int) -> list[tuple[bytes, str, str]]:
        conn = await self.db()
        quiet = int(self.settings.get("search.embed.quiet_seconds", 120))
        now = time.monotonic()
        self.paid = {h: t for h, t in self.paid.items() if now - t < PAID_MEMORY}
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT DISTINCT ON (c.hash) c.hash, c.text, c.file_path
                FROM kb.chunks c
                JOIN kb.files f ON f.path = c.file_path
                WHERE f.mtime < extract(epoch FROM now()) - %(quiet)s
                  AND NOT EXISTS (SELECT 1 FROM kb.embeddings e WHERE e.hash = c.hash)
                  AND NOT EXISTS (SELECT 1 FROM kb.embed_failures x WHERE x.hash = c.hash
                                  AND (x.attempts >= %(park)s OR x.next_try > now()))
                  AND NOT EXISTS (SELECT 1 FROM kb.embed_file_day d
                                  WHERE d.day = %(today)s AND d.file_path = c.file_path
                                    AND d.chunks >= %(factor)s * (SELECT count(*) FROM kb.chunks c2
                                                                  WHERE c2.file_path = c.file_path))
                ORDER BY c.hash
                LIMIT %(scan)s
                """,
                {"quiet": quiet, "park": PARK_AFTER, "today": utc_today(), "factor": FILE_DAY_FACTOR,
                 "scan": limit * 8})
            rows = await cur.fetchall()
        out, chars = [], 0
        for h, text, path in rows:
            h = bytes(h)
            if h in self.paid or not self.in_scope(path):
                continue
            if out and chars + len(text) > MAX_BATCH_CHARS:
                break
            out.append((h, text, path))
            chars += len(text)
            if len(out) >= limit:
                break
        return out

    # ---- one provider call, with everything around it -----------------------
    async def call(self, items: list[tuple[bytes, str, str]]) -> None:
        """Embed `items` or raise ProviderError. Reserves, calls, reconciles,
        stores — and never leaves a paid-for result unaccounted."""
        chars = sum(len(t) for _, t, _ in items)
        est_units = math.ceil(chars / EST_CHARS_PER_TOKEN)
        price = self.price("search.price.embed_per_1m_usd")
        est_usd = est_units * price / 1e6
        await self.charge("embed_index", 1, est_units, est_usd)          # reserve
        self.brake.note()
        try:
            res = await self.provider.embed(self.session, [t for _, t, _ in items])
        except embedding.ProviderError as e:
            if e.status:                       # the provider answered: an error is not billed
                await self.charge("embed_index", 0, -est_units, -est_usd)
            raise
        now = time.monotonic()
        for h, _, _ in items:
            self.paid[h] = now
        await self.charge("embed_index", 0, res.tokens - est_units, (res.tokens - est_units) * price / 1e6)
        self.rate.append((time.monotonic(), len(items)))
        conn = await self.db()
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO kb.embeddings(hash, model, embedding, tokens) "
                        "VALUES (%s, %s, %s::halfvec, %s) ON CONFLICT (hash) DO NOTHING",
                        [(h, self.model, embedding.vector_literal(v),
                          max(1, round(res.tokens * len(t) / max(chars, 1))))
                         for (h, t, _), v in zip(items, res.vectors)])
                    await cur.executemany("DELETE FROM kb.embed_failures WHERE hash = %s",
                                          [(h,) for h, _, _ in items])
                    per_file = collections.Counter(p for _, _, p in items)
                    await cur.executemany(
                        "INSERT INTO kb.embed_file_day(day, file_path, chunks) VALUES (%s,%s,%s) "
                        "ON CONFLICT (day, file_path) DO UPDATE "
                        "SET chunks = kb.embed_file_day.chunks + EXCLUDED.chunks",
                        [(utc_today(), p, n) for p, n in per_file.items()])
        except psycopg.Error:
            # Paid for, not stored. The `paid` memory keeps these hashes out of
            # the next hour's batches even if the next write fails too; record
            # the failure where we can so they back off beyond that.
            await self.remember_failure([h for h, _, _ in items], "stored: database error")
            raise

    async def remember_failure(self, hashes: list[bytes], why: str) -> None:
        try:
            conn = await self.db()
            async with conn.cursor() as cur:
                for h in hashes:
                    await cur.execute(
                        "INSERT INTO kb.embed_failures(hash, attempts, next_try, last_error) "
                        "VALUES (%s, 1, now() + make_interval(secs => %s), %s) "
                        "ON CONFLICT (hash) DO UPDATE SET attempts = kb.embed_failures.attempts + 1, "
                        "next_try = now() + make_interval(secs => (%s)[LEAST(kb.embed_failures.attempts + 1, %s)]), "
                        "last_error = EXCLUDED.last_error, updated_at = now()",
                        (h, BACKOFF[0], why[:120], list(BACKOFF), len(BACKOFF)))
        except psycopg.Error as e:
            log.warning("could not record an embedding failure: %s", e)

    async def embed(self, items, budget: list[int]) -> None:
        """Embed a batch; on a per-input rejection, bisect to the culprit."""
        try:
            await self.call(items)
        except embedding.ProviderError as e:
            if e.global_failure:
                raise
            if len(items) == 1 or budget[0] <= 0:
                await self.remember_failure([h for h, _, _ in items], str(e))
                return
            mid = len(items) // 2
            for half in (items[:mid], items[mid:]):
                budget[0] -= 1
                if self.brake.allows():
                    await self.remember_failure([h for h, _, _ in half], "rejected (brake)")
                    continue
                await self.embed(half, budget)

    # ---- the loop ----------------------------------------------------------
    async def step(self) -> float:
        """One tick. Returns seconds to sleep."""
        self.refresh_settings()
        now = time.monotonic()
        if self.provider is None:
            self.paused, self.paused_reason = "unconfigured", "no embedding key or provider in /etc/kb"
            return 60.0
        if not self.settings.get("search.enabled", True):
            self.paused, self.paused_reason = "disabled", "turned off in Settings"
            return 30.0
        if not await self.dims_match():
            return 300.0
        if now < self.wait_until:
            return min(self.wait_until - now, IDLE_TICK)
        if self.breaker.is_open(now):
            self.paused, self.paused_reason = "breaker", self.breaker.reason
            return min(self.breaker.open_until - now, 60.0)
        wait = self.brake.allows(now)
        if wait:
            self.paused, self.paused_reason = "brake", "call limit of this process reached"
            return min(wait, 60.0)
        items = await self.pick(1 if self.breaker.half_open else BATCH)
        if not items:
            self.paused, self.paused_reason = None, None
            return IDLE_TICK
        est = sum(len(t) for _, t, _ in items) / EST_CHARS_PER_TOKEN
        blocked = await self.budget_block(est * self.price("search.price.embed_per_1m_usd") / 1e6)
        if blocked:
            self.paused, self.paused_reason = "budget", blocked
            return 60.0
        self.paused, self.paused_reason = None, None
        try:
            await self.embed(items, [BISECT_EXTRA])
        except embedding.ProviderError as e:
            self.note_error(str(e))
            if e.kind == "auth" and not self.key_reloaded:
                # A rotated key: read the file again once before blaming the provider.
                self.key_reloaded = True
                fresh = embedding.load_config()
                if fresh.embed_key and fresh.embed_key != self.cfg.embed_key:
                    self.cfg = fresh
                    self.provider = embedding.make_embedder(fresh)
                    log.warning("embedding key changed on disk; using the new one")
                    return 1.0
            wait = self.breaker.failure(e, time.monotonic())
            self.wait_until = time.monotonic() + wait
            log.warning("embedding provider failed (%s); waiting %.0fs", e, wait)
            return min(wait, 60.0)
        self.breaker.success()
        self.key_reloaded = False
        return TICK

    async def dims_match(self) -> bool:
        """Is there a vector table, and is it this model's width? Both are
        STATES — a box on pgvector < 0.7 has no table at all (schema.sql skips
        it) and simply stays on full-text search."""
        if self.dims_ok is None:
            conn = await self.db()
            async with conn.cursor() as cur:
                await cur.execute("SELECT to_regclass('kb.embeddings') IS NOT NULL")
                self.has_table = bool((await cur.fetchone())[0])
                row = None
                if self.has_table:
                    await cur.execute("SELECT atttypmod FROM pg_attribute "
                                      "WHERE attrelid = 'kb.embeddings'::regclass AND attname = 'embedding'")
                    row = await cur.fetchone()
            width = row[0] if row else None
            self.dims_ok = self.has_table and width == self.cfg.dims
            if not self.has_table:
                log.error("no kb.embeddings table: pgvector >= 0.7 is needed (halfvec); search stays "
                          "full-text. Upgrade pgvector, re-run scripts/deploy.sh, restart kb-embedd.")
            elif not self.dims_ok:
                log.error("KB_EMBED_DIMS=%s but kb.embeddings holds %s-dimension vectors; "
                          "not embedding (docs/semantic-search.md, 'Changing the model')",
                          self.cfg.dims, width)
        if not self.dims_ok:
            if not self.has_table:
                self.paused = "unsupported"
                self.paused_reason = "pgvector 0.7 or newer is needed; search is full-text only"
            else:
                self.paused = "dims mismatch"
                self.paused_reason = f"KB_EMBED_DIMS={self.cfg.dims} does not match the table"
        return bool(self.dims_ok)

    async def gc(self) -> None:
        """Orphaned vectors (no chunk carries their hash) get a grace period,
        then go; vectors for text now out of scope go at once; old counters
        are dropped. The ledger itself is kept for good."""
        if not await self.dims_match() and not self.has_table:
            return                             # no vectors on this box to collect
        conn = await self.db()
        async with conn.cursor() as cur:
            await cur.execute("UPDATE kb.embeddings e SET missing_since = now() WHERE missing_since IS NULL "
                              "AND NOT EXISTS (SELECT 1 FROM kb.chunks c WHERE c.hash = e.hash)")
            await cur.execute("UPDATE kb.embeddings e SET missing_since = NULL WHERE missing_since IS NOT NULL "
                              "AND EXISTS (SELECT 1 FROM kb.chunks c WHERE c.hash = e.hash)")
            await cur.execute("DELETE FROM kb.embeddings WHERE missing_since < now() - make_interval(days => %s)",
                              (GC_GRACE_DAYS,))
            gone = cur.rowcount
            await cur.execute("DELETE FROM kb.embed_failures x WHERE updated_at < now() - interval '30 days' "
                              "AND NOT EXISTS (SELECT 1 FROM kb.chunks c WHERE c.hash = x.hash)")
            await cur.execute("DELETE FROM kb.embed_file_day WHERE day < %s", (utc_today() - dt.timedelta(days=40),))
            await cur.execute("DELETE FROM kb.spend_user WHERE day < %s", (utc_today() - dt.timedelta(days=40),))
            await cur.execute("DELETE FROM kb.spend_flags WHERE at < now() - interval '400 days'")
            # out of scope now (Settings narrowed, or a .noembed appeared): a
            # vector whose every chunk is excluded is removed without grace
            await cur.execute("SELECT DISTINCT c.hash, c.file_path FROM kb.chunks c "
                              "JOIN kb.embeddings e ON e.hash = c.hash")
            keep, drop = set(), set()
            for h, path in await cur.fetchall():
                (keep if self.in_scope(path) else drop).add(bytes(h))
            drop -= keep
            if drop:
                await cur.executemany("DELETE FROM kb.embeddings WHERE hash = %s", [(h,) for h in drop])
        if gone or drop:
            log.info("gc: %d orphaned and %d out-of-scope vectors removed", gone, len(drop))

    async def status(self) -> dict:
        conn = await self.db()
        async with conn.cursor() as cur:
            # Counted in Python, not SQL: "in scope" includes .noembed folders,
            # and a chunk that is deliberately never sent must not read as
            # pending forever.
            await cur.execute(
                "SELECT c.hash, c.file_path, (e.hash IS NOT NULL), x.attempts "
                "FROM kb.chunks c LEFT JOIN kb.embeddings e ON e.hash = c.hash "
                "LEFT JOIN kb.embed_failures x ON x.hash = c.hash" if self.has_table else
                "SELECT c.hash, c.file_path, false, NULL::int FROM kb.chunks c")
            seen: dict[bytes, tuple[bool, int | None]] = {}
            excluded: set[bytes] = set()
            for h, path, done, attempts in await cur.fetchall():
                h = bytes(h)
                if not self.in_scope(path):
                    excluded.add(h)
                    continue
                seen[h] = (bool(done), attempts)
            chunks = len(seen)
            embedded = sum(1 for d, _ in seen.values() if d)
            parked = sum(1 for d, a in seen.values() if not d and a is not None and a >= PARK_AFTER)
            failing = sum(1 for d, a in seen.values() if not d and a is not None and a < PARK_AFTER)
            # One statement, so today and the month are the same snapshot of a
            # ledger the embed loop is writing to right now.
            day = utc_today()
            await cur.execute(
                "SELECT kind, sum(calls) FILTER (WHERE day = %s), sum(units) FILTER (WHERE day = %s), "
                "sum(usd) FILTER (WHERE day = %s), sum(calls), sum(units), sum(usd) FROM kb.spend "
                "WHERE day >= %s GROUP BY kind", (day, day, day, day.replace(day=1)))
            today, month = {}, {}
            for k, tc, tu, td, mc, mu, md in await cur.fetchall():
                if tc is not None:
                    today[k] = {"calls": int(tc), "units": int(tu), "usd": float(td)}
                month[k] = {"calls": int(mc), "units": int(mu), "usd": float(md)}
        now = time.monotonic()
        while self.rate and now - self.rate[0][0] > 600:
            self.rate.popleft()
        return {
            "version": 1,
            "at": _iso(time.time()),
            "configured": self.provider is not None,
            "provider": self.cfg.embed_provider,
            "model": self.model,
            "rerank": {"configured": self.cfg.rerank_ready, "provider": self.cfg.rerank_provider,
                       "model": self.cfg.rerank_model or None},
            "enabled": bool(self.settings.get("search.enabled", True)),
            "paused": self.paused,
            "reason": self.paused_reason,
            "breaker_until": _iso(time.time() + (self.breaker.open_until - now))
            if self.breaker.open_until > now else None,
            "chunks": chunks,
            "embedded": embedded,
            "pending": max(0, chunks - embedded - parked),
            "parked": parked,
            "failing": failing,
            "excluded": len(excluded - set(seen)),
            "per_minute": round(sum(n for _, n in self.rate) / 10.0, 1),
            "spend": {"today": today, "month": month},
            "budget": {k.split(".")[-1]: self.settings.get(k) for k in
                       ("search.budget.embed_day_usd", "search.budget.embed_month_usd",
                        "search.budget.rerank_day_usd")},
            "warnings": list(self.warnings) + (self.service.warnings if self.service else []),
            "query": self.service.state() if self.service else None,
            "last_error": self.last_error,
            "last_error_at": _iso(self.last_error_at),
        }

    async def write_status(self) -> None:
        try:
            self.last_status = await self.status()
            body = json.dumps(self.last_status, indent=1)
        except Exception as e:                  # noqa: BLE001 — a status must always be written
            body = json.dumps({"version": 1, "at": _iso(time.time()),
                               "paused": "database" if isinstance(e, psycopg.Error) else "error",
                               "reason": str(e)[:200], "configured": self.provider is not None})
        tmp = STATUS_DIR / f".status.{os.getpid()}.tmp"
        try:
            tmp.write_text(body)
            # Readable by whoever can enter the directory: /run/kb/search is
            # 0750 kbindexer:kb-users, so members (and their agents) yes,
            # kbshare and the share container no.
            os.chmod(tmp, 0o644)
            os.replace(tmp, STATUS_FILE)
        except OSError as e:
            log.warning("could not write %s: %s", STATUS_FILE, e)

    # ---- the daemon ----------------------------------------------------------
    async def embed_loop(self) -> None:
        while True:
            await asyncio.sleep(await self.tick())

    async def tick(self) -> float:
        """One step with the loop's error handling around it. Returns seconds
        to sleep. Never raises."""
        if True:
            try:
                delay = await self.step()
                self.db_backoff = 2.0
            except psycopg.Error as e:
                # The database, not the provider: back the whole loop off, and
                # make no call until it answers — every step starts with a query.
                self.note_error(f"database: {e}"[:200])
                self.paused, self.paused_reason = "database", str(e)[:200]
                delay, self.db_backoff = self.db_backoff, min(self.db_backoff * 2, 600.0)
                try:
                    if self.conn is not None:
                        await self.conn.close()
                except Exception:              # noqa: BLE001
                    pass
                self.conn = None
            except Exception as e:             # noqa: BLE001
                # A bug, not weather. Crashing would restart the unit and wipe
                # the in-memory brake; backing off keeps it, and the ledger's
                # reservation already bounds whatever the failed step spent.
                log.exception("embedding step failed")
                self.note_error(f"internal: {type(e).__name__}: {e}"[:200])
                self.paused, self.paused_reason = "error", self.last_error
                delay = 300.0
            return delay

    async def status_loop(self) -> None:
        while True:
            await self.write_status()
            await asyncio.sleep(10)

    async def gc_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self.gc()
            except psycopg.Error as e:
                log.warning("gc failed: %s", e)
            await asyncio.sleep(3600)

    async def run(self) -> None:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            self.session = session
            log.info("kb-embedd starting: provider=%s model=%s rerank=%s",
                     self.cfg.embed_provider, self.model, self.cfg.rerank_provider)
            self.service = Service(self)
            runner = await self.service.start(SOCK_FILE)
            try:
                await asyncio.gather(self.embed_loop(), self.status_loop(), self.gc_loop())
            finally:
                await runner.cleanup()


class Service:
    """The socket: /run/kb/search/api.sock. Searches embed their query and
    rerank their hits HERE, so query-time spend lands in the same ledger, under
    the same budgets, as the index — and every caller is known by the uid the
    kernel reports (SO_PEERCRED), not by anything it says about itself.

    Who can connect: the directory is 0750 kbindexer:kb-users, so every person
    (and their agents and cron jobs) and root; kbshare and the share container
    cannot. The handler checks group membership again anyway.

    It has its OWN database connection: the embed loop holds transactions open
    on the other one, and a query's ledger entry must never ride inside (and
    roll back with) an index write.
    """

    def __init__(self, emb: Embedder):
        self.e = emb
        self.conn: psycopg.AsyncConnection | None = None
        self.reranker = embedding.make_reranker(emb.cfg) if emb.cfg.rerank_ready else None
        self.rbreaker = Breaker()
        self.cache: collections.OrderedDict[str, tuple[float, list[float]]] = collections.OrderedDict()
        self.minute_embed: collections.deque[float] = collections.deque()
        self.minute_rerank: collections.deque[float] = collections.deque()
        self.uid_embed: dict[int, collections.deque[float]] = collections.defaultdict(collections.deque)
        self._members: tuple[float, int | None, int | None] = (0.0, None, None)
        self.warnings: list[str] = []
        self.counts = collections.Counter()
        self.rerank_paused: str | None = None
        self.last_error: str | None = None

    async def db(self) -> psycopg.AsyncConnection:
        if self.conn is None or self.conn.closed:
            self.conn = await psycopg.AsyncConnection.connect(f"dbname={common.PG_DB}", autocommit=True)
        return self.conn

    def state(self) -> dict:
        return {"socket": True, "rerank_paused": self.rerank_paused,
                "rerank_breaker": self.rbreaker.is_open(time.monotonic()),
                "embeds": self.counts["embed"], "cached": self.counts["cached"],
                "reranks": self.counts["rerank"], "refused": self.counts["refused"],
                "last_error": self.last_error}

    # ---- who is asking -----------------------------------------------------
    def _gids(self) -> tuple[int | None, int | None]:
        now = time.monotonic()
        if now - self._members[0] > 60:
            import grp
            def gid(name):
                try:
                    return grp.getgrnam(name).gr_gid
                except KeyError:
                    return None
            self._members = (now, gid("kb-users"), gid(os.environ.get("KB_ADMIN_GROUP", "sudo")))
        return self._members[1], self._members[2]

    def caller(self, request) -> tuple[int, str, bool] | None:
        """(uid, name, is_admin) of the peer, or None when it may not use this."""
        import pwd
        import socket
        import struct
        sock = request.transport.get_extra_info("socket") if request.transport else None
        if sock is None:
            return None
        try:
            _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        except OSError:
            return None
        if uid == 0:
            return 0, "root", True
        try:
            pw = pwd.getpwuid(uid)
        except KeyError:
            return None
        members, admins = self._gids()
        groups = set(os.getgrouplist(pw.pw_name, pw.pw_gid))
        if members is None or members not in groups:
            return None
        return uid, pw.pw_name, admins is not None and admins in groups

    @staticmethod
    def _window(q: collections.deque, span: float, now: float) -> int:
        while q and now - q[0] > span:
            q.popleft()
        return len(q)

    def refuse(self, status: int, error: str, why: str):
        from aiohttp import web
        self.counts["refused"] += 1
        return web.json_response({"error": error, "why": why}, status=status)

    # ---- routes ------------------------------------------------------------
    async def h_embed(self, request):
        from aiohttp import web
        who = self.caller(request)
        if who is None:
            return self.refuse(403, "forbidden", "only members of kb-users may search")
        uid, name, _ = who
        try:
            body = await request.json()
            text = str(body.get("text", "")).replace("\x00", "").strip()[:Q_MAX_CHARS]
        except Exception:                      # noqa: BLE001
            return self.refuse(400, "bad_request", "expected {\"text\": ...}")
        if not text:
            return self.refuse(400, "bad_request", "empty text")
        e = self.e
        e.refresh_settings()
        if e.provider is None:
            return self.refuse(503, "unconfigured", "no embedding provider configured")
        if not e.settings.get("search.enabled", True):
            return self.refuse(503, "disabled", "semantic search is turned off in Settings")
        if e.dims_ok is False:
            return self.refuse(503, e.paused or "dims", e.paused_reason or "no usable vector index")
        now = time.monotonic()
        hit = self.cache.get(text)
        if hit and now - hit[0] < Q_CACHE_SECONDS:
            self.cache.move_to_end(text)
            self.counts["cached"] += 1
            return web.json_response({"vector": hit[1], "tokens": 0, "cached": True, "model": e.model,
                                      "max_distance": e.cfg.max_distance})
        if e.breaker.is_open(now):
            return self.refuse(503, "breaker", e.breaker.reason or "provider failing")
        if self._window(self.minute_embed, 60, now) >= Q_EMBED_PER_MIN:
            return self.refuse(429, "limit", "query embeddings per minute (all callers)")
        if self._window(self.uid_embed[uid], 3600, now) >= Q_EMBED_PER_UID_HOUR:
            return self.refuse(429, "limit", f"query embeddings per hour for {name}")
        price = e.price("search.price.embed_per_1m_usd")
        est_units = max(1, math.ceil(len(text) / EST_CHARS_PER_TOKEN))
        conn = await self.db()
        blocked = await e.budget_block(est_units * price / 1e6, conn)
        if blocked:
            return self.refuse(429, "budget", blocked)
        self.minute_embed.append(now)
        self.uid_embed[uid].append(now)
        await e.charge("embed_query", 1, est_units, est_units * price / 1e6, conn=conn)   # reserve
        try:
            res = await e.provider.embed(e.session, [text])
        except embedding.ProviderError as err:
            if err.status:
                await e.charge("embed_query", 0, -est_units, -est_units * price / 1e6, conn=conn)
            self.last_error = str(err)[:200]
            if err.global_failure:
                e.breaker.failure(err, time.monotonic())
            return self.refuse(502, "provider", str(err)[:200])
        e.breaker.success()
        await e.charge("embed_query", 0, res.tokens - est_units, (res.tokens - est_units) * price / 1e6,
                       conn=conn)
        await self.count_user(conn, name, "embed_query")
        vec = [round(float(x), 6) for x in res.vectors[0]]
        self.cache[text] = (now, vec)
        while len(self.cache) > Q_CACHE_SIZE:
            self.cache.popitem(last=False)
        self.counts["embed"] += 1
        return web.json_response({"vector": vec, "tokens": res.tokens, "cached": False, "model": e.model,
                                  "max_distance": e.cfg.max_distance})

    async def count_user(self, conn, name: str, kind: str) -> int:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO kb.spend_user(day, usr, kind, calls) VALUES (%s,%s,%s,1) "
                "ON CONFLICT (day, usr, kind) DO UPDATE SET calls = kb.spend_user.calls + 1 "
                "RETURNING calls", (utc_today(), name, kind))
            return int((await cur.fetchone())[0])

    async def h_rerank(self, request):
        from aiohttp import web
        who = self.caller(request)
        if who is None:
            return self.refuse(403, "forbidden", "only members of kb-users may search")
        uid, name, _ = who
        try:
            body = await request.json()
            query = str(body.get("query", "")).replace("\x00", "").strip()[:Q_MAX_CHARS]
            docs = [str(d).replace("\x00", "")[:RERANK_MAX_DOC_CHARS] for d in body.get("docs", [])]
        except Exception:                      # noqa: BLE001
            return self.refuse(400, "bad_request", "expected {\"query\": ..., \"docs\": [...]}")
        if not query or not docs:
            return self.refuse(400, "bad_request", "a query and at least one document")
        if len(docs) > RERANK_MAX_DOCS:
            return self.refuse(400, "bad_request", f"at most {RERANK_MAX_DOCS} documents")
        e = self.e
        e.refresh_settings()
        if self.reranker is None:
            return self.refuse(503, "unconfigured", "no rerank provider configured")
        if not (e.settings.get("search.enabled", True) and e.settings.get("search.rerank.enabled", True)):
            return self.refuse(503, "disabled", "reranking is turned off in Settings")
        now = time.monotonic()
        if self.rbreaker.is_open(now):
            return self.refuse(503, "breaker", self.rbreaker.reason or "rerank provider failing")
        if self._window(self.minute_rerank, 60, now) >= Q_RERANK_PER_MIN:
            return self.refuse(429, "limit", "reranks per minute (all callers)")
        conn = await self.db()
        per_user = int(e.settings.get("search.rerank.per_user_day", 200))
        async with conn.cursor() as cur:
            await cur.execute("SELECT calls FROM kb.spend_user WHERE day = %s AND usr = %s AND kind = 'rerank'",
                              (utc_today(), name))
            row = await cur.fetchone()
        if row and row[0] >= per_user:
            return self.refuse(429, "limit", f"{name} has used today's {per_user} reranks")
        # Cohere bills one search unit per 100 documents, a document over 500
        # tokens (query included) counting once per 500.
        q_tok = len(query) / EST_CHARS_PER_TOKEN
        pieces = sum(max(1, math.ceil((len(d) / EST_CHARS_PER_TOKEN + q_tok) / 500)) for d in docs)
        est_units = max(1, math.ceil(pieces / 100))
        price = e.price("search.price.rerank_per_1k_usd")
        s = await e.spent(conn)
        cap = e.settings.get("search.budget.rerank_day_usd", 20)
        used = s["today"].get("rerank", 0.0)
        self.warnings = []
        if cap and used >= cap / 2:
            self.warnings.append(f"rerank spend today: ${used:.2f} of ${cap}")
            if await e.flag_once(str(utc_today()), "rerank_50_today", conn):
                log.warning("rerank budget half used today: $%.2f of $%s", used, cap)
        if used + est_units * price / 1000 > cap:
            self.rerank_paused = f"daily rerank budget (${used:.2f} of ${cap})"
            return self.refuse(429, "budget", self.rerank_paused)
        self.rerank_paused = None
        self.minute_rerank.append(now)
        model = f"{e.cfg.rerank_provider}:{e.cfg.rerank_model}"
        await e.charge("rerank", 1, est_units, est_units * price / 1000, model=model, conn=conn)   # reserve
        await self.count_user(conn, name, "rerank")
        try:
            res = await self.reranker.rerank(e.session, query, docs)
        except embedding.ProviderError as err:
            if err.status:
                await e.charge("rerank", 0, -est_units, -est_units * price / 1000, model=model, conn=conn)
            self.last_error = str(err)[:200]
            if err.global_failure:
                self.rbreaker.failure(err, time.monotonic())
            return self.refuse(502, "provider", str(err)[:200])
        self.rbreaker.success()
        await e.charge("rerank", 0, res.units - est_units, (res.units - est_units) * price / 1000,
                       model=model, conn=conn)
        self.counts["rerank"] += 1
        return web.json_response({"scores": [round(float(x), 6) for x in res.scores], "units": res.units})

    async def h_status(self, request):
        """The status file's content, plus the caller's own use today."""
        from aiohttp import web
        who = self.caller(request)
        if who is None:
            return self.refuse(403, "forbidden", "only members of kb-users")
        out = dict(self.e.last_status or {"version": 1, "starting": True})
        me = {"user": who[1], "rerank": 0, "embed_query": 0,
              "rerank_cap": int(self.e.settings.get("search.rerank.per_user_day", 200))}
        try:
            conn = await self.db()
            async with conn.cursor() as cur:
                await cur.execute("SELECT kind, calls FROM kb.spend_user WHERE day = %s AND usr = %s",
                                  (utc_today(), who[1]))
                for kind, calls in await cur.fetchall():
                    me[kind] = int(calls)
        except psycopg.Error:
            self.conn = None
        out["me"] = me
        return web.json_response(out)

    async def h_retry(self, request):
        """Give parked chunks another chance (admins; the Settings button)."""
        from aiohttp import web
        who = self.caller(request)
        if who is None or not who[2]:
            return self.refuse(403, "forbidden", "admins only")
        conn = await self.db()
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM kb.embed_failures")
            n = cur.rowcount
        log.info("%s cleared %d embedding failures", who[1], n)
        return web.json_response({"cleared": n})

    def guarded(self, handler):
        """A database that went away (a Postgres restart) is an answer, not a
        traceback: drop the connection — the next request opens a fresh one —
        and say so. The caller falls back to what it has. A reservation made
        before the failure stays in the ledger: over-counting is the safe side."""
        async def run(request):
            try:
                return await handler(request)
            except psycopg.Error as e:
                try:
                    if self.conn is not None:
                        await self.conn.close()
                except Exception:              # noqa: BLE001
                    pass
                self.conn = None
                self.last_error = f"database: {type(e).__name__}"
                return self.refuse(503, "database", "the index database is unavailable; try again")
        return run

    async def start(self, path: Path):
        from aiohttp import web
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_post("/embed", self.guarded(self.h_embed))
        app.router.add_post("/rerank", self.guarded(self.h_rerank))
        app.router.add_get("/status", self.guarded(self.h_status))
        app.router.add_post("/retry-parked", self.guarded(self.h_retry))
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        await web.UnixSite(runner, str(path)).start()
        # The DIRECTORY is the gate (0750 kbindexer:kb-users); the socket itself
        # must be connectable by every member who can reach it.
        os.chmod(path, 0o666)
        return runner


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    asyncio.run(Embedder().run())


if __name__ == "__main__":
    main()
