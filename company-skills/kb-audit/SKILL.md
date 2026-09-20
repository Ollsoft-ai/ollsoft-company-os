---
name: kb-audit
description: Investigate the platform's audit trail for rogue users, abuse of access, or an intruder — "did anyone touch X", "who un-shared this", "is someone probing logins", "check for suspicious activity". Explains where the evidence is, what normal looks like on THIS box, and which findings are real versus noise.
---

# Reading the audit trail

**Conclusion first: most "suspicious" lines here are the platform working.**
Before reporting anything, check it against *Normal, not an incident* below.
A false alarm costs trust; the next real one gets ignored.

## Where the evidence is

| Source | Command | Answers |
|---|---|---|
| **Hub audit** | `journalctl -u kb-hub -g AUDIT --since yesterday` | who logged in, shared, changed permissions, created accounts |
| **Access audit** | `journalctl -u kb-hub -g 'AUDIT (document.open\|file.preview\|file.download)' --since yesterday` | who opened a document, previewed or downloaded an attachment (since 2026-08-29) |
| Hub raw | `journalctl -u kb-hub --since yesterday` | every HTTP request, throttle lockouts, tracebacks — the request line, never the caller |
| Artifact egress | `sudo cat /var/log/kb/egress.log` | every outbound call an artifact made: user, artifact, method, URL, status |
| Dictation | `sudo cat /var/log/kb/stt.log` | who dictated, how much and when — never what was said |
| Content history | `kb-history --since '7 days ago' --author <user>` | what someone *wrote* — never who changed permissions |
| Sync daemon | `journalctl -u kb-syncd --since yesterday` | live editing, revocations, room retires |
| Shell access | `journalctl -u ssh --since yesterday` | SSH logins (key-only since 2026-08-24) |
| Root use | `journalctl --since yesterday | grep 'sudo:.*COMMAND'` | privilege escalation attempts |

Audit lines look like:

```
hub AUDIT share.set actor=alice result=ok path='company/HR/x.md' scope='people' group='kbs-hr'
hub AUDIT login actor=mallory result=DENIED source='203.0.113.4'
hub AUDIT document.open actor=bob result=ok path='company/HR/x.md'
hub AUDIT file.download actor=bob result=ok path='company/HR/salaries.xlsx' bytes=48211
```

Mutation events: `login`, `share.set`, `props.set`, `group.member`,
`user.create` — and no others. `result=DENIED` is the interesting half — that is
someone being refused.

Access events: `document.open`, `file.preview`, `file.download`. These are
always `result=ok` by construction — they are written only after the service
served the bytes, so a refusal never reaches them (look in the raw log for
those). Read each for exactly what it says:

| Event | Means | Does NOT mean |
|---|---|---|
| `document.open` | a live editing session for that doc was joined and accepted | that it was read, or stayed open |
| `file.preview` | the server served the attachment inline | that anyone looked at it |
| `file.download` | the server served an explicit download (`dl=1`) | that it landed on their disk |

One `document.open` per accepted session, not per keystroke — but a reconnect
(dropped wifi, reopened tab, a lineage refresh) is a new join and a new line, so
count *people and documents*, never lines.

## Who can read this

Only root and members of `sudo`, `adm` or `systemd-journal` — on this box, that
is krystof. Verified: an ordinary employee running the same command sees **2
lines out of 3,782**, and both are their own cron entries. journald shows a user
only their own messages otherwise.

That matters twice over. The people being audited cannot read the audit. And an
agent running AS krystof inherits the whole trail — which is the real exposure
here, not a colleague.

## The threat model that matters here

Not an anonymous internet attacker. **An authenticated colleague, or an agent
running as one.** They already have a login, a terminal and the ability to
create files, artifacts and cron jobs. So look for someone reaching for things
*outside their own work*, not for someone "breaking in".

## What actually deserves attention

**1. Reading something they were never given**
```
journalctl -u kb-hub -g AUDIT --since '7 days ago' | grep -E 'share.set|props.set'
```
Look for a person widening a folder they do not work in, or setting
`scope='everyone'` on anything under `projects/` or `company/🫂 Human Resources`.
Cross-check: did the same actor then read it? Since 2026-08-29 there is a real
answer — `journalctl -u kb-hub -g AUDIT | grep "path='<path>'"` shows the opens
and downloads alongside the sharing change.

**2. Someone adding themselves to a group**
```
journalctl -u kb-hub -g AUDIT | grep group.member
```
`actor` and `target` being the same person is worth a question. So is any
`result=DENIED` on `group.member` — the hub refuses `sudo`, `root` and `docker`
via the UI, and a refusal means someone tried.

**3. Login probing**
```
journalctl -u kb-hub -g AUDIT | grep 'login.*DENIED' \
  | grep -oP 'actor=\K\S+' | sort | uniq -c | sort -rn
```
(Match on `actor=` rather than a column number — the journal prefixes every
line with a timestamp, host and pid, so field positions shift.)
The throttle allows 8 failures per 5 min per account and per source, then locks
for 15. Many DENIED for ONE account = someone guessing that person's password.
Many DENIED across MANY accounts from one source = spraying. Both are real.

**4. A download burst out of someone's own area**
```
journalctl -u kb-hub -g 'AUDIT file.download' --since '7 days ago' \
  | grep -oP "actor=\\K\\S+" | sort | uniq -c | sort -rn
```
This is an **activity signal, not an incident**. Normal work downloads things.
What is worth a question is a shape: one actor pulling many files out of an
area they do not work in, in a short window, especially right after a
`share.set` widened it or right before a departure. Scope it to the paths, not
the count:
```
journalctl -u kb-hub -g 'AUDIT file.(download|preview)' --since '7 days ago' \
  | grep "actor=<user>" | grep -oP "path='\\K[^']+" | sort | uniq -c | sort -rn
```
Do not use these events to report on how much someone works, what hours they
keep, or which documents they linger in. That is not what the trail is for, and
reading it that way is how a security control becomes surveillance.

**5. An account created that nobody remembers**
```
journalctl -u kb-hub -g AUDIT | grep user.create
```
Every real hire should be traceable to a conversation. `kind='viewer'` is the
restricted tier; a `full` account nobody asked for is serious.

**6. Escalation attempts in the raw log**
```
journalctl -u kb-hub --since '7 days ago' | grep -E ' 40[13] | 500 '
```
A burst of 403s from one user walking paths they cannot read is enumeration.
500s clustered on one endpoint usually mean a bug, not an attack — but check.

## Normal, not an incident

Do **not** report these:

- **`source=''` on a login.** Deliberate: a local caller with no proxy header
  gets empty rather than `127.0.0.1`, so test runs cannot lock out the box.
  Only Cloudflare-tunnelled requests carry a real IP.
- **`kb-syncd` as a git author.** The daemon commits external edits nobody typed
  in the editor. Not an impersonation.
- **`kbindexer` reading everything.** It is the search indexer and holds a named
  ACL on every file, private ones included. It is *supposed* to see all of it.
- **`kbt_*` accounts and `kbtest-*` folders.** Test fixtures. They are created
  and destroyed constantly and are namespaced per run.
- **Bursts of `/api/presence` and `/api/tree`.** The open tab polls.
- **A stream of `document.open` for one person.** Every tab they open, and
  every reconnect after a dropped socket, writes one. It is activity, not
  enumeration.
- **`file.preview` on images inside a document.** Rendering a page with five
  embedded images serves five attachments; that is one open, not five.
- **`props.set` with `granted_traverse=[]`.** Routine sharing bookkeeping.

## How to investigate properly

1. **Establish the baseline.** Run the query over a quiet week first. "Unusual"
   only means anything against a normal.
2. **Name the actor, the object and the time.** "alice widened
   `projects/acme` to everyone at 02:14" is a finding. "suspicious sharing
   activity" is not.
3. **Check whether it was allowed.** `result=ok` means the kernel permitted it —
   so either it was legitimate, or the permission model is wrong. Both matter,
   differently.
4. **Correlate.** A `share.set` with no matching `kb-history` edit means someone
   changed *access* without touching content. That is the shape of exfiltration.
5. **Say what you could not determine.** The gaps below are real.

## What this trail CANNOT tell you

Be honest about these rather than inferring past them:

- **Reads are logged only through the app, and only since 2026-08-29.** A
  document joined in the editor, and an attachment the server served, leave a
  line. Nothing else does: SSH, the mounted drive, `cat` in a web terminal, the
  search index and the secrets viewer are all invisible. So an access event is
  evidence that something *was* opened; the absence of one is not evidence that
  it was not.
- **No access event proves reading.** `file.download` means the server sent the
  bytes with a save-me header. Whether a human looked, understood, or kept the
  file is outside what any of this can see — say so rather than implying it.
- **Not every mutation is recorded either.** Deleting a user, flipping an
  account between full and viewer, creating or deleting a group, and editing
  `.os/egress.json` write no AUDIT line. The raw hub log shows the
  `POST /admin/...` that did it, but not who sent it — so corroborate with
  `getent`, `/etc/passwd` and, for the allow-list,
  `sudo git -C /srv/kb log -p --follow -- .os/egress.json` (`--follow`: before
  2026-09 the file lived at `.claude/egress.json`; `kb-history` covers
  `.md`/`.html` only).
- **The mutation events start 2026-08-25, the access events 2026-08-29.** There
  is nothing before the log existed, and no way to reconstruct it.
- **Anything done as root, or directly on disk, bypasses it entirely** — an SSH
  user running `setfacl` by hand leaves no audit line. Check `journalctl` for
  `sudo` separately.
- **Anyone in `sudo` can edit or clear the journal.** This trail deters and
  reconstructs; it does not bind an administrator. Treat it as evidence about
  users, not about admins.
- **journald rotates.** Old evidence ages out; `--since` beyond the retention
  window silently returns less, not an error.

## If you find something real

Do not "fix" it quietly — preserve it. Copy the relevant lines to a dated file
under `users/<you>/`, record the exact commands you ran, and tell krystof
before changing any permission, because changing it destroys the state you would
need to understand what happened.
