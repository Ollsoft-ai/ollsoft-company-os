-- KB index schema. Owned by role "kbindexer" (the only writer).
-- Every human logs in via peer auth as their own PG role (== OS username),
-- gets SELECT only, and RLS re-implements the Unix read check in SQL so a raw
-- query can never return a row for a file the caller couldn't read on disk.

-- NOTE: `CREATE EXTENSION vector` is run separately as a superuser (see
-- install.sh). Nothing uses it today — see the embedding note below — but it
-- stays installed so semantic search can be added without a superuser step.
CREATE SCHEMA IF NOT EXISTS kb AUTHORIZATION kbindexer;

-- Denormalised filesystem permissions, refreshed by the indexer from stat().
CREATE TABLE IF NOT EXISTS kb.files (
    path       text PRIMARY KEY,           -- repo-relative path
    owner_name text NOT NULL,
    group_name text NOT NULL,
    mode       int  NOT NULL,              -- st_mode & 0o7777
    is_dir     bool NOT NULL DEFAULT false,
    size       bigint,
    mtime      double precision,
    acl_users   text[] DEFAULT '{}',       -- named users granted READ via POSIX ACL
    acl_groups  text[] DEFAULT '{}',       -- named groups granted READ via POSIX ACL
    acl_x_users  text[] DEFAULT '{}',      -- named users granted TRAVERSE (x) — reach a shared file
    acl_x_groups text[] DEFAULT '{}',      -- named groups granted TRAVERSE (x)
    updated_at timestamptz DEFAULT now()
);

-- OS group membership mirror (refreshed from getent by the indexer).
CREATE TABLE IF NOT EXISTS kb.user_groups (
    usr text NOT NULL,
    grp text NOT NULL,
    PRIMARY KEY (usr, grp)
);

-- Materialized per-user visibility: every (usr, path) the Unix read check
-- grants, maintained by the indexer (kb_platform/indexer.py:compute_visibility
-- — the Python mirror of kb.can_read below) and diff-synced on the same ~1 s
-- sweep that refreshes kb.files' permission columns. Policies gate on THIS
-- table, so the per-statement RLS cost is one hashed subplan over the caller's
-- visible paths (~1 ms) instead of can_read() × N_files (plpgsql, measured
-- ~0.34 s per statement at ~650 files — the old policies' scaling wall).
-- Revocation latency is unchanged: kb.files' permission columns were only ever
-- as fresh as the same sweep that now also rewrites this table.
CREATE TABLE IF NOT EXISTS kb.visible_files (
    usr  text NOT NULL,
    path text NOT NULL REFERENCES kb.files(path) ON DELETE CASCADE,
    PRIMARY KEY (usr, path)
);
-- for the FK cascade when a file row is deleted (PK leads with usr, not path)
CREATE INDEX IF NOT EXISTS visible_files_path_idx ON kb.visible_files (path);
ALTER TABLE kb.visible_files ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS visible_files_self ON kb.visible_files;
-- Own rows only: a user must not enumerate paths (or their audiences) via
-- someone else's visibility. texteq is leakproof, so this policy never blocks
-- index use on the table.
CREATE POLICY visible_files_self ON kb.visible_files FOR SELECT
    USING (usr = session_user);

-- Parsed blocks: tasks (checkboxes), headings, links, plain lines.
CREATE TABLE IF NOT EXISTS kb.blocks (
    id         bigserial PRIMARY KEY,
    file_path  text NOT NULL REFERENCES kb.files(path) ON DELETE CASCADE,
    line       int  NOT NULL,              -- 1-based line number in the file
    kind       text NOT NULL,              -- 'task' | 'heading' | 'link' | 'text'
    checked    bool,                       -- tasks only
    block_ref  text,                       -- ^id anchor if present
    text       text NOT NULL,
    assignees  text[] DEFAULT '{}',       -- @mentions on a task
    tags       text[] DEFAULT '{}',       -- #labels on a task
    tsv        tsvector
);

CREATE INDEX IF NOT EXISTS blocks_file_idx ON kb.blocks (file_path);
CREATE INDEX IF NOT EXISTS blocks_task_idx ON kb.blocks (kind) WHERE kind = 'task';
-- Kept for the tsv column's sake, but be aware it does NOT serve user search:
-- `@@` (ts_match_vq) is not leakproof, so under RLS the planner may not push it
-- below the policy qual and cannot use this index — every content search is a
-- sequential scan over the caller's visible blocks, by design of the security
-- model. That is why kb.blocks is kept NARROW (see the embedding note): heap
-- width, not indexing, is what search latency scales with here.
CREATE INDEX IF NOT EXISTS blocks_tsv_idx ON kb.blocks USING gin (tsv);

-- Converge boxes built before 2026-08-07.
--  • The three indexes had ZERO lifetime scans — no query path ever used them,
--    while every block INSERT paid their maintenance (the HNSW one alone was
--    61 MB for hash stand-in embeddings).
--  • `embedding` was a 64-dim MD5 signed-hash placeholder — never a model, so
--    never semantically useful, and nothing ever read it. It cost ~24 MB of an
--    84 MB heap, i.e. ~2.4× on every (necessarily sequential) content search.
--    Semantic search is a future feature: it belongs in a chunk-level side
--    table with real embeddings, not as dead weight in the hot heap.
--    Dropping a column only marks it; run VACUUM FULL kb.blocks once to
--    actually reclaim the pages a sequential scan must still read.
DROP INDEX IF EXISTS kb.blocks_assignees_idx;
DROP INDEX IF EXISTS kb.blocks_tags_idx;
DROP INDEX IF EXISTS kb.blocks_emb_idx;
DROP INDEX IF EXISTS kb.blocks_trgm_idx;
ALTER TABLE kb.blocks DROP COLUMN IF EXISTS embedding;

-- Reindexing a file is DELETE+INSERT of all its blocks, so dead tuples pile up
-- fast; at the default scale factor the heap a search must scan stays bloated
-- long after the rows are gone.
ALTER TABLE kb.blocks SET (autovacuum_vacuum_scale_factor = 0.02,
                           autovacuum_analyze_scale_factor = 0.02);

-- Does user u hold permission `nbit` (4=read, 1=exec/traverse) on a file row's mode?
-- The kernel picks exactly ONE class — owner, else group, else other — and a
-- denial by the applicable class is FINAL. ORing the three (the old shape) let
-- `other` rescue a user their own class had denied: mode 0604 root:kb-users is
-- unreadable to a kb-users member on disk, but was readable through the index.
CREATE OR REPLACE FUNCTION kb._has(mode int, owner_name text, group_name text, u text, nbit int)
RETURNS bool LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN owner_name = u THEN (mode & (nbit << 6)) <> 0
        WHEN EXISTS (SELECT 1 FROM kb.user_groups g WHERE g.usr = u AND g.grp = group_name)
            THEN (mode & (nbit << 3)) <> 0
        ELSE (mode & nbit) <> 0
    END;
$$;
REVOKE EXECUTE ON FUNCTION kb._has(int,text,text,text,int) FROM PUBLIC;

-- Full Unix path resolution in SQL: the caller (session_user — NOT current_user,
-- which is the definer inside SECURITY DEFINER) must be able to READ the file AND
-- TRAVERSE every ancestor directory. Takes no user argument, so it can't be used
-- as an oracle to probe another user's access.
--
-- Since the visible_files materialization the POLICIES no longer call this —
-- it stays as the live-computed reference: kb_platform/indexer.py's
-- compute_visibility() must agree with it pair-for-pair, and the parity check
-- (`SELECT count(*) FROM kb.visible_files v WHERE NOT kb.can_read(v.path)`,
-- asserted per user in tests/cli/test_visible_files.py) uses it as the oracle.
-- Change the two together or not at all.
CREATE OR REPLACE FUNCTION kb.can_read(p text) RETURNS bool
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = kb AS $$
DECLARE
    u text := session_user;
    f record;
    parts text[];
    prefix text := '';
    i int;
BEGIN
    SELECT * INTO f FROM kb.files WHERE path = p;
    IF NOT FOUND THEN RETURN false; END IF;
    -- read the file: via mode bits, OR a named-user ACL, OR a named-group ACL
    IF NOT (kb._has(f.mode, f.owner_name, f.group_name, u, 4)
            OR u = ANY(f.acl_users)
            OR EXISTS (SELECT 1 FROM kb.user_groups g WHERE g.usr = u AND g.grp = ANY(f.acl_groups)))
    THEN RETURN false; END IF;
    parts := string_to_array(p, '/');
    FOR i IN 1 .. GREATEST(coalesce(array_length(parts, 1), 0) - 1, 0) LOOP
        prefix := CASE WHEN prefix = '' THEN parts[i] ELSE prefix || '/' || parts[i] END;
        SELECT * INTO f FROM kb.files WHERE path = prefix;
        IF NOT FOUND THEN RETURN false; END IF;
        -- traverse the ancestor dir: via mode x, OR a named-user/group traverse ACL
        IF NOT (kb._has(f.mode, f.owner_name, f.group_name, u, 1)
                OR u = ANY(f.acl_x_users)
                OR EXISTS (SELECT 1 FROM kb.user_groups g WHERE g.usr = u AND g.grp = ANY(f.acl_x_groups)))
        THEN RETURN false; END IF;
    END LOOP;
    RETURN true;
END $$;

-- Removed 2026-08-07 (converge boxes that briefly had it): a SECURITY DEFINER
-- search function scanned kb.blocks with RLS OFF and semijoined visibility
-- afterwards, to reach the GIN index. That is not safe and must not come back:
-- with RLS off, attacker-controlled non-leakproof predicates run against rows
-- the caller cannot see. A LIKE pattern ending in a lone backslash raises only
-- when a row matches up to that point, which is a one-bit-per-call oracle over
-- the WHOLE corpus (and timing leaks even without the error). Postgres's
-- leakproof rule under RLS is exactly the protection being given up; the
-- sequential scan it forces is the price of the security model.
DROP FUNCTION IF EXISTS kb.search_blocks(text,text,text);

ALTER TABLE kb.files  ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.blocks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS files_read  ON kb.files;
DROP POLICY IF EXISTS blocks_read ON kb.blocks;
-- Both policies gate on the materialized visibility set — one cheap hashed
-- subplan per statement. Consequences to keep in mind:
--  • files and blocks visibility are COUPLED to kb.visible_files: any bug in
--    the indexer's compute_visibility() widens (or blanks) BOTH. The parity
--    oracle is kb.can_read() — see its comment.
--  • an empty visible_files means everyone sees NOTHING via the index (fresh
--    install before the indexer's first pass; the filesystem, tree and open
--    documents are unaffected). The indexer populates it within seconds of
--    starting, before its full content walk.
CREATE POLICY files_read ON kb.files FOR SELECT
    USING (path IN (SELECT v.path FROM kb.visible_files v WHERE v.usr = session_user));
CREATE POLICY blocks_read ON kb.blocks FOR SELECT
    USING (file_path IN (SELECT v.path FROM kb.visible_files v WHERE v.usr = session_user));

-- Readers (any logged-in user) get SELECT; RLS still gates rows. Writes are
-- owner-only (kbindexer), preserving the "DB is a disposable index" invariant.
GRANT USAGE ON SCHEMA kb TO PUBLIC;
GRANT SELECT ON kb.files, kb.blocks, kb.user_groups, kb.visible_files TO PUBLIC;
GRANT EXECUTE ON FUNCTION kb.can_read(text) TO PUBLIC;

-- ===========================================================================
-- Semantic search (docs/semantic-search.md)
--
-- Three tables, three owners of the truth:
--   kb.chunks      what the documents SAY, a section at a time — rewritten by
--                  the indexer with a file's blocks, readable under the same
--                  RLS, so a user sees a chunk exactly when they see the file;
--   kb.embeddings  what a chunk MEANS, keyed by the hash of its text and of the
--                  model that read it. No grant to anyone: vectors are reached
--                  only through kb.search_vec, which filters by visibility.
--                  Because the key is content, not a row id, re-indexing a
--                  file whose text did not change costs nothing — the indexer
--                  rewrites every row on each restart, and not one cent;
--   kb.spend       what it COST, per UTC day, in the provider's own reported
--                  units. kb-embedd checks it before every paid call.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS kb.chunks (
    file_path  text NOT NULL REFERENCES kb.files(path) ON DELETE CASCADE,
    seq        int  NOT NULL,              -- order within the file
    start_line int  NOT NULL,              -- 1-based, inclusive
    end_line   int  NOT NULL,
    heading    text,                       -- "Title › Section › Subsection", for display
    text       text NOT NULL,              -- EXACTLY what is sent to the embedding model
    hash       bytea NOT NULL,             -- sha256(model id + text): the key into kb.embeddings
    PRIMARY KEY (file_path, seq)
);
-- lines[i]: the file line of text line i+1 (line 0 is the heading trail), so a
-- search result can point at its best line without reading kb.blocks again.
ALTER TABLE kb.chunks ADD COLUMN IF NOT EXISTS lines int[];
CREATE INDEX IF NOT EXISTS chunks_hash_idx ON kb.chunks (hash);
ALTER TABLE kb.chunks SET (autovacuum_vacuum_scale_factor = 0.02,
                           autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE kb.chunks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS chunks_read ON kb.chunks;
CREATE POLICY chunks_read ON kb.chunks FOR SELECT
    USING (file_path IN (SELECT v.path FROM kb.visible_files v WHERE v.usr = session_user));
GRANT SELECT ON kb.chunks TO PUBLIC;

-- halfvec: half the memory of vector for no measurable loss at this width,
-- and HNSW indexes `vector` only up to 2,000 dimensions anyway. The width is
-- the installation's KB_EMBED_DIMS; kb-embedd refuses to run on a mismatch.
CREATE TABLE IF NOT EXISTS kb.embeddings (
    hash          bytea PRIMARY KEY,
    model         text NOT NULL,           -- provider:model:dims, for audit and GC
    embedding     halfvec(1024) NOT NULL,
    tokens        int NOT NULL,            -- what embedding this chunk was billed
    created_at    timestamptz NOT NULL DEFAULT now(),
    missing_since timestamptz              -- set when no chunk carries this hash; GC after 7 days
);
-- m/ef_construction above pgvector's defaults (16/64): measured on a real
-- corpus, the defaults found 92% of the true top ten at ef_search 200 and
-- NONE for one question whose neighbours sat behind a cluster of near-identical
-- spreadsheet rows; 32/256 finds 99%. scripts/search-recall.py measures it.
CREATE INDEX IF NOT EXISTS embeddings_hnsw ON kb.embeddings
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 32, ef_construction = 256);
-- An index built with the old parameters is rebuilt once (seconds at this size).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = 'kb.embeddings_hnsw'::regclass
             AND NOT coalesce(c.reloptions @> ARRAY['m=32'], false)) THEN
    DROP INDEX kb.embeddings_hnsw;
    CREATE INDEX embeddings_hnsw ON kb.embeddings
        USING hnsw (embedding halfvec_cosine_ops) WITH (m = 32, ef_construction = 256);
  END IF;
END $$;
-- Inline, not TOASTed: a halfvec(1024) is 2,052 bytes, just over the
-- out-of-line threshold, and pgvector's default storage for it is EXTERNAL.
-- Every distance then fetched its vector from the TOAST table (45 MB for 7k
-- rows; ~6 buffer reads each). Existing rows move inline on the next rewrite
-- (VACUUM FULL kb.embeddings — seconds at this size).
ALTER TABLE kb.embeddings ALTER COLUMN embedding SET STORAGE PLAIN;
REVOKE ALL ON kb.embeddings FROM PUBLIC;

-- A chunk the provider refused. Retried on a widening schedule, parked for
-- good after `attempts` reaches the limit — it can never loop.
CREATE TABLE IF NOT EXISTS kb.embed_failures (
    hash       bytea PRIMARY KEY,
    attempts   int NOT NULL DEFAULT 0,
    next_try   timestamptz,
    last_error text,                       -- a short classified reason, never the upstream body
    updated_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON kb.embed_failures FROM PUBLIC;

-- The ledger. `units` is tokens for embeddings and search units for reranks,
-- exactly as the provider's response reports them; `usd` is units × the price
-- in company settings. A call is RESERVED here (estimate) before it is made
-- and reconciled after, so a crash between the two over-counts, never under.
CREATE TABLE IF NOT EXISTS kb.spend (
    day   date NOT NULL,                   -- UTC
    kind  text NOT NULL,                   -- embed_index | embed_query | rerank
    model text NOT NULL,
    calls bigint NOT NULL DEFAULT 0,
    units bigint NOT NULL DEFAULT 0,
    usd   numeric(20,10) NOT NULL DEFAULT 0,   -- one embedding call is ~$0.00001: six
                                             -- decimals would round every charge
    PRIMARY KEY (day, kind, model)
);
GRANT SELECT ON kb.spend TO kb_users;

-- Per-person counters for the query side (reranks and query embeddings), so
-- one person — or their agent in a loop — cannot spend everyone's budget.
CREATE TABLE IF NOT EXISTS kb.spend_user (
    day   date NOT NULL,
    usr   text NOT NULL,
    kind  text NOT NULL,
    calls int  NOT NULL DEFAULT 0,
    PRIMARY KEY (day, usr, kind)
);
REVOKE ALL ON kb.spend_user FROM PUBLIC;

-- Chunks embedded per file per day: a file that keeps changing (an agent
-- stamping a timestamp every minute) is capped, then parked until tomorrow.
CREATE TABLE IF NOT EXISTS kb.embed_file_day (
    day       date NOT NULL,
    file_path text NOT NULL,
    chunks    int  NOT NULL DEFAULT 0,
    PRIMARY KEY (day, file_path)
);
REVOKE ALL ON kb.embed_file_day FROM PUBLIC;

-- One row per alert already raised for a period ("2026-09-22", "2026-09"),
-- so "half the budget is gone" is said once, not every two seconds.
CREATE TABLE IF NOT EXISTS kb.spend_flags (
    period text NOT NULL,
    flag   text NOT NULL,
    at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (period, flag)
);
REVOKE ALL ON kb.spend_flags FROM PUBLIC;

-- The only way to reach a vector. SECURITY DEFINER because kb.embeddings has
-- no grant — and that is exactly why the visibility filter below is load-
-- bearing: inside this function the owner's view of kb.chunks is NOT filtered
-- by RLS, so the explicit `visible_files ... usr = session_user` join is the
-- whole permission check (same shape as the policies; session_user is the
-- caller even inside a definer, as kb.can_read relies on).
--
-- What the 2026-08-07 note above forbids — attacker-controlled predicates over
-- hidden rows — is avoided by construction: the caller supplies a VECTOR and a
-- COUNT, nothing that is evaluated against text; there is no LIKE, no @@, no
-- error path that depends on another row's content. Distance is computed over
-- rows the caller cannot see (that is what an index scan is), and only rows
-- that pass the visibility join are returned — WITHOUT their text, which the
-- caller then reads through kb.chunks under RLS like any other row. The one
-- residual channel is timing, and it carries no content (docs/SECURITY.md).
--
-- iterative_scan: an HNSW scan stops after ef_search candidates, so a person
-- who can see 3% of the corpus would get a handful of hits out of 100. With
-- relaxed_order pgvector keeps walking the graph until the LIMIT is met or
-- max_scan_tuples says enough.
-- Nearest sections the caller may read. Two steps, deliberately:
--   1. the POOL nearest vectors from the HNSW index alone (a MATERIALIZED
--      CTE: one table, ORDER BY distance, LIMIT — the shape the index serves);
--   2. only then join the sections and keep the caller's visible files.
-- Written as one join, the planner judged the small table cheap to scan and
-- computed every distance exactly — 100+ ms. If the caller can see too few of
-- the pool (someone with access to a small corner), the pool widens ×5 up to
-- max_scan_tuples, so a narrow view still gets its k nearest.
CREATE OR REPLACE FUNCTION kb.search_vec(qvec halfvec(1024), k int DEFAULT 40)
RETURNS TABLE (file_path text, seq int, start_line int, distance real)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = kb, public, pg_temp
SET hnsw.iterative_scan = 'relaxed_order'
SET hnsw.ef_search = 400
SET hnsw.max_scan_tuples = 20000
AS $$
DECLARE
    want int := LEAST(GREATEST(k, 1), 100);
    pool int := GREATEST(want * 8, 400);
    paths text[]; seqs int[]; starts int[]; ds real[];
BEGIN
    LOOP
        SELECT array_agg(x.file_path ORDER BY x.d), array_agg(x.seq ORDER BY x.d),
               array_agg(x.start_line ORDER BY x.d), array_agg(x.d ORDER BY x.d)
          INTO paths, seqs, starts, ds
          FROM (
            WITH near AS MATERIALIZED (
                SELECT e.hash, (e.embedding <=> qvec)::real AS d
                FROM kb.embeddings e
                ORDER BY e.embedding <=> qvec
                LIMIT pool)
            SELECT c.file_path, c.seq, c.start_line, n.d
            FROM near n
            JOIN kb.chunks c ON c.hash = n.hash
            -- the owner bypasses RLS in here: this filter is the permission check
            WHERE c.file_path IN (SELECT v.path FROM kb.visible_files v WHERE v.usr = session_user)
            ORDER BY n.d
            LIMIT want) x;
        EXIT WHEN coalesce(cardinality(paths), 0) >= want OR pool >= 20000;
        pool := LEAST(pool * 5, 20000);
    END LOOP;
    RETURN QUERY SELECT * FROM unnest(paths, seqs, starts, ds);
END
$$;
REVOKE EXECUTE ON FUNCTION kb.search_vec(halfvec, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.search_vec(halfvec, int) TO kb_users;
