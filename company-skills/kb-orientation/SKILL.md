---
name: kb-orientation
description: Read this FIRST whenever you (an AI agent) are working inside this company knowledgebase. Explains where you are, who you are, how the filesystem and permissions work, and the rules for editing docs. Load it at the start of any task in /srv/kb.
---

# Where you are

You are an AI agent working inside a company knowledgebase platform. The knowledgebase is a **git repository at `/srv/kb`**, and **markdown files are the source of truth**. There is a web app, a live multiplayer editor, a Postgres database, a terminal, and an artifact system on top — but underneath, everything is just files you can read and write normally.

# Who you are (this is the whole security model)

**Your identity is your Linux user.** Run `whoami` to see it. Everything you do — every file you open, every database query, every script you schedule — happens as that user, and the **kernel enforces** what that user may touch. You cannot escape it, and you should not try. If an action is denied, it is denied because your user lacks access — that is correct behavior, not an obstacle to work around.

This is good news: you can act freely and safely. The worst you can do is limited to what your human user could do themselves.

# The layout

```
/srv/kb/
├── company/          shared with everyone — the common knowledgebase
├── projects/<name>/  restricted to that project's team (you may not see all of these)
├── users/<you>/      your own private space (only you can read it)
└── .git/             full version history (auto-committed for you)
```

Use ordinary tools — `ls`, `cat`, `grep`, `find`. **You will only ever see what you are allowed to see**; restricted folders simply won't list for you. Don't interpret a "permission denied" as something to bypass.

# Editing documents

Just edit the `.md` files with your normal file tools. You don't need any special API. When you save:
- the change **syncs live** to anyone viewing that doc in the web app (it's a shared CRDT, so your edit merges with theirs — no clobbering),
- and it is **auto-committed to git**.

Write markdown normally. Tasks are `- [ ] todo` / `- [x] done`. Headings with `#`. Keep files human-readable.

# Reading office files and PDFs

You cannot read a `.docx`/`.pptx`/`.xlsx`/`.pdf` directly — but you don't have
to. The platform keeps a **hidden, read-only markdown sidecar** next to every
such binary with its extracted text: `report.docx` → `.report.docx.md` (same
folder, dot-prefixed). Two things to know:

- `rg` and most search tools **skip dotfiles by default** — pass `--hidden`,
  or query `kb.blocks` in Postgres, where sidecar content IS indexed.
- Sidecars are regenerated whenever the source changes. Never edit one (the
  kernel will refuse anyway); if the text looks stale or wrong, check the
  `status:` line in its frontmatter — `failed`/`empty`/`unsupported` explain
  themselves.

# Sharing a folder with teammates — use a group, NEVER per-user ACLs

Learned the hard way (July 2026, the Nextcloud-collectives migration): folders
were shared with `setfacl -m u:<name>:rwx` and it *looked* right — `ls`, the
file tree, search and even a kernel-level read test all worked — but the
granted colleagues still could not open a single document. The live-editor
daemon (syncd) and the hub's create/upload checks evaluate **only the classic
owner/group/other mode bits**; they never read ACLs. And because an extended
ACL makes `ls`'s group bits show the ACL *mask*, a folder can even look
group-accessible when the real group entry is `---` — so the editor can
falsely admit people a restricted folder was never shared with.

The only sharing mechanism that works end-to-end is the folder's **owning
group** (the pattern of `proj-olingo`):

1. A dedicated OS group per audience, e.g. `proj-<name>` / `team-<name>`,
   with **`kbindexer` as a member** (so the search index can read it) —
   creating groups needs an admin, so ask krystof.
2. `chgrp -R <group> <folder>`; `chmod -R g+rwX,o-rwx <folder>`; `chmod g+s`
   on every directory (so new files inherit the group).
3. Strip any leftover extended ACLs: `setfacl -R -b <folder>` (skip the
   read-only `.*.md` sidecars — kb-convert manages those itself).
4. Whole-company audience = the existing `kb-users` group; no new group, and
   new hires inherit access automatically — same reasoning as `kb_users` in
   **kb-database**.

Per-user ACLs remain fine for exactly one case: read-sharing a single
non-editor file, e.g. an artifact `.html` (see **kb-artifacts**). Never use
them on folders or `.md` documents.

# What else you can do (see the other skills)

- **`kb-database`** — query the shared index (respects permissions automatically) and create your own private tables.
- **`kb-automation`** — write scripts and schedule them with cron, running as you.
- **`kb-artifacts`** — build live, shareable dashboards that render in the web app.

# Golden rules

1. Work **through files** and your own user — never try to escalate privileges or read another user's private data.
2. If something is denied, respect it. It reflects a real permission boundary.
3. Persist anything important as **markdown in `/srv/kb`** — that's the backed-up source of truth. Databases and scratch files are convenience, not durability.
4. Prefer small, reversible changes. Git has your history if you need to look back.
