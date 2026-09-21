---
name: kb-settings
description: How platform settings work — company-wide defaults in .os/settings.json, personal overrides in users/<you>/.os/settings.json, every key and its values, how an agent changes them, and what to update when a setting is added. Load it when asked to change a preference, a company-wide default, the theme, or to add a setting to the platform.
---

# Settings

Settings resolve **per key**, lowest to highest: the shipped default, the
company layer, the person's own layer. One value from exactly one layer; the
API says which (`source`). Everything the app lets people choose — the theme,
and whatever joins it — is a setting. Nothing is a config file of its own.

| Layer | File | Who may write |
|---|---|---|
| shipped default | `kb_platform/settings.py` in the platform repo (`REGISTRY`) | platform developers |
| company | `/srv/kb/.os/settings.json` (root-owned, everyone reads) | an admin only — the Settings dialog's Company tab, or `POST /admin/settings`. If you run as a non-admin, ask one; do not try to write it |
| yours | `/srv/kb/users/<you>/.os/settings.json` (0600) | you. Edit it with any tool, or `POST /api/settings` |

Both files are flat maps of `"key": value`:

```json
{
  "ui.theme": "light"
}
```

Rules when editing a file yourself:

- Unknown keys and wrong values are ignored one by one (the dialog reports
  them as "ignored"); a file that is not valid JSON counts as empty until the
  next save repairs it.
- Keys starting with `_` are reserved and ignored — safe for a `_note`.
- `users/<you>/.os/` is created 0700 by your backend the first time you save
  from the app; if it does not exist yet, create it with mode 700.
- To go back to the company default, remove the key from your file. To go
  back to the shipped default company-wide, an admin removes it from the
  company file (or unsets it in the dialog).

## Keys

| Key | Values | Default | Layers | Meaning |
|---|---|---|---|---|
| `brand.name` | any text up to 40 characters | `Company OS` | company | The product name next to the logo, in the tab title and on the sign-in page. |
| `brand.logo` | `logo.svg` · `logo.png` · `""` (the built-in Ollsoft mark) | `""` | company | The logo in the app and on the sign-in page. A file, not a value: an admin uploads it in Settings → Company (SVG or PNG, ≤ 512 KB) or with `POST /admin/brand/logo`; `{"reset": true}` there removes it. Do not write the value into `settings.json` by hand. |
| `ui.theme.custom` | an object of token → value. Colours (`#rrggbb`): `bg`, `chassis`, `panel`, `panel2`, `border`, `ink`, `muted`, `faint`, `heading`, `accent`, `accent-deep`, `ok`, `warn`, `danger`, `code-bg`, `code-ink`, `term-bg`, `term-fg`. Type and space: `sans`, `mono` (font stacks), `font-size` (`10px`–`24px`), `editor-size`, `editor-lh`, `rich-lh` (line heights, e.g. `1.8`), `content-x` (gutter on a wide pane), `content-x-narrow` (its floor on a phone or split pane), `content-y`, `content-max` (the column, e.g. `708px`), `source-x`, `row-y`, `tab-y`, `pad`, `r` (corner radius, px) | `{}` | company, user | Single tokens changed on top of the chosen theme (`{"accent": "#ff6600", "font-size": "17px", "content-max": "720px"}`). Your map replaces the company's whole; remove the key to get the theme as shipped. |
| `ai.agent` | `claude` · `codex` · `gemini` · `copilot` · `grok` · `qwen` · `opencode` · `deepseek` | `claude` | company, user | The agent a new agent chat opens with (Alt+C). Installed agents are listed in the chat's picker regardless; each person signs in to an agent themselves (a terminal tab runs the agent's login, or they paste an API key), and that is not a setting. |
| `ui.theme` | `deep-blue` · `dark` · `light` | `deep-blue` | company, user | The whole look: colours, fonts, sizes and spacing, editor and terminal included. Deep blue is the house look; Dark and Light are Notion's measured night and paper (system font, 16px, a 708px column, its colours per mode). |

`GET /api/settings` (as you, with your session) returns the full picture:
`schema` (every key, its type, options and labels), `effective` (what
applies), `source` (which layer each value came from), `company` and `user`
(each layer's values, plus anything ignored and why). Prefer it over reading
the files when you only need to know what applies.

## Adding a setting (platform developers)

A setting is one entry in `REGISTRY` in `kb_platform/settings.py` — key, type
(`bool`, `int` with min/max, `enum` with options and labels, `string` with a
pattern), default, `scopes` (which layers may set it: company, user, or both),
group, label, help. The validation, the two files, `/api/settings`,
`/admin/settings` and the dialog row follow from the entry; add a
`settings.subscribe(key, fn)` in `frontend/src/app.js` if the value has a
live effect. Then, in the same change:

1. a row in the table above — **this skill is the agents' reference and must
   list every key**; `tests/cli/test_settings.py` fails when it does not;
2. the same row in `docs/settings.md`;
3. redeploy the skill to `/srv/kb/.claude/skills/kb-settings/SKILL.md`
   (`scripts/install.sh` does it; on a running box copy it as root:kb-users 0644).

A new theme is a `:root[data-theme="<name>"]` block in
`frontend/assets/style.css` setting every colour token plus the name in
`ui.theme`'s options; `tests/cli/test_theme_tokens.py` refuses a theme that
forgets a token and any colour named outside the token blocks.
