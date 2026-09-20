# Settings

One registry, two flat JSON files, three endpoints, one dialog. A setting is
declared once, in Python; the validation, the files, the API and the dialog
row all follow from that one entry.

## Layers

A value resolves **per key**, lowest to highest:

| Layer | Where | Who writes it |
|---|---|---|
| shipped default | `REGISTRY` in `kb_platform/settings.py` | the code |
| company | `<repo>/.os/settings.json` (root:kb-users 0644) | an admin, through the dialog's Company tab or `POST /admin/settings` |
| yours | `<repo>/users/<you>/.os/settings.json` (0600, in a 0700 dir) | you, through the dialog or `POST /api/settings` — or directly with any editor, or an agent running as you |

One value comes from exactly one layer, and the dialog's pill says which
(`default`, `company default`, `your setting`). Yours follow you to every
browser; the browser keeps only a cache of the last resolved values so the
first paint (the theme) does not wait for the network. There is no per-device
layer yet — it is added the day a setting needs one.

## The files

Flat maps, nothing else:

```json
{
  "ui.theme": "deep-blue"
}
```

Keys starting with `_` are reserved for future metadata and ignored. A key the
registry does not know, or a value that fails its type, is dropped **alone** and
reported back as `rejected`; a file that is not valid JSON yields no values and
an `error` string. Nothing ever blanks the rest, and the next save through the
API rewrites the whole file, which repairs it. The company file is in the
repo's git history (`.gitignore` un-ignores `.os/*.json`); yours is private and
unversioned.

## Endpoints

- `GET /api/settings` (as you) → `{schema, defaults, company:{values,rejected,error}, user:{values,rejected,error}, effective, source}`.
- `POST /api/settings` (as you) `{"set": {key: value}, "unset": [key]}` against your layer; 400 on a bad key or value, then the new snapshot.
- `POST /admin/settings` (hub, admin group) — the same body against the company layer, written in place like the egress allow-list (a write ACL on the file would survive) and audited as `settings.company`.

The frontend module `frontend/src/settings.js` wraps them: `get`, `source`,
`set`, `unset`, `subscribe`, `fetch`. It fetches once at boot (in the same burst
as whoami), after every save, and whenever the tab becomes visible again. It is
exposed as `window.__kbsettings` for tests.

## The registry

| Key | Type | Default | Scopes | What it does |
|---|---|---|---|---|
| `brand.name` | string, up to 40 chars | `Company OS` | company | The product name next to the logo in the app, the tab title, and the sign-in page (substituted by the hub on the way out, so it shows before sign-in). |
| `brand.logo` | image (`logo.svg` or `logo.png` in `.os/`) | `""` = the built-in Ollsoft mark | company | Set only through `POST /admin/brand/logo` (multipart `file`, SVG or PNG, ≤ 512 KB; `{"reset": true}` removes it). Served to everyone at `GET /brand/logo` with a no-script CSP; an SVG with script is refused. |
| `ui.theme.custom` | map of token → value. Colours (`#rrggbb`): bg, chassis, panel, panel2, border, ink, muted, faint, heading, accent, accent-deep, ok, warn, danger, code-bg, code-ink, term-bg, term-fg. Type and space: `sans`, `mono` (font stacks), `font-size` (10–24px), `editor-size`, `editor-lh`, `rich-lh`, `content-x`, `content-y`, `content-max` (px), `source-x`, `row-y`, `tab-y`, `pad`, `r` (px) | `{}` | company, user | Single tokens overridden on top of the chosen theme, as inline custom properties on `<html>`; the stylesheet derives the rest. A layer's map replaces the other's whole, it does not merge. "Customize…" shows a colour picker per colour and a text field per dimension, seeded from what the theme paints. Deep blue keeps today's type and spacing; Dark and Light take Notion's air (16px base, taller lines, a centred 760px column, softer corners). |
| `ui.theme` | enum `deep-blue` · `dark` · `light` | `deep-blue` | company, user | Colours for the whole app, editor and terminal included, applied as `data-theme` on `<html>` before first paint. Deep blue is the house look; Dark and Light follow Notion's greys and paper. |

Types: `bool`; `int` with `min`/`max`; `enum` with `options` and optional
`labels` (shown in the dialog); `string` with an optional `pattern` (full
match) and `maxlen`, never control characters; `image`, a file in `.os/`
whose value is the file's name — set only through its upload endpoint, never
through `set`; `map`, an object of allowed `keys` to values matching a
`pattern` (a layer replaces the map whole).

## Adding a setting

1. One entry in `REGISTRY` (`kb_platform/settings.py`): key, type, default,
   `scopes` (which layers may set it), `group`, `label`, `help`.
2. If it has a live effect, one `settings.subscribe(key, fn)` in `app.js` — the
   subscriber runs for a change made in the dialog and for one that arrives
   from the server alike.
3. A row in the table above and in `company-skills/kb-settings/SKILL.md`
   (`tests/cli/test_settings.py` checks both name every key).

Nothing else: the dialog row, the validation, the files and the endpoints come
from the entry.

## What is not a setting

The open tabs, the tree state, the recent files and the dictation history are
session state and stay in the browser. The older per-browser preferences (edit
mode, hidden files, dictation language, terminal font, sidebar width) also stay
where they are for now; each can be promoted into the registry when it should
follow the person rather than the device.
