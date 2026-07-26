# Converted documents — office/PDF binaries as searchable, agent-readable text

A knowledgebase fills up with `.docx`, `.pptx`, `.xlsx` and `.pdf` the moment
real people use it — uploads, network-drive saves ([windows-drive.md](windows-drive.md)),
OneDrive syncs. Those files
are opaque to everything the platform is good at: agents' file tools read
text, ripgrep reads text, the indexer parses only markdown. **kb-convert**
closes that gap by shadowing every such binary with a machine-extracted
markdown sidecar:

```
company/reports/_files/Q3_Review.pptx        ← the source, untouched
company/reports/_files/.Q3_Review.pptx.md    ← derived text, hidden, read-only
```

## The design in one paragraph

The sidecar lives in the **same directory** as its source, so the kernel's
answer to "who can read this?" is identical for both — directory traversal is
shared, and the file ACL is cloned from the source, stripped to read-only. The
**dot prefix** hides it everywhere the platform already hides dot-entries (the
file tree, quick-open), while the indexer makes exactly one exception
(`common.is_derived_sidecar`) so the **content** still lands in `kb.blocks` —
full-text search, vectors, even checkboxes. The file is **owned by the service
user** (`kbindexer`) with no group/other write bit, so for every human and
agent it is read-only: the kernel refuses writes, and syncd's write-permission
check serves it as a live read-only document in the editor.

## What converts

| Source | Result |
|---|---|
| `.docx` `.pptx` `.pdf` | full text via markitdown (slides include speaker notes) |
| `.xlsx` | a **sample** of each sheet, read lazily: 500 rows per sheet, 40 columns, 120 000 characters overall — whichever binds first |
| scanned `.pdf` (no text layer) | `status: empty` stub — OCR is not attempted |
| `.doc` `.ppt` `.xls` (legacy) | `status: unsupported` stub suggesting a re-save as the modern format |
| anything over 100 MB | `status: too-large` stub |

Every sidecar carries frontmatter: `derived_from`, `source_sha256` (what makes
re-conversion idempotent), `converted_at`, `converter`, and `status`
(`ok | empty | failed | unsupported | too-large`). A failed conversion is a
visible stub with the error, not a silent gap — and it retries only when the
source actually changes.

## One bad document cannot take the service down

Parsers allocate in proportion to *decompressed* content, so file size is a poor
predictor of memory use. Two independent guardrails follow from that:

**Spreadsheets are read lazily and stopped early.** `.xlsx` bypasses markitdown
entirely for openpyxl's streaming reader, so `iter_rows` pulls one row at a time
and stops at the caps above. Peak memory is a function of the cap, not the file:
a 5 MB sheet and a 500 MB sheet cost the same. This replaced trimming the
markdown *after* the parse, which was always too late — the memory was already
spent. A sidecar exists so search and agents know a file's shape and vocabulary;
nobody ever needed the whole table, and the original is one click away.

**Every parse runs in a child process** (`kb_platform/convert_extract.py`) that
pins its own `oom_score_adj` to the maximum. If a document blows the unit's
`MemoryMax`, the kernel reaps the child and the parent survives to write
`status: failed` explaining why — stamped with the source hash, so the file is
attempted once per version, not once per sweep. There is a 30-minute wall-clock
budget as a runaway guard.

This is why `kb-convert.service` sets **`OOMPolicy=continue`**. systemd's default
(`stop`) treats any process in the cgroup being OOM-killed as the unit failing,
which would defeat the isolation entirely. Before this design, one 54 MB
spreadsheet OOM-killed the service every 15 minutes, and because each restart
re-swept the tree in the same order it died on the same file forever — leaving
**31 documents behind it in walk order with no sidecar at all**, invisible to
search and to every agent, while the phone filled with identical alerts.

## When it runs

`kb-convert.service` watches the repo (watchfiles/inotify) and converts a file
once its size and mtime have been stable for a second — Word saves via a
temp-file dance and network-drive writes land in chunks, so converting on the
first event would parse half a file. A full reconciliation sweep runs at boot
and every 10 minutes: it converts anything missed (dropped inotify events stay
invisible, same reasoning as the indexer's rescan), refreshes anything stale,
regrows deliberately deleted sidecars and removes orphans whose source is
gone. Office lock files (`~$…`), `.tmp`/`.kbtmp` and `_secrets/` never
convert.

Deleting every sidecar and restarting the service reproduces them exactly —
sidecars are derived data, the same contract as the Postgres index.

## What stays out

- **git history**: `.gitignore` ends with `.*.md` — sidecars are regenerable
  machinery, not content people wrote. (Installs older than this feature keep
  their existing `.gitignore`; append that line manually when upgrading.)
- **private user dirs**: `users/<name>/` is 700 — the service user cannot read
  what's there, so nothing is converted. Same boundary as the index.
- **the tree and quick-open**: hidden by the existing dot-entry filtering; the
  sidebar's `.*` toggle reveals them like any other machinery.

## Operations

```bash
journalctl -u kb-convert -f           # one line per conversion, with status
systemctl restart kb-convert          # force a full resweep (safe, self-contained)
systemctl disable --now kb-convert    # opt out entirely; sidecars remain until deleted
```

The service runs as `kbindexer` deliberately: that user is already in the
content groups, so **what can be indexed is exactly what can be converted** —
one service account to remember when granting a new project group. It uses a
separate venv (`/opt/kb-convert-venv`, from `requirements-convert.txt`)
because document parsers are heavy and version-churny; the platform venv
stays lean.
