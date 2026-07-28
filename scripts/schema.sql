-- KB index schema. Owned by role "kbindexer" (the only writer).
-- Every human logs in via peer auth as their own PG role (== OS username),
-- gets SELECT only, and RLS re-implements the Unix read check in SQL so a raw
-- query can never return a row for a file the caller couldn't read on disk.

-- NOTE: `CREATE EXTENSION vector` is run separately as a superuser (see install.sh).
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
    tsv        tsvector,
    embedding  vector(64)
);

CREATE INDEX IF NOT EXISTS blocks_file_idx      ON kb.blocks (file_path);
CREATE INDEX IF NOT EXISTS blocks_tsv_idx       ON kb.blocks USING gin (tsv);
CREATE INDEX IF NOT EXISTS blocks_task_idx      ON kb.blocks (kind) WHERE kind = 'task';
CREATE INDEX IF NOT EXISTS blocks_assignees_idx ON kb.blocks USING gin (assignees);
CREATE INDEX IF NOT EXISTS blocks_tags_idx      ON kb.blocks USING gin (tags);
-- HNSW for vector search; iterative scan keeps recall sane under RLS filtering.
CREATE INDEX IF NOT EXISTS blocks_emb_idx   ON kb.blocks USING hnsw (embedding vector_cosine_ops);

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

ALTER TABLE kb.files  ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.blocks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS files_read  ON kb.files;
DROP POLICY IF EXISTS blocks_read ON kb.blocks;
CREATE POLICY files_read  ON kb.files  FOR SELECT USING (kb.can_read(path));
CREATE POLICY blocks_read ON kb.blocks FOR SELECT USING (kb.can_read(file_path));

-- Readers (any logged-in user) get SELECT; RLS still gates rows. Writes are
-- owner-only (kbindexer), preserving the "DB is a disposable index" invariant.
GRANT USAGE ON SCHEMA kb TO PUBLIC;
GRANT SELECT ON kb.files, kb.blocks, kb.user_groups TO PUBLIC;
GRANT EXECUTE ON FUNCTION kb.can_read(text) TO PUBLIC;
