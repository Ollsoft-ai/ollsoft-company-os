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
│   └── .os/settings.json   your settings (see the kb-settings skill)
├── .os/              platform config: pinned items (launchers.json), egress allow-list, company settings.json
│                     …and per person: settings.json, chats.json, inbox.jsonl (their notifications)
├── .trash/           a deleted file waits in a .trash/ in ITS OWN folder — put it
│                     back by moving it up one level. Kept until someone empties it
└── .git/             version history — 0700 root-only, you cannot read it
```

Don't run `git` here; it will just fail. Ask history questions with the
`kb-history` CLI (or the **kb-history** skill), which is the supported way in.

Use ordinary tools — `ls`, `cat`, `grep`, `find`. **You will only ever see what you are allowed to see**; restricted folders simply won't list for you. Don't interpret a "permission denied" as something to bypass.

# Editing documents

Just edit the `.md` files with your normal file tools. You don't need any special API. When you save:
- the change **syncs live** to anyone viewing that doc in the web app (it's a shared CRDT, so your edit merges with theirs — no clobbering),
- and it is **auto-committed to git**.

Write markdown normally. Tasks are `- [ ] todo` / `- [x] done`. Headings with `#`. Keep files human-readable.

## House style: short, dense, scannable

**We write docs to organize ourselves, not to drown in text.** Length is a cost,
not proof of effort. A wall of prose is a bug — nobody reads it, so the
information in it may as well not exist. Every `.md` you write or edit here:

- **Bullets and tables over paragraphs.** Prose only when the logic genuinely
  needs connecting words.
- **One fact per line.** Front-load the fact; skip the wind-up.
- **Bold the load-bearing words** so a line survives being skimmed.
- **Conclusion first**, background below it and only if someone would ask.
- **No filler.** No restating the heading, no "as mentioned above", no summary
  of what the reader just read, no closing paragraph that adds nothing.
- **Short headings, short sections.** If a section outgrows ~10 lines, split it
  or cut it.

Delete every sentence that carries no new information. When you finish, reread
and cut again — the shorter version is almost always the better doc.

This applies to your own output too: don't hand back a long summary of a short
change.

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

# Sharing a folder with a team — give it an owning GROUP

**A folder's audience is its owning group.** That is the mechanism every layer
agrees on — the kernel, the live editor, the search index and the file tree —
so it is what you should reach for every time:

1. A dedicated OS group per audience, e.g. `proj-<name>` / `team-<name>`,
   with **`kbindexer` as a member** (otherwise the folder is invisible to
   search). Creating a group needs an admin — ask krystof.
2. `chgrp -R <group> <folder>`; `chmod -R g+rwX,o-rwx <folder>`; and `chmod
   g+s` on every directory, so new files inherit the group instead of the
   creator's private one.
3. Whole-company audience = the existing `kb-users` group. No new group, and
   new hires inherit access automatically — same reasoning as `kb_users` in
   **kb-database**.
4. Changing who is *in* a group takes effect for new logins; already-running
   processes keep their old group list. **Search needs no restart** — kb-indexer
   re-reads `getent` every 5 s, so RLS sees the new membership within seconds.
   The one case that DOES need `systemctl restart kb-indexer` is adding
   **`kbindexer` itself** to a brand-new group: its own supplementary groups are
   fixed when systemd execs it, so until it restarts it cannot read the folder's
   files at all and they never enter the index. Ask krystof, and expect ~10 min
   of stale search while it resweeps.

Per-user ACLs (`setfacl -m u:<name>:r`) still work and are the right tool for
sharing ONE file with ONE person — the "share" button uses them, and the
platform now evaluates them exactly as the kernel does. But do not build a
team folder out of them: you would be hand-maintaining a list that no new hire
ever joins, and the next person to look at `ls -l` cannot see who has access.

## Two traps that make a share look broken when the permissions are fine

**The `ls` group column can lie.** On a file with an extended ACL, the group
bits shown by `ls -l` are the ACL *mask*, not the real `group::` entry — so a
folder can read as group-accessible while the group is actually denied, and
vice versa. Never diagnose from `ls` alone; run `getfacl -cpE <path>`, which
prints `group::` and `mask::` separately and marks ineffective entries.

**A file born 0600 is invisible to the team AND to search.** Anything that
creates files with a restrictive umask — `tempfile.mkstemp()` in a cron
script, `install -m600`, an editor writing a temp file and renaming it — lands
a file the folder's group cannot read, and the file silently never appears in
search results (the indexer is just another user; if it cannot read the file,
the file does not exist as far as search is concerned). If a document is
missing from search, check `getfacl` on it first. In your own scripts, write
files as `0o660` inside shared folders.

Whenever a share does not behave, verify from the other person's side rather
than guessing — `sudo -u <them> test -r <path> && echo yes` answers it in one
line, and `sudo -u kbindexer test -r <path>` answers the search question.

# What else you can do (see the other skills)

- **`kb-database`** — query the shared index (respects permissions automatically) and create your own private tables.
- **`kb-automation`** — write scripts and schedule them with cron, running as you.
- **`kb-artifacts`** — build live, shareable dashboards that render in the web app.

# Golden rules

1. Work **through files** and your own user — never try to escalate privileges or read another user's private data.
2. If something is denied, respect it. It reflects a real permission boundary.
3. Persist anything important as **markdown in `/srv/kb`** — that's the backed-up source of truth. Databases and scratch files are convenience, not durability.
4. Prefer small, reversible changes. Git has your history if you need to look back.

## The screen: groups, the dock, the agent chat

Everything open is a tab in a group; groups stack in columns; the terminal
panel at the bottom is the *dock*, a group like the others. A person can drag
any tab — a document, a terminal, an agent chat — beside, above or below any
other, split with Alt+\ / Alt+Shift+\, maximize a group with Alt+Z, and put
the dock on the right from the palette. The layout is per browser
(localStorage), never a file in the knowledgebase.

The top-right chat button (Alt+C) opens an **agent chat**: Claude Code,
Codex, Gemini CLI or another ACP agent, running as the person in the
knowledgebase — the same access you have in a terminal, with streamed
answers, tool calls, diffs and permission prompts on screen. Sign-in is per
person, from the chat's picker. `docs/agent-chat.md` has the details; the
default agent for new chats is the `ai.agent` setting (see kb-settings).
