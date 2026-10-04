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

## Who wrote this line

```bash
/usr/local/bin/kb-history company/notes.md --blame      # rev, time, author per line
```

- **Last touch, not first write**: one edited word re-credits the whole line — and here a line is a whole paragraph.
- **`(machine)`** = an author that is not a login account, i.e. `kb-syncd`: the change reached the file outside the editor — an agent, a script, a sync. Not proof of AI; proof of "not typed in the editor".
- **Moves are followed** (below): a line keeps its real author, not whoever moved the file.
- The editor shows the same thing as a stripe per line (setting `editor.blame`).

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

## History follows a moved file

**A renamed or moved document keeps its past**: its version list, diffs, restore, `--blame` and the `--author` feed all reach back to before the move. `kb-syncd` records each move in a ledger (`.git/kb-moves.jsonl`) and every history read follows it.

- **Who sees the old versions**: anyone who can read the document **now** — the same rule as a file shared in place.
- **Who sees the old name**: only someone who can list the folder it was in. Everyone else gets `(before a move)` / `an earlier location`. A folder that no longer exists cannot be checked, so its names are hidden from everybody.
- **The feed lists pre-move work under the document's current path**; the move itself shows as `renamed`.
- **What counts as a move**: a rename git sees inside one commit (≥ 90% similar), or a delete and an add of **identical** content within 10 s. Copy, then delete the original later = a fresh start without the past.
- **Moves made before the ledger existed were backfilled** from history: a folder moved months ago gets its authors' earlier work back.

## State the blind spot

`kb-history` shows only what **you** can read. Another user's `users/<them>/` and projects whose
group you are not in stay invisible, and work done outside the KB (GitLab, tickets) never appears
at all. When you report someone's activity, **say what the answer does not cover** — otherwise
"almost nothing" reads as "they did nothing".
