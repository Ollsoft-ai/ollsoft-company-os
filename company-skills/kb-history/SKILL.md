---
name: kb-history
description: Use whenever a question is about WHO changed something, WHAT someone worked on, or what a document used to say — "co dělal X za poslední týden", "kdo tohle psal", "co se změnilo", "vrať to zpátky". The knowledgebase IS versioned; answer from `kb-history`, never from file owners or mtimes, and never by running git yourself.
---

# Version history

**The KB is a git repo, but `/srv/kb/.git` is root-only (0700).** Running `git -C /srv/kb log`
fails with *"not a git repository"* — that is by design, not evidence that history is missing.
Git objects hold every version of every file including private ones, so group access to `.git`
would leak past everything. **Never** try to read it, and never reach for `sudo`.

**The only door is `/usr/local/bin/kb-history`** (and `/api/vc/*` in the web app). It runs
permission-gated over the root-only repo and shows you a file's history **exactly when you can
read the file now** — re-checked by the kernel, as you, per query.

> Use the **absolute path**. `/usr/local/bin` is often not on `PATH` in agent shells, and
> `kb-history` alone gives "command not found".

## Who did what

```bash
/usr/local/bin/kb-history --author tomas_vargosko --since "8 days ago" --limit 1000 --json
```

**Pass `--author` for per-person questions** — it filters server-side, so the window's
budget is spent on that person's commits instead of everyone's.

> **Read `truncated` before you conclude anything.** The feed pages newest-first until
> `--limit` visible commits (default 200), a 20 000-commit scan ceiling, or a 20 s budget
> — whichever comes first. When it stops early it says so: `! showing the newest N changes
> only` in text, `"truncated": true` in `--json`. An unfiltered week-long sweep reaches that
> easily, and a report built on a truncated feed makes someone look inactive. Narrow it
> (`--author`, `--until`) or raise `--limit`.

Other flags: `--until`, `--limit`, `--json`, and per-file `--rev <id>` with `--diff` (default),
`--show` (full content at that revision), `--restore` (write it back, as you).

## Read the output right

- **Commits are autosaves, not units of work.** The editor snapshots every few seconds — 21
  commits can be one 2-minute typing session on one file. **Aggregate by file and by day**;
  never quote a commit count as effort.
- **`--diff` the first and last rev of a session** to see what actually landed. A first-rev diff
  against the empty blob (`index e69de29b..`) means the file was written from scratch.
- **`kb-syncd` as author** = an unattributed snapshot (a change arriving outside the editor, e.g.
  a script or an rsync), not a person.
- **Scope**: only `.md` and `.html` are versioned. Secrets under `_secrets/` are excluded on
  purpose — history outlives deletions.

## Never answer "who did what" from the filesystem

| Signal | Why it lies |
|---|---|
| `find -user <name>` | Owner = whoever **created** the file. Edits to someone else's file keep the original owner — the editor's work is invisible. |
| `mtime` | Shows the last write, attributed to nobody. |
| `kb.files` / `kb.blocks` | Current state only. **No history at all.** |

Real example: `company/T-Systems compliance/ci-cd.md` is owned by `krystof` and every
filesystem probe said so — `kb-history` shows `tomas_vargosko` typed the entire file.

## A moved file loses its history (open defect)

**When a file is renamed or moved, every past commit becomes invisible — to everyone.**
The permission filter runs `test -r` on the path *as recorded in the commit*; once that path is
gone, the check fails and the row is dropped. The commits still exist in git; nothing reaches them.

`kb-syncd` commits **one path per commit**, so a move lands as a delete-commit plus an
add-commit. Git only detects a rename when both sides are in the same commit — so it never does
here, and `--follow` cannot rescue it either. The new path starts life with a single
`sync: auto-snapshot` commit and no past.

Real case: on 2026-08-03 `company/T-Systems compliance/` moved to
`projects/🔝 T-Systems Code Compliance Copilot/`. Before the move,
`kb-history --author tomas_vargosko --since "8 days ago"` returned 21 commits. After it, the same
query returns **"no visible changes"**, and the old path answers `forbidden`.

**So: before reporting that someone did nothing, check whether the folder moved.** `git log`-level
truth still exists; only the path-keyed door closed. Say "their history was orphaned by a move",
never "they did nothing".

## State the blind spot

`kb-history` shows only what **you** can read. Another user's `users/<them>/` and projects whose
group you are not in stay invisible, and work done outside the KB (GitLab, tickets) never appears
at all. When you report someone's activity, **say what the answer does not cover** — otherwise
"almost nothing" reads as "they did nothing".
