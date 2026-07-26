# Company knowledgebase — agent context

This repo is the company brain. Markdown files are the source of truth.

- Shared docs live in `company/`. Project docs under `projects/<name>/`
  (group-restricted).
- Your private scratch is `users/<you>/`.
- Never write outside paths your OS user can access — the kernel enforces this
  anyway, but failing cleanly beats failing confusingly.
- To aggregate tasks, read checkboxes (`- [ ]` / `- [x]`) across the files you
  can see, or query `kb.blocks` in Postgres.
- Office files and PDFs (docx/pptx/xlsx/pdf) each have a hidden, read-only
  markdown sidecar next to them with the extracted text: `report.docx` →
  `.report.docx.md`. Read the sidecar, not the binary. Note that `rg`/Grep
  skip dotfiles unless you pass `--hidden`; the sidecars ARE indexed in
  `kb.blocks`. Never edit a sidecar — it is regenerated from its source.
- Read the skills in `.claude/skills/` before using the database, writing an
  artifact, or scheduling automation. They describe how this platform works and
  what the conventions are.
