# Changelog

What changed in each release of Ollsoft Company OS, newest first. `scripts/release.sh` writes each section from the commit titles since the previous release (`scripts/changelog.sh`); Settings → About shows this file. Fix a line that reads badly right here — a section is written once and never regenerated.

## 1.5.0 — 2026-11-15

- Agent chat no longer piles up unused sessions until it stops answering
- Semantic search keeps up to date when one folder cannot be read
- Update DOMPurify to 3.4.17, which fixes two security advisories
- Agent questions in chat show the question and its options to pick from
- A folder named _private is born owner-only, whatever the folder around it shares
- Links in artifacts open the linked file in the app, and the To-dos page links each to-do to its line
- JSON, YAML, CSV and other text files open in an app tab, highlighted and read-only
- Undo in a document steps back only your own edits, never the note loading or someone else's change
- A setting or pin changed while a tab was loading or reconnecting now reaches that tab

## 1.4.0 — 2026-11-11

- Switch accounts in one click, stay signed in longer, and sign in with a password manager
- Change any keyboard shortcut from the shortcut sheet, saved to your own settings
- Let the daily health check take instructions for your own box and read the audit digest

## 1.3.0 — 2026-10-18

- Show the running version and every release's changes in Settings → About
- Name a build from a clone without tags by its version and commit
- Fix tab drag in real Chrome, moves/deletes with a terminal or chat open, and the shortcuts panel (#5)
- Open the terminal on the right by default, and let the company or each person choose its side

## 1.2.0 — 2026-10-09

- Give every agent one skills folder: .agents/skills
- Fold all keeps the top level open and leaves leaf folders shut
- Show the default a setting falls back to instead of "not set"
- Tell agents what kb-search covers
- Make text written from outside the editor glow until you look
- Glow outside edits in the blame stripe's machine colour

## 1.1.0 — 2026-10-04

- A CI run is not an installation
- Show who wrote each line, and let history follow a moved document

## 1.0.0 — 2026-10-03

- First release.
