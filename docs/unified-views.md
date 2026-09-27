# Unified views — one layout for documents, artifacts and terminals

**Status: built** (designed and shipped 2026-09-20). ARCHITECTURE.md §8
describes the result in brief; this is the design it was built from, kept
because it says why. What was built differs from the plan below in three
places: the layout model and the view-kind registry live in
`frontend/src/layout.js` and `frontend/src/views.js` (§8 step 1 called the
registry out only as a direction); the dock's side is changed from the
palette, not a setting (§6); the phone keeps its keybar inside the dock
(§4.5's fixed keybar was not needed while nothing but the dock shows a
terminal on a phone); and touch got a real drag in the first version after
all — hold a tab a third of a second to lift it, carry it, let go on a strip,
a group's middle, its top or bottom, or the panel (§6 had deferred it to a
long-press menu). Sections 2 and 9 describe the code as it was *before*;
their line numbers are historical.

Hardened 2026-09-21 after a torture pass over every action in every layout at
every size. The invariants the code now keeps, whatever the order of folds,
maximizes, closes and drags: the workspace always shows at least one group
(the last open one refuses to fold, an emptied group goes and the last one
stays open, a saved layout with everything folded opens its first group);
what you activate is what you see (a folded group opens, another group's
maximize is undone, a document opened while a group is maximized lands in
that group); a folded group's handle takes a dropped tab; a split handle
never writes a negative share; the phone's sheet remembers half or full
whichever button asked; sizes are applied as shares of the *visible* groups
only, so nothing leaves a gap when a sibling folds, maximizes away or
leaves (flex hands out only `sum(flex-grow)` of the space when that sum is
below 1 — which every reloaded fraction was); a left dock's handle drags the
right way in its `row-reverse`; the dock's drag and its record agree on
[0.1, 0.9]; the keyboard on a folded handle acts on that group, and a
middle-click on the handle closes the tab it names; Ctrl+` while a group is
maximized restores the layout and goes to the terminal; under the phone
breakpoint groups only stack, and a stack refuses a group it cannot give
110px; a group too small for the floating formatting toolbar hides it
(mouse screens only). A second pass verified all of that and added: the
sheet's automatic "full" is a phone's (coarse pointer), never a narrow
desktop window's; the dock's handle hides while a group is maximized even
after the breakpoint is crossed; the dock's drag clamps to [0.1, 0.9] while
dragging, not on release; the keyboard never lands on `<body>` (`refocus()`
after close / fold / maximize); "Focus the next group" reaches folded
handles; narrow strips yield ＋ and ▾ before a tab name; a drop on a tab's
own group is a no-op without a hint; a drawer opened over a maximized
group on a phone can be dismissed.
A later pass removed what the window system did not need: the per-strip ＋
(a terminal comes from Ctrl+`, Ctrl+Shift+`, the menu or the palette) and
the ⤢ of a group that is the only one holding tabs — maximizing a lone
group does nothing, so the button is not offered.
Regression tests: `tests/e2e/test_unified_views.py`. Still open: shortcuts
while the keyboard is inside an artifact's iframe (the sandbox keeps the
keys; the fix is a forwarder script inside artifacts — a backend change).

## 1. Goal

Everything that shows content becomes a **tab** in a **group**; groups are
arranged in a **grid** (columns of stacked groups) plus one **dock**; a
terminal is a tab like a document. From the user's chair nothing changes by
default: terminals still live in the bottom panel with the same button, the
same Ctrl+`, the same resizer and hide, and on a phone the same half / full
modes and keybar. What becomes possible: drag a terminal tab beside a
document, under it, or full screen; drag a document into the bottom panel;
split anything above or below anything; put the whole terminal panel on the
right of an ultra-wide monitor.

Non-goals, on purpose: no floating windows; no arbitrary nesting (see §3.3
for the one arrangement that costs); no touch drag of tabs in the first
version; no per-group document header (the header keeps describing the one
active document); no change to the pty protocol, the collaboration protocol
or the backends.

## 2. What exists

- **Tabs** (`app.js` 3363–3685): `tabs` is one flat list of `{id, path,
  kind: doc|artifact|secret, name, paneId, el, view, provider, ydoc, frame,
  access, synced, conn, mode, modeComp, announce}`. A tab's live mount is
  `t.el` (a `.tab-content` div) and survives being re-parented: CodeMirror
  untouched, an artifact iframe reloads (accepted). One funnel repaints
  everything: `activateTab` (3609).
- **Panes** (3120–3361): `panes` is a row of `{id, el, barEl, hostEl,
  dropEl, active, grow}`; widths are flex fractions; a pane retires with its
  last tab, but the last pane always stays (empty, "No document open"); the
  leftmost keeps the historic `#tabbar` / `#editor` ids; split handles are
  rebuilt freely, pane elements never (an iframe would reload). Drag zones:
  left / right edge (< 0.28 / > 0.72) split, centre moves, the strip
  reorders; `MIN` 160px per pane; restore caps the row at six.
- **The terminal panel** (6539–7521, `app.html` 129–159): a singleton
  `#terminal-panel`, sibling of `.editor-wrap` inside `.main-col`, with its
  own strip (`#term-tabs .term-tab`, `.term-x`), its own `terms` array and
  `activeTerm`, one `#terminal` host with a `.term-content` per terminal
  switched by `display:none`, one ResizeObserver, one 25 s ping loop, a
  height of 38% (50% on phones, `min-height` 110px) plus an unpersisted
  dragged px, and "open" meaning literally the `hidden` attribute. On
  phones: half (in flow) or max (`body.term-max`, fixed overlay, z-index 58
  under the scrim at 59 and the sidebar at 60), keybar `#term-keys` on
  coarse pointers. Only the terminal resizer is coarse-gated; pane split
  handles are hidden below 880px and work with a finger on a wider tablet.
- **Terminals never touch `active`**: the header, the formatting dock, the
  badge, presence and the URL describe the active document; focusing a
  terminal changes none of it. Alt+] / Alt+[ / Alt+1…9 / Alt+W carry
  `term: true`, so pressed inside a terminal they act on the *document* tabs
  of the active pane; terminals are switched only by clicking their tab.
- **Routing keys off the singleton**: shortcuts decide "am I in a terminal"
  with `closest("#terminal-panel")` (5831); dictation's sticky `lastPane`
  and `dictationTarget` (1219–1272) do the same and fall back to "the
  terminal if the panel is showing"; `wireViewport` (6609) pins the body
  and sizes the fixed overlay to the visual viewport when the soft keyboard
  is up, then refits `activeTerm`.
- **Persistence** (`saveSession` 1721): `kbOpen = {tabs:[{path, kind,
  pane}], panes:[grow…], paneActive:[path|null], active, terms:[{sid,
  name}], activeTerm, termOpen}`; restore in two phases: panes and document
  tabs at t=0 from localStorage alone (`restoreTabs`), terminals right after
  whoami (`restoreRest`), then the per-pane selection and the empty-pane
  clean-up once every mount settled.
- **Form factors**: one breakpoint, `(max-width: 880px)`, in CSS and in
  `isMobile()`: panes stack, the sidebar is a drawer, the terminal is a
  sheet. Nothing else adapts.

## 3. The model

### 3.1 Vocabulary

| Word | Meaning |
|---|---|
| **tab** | one view of one thing: `doc`, `artifact`, `secret`, `term` (later: image, pdf, table) |
| **group** | a tab strip and one visible tab — today's pane, and also the terminal panel |
| **column** | a vertical stack of groups with height fractions |
| **workspace** | the columns, side by side, with width fractions — today's `#panes` |
| **dock** | the one group outside the workspace: a band at the bottom (default) or a column on the right or left; where terminals open, what Ctrl+` toggles, the only group that may be empty and collapsed |
| **layout** | workspace + dock |

### 3.2 The shape

```json
{ "v": 2,
  "columns": [
    { "size": 0.5, "groups": [
        { "id": "g1", "size": 0.6, "active": 0,
          "tabs": [ {"kind": "doc", "path": "company/notes.md"},
                    {"kind": "artifact", "path": "company/todos.html"} ] },
        { "id": "g2", "size": 0.4, "active": 0,
          "tabs": [ {"kind": "term", "sid": "9f3a…", "name": "claude"} ] } ] },
    { "size": 0.5, "groups": [
        { "id": "g3", "size": 1, "active": 0,
          "tabs": [ {"kind": "doc", "path": "projects/acme/plan.md"} ] } ] } ],
  "dock": { "side": "bottom", "size": 0.38, "collapsed": false,
            "group": { "id": "dock", "active": 0,
                       "tabs": [ {"kind": "term", "sid": "1c07…", "name": "bash 1"} ] } },
  "focused": "g1", "maximized": null }
```

Invariants, enforced by one `normalize()`:
- the workspace has at least one column and every column at least one
  group; a group that loses its last tab is removed, except the last group
  of the workspace, which stays and may be empty (today's rule); a column
  that loses its last group is removed;
- sizes are fractions summing to 1 per column and per workspace; a new
  column is refused (toast) when it cannot give every column 160px *now*;
  columns squeeze on a later shrink, as today; a group is never below 110px;
- at most 8 groups in total, workspace and dock together, refused with a
  toast;
- the dock always exists, may be empty and `collapsed`, and takes only
  "in" drops (its edges are not split targets);
- a `term` tab is identified by its pty session id, so a reload reattaches
  the same shell wherever the tab now lives; term tabs carry `path: null`.

### 3.3 Why a grid and not a tree

Every arrangement anyone asked for fits columns of stacked groups plus the
dock: two documents side by side (today), a document above a document
(portrait monitors), a document above its own terminal per column (two
ultra-wides), the terminal panel on the right (one ultra-wide). An
arbitrary tree adds flattening, a depth cap, "split the parent or the node"
and a re-parenting hazard that reloads artifacts on every top / bottom
split — for exactly one extra arrangement: two groups side by side *above
a third inside one column*. The dock band already covers the case that
matters (something wide under everything). The grid is array operations;
the tree is a layout engine.

### 3.4 The default is today's screen

One column, one group, the dock at the bottom at 38%, collapsed until the
first terminal. A first-time user, or a v1 session, sees exactly what they
see now.

### 3.5 Active document, focused group, last terminal

Three notions, kept apart because they are apart today:

- **The active document** (`active`) keeps its meaning: the document,
  artifact or secret the header, formatting dock, badge, presence and URL
  describe. Terminals never become `active`, so a terminal tab never carries
  `.tab.active` (tests use it as a strict single-element locator); it is
  `.current` in its strip. Documents open in the group of `active`, else the
  first group — no separate "home" to persist.
- **The focused group** (`focused`) is where the keyboard is: the group of
  the element that last received focus, or whose strip was last clicked.
  Group-scoped commands — next / previous tab, go to tab N, close tab, split
  right / below, move — act on the focused group. `hasTab` (the `when:` of
  those bindings) becomes "the focused group has a tab".
- **The last terminal** (`lastTerm`) is the terminal record most recently
  focused. `window.__kbterm` stays the xterm `Terminal` *instance* of
  `lastTerm` (tests call `.buffer`, `.focus()`, `.selectAll()`, `.onData`,
  `.options`); `window.__kbterms` stays the array of records with `.ws` and
  `.term`.
- "In a terminal" becomes `closest(".tab-content.term")` for shortcuts,
  `lastPane` and `dictationTarget`. `lastPane` stores `{kind: "term", t}`
  instead of the string `"term"`, revalidated at use by `terms.includes(t)`
  and "its group is displayed and not collapsed"; the last-resort fallback
  ("the terminal if it is showing") becomes "the most recently focused
  terminal that is displayed".
- Existing paths that would otherwise make a terminal tab `active` once
  terminals sit in `panes[]` — `focusPane` (3234), `moveTabToPane` (3353),
  the `closeTab` fallback (3662), `openPath` (3681), `splitActiveTab`
  (3356), `cycleTab` / `gotoTab` (5856) — each get a "term tab → focus its
  group and its terminal, leave `active` alone" branch. `closeTab` on a term
  tab dispatches to `killTerminal`, which is what × and Alt+W do.
- Path-keyed helpers guard `kind === "term"`: `pruneVanishedTabs` (2195,
  `t.path.includes` would throw), `tabEl`'s `dataset.path`, the `openPath`
  dedupe, `noteRecent`, the duplicate-name map, `syncUrl`.

### 3.6 Where things open, what Ctrl+` does

- A **document** opens in the group of `active`, else the first group.
- A **terminal** opens in the dock, expanding it if collapsed — today's
  behaviour — from ＋ in the dock header, Ctrl+Shift+`, the user menu's
  Terminal item (always a new shell, never a jump to an old one), and
  launchers.
  Terminals reach other groups by being dragged there (or "Move tab" in the
  palette). There is no per-group ＋ in the first version.
- **Ctrl+`**: the dock has tabs → toggle it (hide,
  or show and focus its terminal); the dock is empty but a terminal exists
  elsewhere → focus the most recently used terminal, no spawn (the user who
  dragged their only terminal to the right and presses Ctrl+` "to hide it"
  must not get a second shell); no terminal anywhere → spawn one in the
  dock, as now. While a group is maximized, Ctrl+` restores and shows the
  dock.
- **Collapsing the dock while it holds `active`**: `active` becomes the
  visible tab of the focused workspace group, else the first group's. Alt+W
  never acts on a hidden tab.
- **`exit` in the dock's last terminal** hides the dock only when nothing
  else is in it (a document left there keeps it open) — the same as today in
  the default layout, which is what the test expects.
- A non-dock group that holds only terminals cannot be collapsed; × its
  tabs or drag them back.

## 4. Rendering

### 4.1 The DOM stays where it is

The skeleton does not move: `.main-col` is the root (`.editor-wrap` holding
`#panes`, then `#terminal-panel`), so the formatting dock keeps floating
inside `.editor-wrap` above the terminal band, the 38% keeps resolving
against `.main-col`, the `body.term-max` overlay and the mobile sheet keep
their rules, and `#terminal-panel` keeps every id and test hook it has
(§9). The dock's side is `data-dock="bottom|right|left"` on `.main-col`:
`right` / `left` flip its flex direction and order, `#term-resizer` turns
into an east–west handle, and `.terminal-panel`'s `height: 38%` becomes a
width. `#term-resizer` persists `dock.size` (today a dragged height dies
with the page — an intended, visible change).

The workspace renders columns as `#panes > .col > .pane`, always wrapped,
even a column of one group. Uniform shape, no re-parenting ever (a lazy
wrapper would move a group's element — and reload its artifacts — the
first time it gets a neighbour below). Handles: `.pane-split` between
columns (`col-resize`, as now) and `.row-split` between groups in a column
(`ns-resize`, new CSS incl. the `body.pane-resizing` cursor). The first
group of the first column carries `#tabbar` / `#editor`, as the leftmost
pane does today; `body.split` toggles on "more than one workspace group",
never on the dock. This is the one DOM change the tests see: nine
direct-child selectors in `tests/e2e/test_split_panes.py` (`#panes > .pane`,
`#panes > .pane-split`, `:first-of-type` / `:last-of-type`) become
column-aware. Every other test keeps passing unchanged (§9).

The dock's header row (`.term-header`: `#term-tabs`, `#term-new`,
`#term-hint`, "shell as `#term-user`", `#term-max`, `#term-hide`) *is* the
dock group's strip: `#term-tabs` is its `barEl`. Workspace groups keep
their plain `.tabbar`; the dock strip is never `#tabbar`.

### 4.2 Sizing

Fractions become `flex-grow` per column and per group, as `grow` is today.
Minimums are enforced when a split is created or resized. The dock's
collapsed state is the `hidden` attribute (`[hidden] { display: none
!important }` wins over `.terminal-panel { display: flex }`), so a closed
dock takes no space and needs no JavaScript to give it back; the handle
beside a collapsed dock is hidden with it.

### 4.3 Terminal tabs in a group

A terminal's element becomes `.tab-content.term.term-content` (the third
class kept for `test_mobile`'s helpers) inside its group's host, hidden with
`display:none` when not the visible tab — the mechanism the panel uses now,
so no xterm instance is ever detached. The singleton CSS that a terminal
outside the dock would lose moves onto that element: `touch-action: none`
(without it the browser's pan cancels `wireTouchScroll`'s swipe / fling /
long-press), `overflow: hidden`, `background: var(--term-bg)`, `.xterm {
height: 100% }`, the offline dimming; the host stays `overflow: auto` for
documents. Moving a terminal tab between groups is a DOM move followed by a
refit.

Fit timing is the one thing a ResizeObserver cannot do: flipping a tab
`display:none → block` inside an unchanged host fires nothing, and `fit()`
on a hidden element measures nothing. So: fit on tab activation, on move,
on dock expand, on maximize / restore, always in `requestAnimationFrame`
(as `activateTerm` does at 7420); one ResizeObserver per group host refits
its visible terminal; `fitTerm`'s bail-out becomes "this tab is not
displayed or its group is collapsed"; `wireViewport`, `document.fonts.ready`,
window resize, `retintTerminals` and `setTermFontSize` refit every
*displayed* terminal, not `activeTerm`. `term.js` stays lazily imported;
the renderer must not pull it in for a placeholder (`test_loading_ux`
asserts the chunk is not fetched before a terminal opens).

### 4.4 The formatting dock and the header when a terminal has focus

`#mdbar` floats at the bottom of `.editor-wrap` today, i.e. above the
terminal band. With a terminal group possibly at the bottom of a column, it
anchors to the *active document's group element* instead (moved into that
`.pane` on `activateTab`; pane elements are never recreated, and on phones
it is the fixed keyboard row anyway). While a terminal has focus
(`body.term-focus`) the formatting dock and the mode switch hide; the header
text keeps naming the active document, as it does today when you click
into the terminal.

### 4.5 The keybar on touch

One `#term-keys`, `position: fixed` at the viewport bottom with the same
`--kb-lift` the formatting row uses, never re-parented (under the first of
three stacked groups it would be off-screen with the keyboard up). Shown
while the dock is visible (today's rule — Android taps move focus to
buttons, so "while a terminal has focus" alone would blink it) or while any
terminal has focus. Its buttons act on `lastTerm`.

## 5. Form factors — one layout, one rendering policy

The layout never changes with the screen. The rendering does. The rule:
**never rewrite the user's layout because the window changed; render it
differently, and render it the old way again when the window grows.**
"Phone" stays what it is today — the CSS `(max-width: 880px)` blocks and
`isMobile()` — and the JavaScript plan decides only what CSS cannot: which
groups display, the dock last, the keybar.

| Situation | Policy |
|---|---|
| **16:9 desktop** (1920×1080, 2560×1440) | the full grid; the bottom dock at 38% suits this shape; two or three columns are comfortable, 160px each is the floor |
| **4:3, 3:2, tall laptops** (1600×1200, Surface) | the full grid; height is plentiful, so a document *above* a document is worth its shortcut (Alt+Shift+\) |
| **Portrait monitor** (1080×1920) | the full grid; stacked groups are the point of the screen; the persisted dock size lets the user keep the terminal small |
| **Ultra-wide** (3440×1440, 21:9) | a terminal 3440px wide is a waste: "Terminal panel: right" in the palette makes the dock a column (`dock.side`), the same layout with one field changed |
| **Two ultra-wides, 32:9 super-wide** (5120×1440, 6880×1440) | two workspaces side by side: two columns, each a document above its own terminal, plus or minus the dock — no nesting needed |
| **Half a screen, split view** (881–1399px) | the full grid; a column that cannot get 160px is refused at creation, and existing columns squeeze on a later shrink, as today. No automatic re-stacking: it would flap at the boundary while the sidebar is dragged |
| **≤ 880px: phones, folded foldables, portrait tablets** | groups stack in reading order (today's "no side-by-side on a phone"); the dock always renders **last, as the sheet** — half (in flow, 50%) or full (fixed overlay), with ⤢ and the remembered mode, drawer and scrim above it — whatever `dock.side` says |
| **Landscape phone** (coarse pointer and height ≤ 500px) | the dock's half mode renders as full and `#term-max` hides; `kbTermMode` is not overwritten, so rotating back restores half |
| **Foldables** | unfolded, they are 700–840 CSS px wide (Pixel Fold ≈ 841×701, Galaxy Fold ≈ 830×714) — *under* the breakpoint, so folding and unfolding both render the phone way and lose nothing; in laptop posture that is already "document above, terminal below". Side-by-side columns on an unfolded book posture need a tablet tier (§12), which the grid supports when someone asks |
| **Tablets** (1024×768 landscape, 768×1024 portrait) | landscape renders the desktop grid, portrait stacks; coarse pointer means keybar and no terminal resizer (today); column handles work with a finger |
| **Rotation, window resize** | fractions rescale; minimums re-enforced; nothing persisted; every group size change refits the terminal it shows |

The plan is a function `plan(layout, {width, height, coarse}) → {display
order, which groups display, dock mode, keybar}` in `layout.js`, unit-tested
at each width above.

## 6. Interactions

- **Drag** (mouse): five zones on a group's body — left / right (new column
  beside it), top / bottom (new group above / below it in its column),
  centre (move into it) — and the strip for reorder or move. Any tab kind,
  terminals included. Dropping into the dock is a centre drop; the dock's
  edges are not zones. Dropping the last tab out of a group removes the
  group (never the workspace's last one); the dock never disappears —
  empty, it collapses. A drop that would exceed 8 groups or starve a column
  below 160px is refused with a toast.
- **Keyboard**: everything that exists (Alt+] / Alt+[ within the focused
  group, Alt+1…9, Alt+W, Alt+\ split right, Ctrl+` dock, Ctrl+Shift+` new
  terminal) plus **Alt+Shift+\ split below** (free in CodeMirror and xterm,
  matched by `e.code` so layout-independent) and **Alt+Z maximize /
  restore** the focused group (`term: true`; it takes Meta+Z from shell
  programs, which readline leaves unbound — decision 5). Alt+\ acts on the
  focused group's visible tab, terminals included, and requires the group to
  have more than one tab. No arrow chords: Alt+Shift+arrows and
  Ctrl+Alt+arrows belong to CodeMirror (copy line, add cursor) and to GNOME
  (workspaces); Alt+Enter is `ESC CR` in xterm and Claude Code's newline.
- **Palette**: "Move tab left / right / above / below / to the terminal
  panel", "Maximize group" / "Restore", "Focus next group", "Terminal
  panel: bottom / right / left". These ship with drag (step 3), not after
  it: they are the keyboard-only and screen-reader path.
- **Maximize** is render state (`maximized`), persisted beside the layout:
  the group fills `.main-col`, every other group and the dock hide, ⤢ in
  the strip and a double-click on the strip's empty area toggle it (the
  panel-title convention), Escape does nothing (it belongs to modals). On
  phones the dock's own ⤢ stays what it is: `kbTermMode`.
- **The dock's side** is layout state (`dock.side`), *not* a setting: it
  is a per-monitor choice (ultra-wide at work, laptop at home), settings
  roam per user across browsers, and a second source of truth for the root
  direction would have to re-root a layout it did not write. The palette
  command edits the layout; phones ignore it (the dock is always the sheet).
- **Touch**: no HTML5 drag. Long-press on a tab → "Move to terminal panel /
  Move to main / Maximize" (§12).
- **Accessibility**: split handles copy `#sb-resizer` (`role="separator"`,
  `aria-orientation`, `tabindex="0"`, `aria-label`, arrow keys resize);
  strips are `role="tablist"`, tabs `role="tab"` with `aria-selected`; the
  focused group gets a visible ring (today `body.split .pane.focused >
  .tabbar` is a 2px wash).
- **Deep links and the URL**: the URL is always `active.path`; focusing,
  moving or maximizing a terminal never changes it; `openDeepLink` on an
  already-open tab focuses that tab's group.

## 7. Persistence

`kbOpen` version 2 is the §3.2 record **plus every v1 field, derived from
it**: `tabs` (workspace groups flattened in reading order, `pane` = group
index), `panes` (column fractions, one per group), `paneActive`, `active`,
`terms`, `activeTerm`, `termOpen` (= `!dock.collapsed`). The hazard is not
new code reading v1 — it is *old* code reading v2: a cached bundle, or a
second window still on the previous build, reads `(s.tabs || [])`, restores
nothing, and its `restoreRest` … `finally { saveSession() }` overwrites the
record with an empty v1. With the shadow it restores the tabs and the
terminals, loses only the stacking (columns flatten to a row of groups),
and the next new-code load migrates again. Keep the shadow for two
releases; `test_ui_ux_round.py:215` reads top-level `kbOpen.active`, so it
keeps passing.

Reading v1 (`migrateV1`):

| v1 | v2 |
|---|---|
| `panes[i]` (grow, up to 6 used) | column `i` with `size` ∝ grow, one group each |
| `tabs[].pane` (index; missing → 0; beyond the columns → last) | tab in that column's group |
| `paneActive[i]` (path or null) | that group's `active` index, else 0 |
| `active` | `active` (and `focused` = its group) |
| `terms[]` (records, or bare sid strings from older saves) | the dock group's tabs, `name` when present |
| `activeTerm` (sid) | the dock group's `active` |
| `termOpen` (missing → true when terms exist) | `!dock.collapsed` |
| — | `dock.side = "bottom"`, `dock.size = 0.38`, `maximized = null` |

A record that fails `normalize()` on read falls back to the default layout
with its tabs flattened into it — never a blank screen.

**Restore order** stays two-phase, extended to terminals anywhere: at t=0
the whole grid and the dock exist, document tabs mount as today, and every
`term` tab gets its strip element at once (`$` icon, saved name,
`.connecting`) with an empty host; `warmTerminal()` fires when any term tab
exists anywhere (today: only `s.terms.length`). After whoami, `restoreRest`
attaches each terminal into its saved group. A dead session id is *not* a
lost tab: the first `connectTerm` has no `create=0`, so the pty is recreated
under the same name. The real failures are `canShell` false, the chunk
404-ing right after a deploy (`newTerminal` returns null), or a `gone` /
`detached` frame seconds later; each drops that tab, and `normalize()` runs
once after `mounts` settle (today's `removePane`-after-mounts), a later
`gone` behaving like closing that group's last tab. The gate is
`test_loading_ux::test_boot_restores_tabs_and_terminal_before_the_tree_arrives`.

**Two windows of one user** stay what they are: last writer wins on
`kbOpen`, and a session id attached by the second window gets a `detached`
frame in the first (7276). Not a regression of this work.

## 8. Code plan

Six steps, each shippable, each leaving the suite green.

**Step 1 — the grid underneath.** (~600 lines: `layout.js` 250 + its tests
150, render / restore rewiring 170, CSS 30)
- New `frontend/src/layout.js`, pure and DOM-free: `defaultLayout()`,
  `splitAt(layout, groupId, side)`, `removeGroup`, `moveTab`, `normalize`,
  `resize`, `plan(layout, viewport)` (§5), `serialize` (with the v1 shadow),
  `deserialize` / `migrateV1`. Unit tests with `node --test` — the first
  JavaScript unit tests in the repo — in a CI step right after the
  installer (Node is already there: `install.sh` builds the bundle with it).
- `app.js`: `panes[]` is produced by walking the layout; `insertPane` /
  `removePane` / `normalizeSplits` become `renderLayout(layout)`, which
  reconciles `#panes` (column wrappers, handles) without recreating group
  elements; `makeSplit` gains the `ns-resize` variant; `#term-resizer`
  persists `dock.size`; the dock is the layout's dock group rendered onto
  the existing `#terminal-panel`; terminals become `{kind: "term", sid,
  path: null}` tabs whose `el` is the existing `.term-content`; `terms`
  stays the runtime registry the tab points into; `saveSession` writes v2
  plus shadow; `restoreTabs` / `restoreRest` read v2 or migrate v1 and
  create terminals into their saved groups.
- Visible: nothing but the dock size surviving a reload. Tests: the nine
  `#panes >` selectors in `test_split_panes.py`.

**Step 2a — focus and dispatch.** (~150) `focused`, `lastTerm`,
`lastPane` as a record, `closest(".tab-content.term")`, `closeTab` →
`killTerminal`, `hasTab` on the focused group, the six "term tab" branches
of §3.5, `body.split` on workspace groups only, `body.term-focus`, the
formatting dock anchored to the active document's group.

**Step 2b — one strip.** (~200) Terminal tabs render through `tabEl` with
a `$` icon, `data-kind="term"`, `.current` never `.active`, the "⟳ "
reconnecting marker and click-retries kept; in the dock the strip is still
`#term-tabs`; `renderTermTabs`, `.term-tab` and `.term-x` go. Tests:
`.term-tab` → `.tab[data-kind="term"]`, `.term-x` → `.tab-x` in
`test_tabs_terminal.py`; `test_mobile.py` unchanged thanks to `.term-content`.

**Step 3a — drag anywhere.** (~350 incl. ~150 of browser tests) Top and
bottom zones, `.row-split` handles, terminal tabs draggable, drops into the
dock, dock-collapses-when-empty, the caps, the move-tab palette commands,
the separator / tablist roles. Tests: a terminal dragged beside a document
and typed into, a document dragged into the dock, a split below, reload
with a two-column stacked layout, Ctrl+` after emptying the dock, a
planted v1 record.

**Step 3b — maximize and the dock's side.** (~200) `maximized`, ⤢ /
double-click / Alt+Z / palette, "Terminal panel: right / left", the hidden
handle beside a collapsed dock. Tests: maximize and restore; the dock on
the right at 3440px; the sheet still last on a phone with `dock.side =
right`.

**Step 4 — the phone policy.** (~250) `plan()` wired: dock last, the
fixed keybar, the landscape rule, refit-all-displayed. Mobile tests keep
their assertions (`body.term-max` by default on a phone, the sheet fits the
viewport, the scrim wins over the sheet).

**Step 5 — polish and docs.** (~200) The touch long-press menu, "Focus
next group", ARCHITECTURE §8 rewritten around groups (this document becomes
the description), the kb-orientation skill's shortcut list.

Total ≈ 1.7–1.9k lines changed, about a quarter of `app.js`, of which
`layout.js` and its tests are new and everything else replaces code that
exists today.

## 9. Compatibility checklist — what the tests and CSS reach for

Everything below keeps resolving through steps 1–4 (by assignment, not by
markup, where the element moves).

- **Attributes**: `[data-testid="toggle-term"]` (user-menu button
  `#toggleterm`, hidden for viewers), `[data-testid="term-new"]`,
  `[data-testid="term-hide"]`, `[data-testid="term-keys"]`; `#term-hide`;
  `#terminal-panel:not([hidden])` and its `hidden` attribute everywhere;
  `#terminal-panel` bounding box `None` when hidden, width 390 / bottom ≤
  845 in term-max on a 390×844 phone; `document.elementFromPoint(370, 500)`
  is `#scrim` over the full-screen dock.
- **Terminal host**: `#terminal .xterm-rows`, `#terminal .xterm-screen`,
  `page.click("#terminal")`, `page.inner_text("#terminal")` (innerText skips
  `display:none` siblings, so a shared host is fine) — across
  `test_tabs_terminal`, `test_session_restore`, `test_mobile`,
  `test_launchers`, `test_multiplayer`, `test_loading_ux`, `test_dictation`.
- **Strips**: `#term-tabs .term-tab` counts and `.term-x` (until step 2b);
  `.tab.active` as a strict single-element locator and
  `.tab.active[data-path=…]`; `#tabbar .tab` counts (the dock strip is never
  `#tabbar`); `#editor` exactly one, with a stable bounding box while the
  tree arrives.
- **Keybar internals**: `#tk-ctrl.active`, `#tk-live.away`, `#term-keys
  button[data-k="up" | "cc" | "ctrl" | "top" | "live"]`, `#tk-mic` — the
  whole `#term-keys` subtree moves as one element.
- **Hooks**: `window.__kbterm` (an xterm `Terminal` instance),
  `window.__kbterms` (records with `.ws`, `.term.options.theme`),
  `kbOpen.active` (read), `kbOpen` removed by tests to force a clean start,
  `kbTermMode`, `.term-content` boxes in `test_mobile`.
- **CSS that assumed the singleton** and moves to `.tab-content.term`:
  `#terminal { flex; min-height: 0; overflow: hidden; padding }`, `#terminal
  { touch-action: none }`, `.terminal-panel .xterm { height: 100% }`, the
  `.term-content.term-offline` dimming, `background: var(--term-bg)`.
  Rules to add: `.row-split`, `body.pane-resizing` for `ns-resize`,
  `.main-col[data-dock="right"|"left"]`, `.col`, the `.pane + .pane` border
  rewritten for columns. `.pane { flex: 1 1 0 }` and `.terminal-panel {
  flex: 0 0 auto }` never meet on one element, because the dock keeps its
  own element.
- **Fixed overlay inside the tree**: no ancestor of `#panes` or
  `#terminal-panel` has `transform`, `filter`, `contain` or `will-change`
  (only `.sidebar`, `.mdbar`, `.modal-overlay`, `.ptt-bar` do, none an
  ancestor), so `position: fixed; z-index: 58` keeps working and the
  58 < 59 (scrim) < 60 (sidebar) order holds.

## 10. Risks and pre-existing behaviours

- **Artifacts reload when moved**: a tab move re-parents `t.el`; an iframe
  reloads (today's behaviour, kept). Group elements are never re-parented.
- **Presence and the access badge are header-only**: a document in another
  column shows no presence; pre-existing, kept by the "no per-group header"
  non-goal.
- **Two windows**: §7.
- **Stacked layouts on phones**: a browser's layout is its own
  (localStorage) and touch cannot create a split, so a phone shows several
  workspace groups only when a keyboard or mouse built them on that very
  device — a tablet, a foldable with a keyboard. Then they stack and scroll,
  110px each at worst, and "Maximize" is the escape. One-group-at-a-time is
  the upgrade if that ever matters (§12).
- **app.js size**: the model and the policy leave `app.js` for `layout.js`;
  the renderer replaces the pane code in place.

## 11. Decisions to take before step 1

1. **Grid, not tree** (§3.3). Costs one arrangement nobody has asked for.
   Recommended.
2. **Phones keep stacking** (§10) rather than one group at a time.
   Recommended: zero code for a case a phone cannot create by itself.
3. **Documents may live in the dock.** Recommended: it is what makes the
   dock "just a group".
4. **Tab shortcuts inside a terminal act on the focused group**: today
   Alt+] / Alt+[ / Alt+1…9 / Alt+W pressed in a terminal act on the
   *document* tabs; afterwards they cycle and close terminals when the dock
   is focused. The one visible behaviour change; no test presses them
   inside a terminal. Recommended.
5. **Alt+Z for maximize** with `term: true` (steals Meta+Z from shell
   programs; readline leaves it unbound, Emacs uses it for zap-to-char),
   or palette / ⤢ / double-click only. Recommended: Alt+Z.
6. **Caps**: 8 groups, 160px per column, 110px per group. Raise on
   evidence.

## 12. Later, deliberately

- A **tablet tier** (700–880px landscape): columns side by side with the
  drawer kept — for unfolded foldables in book posture. The grid supports
  it; the policy gets one more row.
- **Hinge snapping** via viewport segments: Chromium-only and effectively
  unshipped; a handle snapping to the fold is a small addition when it
  ships.
- **One group at a time on phones** (a switcher instead of a stack).
- **Touch long-press** move menu; **group-move / group-focus key chords**
  once a free set is agreed (arrows are taken).
- **Per-group ＋** for terminals, if dragging from the dock proves tedious.

## 13. The chrome to the edges (designed and built 2026-09-21)

What the first day of use showed: three bars sat above a document on a phone
(the top bar, the document bar, the tab strip) while the top bar was mostly
empty on a desktop; and the file panel had a button on a phone (☰) but only a
shortcut on a desktop (Alt+B). What was built, the same night, differs from
the sketch below in one respect: the document bar (path and actions) is one
element that lives inside the *active document's* group — under its strip on a
desktop, at the group's bottom on a phone — rather than one bar per group;
per-group actions remain the next step. On a desktop the brand row is the
file panel's head (☰ · logo · chat) and the editor column starts at the top of
the window, so the first strips are the top of the screen; the Pinned rows sit
under the search, above the chats and the files; ☰ collapses the panel
(remembered) and a corner control keeps ☰ and the chat reachable. A phone
shows the same three sections in the drawer. Since 2026-09-27 a phone drops
its brand row too: the first strip is the top of the screen, with a small ☰
fixed in its corner (ARCHITECTURE.md, "The chrome").

### Desktop

```
┌ ☰  Ollsoft Company OS    company / plans / business-plan.md              ▢ 💬 ┐
├ [ business-plan.md ×][ notes.md ×]          ⟲  KR  Rich | Source  ✎  ● ┤   ← the group's strip
│ document                                                                     │
```

- **The tab strips move to the top of their groups' area, directly under the
  top bar**: the document bar between them goes away. The strip is the
  first row of every group already; this only removes the row above it.
- **The breadcrumb moves into the top bar's empty middle**, describing the
  active document, as Notion and Finder title their windows. It stays one
  global line because it is the URL.
- **The document's actions move to the right end of its own group's strip**
  (history, presence, Rich | Source, the pencil, the access badge) — the way
  VS Code keeps editor actions in the tab bar. Per group, so a split shows
  each document's presence and mode, which the global bar never could.
  Implementation: `renderPresence`, `renderSyncBadge`, `updateModeUI` and
  `setAccessBadge` take a group and render into its `.group-actions`
  container; the elements move, the ids stay on the focused group's copies.
- **☰ is visible on the desktop too** and collapses the file panel to
  nothing (today's `nav-hidden`, remembered), with the same 44px hit target
  as on a phone; Alt+B stays. A collapsed panel leaves a slim edge to bring
  it back, and the search stays reachable from the palette.

### Phone

```
┌ ☰  Ollsoft Company OS                                          💬 ┐
├ [ business-plan.md ×][ notes.md ×]                                ┤   ← tabs right under the top bar
│ document                                                          │
│                                                                   │
├ business-plan.md         ⟲   KR   Rich | Source   ✎   ●          ┤   ← the document bar, at the thumb
└ ─────────────────────────────────────────────────────────────── ┘
```

- **Tabs on top, the document bar at the bottom**: Safari's bottom URL bar,
  for the same reason — the thumb lives there. The bar holds the path
  (truncated from the left) and the same actions as the desktop strip.
- **When the keyboard is up, the formatting row takes the bar's place**
  (it is already fixed to the keyboard's top edge), so nothing stacks; when
  the terminal sheet is full screen the bar hides (the sheet has its own
  header); the chat's composer stays where it is (the bar is a document's).
- **Group actions**: on a phone each group's strip keeps only the tabs; the
  bottom bar describes the focused group's document, and switching groups
  switches the bar.

### Order of work

1. Move the breadcrumb into the top bar and the actions into the strips
   (desktop); hide the old document bar. One `.group-actions` per group,
   render functions parameterised by group. The e2e tests that read
   `#doc-title`, `#mode-switch`, the badge and the avatars keep their ids
   (they move with the focused group).
2. The phone's bottom bar: a fixed row under the content, hidden while the
   keyboard row shows or the sheet is full screen; the tests in
   `test_mobile.py` that look for the mode switch find it there.
3. ☰ on the desktop; a remembered collapsed state; a 6px edge to reopen.

Left alone: the layout model, the dock, the chat, the persistence — this is
chrome, not structure.
