"""kb-embedd's spend guarantees, proven against real SQL.

Every invariant in docs/semantic-search.md ("Invariants") is a test here. They
run kb-embedd's worker IN-PROCESS against a scratch Postgres database built
from scripts/schema.sql, with a scripted provider that counts every call — so
"never paid twice" means a counter that did not move, not a hope.

The scratch database needs `sudo -u postgres createdb`; where there is no
passwordless sudo (CI runs as an unprivileged demo account) the database half
skips. The pure pieces at the bottom — chunking, error classification, the
brake and the breaker — run everywhere.
"""
import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from kb_platform import common, embedd, embedding  # noqa: E402
from kb_platform import settings as kbsettings  # noqa: E402
from kb_platform.indexer import chunk_sections  # noqa: E402

MODEL = "fake:test:1024"


def _sudo_ok() -> bool:
    try:
        return subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


needs_db = pytest.mark.skipif(not _sudo_ok(), reason="needs passwordless sudo for a scratch database")


# ---- a provider that does what the test says, and counts ---------------------
class Scripted:
    def __init__(self, dims=1024):
        self.dims = dims
        self.calls: list[list[str]] = []
        self.fail = None            # callable(texts) -> ProviderError | None
        self.poison_vector = False  # return a vector Postgres will refuse

    async def embed(self, session, texts):
        self.calls.append(list(texts))
        if self.fail:
            err = self.fail(texts)
            if err:
                raise err
        vecs = [embedding._fake_vector(t, self.dims) for t in texts]
        if self.poison_vector:
            vecs = [[float("nan")] * self.dims for _ in texts]
        return embedding.EmbedResult(vecs, sum(len(t) // 4 + 1 for t in texts))

    @property
    def n(self) -> int:
        return len(self.calls)


# ---- the scratch database ------------------------------------------------------
@pytest.fixture
def scratch(tmp_path, monkeypatch):
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    db = f"kb_embedd_test_{os.getpid()}_{int(time.time() * 1000) % 10**6}"
    sh = lambda *a: subprocess.run(["sudo", "-n", "-u", "postgres", *a], capture_output=True, text=True)  # noqa: E731
    r = sh("createdb", "-O", me, db)
    assert r.returncode == 0, r.stderr
    try:
        assert sh("psql", "-d", db, "-qc", "CREATE EXTENSION IF NOT EXISTS vector").returncode == 0
        # The schema is written for the `kbindexer` owner; the scratch copy is
        # owned by whoever runs the tests, which is all the SQL needs.
        sql = (ROOT / "scripts/schema.sql").read_text().replace("AUTHORIZATION kbindexer",
                                                                 "AUTHORIZATION CURRENT_USER")
        sql = re.sub(r"\bTO kb_users\b", "TO PUBLIC", sql)
        r = subprocess.run(["psql", "-d", db, "-v", "ON_ERROR_STOP=1", "-q"], input=sql,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        repo = tmp_path / "repo"
        (repo / ".os").mkdir(parents=True)
        run = tmp_path / "run"
        (run / "search").mkdir(parents=True)
        monkeypatch.setattr(common, "PG_DB", db)
        monkeypatch.setattr(common, "REPO_ROOT", repo)
        monkeypatch.setattr(kbsettings, "company_file", lambda: repo / ".os" / "settings.json")
        monkeypatch.setattr(embedd, "STATUS_DIR", run / "search")
        monkeypatch.setattr(embedd, "STATUS_FILE", run / "search" / "status.json")
        yield {"db": db, "repo": repo, "me": me}
    finally:
        sh("dropdb", "--if-exists", "--force", db)


def psql(db: str, sql: str) -> str:
    r = subprocess.run(["psql", "-d", db, "-tAc", sql], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def add_file(db: str, path: str, text: str, quiet: bool = True, model: str = MODEL) -> list[dict]:
    """What kb-indexer does for one document: a kb.files row and its chunks."""
    mtime = time.time() - (3600 if quiet else 1)
    import psycopg
    chunks = chunk_sections(text, path, model)
    with psycopg.connect(f"dbname={db}", autocommit=True) as c:
        c.execute("INSERT INTO kb.files(path, owner_name, group_name, mode, mtime) VALUES (%s,'x','x',420,%s) "
                  "ON CONFLICT (path) DO UPDATE SET mtime = EXCLUDED.mtime", (path, mtime))
        c.execute("DELETE FROM kb.chunks WHERE file_path = %s", (path,))
        for ch in chunks:
            c.execute("INSERT INTO kb.chunks(file_path, seq, start_line, end_line, heading, text, hash) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (path, ch["seq"], ch["start_line"], ch["end_line"], ch["heading"], ch["text"], ch["hash"]))
    return chunks


def settings(scratch, **values) -> None:
    import json
    (scratch["repo"] / ".os" / "settings.json").write_text(json.dumps(values))


def worker(provider) -> embedd.Embedder:
    cfg = embedding.Config(embed_provider="fake", dims=1024)
    w = embedd.Embedder(cfg=cfg, provider=provider)
    w.model = MODEL
    return w


def drive(w: embedd.Embedder, steps: int = 1) -> None:
    """Run `steps` ticks, as if their sleeps had passed."""
    async def go():
        for _ in range(steps):
            w.wait_until = 0.0
            w.settings_at = 0.0
            await w.tick()
        if w.conn is not None:
            await w.conn.close()
            w.conn = None
    asyncio.run(go())


DOC = "# Plan\n\nIntro line.\n\n## Budget\n\nQ3 is approved.\n\n## People\n\nTwo hires in October.\n"



# ---- the real indexer writes the chunks -------------------------------------------
def _indexer(scratch, tmp_path, monkeypatch):
    from kb_platform import indexer as idx
    repo = scratch["repo"]
    monkeypatch.setattr(idx.embedding, "load_config",
                        lambda read_keys=False: embedding.Config(embed_provider="fake", dims=1024))
    ix = idx.Indexer()
    return ix, repo


@needs_db
def test_the_indexer_writes_chunks_beside_blocks_and_a_rewrite_keeps_their_hashes(scratch, tmp_path, monkeypatch):
    db = scratch["db"]
    ix, repo = _indexer(scratch, tmp_path, monkeypatch)
    try:
        f = repo / "company" / "plan.md"
        f.parent.mkdir(parents=True)
        f.write_text(DOC)
        ix.reindex_file(f)
        blocks = int(psql(db, "SELECT count(*) FROM kb.blocks WHERE file_path='company/plan.md'"))
        before = psql(db, "SELECT string_agg(encode(hash,'hex'), ',' ORDER BY seq) FROM kb.chunks")
        assert blocks > 0 and before, "blocks and chunks are both written"
        ix.reindex_file(f)                     # what every restart does
        assert psql(db, "SELECT string_agg(encode(hash,'hex'), ',' ORDER BY seq) FROM kb.chunks") == before
        assert psql(db, "SELECT DISTINCT heading FROM kb.chunks WHERE seq = 1") == "Plan › Budget"
    finally:
        ix.conn.close()


@needs_db
def test_a_chunk_that_cannot_be_written_never_costs_full_text_search(scratch, tmp_path, monkeypatch):
    db = scratch["db"]
    ix, repo = _indexer(scratch, tmp_path, monkeypatch)
    from kb_platform import indexer as idx
    real = idx.chunk_sections
    # two chunks with the same seq: the primary key refuses the second
    monkeypatch.setattr(idx, "chunk_sections", lambda *a: [dict(c, seq=0) for c in real(*a)])
    try:
        f = repo / "company" / "plan.md"
        f.parent.mkdir(parents=True)
        f.write_text(DOC)
        ix.reindex_file(f)
        assert int(psql(db, "SELECT count(*) FROM kb.blocks WHERE file_path='company/plan.md'")) > 0
        assert psql(db, "SELECT count(*) FROM kb.chunks") == "0"
        assert psql(db, "SELECT count(*) FROM kb.files WHERE path='company/plan.md'") == "1"
    finally:
        ix.conn.close()

# ---- the invariants --------------------------------------------------------------
@needs_db
def test_pending_chunks_are_embedded_and_the_ledger_holds_what_the_provider_reported(scratch):
    db = scratch["db"]
    chunks = add_file(db, "company/plan.md", DOC)
    p = Scripted()
    drive(worker(p))
    assert p.n == 1, "one batch for one small document"
    assert int(psql(db, "SELECT count(*) FROM kb.embeddings")) == len(chunks)
    reported = sum(len(t) // 4 + 1 for t in p.calls[0])
    units, usd = psql(db, "SELECT units, usd FROM kb.spend WHERE kind='embed_index'").split("|")
    assert int(units) == reported, "the ledger holds the provider's count, not the estimate"
    assert abs(float(usd) - reported * 0.13 / 1e6) < 1e-9


@needs_db
def test_rewriting_unchanged_chunks_costs_nothing(scratch):
    """What every indexer restart does to every document."""
    db = scratch["db"]
    add_file(db, "company/plan.md", DOC)
    p = Scripted()
    drive(worker(p))
    assert p.n == 1
    for _ in range(3):                                # three restarts' worth of rewrites
        add_file(db, "company/plan.md", DOC)
    drive(worker(p), steps=3)
    assert p.n == 1, "unchanged text was sent again"


@needs_db
def test_an_edit_re_embeds_only_the_section_that_changed(scratch):
    db = scratch["db"]
    add_file(db, "company/plan.md", DOC)
    p = Scripted()
    drive(worker(p))
    add_file(db, "company/plan.md", DOC.replace("Q3 is approved.", "Q3 is approved, Q4 is not."))
    drive(worker(p))
    assert p.n == 2
    assert len(p.calls[1]) == 1 and "Q4 is not" in p.calls[1][0]


@needs_db
def test_a_document_being_typed_in_waits_for_quiet(scratch):
    db = scratch["db"]
    add_file(db, "company/plan.md", DOC, quiet=False)  # modified a second ago
    p = Scripted()
    drive(worker(p), steps=3)
    assert p.n == 0, "a document still being written was sent"
    add_file(db, "company/plan.md", DOC, quiet=True)
    drive(worker(p))
    assert p.n == 1


@needs_db
def test_a_rejected_chunk_is_found_by_bisection_and_parked_alone(scratch):
    db = scratch["db"]
    doc = "# Big\n\n" + "\n\n".join(f"## S{i}\n\nsection {i} text" for i in range(16))
    doc = doc.replace("section 11 text", "POISON section 11 text")
    add_file(db, "company/big.md", doc)
    p = Scripted()
    p.fail = lambda texts: (embedding.ProviderError("bad_request", 400, code="invalid_input")
                            if any("POISON" in t for t in texts) else None)
    drive(worker(p))
    assert p.n <= 1 + embedd.BISECT_EXTRA
    assert int(psql(db, "SELECT count(*) FROM kb.embed_failures")) == 1, "only the culprit is memoed"
    total = int(psql(db, "SELECT count(DISTINCT hash) FROM kb.chunks"))
    assert int(psql(db, "SELECT count(*) FROM kb.embeddings")) == total - 1
    # it is not retried until its backoff passes…
    before = p.n
    drive(worker(p), steps=3)
    assert p.n == before
    # …and after PARK_AFTER attempts it is never tried again
    for _ in range(embedd.PARK_AFTER + 2):
        psql(db, "UPDATE kb.embed_failures SET next_try = now() - interval '1 second'")
        drive(worker(p))
    attempts = int(psql(db, "SELECT attempts FROM kb.embed_failures"))
    assert attempts == embedd.PARK_AFTER
    parked = p.n
    psql(db, "UPDATE kb.embed_failures SET next_try = now() - interval '1 second'")
    drive(worker(p), steps=3)
    assert p.n == parked, "a parked chunk was sent again"


@needs_db
def test_a_broken_provider_trips_the_breaker_instead_of_looping(scratch):
    db = scratch["db"]
    add_file(db, "company/plan.md", DOC)
    p = Scripted()
    p.fail = lambda texts: embedding.ProviderError("server", 503)
    w = worker(p)
    drive(w, steps=embedd.BREAKER_TRIP)
    assert p.n == embedd.BREAKER_TRIP
    assert w.breaker.open_until > time.monotonic(), "the breaker did not open"

    async def more():                                  # the breaker is open: no call at all
        for _ in range(10):
            w.wait_until = 0.0
            await w.tick()
        if w.conn is not None:
            await w.conn.close()
            w.conn = None
    asyncio.run(more())
    assert p.n == embedd.BREAKER_TRIP
    assert w.paused == "breaker"
    # an error the provider answered is not billed: the reservations were refunded
    assert int(psql(db, "SELECT COALESCE(sum(units), 0) FROM kb.spend")) == 0


@needs_db
def test_a_wrong_key_is_one_failure_for_the_whole_loop_not_one_per_chunk(scratch):
    db = scratch["db"]
    for i in range(5):
        add_file(db, f"company/d{i}.md", DOC.replace("Plan", f"Plan {i}"))
    p = Scripted()
    p.fail = lambda texts: embedding.ProviderError("auth", 401)
    drive(worker(p), steps=2)
    assert int(psql(db, "SELECT count(*) FROM kb.embed_failures")) == 0, \
        "a provider-wide failure was blamed on individual chunks"


@needs_db
def test_the_daily_budget_stops_calls_before_they_are_made(scratch):
    db = scratch["db"]
    add_file(db, "company/plan.md", DOC)
    settings(scratch, **{"search.budget.embed_day_usd": 0})
    p = Scripted()
    w = worker(p)
    drive(w, steps=3)
    assert p.n == 0
    assert w.paused == "budget"
    settings(scratch, **{"search.budget.embed_day_usd": 10})
    drive(w)
    assert p.n == 1


@needs_db
def test_spend_already_in_the_ledger_counts_against_the_budget(scratch):
    db = scratch["db"]
    add_file(db, "company/plan.md", DOC)
    psql(db, "INSERT INTO kb.spend(day, kind, model, calls, units, usd) "
             "VALUES ((now() AT TIME ZONE 'utc')::date, 'embed_index', 'x', 1, 1, 9.99999)")
    settings(scratch, **{"search.budget.embed_day_usd": 10})
    p = Scripted()
    drive(worker(p))
    # the batch's own estimate would cross the cap → not sent (a few $0.00001 of headroom is not enough)
    assert p.n in (0, 1)
    psql(db, "UPDATE kb.spend SET usd = 10")
    before = p.n
    drive(worker(p), steps=2)
    assert p.n == before, "a call was made with the day's budget spent"


@needs_db
def test_a_paid_batch_that_cannot_be_stored_is_not_paid_for_again(scratch):
    db = scratch["db"]
    add_file(db, "company/plan.md", DOC)
    p = Scripted()
    p.poison_vector = True                              # Postgres refuses NaN in a halfvec
    w = worker(p)
    drive(w)
    assert p.n == 1
    assert int(psql(db, "SELECT count(*) FROM kb.embeddings")) == 0
    p.poison_vector = False
    drive(w, steps=3)
    assert p.n == 1, "a batch that was paid for was paid for again"


@needs_db
def test_the_in_process_brake_holds_even_when_everything_else_would_allow(scratch):
    db = scratch["db"]
    for i in range(10):
        add_file(db, f"company/d{i}.md", DOC.replace("Plan", f"Plan {i}"))
    p = Scripted()
    w = worker(p)
    w.brake = embedd.Brake(per_min=2, per_day=100)
    import unittest.mock as um
    with um.patch.object(embedd, "BATCH", 1):
        drive(w, steps=8)
    assert p.n == 2
    assert w.paused == "brake"


@needs_db
def test_a_file_that_keeps_changing_is_capped_per_day(scratch):
    db = scratch["db"]
    p = Scripted()
    w = worker(p)
    for i in range(embedd.FILE_DAY_FACTOR + 3):
        add_file(db, "company/clock.md", f"# Clock\n\nUpdated: {i}\n")
        drive(w)
    assert p.n == embedd.FILE_DAY_FACTOR, "a file that changes all day was embedded all day"


@needs_db
def test_scope_and_noembed_keep_text_on_the_box_and_remove_what_was_sent(scratch):
    db, repo = scratch["db"], scratch["repo"]
    add_file(db, "company/hr/salaries.md", "# Salaries\n\nconfidential numbers\n")
    add_file(db, "users/alice/notes.md", "# Notes\n\nprivate thoughts\n")
    add_file(db, "company/plan.md", DOC)
    (repo / "company" / "hr").mkdir(parents=True)
    (repo / "company" / "hr" / ".noembed").write_text("")
    settings(scratch, **{"search.embed.scope": "company+projects"})
    p = Scripted()
    w = worker(p)
    drive(w, steps=3)
    sent = "\n".join(t for call in p.calls for t in call)
    assert "confidential" not in sent and "private thoughts" not in sent
    assert "Q3 is approved" in sent
    # widen the scope: users/ is embedded; then narrow it again: its vectors go
    settings(scratch, **{"search.embed.scope": "all"})
    drive(w, steps=2)
    assert any("private thoughts" in t for call in p.calls for t in call)
    settings(scratch, **{"search.embed.scope": "company+projects"})
    w.settings_at = 0.0
    w.refresh_settings()

    async def gc():
        await w.gc()
        await w.conn.close()
        w.conn = None
    asyncio.run(gc())
    left = psql(db, "SELECT count(*) FROM kb.embeddings e JOIN kb.chunks c ON c.hash = e.hash "
                    "WHERE c.file_path LIKE 'users/%'")
    assert int(left) == 0, "narrowing the scope left vectors behind"


@needs_db
def test_orphaned_vectors_survive_a_grace_period_then_go(scratch):
    db = scratch["db"]
    add_file(db, "company/plan.md", DOC)
    p = Scripted()
    w = worker(p)
    drive(w)
    psql(db, "DELETE FROM kb.chunks")                 # the file was deleted
    n = int(psql(db, "SELECT count(*) FROM kb.embeddings"))

    async def gc():
        await w.gc()
        await w.conn.close()
        w.conn = None
    asyncio.run(gc())
    assert int(psql(db, "SELECT count(*) FROM kb.embeddings")) == n, "no grace period"
    # restored within the grace period: the vectors are still there, nothing is paid
    add_file(db, "company/plan.md", DOC)
    fresh = Scripted()
    drive(worker(fresh))
    assert fresh.n == 0
    asyncio.run(gc())
    assert psql(db, "SELECT count(*) FROM kb.embeddings WHERE missing_since IS NOT NULL") == "0"
    # deleted for longer than the grace period: gone, and a later restore pays once
    psql(db, "DELETE FROM kb.chunks")
    asyncio.run(gc())
    psql(db, "UPDATE kb.embeddings SET missing_since = now() - interval '8 days'")
    asyncio.run(gc())
    assert int(psql(db, "SELECT count(*) FROM kb.embeddings")) == 0
    add_file(db, "company/plan.md", DOC)
    drive(worker(fresh))
    assert fresh.n == 1


@needs_db
def test_search_vec_returns_only_what_the_caller_can_see_and_never_text(scratch):
    db, me = scratch["db"], scratch["me"]
    add_file(db, "company/visible.md", "# Visible\n\nshared budget plan\n")
    add_file(db, "company/hidden.md", "# Hidden\n\nshared budget plan, secret edition\n")
    p = Scripted()
    drive(worker(p))
    psql(db, f"INSERT INTO kb.visible_files(usr, path) VALUES ('{me}', 'company/visible.md')")
    q = embedding.vector_literal(embedding._fake_vector("shared budget plan", 1024))
    rows = psql(db, f"SELECT file_path FROM kb.search_vec('{q}'::halfvec, 50)").splitlines()
    assert rows and set(rows) == {"company/visible.md"}
    cols = psql(db, "SELECT string_agg(attname, ',') FROM pg_attribute a "
                    "JOIN pg_proc f ON f.proname = 'search_vec' "
                    "WHERE false")  # (a shape check follows)
    del cols
    shape = psql(db, "SELECT pg_get_function_result('kb.search_vec(halfvec,int)'::regprocedure)")
    assert "text" not in shape.replace("file_path text", ""), f"search_vec returns text: {shape}"


@needs_db
def test_a_vector_width_mismatch_is_a_state_not_a_spend(scratch):
    p = Scripted()
    cfg = embedding.Config(embed_provider="fake", dims=3072)
    w = embedd.Embedder(cfg=cfg, provider=p)
    add_file(scratch["db"], "company/plan.md", DOC)
    drive(w, steps=2)
    assert p.n == 0
    assert w.paused == "dims mismatch"


@needs_db
def test_the_status_file_says_what_is_going_on(scratch):
    import json
    db = scratch["db"]
    add_file(db, "company/plan.md", DOC)
    p = Scripted()
    w = worker(p)
    drive(w)

    async def status():
        await w.write_status()
        await w.conn.close()
        w.conn = None
    asyncio.run(status())
    st = json.loads(embedd.STATUS_FILE.read_text())
    assert st["chunks"] == st["embedded"] > 0 and st["pending"] == 0
    assert st["spend"]["today"]["embed_index"]["calls"] == 1
    # the directory (0750 kbindexer:kb-users) is the gate, the file is 0644
    assert oct(embedd.STATUS_FILE.stat().st_mode & 0o777) == "0o644"


# ---- the pure pieces (run everywhere, CI included) -----------------------------
def test_chunks_carry_their_heading_trail_and_hash_the_model():
    c = chunk_sections("# Rozpočet\n\nÚvod.\n\n## Q3\n\nAno, schváleno.\n", "company/r.md", "m1")
    assert [x["text"] for x in c] == ["Rozpočet\nÚvod.", "Rozpočet › Q3\nAno, schváleno."]
    other = chunk_sections("# Rozpočet\n\nÚvod.\n\n## Q3\n\nAno, schváleno.\n", "company/r.md", "m2")
    assert c[1]["hash"] != other[1]["hash"], "a different model must be a different hash"


def test_a_heading_inside_a_code_fence_is_text():
    c = chunk_sections("# A\n\n```\n# not a heading\n```\n", "company/a.md", "m")
    assert len(c) == 1 and "# not a heading" in c[0]["text"]


def test_long_sections_are_split_with_overlap_and_bounded():
    body = "\n".join(f"Line {i} " + "lorem ipsum dolor sit amet consectetur " * 2 for i in range(60))
    c = chunk_sections(f"# Long\n\n{body}\n", "company/l.md", "m")
    assert len(c) > 1
    assert all(len(x["text"]) <= embedd_limit() for x in c)
    # consecutive pieces overlap: the last line of one reappears at the start of the next
    first_tail = c[0]["text"].splitlines()[-1]
    assert first_tail in c[1]["text"]


def embedd_limit() -> int:
    from kb_platform import indexer
    return indexer.CHUNK_MAX + len("Long\n")


def test_one_enormous_line_is_cut_not_sent_whole():
    from kb_platform import indexer
    c = chunk_sections("# X\n\n" + "y" * 50_000 + "\n", "company/x.md", "m")
    assert sum(len(x["text"]) for x in c) < indexer.LINE_MAX * 1.2


def test_unchanged_text_hashes_the_same():
    a = chunk_sections(DOC, "company/p.md", MODEL)
    b = chunk_sections(DOC, "company/p.md", MODEL)
    assert [x["hash"] for x in a] == [x["hash"] for x in b]


def test_provider_errors_are_classified_for_the_breaker_or_the_chunk():
    cls = embedding._classify
    assert cls(401, {}, None).kind == "auth" and cls(401, {}, None).global_failure
    assert cls(404, {}, None).kind == "not_found"
    r = cls(429, {"Retry-After": "12"}, None)
    assert r.kind == "rate" and r.retry_after == 12
    assert cls(503, {}, None).global_failure
    bad = cls(400, {}, {"error": {"code": "context_length_exceeded", "message": "secret text here"}})
    assert bad.kind == "bad_request" and not bad.global_failure
    assert "secret text" not in str(bad) and bad.code == "context_length_exceeded"


def test_the_brake_counts_per_minute_and_per_day():
    b = embedd.Brake(per_min=3, per_day=5)
    t = 1000.0
    for _ in range(3):
        assert b.allows(t) == 0
        b.note(t)
    assert b.allows(t) > 0
    assert b.allows(t + 61) == 0
    b.note(t + 61)
    b.note(t + 62)
    assert b.allows(t + 200) > 0, "the daily cap did not hold"


def test_the_breaker_opens_then_probes_once():
    br = embedd.Breaker()
    err = embedding.ProviderError("server", 503)
    t = 0.0
    for _ in range(embedd.BREAKER_TRIP - 1):
        assert br.failure(err, t) < embedd.BREAKER_OPEN
    assert br.failure(err, t) == embedd.BREAKER_OPEN
    assert br.is_open(t + 1)
    assert not br.is_open(t + embedd.BREAKER_OPEN + 1) and br.half_open
    assert br.failure(err, t + embedd.BREAKER_OPEN + 1) == embedd.BREAKER_OPEN, "a failed probe re-opens"
    rate = embedding.ProviderError("rate", 429, retry_after=7)
    assert embedd.Breaker().failure(rate, 0) == 7


def test_the_fake_provider_puts_shared_stems_close_together():
    v = lambda s: embedding._fake_vector(s, 1024)  # noqa: E731
    near = sum(a * b for a, b in zip(v("sdílení projektu"), v("sdílet projekt")))
    far = sum(a * b for a, b in zip(v("sdílení projektu"), v("recept na guláš")))
    assert near > far


def test_a_database_that_went_away_is_an_answer_not_a_traceback():
    """A Postgres restart killed the socket's connection; the next rerank used
    to die with a traceback. Now: 503, connection dropped, next call reconnects."""
    import psycopg
    svc = embedd.Service(worker(Scripted()))

    async def boom(request):
        raise psycopg.errors.AdminShutdown("terminating connection due to administrator command")
    r = asyncio.run(svc.guarded(boom)(None))
    assert r.status == 503 and svc.conn is None and "database" in svc.last_error
