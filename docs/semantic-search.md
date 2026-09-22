# Semantic search

Search finds documents by **meaning**, not only by the words in them — a
Czech question finds an English page, "why can't the local stack reach its
database" finds the note about a stale port — while still finding exact
strings (an invoice number, a class name) the way full-text always has. The
same search serves people (Ctrl+K) and agents (`kb-search`), and every result
is something the asker could `cat`.

It is optional. Without provider keys the platform runs exactly as before:
full-text search, nothing sent anywhere. It needs pgvector ≥ 0.7 (`halfvec`),
which `install.sh` fetches when the distribution's is older; without it the
vector table is skipped and `kb-embedd` reports `paused: unsupported`.

## How a search runs

```
 Ctrl+K keystroke ─┐                      ┌─ kb-search "question" (agents, scripts)
                   ▼                      ▼
   per-user backend, AS THE USER (peer auth, row-level security)
     1. full-text over kb.blocks                (as before)
     2. the question's vector  ── socket ──►  kb-embedd  ── Azure / OpenAI
     3. kb.search_vec(): nearest sections the user may read
     4. fuse both lists per SECTION (reciprocal-rank fusion), 3 hits per file
     5. only when typing has paused (or kb-search): rerank the top 40
        ── socket ──► kb-embedd ── Cohere rerank
     6. point each hit at its best-matching line
```

- **Sections, not lines.** The indexer cuts every document into sections (the
  text under one heading, ≤ 1,200 characters, 100 characters of overlap),
  each prefixed with where it lives: `💱 Acme Billing › data › Setup`. That
  prefix is why a section of a file called `data.md` still answers a question
  about Acme's billing.
- **Hybrid.** Vectors find paraphrases and other languages; full-text finds
  identifiers and numbers. Fusion keeps both. A full-text line counts for the
  section it sits in, so a section that matches both ways is one hit.
- **Reranking is never per keystroke.** Typing gets full-text + vectors
  (~0.2 s). When typing pauses for 700 ms the palette asks once more with
  `final=1` and the order updates (the selected row stays selected).
  `kb-search` reranks by default; `--no-rerank` skips it.
- **Permissions.** Full-text and section text are read as the user under RLS.
  `kb.search_vec` is a `SECURITY DEFINER` function (only `kbindexer` may read
  vectors) and filters by `kb.visible_files` for `session_user` explicitly —
  the owner bypasses RLS inside a definer, so the filter is load-bearing. It
  returns paths and distances, never text. Tests: `tests/cli/test_semantic_search.py`.

## What is sent, and what never is

The embedding provider receives section text; the reranker receives the
question and the text of up to 40 candidate sections. Never sent:

- `_secrets/` folders, hidden paths, files the indexer cannot read (0600
  private files) — they are not indexed at all.
- Anything under a folder containing a file named **`.noembed`**.
- `users/` when **Settings → Company → Search & AI → What is embedded** is
  `company+projects` (default `all`).
- **Credentials inside ordinary documents.** Before a section is embedded,
  private-key blocks, `secret=`/`password:`/`api_key=`-style assignments and
  well-known token shapes (`sk-…`, `ghp_…`, `AKIA…`, JWTs) are replaced by
  `[secret]`. Base64 blobs, GUID columns, table padding and long URLs are
  squeezed out too — they cost tokens and mean nothing. Only the embedded
  copy is cleaned; the file and full-text search are untouched.
- Spreadsheets (`.xlsx.md` sidecars and friends) contribute each sheet's
  header and first rows, not every row; full-text still finds any cell.

Narrowing the scope or adding `.noembed` removes vectors already made for
what falls outside (on the worker's next hourly sweep).

## Spend: what it costs and why it cannot run away

`kb-embedd` is the only process holding the provider keys and the only writer
of the spend ledger (`kb.spend`, readable by every member). Each guarantee
below is a test in `tests/cli/test_embedd_invariants.py`.

1. **Unchanged text is never re-billed.** A vector is stored under
   `sha256(model ‖ section text)`. Restarting the indexer rewrites every
   section row with the same hashes: zero calls (verified in production).
2. **Reserve, then call, then reconcile.** An estimate is committed to the
   ledger before each call and corrected to the provider's reported usage
   after. A crash in between over-counts; nothing under-counts.
3. **An in-process brake** that does not trust the database: ≤ 20 index calls
   a minute, ≤ 3,000 a day per process; query side ≤ 300 embeddings and ≤ 60
   reranks a minute, ≤ 1,200 embeddings per person per hour.
4. **Nothing loops.** A rejected batch is bisected to the one bad section,
   which is retried after 1 min, 10 min, 1 h, 6 h, 24 h and then parked. A
   provider outage or a wrong key opens a breaker for the whole worker
   (15 min, one probe to close; `Retry-After` honoured; a rotated key file is
   re-read once).
5. **Budgets** from Settings, checked before every call: embedding per UTC
   day ($10) and month ($100), reranking per day ($20), reranks per person per
   day (200). At half: one alert. At the cap: `paused: budget` until the
   period rolls; search keeps working without the paid step.
6. **Quiet before embedding.** A document is only sent once nobody has
   touched it for `search.embed.quiet_seconds` (120 s) — syncd saves four
   times a second while someone types — and only its changed sections go.
   One file may cost at most 3× its section count a day.

Measured on this box's corpus (727 documents, 9 M characters → 7,100
sections): the first backfill cost **$0.46**, the re-embed after the section
format changed **$0.30**; a worker or indexer restart costs nothing (verified
in production). Day to day it is cents: edits re-embed a section each, a
question costs ~$0.000002 to embed, a reranked search $0.00275.

## Setting it up (any installation, any provider)

Keys live in files beside the dictation key, readable only by the worker:

```bash
# Azure AI Foundry / Azure OpenAI — reads the key with az (never printed):
sudo bash scripts/install-search-keys.sh --from-azure <account> <resource-group> \
     [--embed-deployment text-embedding-3-large] [--rerank-model Cohere-rerank-v4.0-pro]

# OpenAI + Cohere directly, keys from files:
sudo bash scripts/install-search-keys.sh --embed-provider openai \
     --embed-url https://api.openai.com/v1 --embed-model text-embedding-3-large \
     --embed-key-file ~/embed.key \
     --rerank-url https://api.cohere.com/v2/rerank --rerank-model rerank-v3.5 \
     --rerank-key-file ~/rerank.key

sudo systemctl restart kb-embedd kb-indexer     # indexer: the model is part of every hash
kb-search --status
```

The script checks each key with one tiny request, writes `/etc/kb/embed.key`
and `/etc/kb/rerank.key` (0640 root:kbindexer) and these lines in
`/etc/kb/kb.env`:

| Variable | Meaning |
|---|---|
| `KB_EMBED_PROVIDER` | `azure-openai`, `openai`, `fake` (tests; free, letters not meaning) or `none` |
| `KB_EMBED_URL` | Azure: `https://<account>.openai.azure.com`; OpenAI: `https://api.openai.com/v1` |
| `KB_EMBED_MODEL` | Azure deployment name, or OpenAI model name |
| `KB_EMBED_DIMS` | `1024` — the width of `kb.embeddings.embedding`; the model is asked for exactly this |
| `KB_RERANK_PROVIDER` | `cohere` or `none` |
| `KB_RERANK_URL` | the full rerank URL (Azure AI Foundry: `https://<account>.services.ai.azure.com/providers/cohere/v2/rerank`) |
| `KB_RERANK_MODEL` | e.g. `Cohere-rerank-v4.0-pro` |
| `KB_EMBED_MAX_DISTANCE` | optional; cosine distance past which a nearest section is noise (default 0.62, measured for text-embedding-3 — see "Measuring quality") |

`sudo bash scripts/install-search-keys.sh --off` goes back to full-text only.
Prices are settings (`search.price.*`), so the ledger's dollars match your
contract; tokens and search units always come from the provider's response.

**Changing the model** changes every hash: the next pass re-embeds
everything once (priced against the budgets like any other spend). Old vectors
are collected 7 days after nothing refers to them. A different vector width
needs a schema change and is refused (`paused: dims mismatch`) until then.

## Seeing it

- **Settings → Company → Search & AI** (admins): state, coverage, models,
  today's and this month's spend by kind, the caps, warnings, the last error,
  and a button to retry sections the provider refused.
- `kb-search --status` — the same, in a terminal, for anyone.
- `/run/kb/search/status.json` — the raw state, refreshed every 10 s.
- `SELECT day, kind, calls, units, usd FROM kb.spend ORDER BY day DESC;`

## Monitoring

`kb-heartbeat` check 8 (only where it is set up) reports: `kb-embedd` not
active, a status older than 3 minutes, `paused` for `breaker`, `budget`,
`dims mismatch`, `error` or `database`, a budget half used, a failing
reranker, and coverage under 95% for 6 hours. A backfill in progress is not a
finding. `scripts/kb-maintenance-policy.md` has the triage rules.

## Measuring quality

Two scripts, both run as a person against the live index:

```bash
# Does search find the right document? A JSONL file of questions and the
# files that answer them — keep a company's set OUT of the repo.
.venv/bin/python scripts/search-eval.py questions.jsonl --show-misses
# Where do right answers sit vs nonsense? (sets KB_EMBED_MAX_DISTANCE)
.venv/bin/python scripts/search-eval.py questions.jsonl --calibrate

# Does the vector index return the true nearest neighbours? (as kbindexer)
sudo -u kbindexer /opt/kb-venv/bin/python scripts/search-recall.py questions.jsonl
```

On this box, 41 questions written from real documents (Czech, Slovak, German,
English; paraphrases, cross-language and exact-term lookups):

| Variant | hit@1 | hit@5 | MRR@10 | median latency |
|---|---|---|---|---|
| words only (the search before this) | 17% | 20% | 0.18 | 156 ms |
| meaning only | 56% | 93% | 0.71 | 54 ms |
| hybrid — every keystroke | 68% | **100%** | 0.81 | 183 ms |
| hybrid + rerank — on a pause, `kb-search` | **88%** | **100%** | **0.93** | 406 ms |

Full-text alone finds almost nothing for a question in natural language (it
needs every word to appear on one line); it stays in the mix because it is
what finds an order number or a class name like `InvoiceMatcherTests`, where meaning guesses.
Latencies are server-side with a warm cache; a keystroke that is a new
question adds the embedding call (~90–250 ms to Azure EU), overlapped with
the full-text scan.

What moved the numbers, in order (each measured with this script):

1. **Section context and clean text** — folder prefix, redaction, blob and
   spreadsheet squeeze: hybrid hit@5 90% → 98%, 3,113 spreadsheet-row
   sections → 334, a third fewer tokens.
2. **Index-first vector search, inline vector storage** — 130 ms → 30 ms.
3. **Section-level fusion** (a full-text line counts for its section; the
   reranker reads the whole section) and **one word counts as words** (its
   nearest meanings weigh half) → hit@5 100%.
4. **A distance cutoff** (`KB_EMBED_MAX_DISTANCE`, 0.62 for
   text-embedding-3): the farthest right answer sat at 0.61, nonsense and
   half-typed words at 0.62–0.76. `search-eval.py --calibrate` measures it
   for another model.

The vector index is HNSW with `m = 32, ef_construction = 256`, searched with
`ef_search = 400` and vectors stored inline (`STORAGE PLAIN`). With pgvector's
defaults it found 92% of the true top ten and, for one question, none — its
neighbours sat behind thousands of near-identical spreadsheet rows. Now:
100% at `ef_search` 200, 9 ms. `kb.search_vec` asks the index for the nearest
candidates first and filters by permission after, widening the pool when the
caller can see too few; written as a single join the planner scanned every
vector exactly instead (130 ms). Re-run `search-recall.py` as the corpus grows.

## Code

| | |
|---|---|
| `kb_platform/indexer.py` | `chunk_sections()`, `clean_lines()` — sections, context prefix, redaction |
| `kb_platform/embedding.py` | providers: Azure OpenAI, OpenAI, Cohere rerank, fake; config from `kb.env` |
| `kb_platform/embedd.py` | the `kb-embedd` worker, ledger, budgets, breaker, socket `/run/kb/search/api.sock` |
| `kb_platform/hybrid.py` | the search: full-text, vectors, fusion, rerank, landing line |
| `kb_platform/search_cli.py` | `kb-search` |
| `scripts/schema.sql` | `kb.chunks`, `kb.embeddings`, `kb.spend*`, `kb.search_vec` |
| `systemd/kb-embedd.service` | the unit (`User=kbindexer`) |
