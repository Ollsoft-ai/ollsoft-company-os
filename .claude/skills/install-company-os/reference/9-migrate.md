# Phase 9 — Bring existing knowledge in

**Ask:** "Do you have company knowledge somewhere else that should move in — Notion, Obsidian, Confluence, Google Drive or Docs, SharePoint or OneDrive, Dropbox, a git repo or wiki, Evernote, plain folders, anything else?" Several are fine; one at a time from here on. "Nothing" ends the phase.

## For each source

1. **Research before asking.** A quick web search for the source's current export options and API (`<source> export markdown`, its API docs): menus, scopes and limits change. Pick the path that needs the least from the human, in this order:
   - **An export file on their computer** (Notion zip, Obsidian vault, Confluence space export, Evernote `.enex`). You run on that computer, so you `scp` it yourself — no credentials at all.
   - **A read-only token** with an expiry, scoped to exactly what is moving.
   - **An OAuth sign-in** (`rclone authorize "<backend>"` on their computer; the token goes in by key drop).
2. **Tell them exactly what to give and why** — where to click, which scope, how long it lives — and that you revoke or delete it at the end. Secrets go in with `ssh -t companyos sudo cos-keydrop <name>`; never in the chat unless they insist.
3. **Inventory before copying.** Count pages or files and total size, show the top-level structure, and ask: all of it, or which spaces/folders? Check room with `ssh companyos df -h /srv`. If semantic search is on (phase 5), name the cost: indexing runs about $0.05 per million characters (700 documents cost $0.46), and the daily embedding cap pauses a big import until the next day — nothing breaks.
4. **Fetch into staging, never straight into the knowledgebase:** `~/cos-import/<source>/raw` in the admin's home on the server. Long copies run under `sudo cos-run import-<source> …`.
5. **Convert and clean** into `~/cos-import/<source>/out`:
   - Pages people will keep editing → Markdown. `/opt/kb-convert-venv/bin/markitdown <file>` handles HTML, DOCX, PPTX, XLSX and PDF; install `pandoc` if a source needs it.
   - Office files and PDFs that are documents in their own right stay as they are: Company OS makes them searchable through their sidecars.
   - Attachments and images go into a `_files/` folder next to the page that uses them; rewrite the references.
   - Internal links become relative Markdown links. **`[[wikilinks]]` must be rewritten** — Company OS does not render them. Notion's `Page Title 1a2b…(32 hex).md` names lose their hash, and their links are fixed to match.
   - Checklists become `- [ ] … @user` per `/srv/kb/.claude/skills/kb-todos/SKILL.md`, with people mapped to Company OS usernames; ask about names you cannot map.
   - Databases and tables: a small one (up to ~50 rows) becomes a Markdown table; a bigger one stays CSV, and a status column can become a Kanban like phase 7's.
   - Keep where it came from in a short line at the top: source, original URL, last edited.
6. **Propose the structure and wait for an OK.** Show the tree two levels deep with counts, and who will see what:
   - visible to everyone at the source → `company/…`
   - a team or client space → `projects/<name>/` with its group (phase 7's *Restricted projects* procedure; offer `--watch` if it is sensitive)
   - personal or private pages → `users/<person>/`, owned by that person
   - **Never widen access silently.** When unsure, it goes to the admin's private folder and you ask.
7. **Place it**, as the admin so history shows them as author: `rsync` from `out/` into `/srv/kb/...`; for `users/<person>/` chown to the person. Company OS commits, converts and indexes as files land; big moves take minutes to show in search.
8. **Verify, then show them:** counts match the inventory; no links point at missing files; `kb-search` finds a phrase you know is in the source; imported to-dos appear in the To-dos view; they open three pages in the app and confirm they look right.
9. **Clean up:** `rm -rf ~/cos-import/<source>`; `sudo sh -c "shred -u /root/.cos-*.key"`; `rclone config delete <remote>`. Tell them to revoke the token or integration at the source. Add a line to the server record (phase 10): what came from where, when, and what was left out.

## Starting points — confirm with your research

| Source | Usual path | What they give |
|---|---|---|
| Obsidian | the vault is already Markdown: copy it, minus `.obsidian/` and `.trash/` | the vault folder on their computer |
| Notion | Settings → Export → Markdown & CSV, with subpages and files | the zip; or an internal integration token (read content) with the pages shared to it |
| Confluence | space export (HTML), or the REST API | the export, or site URL + email + read-only API token |
| Google Drive / Docs | rclone `drive` remote, read-only scope; Docs export as DOCX or Markdown | `rclone authorize "drive"` token |
| SharePoint / OneDrive (work) | rclone `onedrive` remote on the site's library | `rclone authorize "onedrive"` token, the site and library name |
| Dropbox, Box | rclone `dropbox` / `box` remote | `rclone authorize` token |
| Git repo or GitHub wiki | `git clone` (wiki: `<repo>.wiki.git`) | the URL; private: a fine-grained read-only token (contents: read) |
| Evernote | `.enex` export per notebook, converted to Markdown | the `.enex` files |
| Folders on a PC or file server | `scp`/`rsync`, or rclone `sftp`/`smb` | the path, or read access |
| Anything else | research its export first, its read-only API second | whatever that research says — explain why |

**Not a migration:** live sync with the old tool. Say so if they ask — after the move Company OS is the source of truth, and the old tool can be archived.
