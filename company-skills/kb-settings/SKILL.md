---
name: kb-settings
description: How platform settings work — the layered files (company defaults in .os/settings.json, personal in users/<you>/.os/settings.json), what each key means, and how an agent may change them. Load it when asked to change a preference or a company-wide default.
---

# Settings

Settings resolve per key, lowest to highest: the shipped default, the company
layer, the person's own layer. One value from exactly one layer.

| Layer | File | Who may write |
|---|---|---|
| company | `/srv/kb/.os/settings.json` (root-owned, everyone reads) | an admin only — the Settings dialog's Company tab, or `POST /admin/settings`. If you run as a non-admin, ask one; do not try to write it |
| yours | `/srv/kb/users/<you>/.os/settings.json` (0600) | you. Edit it with any tool, or `POST /api/settings` |

Both files are flat maps:

```json
{
  "ui.theme": "deep-blue"
}
```

Rules when editing the file yourself:

- Keep it a JSON object of `"key": value`. Unknown keys and wrong values are
  ignored one by one (the dialog reports them as "ignored"); a file that is not
  valid JSON counts as empty until the next save repairs it.
- Keys starting with `_` are reserved and ignored — safe for a `_note`.
- The directory `users/<you>/.os/` is created 0700 by your backend the first
  time you save from the app; if it does not exist yet, create it with mode 700.
- To go back to the company default, remove the key from your file.

## Keys

| Key | Values | Meaning |
|---|---|---|
| `ui.theme` | `deep-blue` | Colour theme for the whole app. One theme today; more arrive with the theme work. |

`GET /api/settings` (as you, with your session) returns the full picture:
`effective` (what applies), `source` (which layer each value came from),
`company` and `user` (each layer's values, plus anything ignored and why).
