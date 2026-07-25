---
name: kb-database
description: Use when you need to query across the knowledgebase (tasks, search, aggregation) or store structured data of your own — and ALWAYS before creating a table that anything other than you will read, because you must ask the human who should have access first. Explains the Postgres database, how permissions apply (row-level security on the shared index vs. grant-based isolation for your own tables), how to create tables, and how to share them with the whole company via the kb_users group role rather than a hardcoded list of names that breaks for the next new hire.
---

# The database

There is a Postgres database named `kb`. **Connect as yourself with no password** — it authenticates you by your OS identity (peer auth):

```bash
psql -d kb
```
```python
import psycopg
conn = psycopg.connect("dbname=kb", autocommit=True)
```

Whatever you connect as, `SELECT current_user` is **you**. The database cannot be tricked into giving you someone else's identity.

The database has two parts with two *different* permission mechanisms — this distinction matters.

# Part 1 — the shared index (`kb` schema): read-only, protected by Row-Level Security

The platform continuously parses every markdown file into the `kb` schema so you can query across the knowledgebase:

- `kb.blocks` — every task, heading, and line: `(file_path, line, kind, checked, text, ...)`
- `kb.files` — file metadata `(path, owner, group, mode, ...)`

**Row-Level Security is enabled here.** A query only ever returns rows for files *your user is allowed to read on disk*. You cannot see another team's confidential content even with raw SQL — the policy re-checks the filesystem permission for every row. So you can run arbitrary queries safely:

```sql
-- every open task across everything you can see:
SELECT file_path, line, text FROM kb.blocks WHERE kind='task' AND checked=false ORDER BY file_path;
-- full-text search:
SELECT file_path, text FROM kb.blocks WHERE tsv @@ plainto_tsquery('english', 'onboarding');
```

You have **SELECT only** on this schema — it's a rebuilt-from-markdown index, not a place to write.

# Part 2 — your private schema (`u_<you>`): your sandbox, protected by grants

You own a schema named `u_<yourusername>` (e.g. `u_alice`). It is **default-deny**: nobody else can read it until you explicitly grant them. Your `search_path` already puts it first, so:

```sql
CREATE TABLE u_alice.notes (id bigserial PRIMARY KEY, body text, created_at timestamptz DEFAULT now());
INSERT INTO u_alice.notes(body) VALUES ('anything you want to remember');
SELECT * FROM u_alice.notes;
```

Create any tables you like here. This is where an agent stores structured state — a scraped feed, a computed rollup, whatever the task needs.

# Sharing your data (a deliberate act)

Isolation is the default; sharing is one explicit grant. Only you can grant on your own tables.

## ALWAYS ASK WHO BEFORE YOU CREATE A SHARED TABLE

**If you are about to store data that anyone other than you will read — an app, a game, a dashboard, a tracker, a vote, a leaderboard — stop and ask the human who should have access, and whether they should be able to write as well as read.** Do not guess, and do not silently pick "just me" or "everybody". Ask before creating the table, so the grant lands with it. Something like:

> This will store its data in `u_<you>.<table>`. Who should have access — everyone at the company, or specific people? And should they be able to add/edit rows, or only read?

Then translate the answer:

| They say | You write |
|---|---|
| "everyone", "the whole company", "the team" | `GRANT ... TO kb_users` |
| specific names | `GRANT ... TO bob, carol` |
| "just me" / no answer yet | grant nothing — it's already private |

## Everyone at the company → grant to `kb_users`

`kb_users` is the group role mirroring the `kb-users` OS group. **Every human account is a member, including people hired after you write the grant** — that is the entire point of using it:

```sql
GRANT USAGE ON SCHEMA u_alice TO kb_users;                  -- reach the schema
GRANT SELECT, INSERT ON u_alice.scores TO kb_users;         -- read + append
GRANT USAGE, SELECT ON u_alice.scores_id_seq TO kb_users;   -- needed for INSERT on bigserial
```

**Never write `GRANT ... TO bob, carol, carol, test` as a way of saying "everyone".** That list is frozen at the moment you type it. The next person hired gets the file (OS groups inherit) and gets the page — and then every query behind it fails with `permission denied for schema u_alice`, with nothing in the UI explaining why. This has already happened once on this platform; `kb_users` exists so it doesn't happen again.

## Specific people → grant by name

Only when access is genuinely meant to be a subset — and say so in a comment so the next agent doesn't "fix" it into `kb_users`:

```sql
GRANT USAGE ON SCHEMA u_alice TO bob;                  -- bob reviews these
GRANT SELECT ON u_alice.notes TO bob;                  -- read-only
GRANT SELECT, UPDATE, DELETE ON u_alice.notes TO bob;  -- also let them edit
```

## Don't forget the sequence

A `bigserial` / `GENERATED ... AS IDENTITY` primary key needs its sequence granted too, or INSERT fails even with INSERT on the table:

```sql
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA u_alice TO kb_users;
```

## Checking what you actually granted

```sql
-- can this person really use it?
SELECT has_table_privilege('oliver_hague', 'u_alice.scores', 'INSERT');
-- who is on the table right now?
SELECT relname, relacl FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'u_alice' AND relkind = 'r';
```

New tables are **not** auto-shared — there are deliberately no default privileges on personal schemas, so default-deny stays true. Every shared table needs its own grant.

# RLS vs. grants — the one-line summary

- **Shared index (`kb.*`)**: Row-Level Security → you can SELECT, but only see rows for files you're allowed to read. Access **inherits** — it follows OS group membership, so new hires are covered automatically.
- **Your tables (`u_you.*`)**: grant-based → private until you `GRANT`; then all-or-nothing per table. Access does **not** inherit from anything, *unless* you grant to `kb_users` — which is why you should, whenever the answer is "everyone".

# Durability warning

The `kb` index is disposable (rebuilt from markdown anytime). Your `u_<you>` schema holds **real data that exists nowhere else** — if it matters long-term, also write it back to a markdown file in `/srv/kb`, which is the git-backed source of truth. Treat your schema as fast, convenient scratch space.
