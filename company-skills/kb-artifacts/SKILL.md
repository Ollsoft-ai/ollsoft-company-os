---
name: kb-artifacts
description: Use when the user wants an interactive view, dashboard, chart, or small app that renders live data inside the knowledgebase web app. Explains how to build a self-contained HTML artifact, query the database and read/write files through the sandbox bridge (all running as the viewer), and share it safely.
---

# Building an artifact

An **artifact** is a single self-contained `.html` file placed in the repo. When someone opens it in the web app, it renders inside a **sandboxed iframe**: no cookies, no access to the app around it, and `connect-src 'none'` so it cannot open a socket of its own. Its only capability is a message **bridge** to the host page, which exposes a few narrow, scoped actions — query the database, read/write a file, read a neighbouring file's raw bytes (`kb-read-bytes`, for showing a video/image/PDF; 512 MB cap), toggle a task, upload, save-as through a prompt the *user* answers, copy to clipboard, and **`kb-fetch`: HTTPS to domains an admin has allowlisted for this specific artifact** — **each executed as the person viewing the artifact**.

Create one by writing an `.html` file, e.g. `company/dashboards/mychart.html` (shared) or `users/<you>/scratch.html` (private).

# The bridge — every action runs AS the viewer

The artifact never connects to a database or the filesystem directly. It posts a message to the host page, which performs the action **with the viewer's own identity and permissions** — the same as that person running `psql` or writing a file in their terminal. So the same artifact does, for each viewer, exactly what that viewer is allowed to do, and nothing more. You never write authorization logic in the artifact; the platform enforces it.

Drop this SDK into every artifact — it gives you `kbQuery`, `kbRead`, and `kbWrite`:

```html
<script>
  let _id = 0; const _pending = {};
  window.addEventListener("message", (ev) => {
    const m = ev.data || {};
    if (m.type === "kb-result" && _pending[m.id]) { _pending[m.id](m); delete _pending[m.id]; }
  });
  const _send = (msg) => new Promise((res) => {
    const id = ++_id; _pending[id] = res; parent.postMessage({ id, ...msg }, "*"); });
  // SQL as the viewer (RLS applies) -> { cols, rows, error? }
  const kbQuery = (sql, params = []) => _send({ type: "kb-query", sql, params });
  // read a file as the viewer (kernel-enforced) -> { ok, content, error? }
  const kbRead  = (path)          => _send({ type: "kb-read", path });
  // write a file as the viewer (kernel-enforced) -> { ok, bytes, error? }
  const kbWrite = (path, content) => _send({ type: "kb-write", path, content });
</script>
```

## Querying the database

Use `%s` placeholders with a `params` array for any values — never string-concatenate into SQL:

```js
const r = await kbQuery("SELECT id, value FROM u_alice.readings ORDER BY id DESC LIMIT 20");
if (r.error) { /* viewer isn't allowed, or SQL error */ } else { /* render r.rows */ }
await kbQuery("UPDATE u_alice.readings SET value=%s WHERE id=%s", [42, 7]);
```

## Reading and writing files

`kbRead`/`kbWrite` operate on repo-relative paths **as the viewer**, so they can only touch files that viewer's OS user can read/write — the kernel enforces it. Reads return the file's text; writes overwrite the whole file in place (its owner/group are preserved) and the parent folder must already exist.

**Scope:** for safety, `kbRead`/`kbWrite` are limited to the artifact's **own folder** (the directory the `.html` lives in) and its subfolders — an artifact in `company/dashboards/` can read/write `company/dashboards/*` but not files elsewhere. Put an artifact's data files alongside it. (This stops a shared artifact from reading a viewer's private files in other folders.)

Use paths **inside the artifact's own folder** — e.g. an artifact at
`company/dashboards/report.html` reads/writes `company/dashboards/…`:

```js
const doc = await kbRead("company/dashboards/data.md");   // co-located with the artifact
if (doc.error) { /* not readable, or outside this folder */ } else { console.log(doc.content); }

const res = await kbWrite("company/dashboards/output.md", "# Generated report\n...\n");
if (res.error) { /* not writable, or outside this folder */ } else { /* saved res.bytes */ }
```

A path in another folder (e.g. `users/<someone>/notes.md`) returns
`{ error: "path outside this artifact's folder" }` — put the data you need next to the artifact.

Writing a `.md` file that someone is live-editing is safe: the change flows through the sync daemon and merges into their session, just like any external edit. To append to a file rather than replace it, `kbRead` it, concatenate, then `kbWrite` the result.

## File naming convention

- Keep the user-facing `.html` artifact at its normal visible path.
- Dot-prefix non-document implementation state next to it, for example
  `pipeline.html` + `.pipeline-data.json`. Dot-files stay out of normal tree and
  quick-open navigation.
- Put screenshots, uploads and other attachment binaries in the nearest
  `_files/` folder and link them relatively from Markdown.
- If data is meaningful company evidence that people should read, search and
  review in history, store it as a normal visible `.md` document instead of
  hiding it as implementation state.

# Sandbox rules (important)

The iframe blocks all external resources. **Everything must be inline**: no `<script src>`, no external CSS, no web fonts, no remote images. Write your CSS in a `<style>` tag and your JS in `<script>`. Use system fonts. To auto-refresh, `setInterval(render, 2000)`.

`fetch()` from inside the artifact is blocked too. To call an API, send `{type:"kb-fetch", url, method, headers, body}` over the bridge: the **hub** makes the request, checks the admin's per-artifact domain allowlist, and substitutes any header value written as `secret:_secrets/<file>` server-side — so the credential is used without the artifact ever seeing it. No allowlist entry = 403 with a message saying so.

# Sharing an artifact = two deliberate acts

**Before you build an artifact that stores data, ask the human who should be able to use it** — everyone at the company, or specific people, and read-only or also write. The answer decides the grants below, and it is much easier to apply when you create the table than to retrofit later. See **kb-database**.

For a colleague to use your dashboard, BOTH must be true — and if you forget one, they see a broken/empty view, never someone else's data:

1. **File access** — they must be able to read the `.html` file. Put it in a folder they can read (e.g. `company/` for everyone), or grant them specifically with an ACL:
   ```bash
   setfacl -m u:bob:r users/<you>/mychart.html   # share this one file with bob
   getfacl -cpE users/<you>/mychart.html         # verify: no "#effective:" downgrade
   sudo -u bob test -r users/<you>/mychart.html && echo "bob can open it"
   ```
   Verify with `getfacl`, not `ls`: on a file that has an ACL, the group column
   in `ls -l` shows the ACL *mask*. If the mask is narrower than the grant,
   `getfacl` marks the entry `#effective:---` and the share is real but inert.
   A file created 0600 (the usual cause — `mkstemp`, a rename-into-place) needs
   `chmod 660` before any grant on it means anything.
2. **Data access** — grant them the database access the artifact's queries need (see **kb-database**):
   ```sql
   -- everyone at the company (new hires included — use this whenever the answer is "everyone"):
   GRANT USAGE ON SCHEMA u_<you> TO kb_users;
   GRANT SELECT, INSERT ON u_<you>.mytable TO kb_users;
   GRANT USAGE, SELECT ON u_<you>.mytable_id_seq TO kb_users;   -- bigserial needs this to INSERT

   -- or, only if access is genuinely meant to be a subset:
   GRANT USAGE ON SCHEMA u_<you> TO bob;
   GRANT SELECT ON u_<you>.mytable TO bob;
   ```

**These two do not fail the same way.** File access *inherits* — a new hire joins the `kb-users` OS group and can immediately open the page. Grants do not: if you granted to a list of names, the new hire opens your artifact and every query inside it dies with `permission denied`, showing an empty widget with no hint why. So write the everyone-case as `kb_users`, never as a list of the people who happen to work here today.

# Minimal working template

```html
<!doctype html><meta charset="utf-8">
<style>body{font-family:system-ui;background:#0D1626;color:#E9EFFA;padding:1rem}
table{border-collapse:collapse}td,th{padding:.4rem .7rem;border-bottom:1px solid #223350;text-align:left}</style>
<h2>My live view</h2><div id="out">loading…</div>
<script>
  let _id=0,_p={};onmessage=e=>{const m=e.data||{};if(m.type==="kb-result"&&_p[m.id]){_p[m.id](m);delete _p[m.id];}};
  const kbQuery=(sql,params=[])=>new Promise(r=>{const id=++_id;_p[id]=r;parent.postMessage({type:"kb-query",id,sql,params},"*");});
  async function render(){
    const r=await kbQuery("SELECT id, value FROM u_alice.readings ORDER BY id DESC LIMIT 10");
    const out=document.getElementById("out");
    if(r.error){out.textContent="no access: "+r.error;return;}
    out.innerHTML="<table><tr><th>id</th><th>value</th></tr>"+
      r.rows.map(x=>`<tr><td>${x[0]}</td><td>${x[1]}</td></tr>`).join("")+"</table>";
  }
  render(); setInterval(render, 2000);
</script>
```

# House style (Ollsoft Company OS)

Match the platform chrome so your artifact feels native. Palette: background
`#0D1626`, panel `#101B2E` / `#172741`, border `#223350`, text `#E9EFFA`,
muted `#8CA1C1`, accent blue `#4D9DFF`, ok green `#2FCE98`, warn `#E5AE58`,
danger `#F2809C`. Monospace (`ui-monospace`) for paths, identities, schedules,
data; uppercase letter-spaced labels for section headers. Dark, quiet,
blue-accented — the live example is `company/cron-demo/pulse.html`.

# Trust note

Every bridge action runs as the viewer, so an artifact can do anything the *viewer* could do in a terminal — including **reading and writing that viewer's files** — and nothing more. The sandbox + allowlisted-only egress + per-viewer identity protect every viewer's session, other users' data, and anything outside the viewer's own permissions. They do **not** protect a viewer from a hostile artifact author acting *within* the viewer's own authority (e.g. overwriting a file the viewer can write). Now that artifacts can write files, treat opening one like running a shared script: only open artifacts from people you'd trust with your own access.
