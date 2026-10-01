# Phase 7 — People, projects, starter content

`admin` and the session are from phase 3 §5. Read `/srv/kb/.claude/skills/kb-todos/SKILL.md` on the server before writing any task line.

## People

**Ask:** "Who should get an account? For each: full name, email, and **full** (browser, terminal, AI agents) or **web-only** (browser only). Should anyone besides you be an admin? Admin means root on the server."

- Username: lowercase, `^[a-z][a-z0-9_]{1,30}$`, usually the first name. Confirm the list before creating anything.
- Initial password: generate one per person (`openssl rand -base64 15`), at least 12 characters.

```bash
admin POST /admin/users '{"username":"jana","first":"Jana","last":"Nováková","email":"jana@acme.com","password":"<generated>","kind":"full"}'
# kind: "full" | "viewer"   ·   verify: admin GET /admin/list
```

- Hand-out list → local `~/company-os-install/<host>-passwords.txt`, mode 0600. Tell the human: deliver each one privately, then **delete the file**.
- **Changing it:** full accounts run `passwd` in a web terminal on first login. Web-only accounts cannot; an admin resets them with `sudo passwd <user>`.
- **Extra admins:** `ssh companyos 'sudo usermod -aG sudo <user>'`. Then protect every admin from deletion by another admin: set `KB_PROTECTED_USERS=<a>,<b>` in `/etc/kb/kb.env`, `sudo systemctl restart kb-hub`.
- **Cloudflare Access:** anyone whose email the policy does not already cover (`email_domain`) must be added — dashboard: edit policy `Staff`; token path: `cf PUT accounts/$ACC/access/policies/$POL` with the full `include` list. **Email in Access first, then the account.**
- Agent CLIs for these people: back to phase 6 if they asked.

## Restricted projects

**Ask:** "Are there areas only some people should see — a client project, HR, management?" For each: a folder name and its members.

```bash
admin POST /admin/groups '{"name":"proj-<slug>"}'
admin POST /admin/groups/member '{"group":"proj-<slug>","username":"<user>","action":"add"}'   # per member
ssh companyos 'G=proj-<slug>; D="/srv/kb/projects/<Folder name>"
  sudo usermod -aG $G kbindexer
  sudo install -d -m 2770 -o $USER -g $G "$D"
  sudo setfacl -d -m u::rwx,g::rwx,o::- "$D"
  sudo systemctl restart kb-indexer'
```

- **The group is the audience** — never per-user ACLs for a team.
- `kbindexer` must be in the group or the folder is invisible to search; its groups are fixed at start, hence the restart (~minutes of stale search).
- Verify: `sudo -u <member> test -r "$D" && echo member-ok`; `sudo -u <outsider> test -r "$D" || echo outsider-denied`; `sudo -u kbindexer test -r "$D" && echo indexed`.
- **Ask per folder:** "Is this sensitive — HR, salaries, legal? Then every change in it lands in the security audit too." If yes: `ssh companyos 'cd ~/ollsoft-company-os && sudo bash scripts/install-audit.sh --watch "<D>"'`. Changes only: outsiders trying to read it are logged anyway, and who opened what in the browser is in the web app's own audit.

## Starter content

**Ask:** "How should your knowledgebase start?"

| Choice | What happens |
|---|---|
| **Empty** | Just the to-do board that ships. |
| **Starter pack** (recommend) | Five questions about the company, then you write 5–8 short pages: `company/README.md` (start here), team and roles, onboarding checklist, how we work, first to-dos assigned to real people. |
| **Example artifacts** | Live apps copied into their `company/`, sample data included: Kanban board, sales pipeline, invoice generator, risk heatmap. Combine with the starter pack. |
| **Demo company** | A complete fictional engineering firm (handbook, ISO quality records, pipeline, Kanban, cockpit) to explore, removable with one command. |

**Starter pack.** Ask what the company does, the team and their roles, the main recurring processes, tools in use, and the three things a new hire must know. Write as the admin (`ssh companyos 'cat > "/srv/kb/company/…"'`) so history shows them as author. Follow `/srv/kb/.claude/CLAUDE.md` on the server: bullets over prose, one fact per line, no hard-wrapped lines. Tasks are `- [ ] … @user`.

**Example artifacts.** Copy `~/ollsoft-company-os/showcase/kb/<path>` → `/srv/kb/<path>` as the admin, **keeping the paths** — the pipeline, invoice and risk apps read their data from fixed locations. The Kanban is the exception: it reads `kanban.md` next to itself, so any folder works. The templates name people as `{{admin}}`/`{{member}}` (logins) and `{{Admin}}`/`{{Member}}` (first names): replace them with real people in every copied `.md`/`.html`/`.json`.

| Artifact | Paths |
|---|---|
| Kanban | `company/dashboards/{delivery-kanban.html,kanban.md}` → `company/board/` — then replace the sample cards in `kanban.md` with their own columns and people (keep its header comment) |
| Sales pipeline | `company/sales/{customer-pipeline.html,.pipeline-data.json}` |
| Invoice generator | `company/finance/{invoice-generator.html,.invoice-data.json}` — put their company details in the data file |
| Risk heatmap | `company/quality/{risk-heatmap.html,.risk-data.json}` |

**Demo company.**

```bash
ssh companyos 'cd ~/ollsoft-company-os && sudo bash scripts/seed-showcase.sh --admin $USER --member <non-admin user>'
ssh companyos 'sudo passwd -l demo'     # it creates web account "demo" / password "companyos" — lock it on a public host
```

- Tasks and owners in it go to `--admin` and the first `--member`, who should be a non-admin so the permission walkthrough works.
- It replaces the pinned launchers (keeps a backup).
- **Remove later with `sudo bash scripts/seed-showcase.sh --undo` — only on a box that has nothing else in those folders.** Undo deletes `company/{handbook,quality,sales,finance,dashboards,media}`, `company/README.md` and `00 START HERE.md` wholesale, including anything the company wrote there since.
- **Never run `seed-demo.sh` on a real server** — it is the test fixture (XSS probe pages, test credentials in `/tmp`).
