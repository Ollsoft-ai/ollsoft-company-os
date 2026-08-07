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
