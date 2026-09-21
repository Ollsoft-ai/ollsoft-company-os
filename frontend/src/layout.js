// The layout: what the editor area holds and how it is divided.
//
// One shape for everything on screen — documents, artifacts, secrets,
// terminals and whatever view kind comes next (views.js): a WORKSPACE of
// COLUMNS, each column a vertical stack of GROUPS (a tab strip and one visible
// tab), plus one DOCK — the group that lives outside the workspace as a band
// at the bottom (the terminal panel of old) or a column on the right or left.
//
//   { v: 2,
//     columns: [ { size, groups: [ { id, size, active, tabs: [spec…] } ] } ],
//     dock:    { side, size, collapsed, group: { id: "dock", active, tabs } },
//     focused: <group id>, maximized: <group id> | null }
//
// A tab SPEC is what a view kind persists: {kind:"doc", path}, {kind:"term",
// sid, name}, … — plain JSON, validated by the kind on restore.
//
// This module is deliberately DOM-free: it is the part with rules (what a
// valid layout is, how an old session record maps onto it, how a screen
// shape renders it) and the part that `node --test` can check without a
// browser. The DOM renderer lives in app.js, next to the tab code.

export const DOCK_SIDES = ["bottom", "right", "left"];
export const MAX_GROUPS = 8;        // workspace and dock together
export const MIN_COL_PX = 160;      // a column narrower than this is not a column
export const MIN_GROUP_PX = 110;    // three lines of terminal
export const DOCK_DEFAULT_SIZE = 0.38;
export const PHONE_MAX_WIDTH = 880; // the one breakpoint, same as the CSS
export const LANDSCAPE_MAX_HEIGHT = 500;

let _seq = 0;
export function newGroupId() { return "g" + (++_seq) + "-" + Date.now().toString(36); }

export function group(id, tabs = [], size = 1) {
  return { id: id || newGroupId(), size, active: 0, tabs };
}

export function defaultLayout() {
  return {
    v: 2,
    columns: [{ size: 1, groups: [group("g1")] }],
    dock: { side: "bottom", size: DOCK_DEFAULT_SIZE, collapsed: true,
            group: { id: "dock", size: 1, active: 0, tabs: [] } },
    focused: "g1",
    maximized: null,
  };
}

export function allGroups(layout) {
  const out = [];
  for (const c of layout.columns) for (const g of c.groups) out.push(g);
  out.push(layout.dock.group);
  return out;
}
export function workspaceGroups(layout) {
  const out = [];
  for (const c of layout.columns) for (const g of c.groups) out.push(g);
  return out;
}
export function findGroup(layout, id) {
  return allGroups(layout).find((g) => g.id === id) || null;
}
export function countGroups(layout) { return allGroups(layout).length; }

const isObj = (x) => !!x && typeof x === "object" && !Array.isArray(x);
const num = (x, d) => (typeof x === "number" && Number.isFinite(x) && x > 0 ? x : d);

// Sizes are fractions that sum to 1; anything else is somebody's stale save.
function share(items) {
  const total = items.reduce((s, it) => s + num(it.size, 1), 0) || 1;
  for (const it of items) it.size = num(it.size, 1) / total;
}

function cleanTabs(tabs) {
  if (!Array.isArray(tabs)) return [];
  return tabs.filter((s) => isObj(s) && typeof s.kind === "string" && s.kind)
             .map((s) => ({ ...s }));
}

function cleanGroup(g, idFallback) {
  if (!isObj(g)) return null;
  const tabs = cleanTabs(g.tabs);
  const id = typeof g.id === "string" && g.id ? g.id : idFallback;
  const active = Number.isInteger(g.active) && g.active >= 0 && g.active < tabs.length ? g.active
    : tabs.length ? tabs.length - 1 : 0;
  return { id, size: num(g.size, 1), active, tabs, collapsed: !!g.collapsed };
}

// Make any record a valid layout, or say it cannot be one (null). Rules:
// - every column has a group, the workspace has a column; a group that has
//   no tabs is dropped — except the last workspace group, which may be empty;
// - group ids are unique (a duplicate gets a fresh one);
// - the dock always exists; any group may be collapsed (it renders as a
//   one-line handle until it is opened again);
// - at most MAX_GROUPS groups: the surplus groups fold into their neighbour
//   rather than vanish, so no tab is ever lost to a cap;
// - `focused` and `maximized` name groups that exist, or fall back.
export function normalize(input) {
  if (!isObj(input) || !Array.isArray(input.columns) || !isObj(input.dock)) return null;
  const seen = new Set();
  const uniq = (g) => {
    if (seen.has(g.id)) g.id = newGroupId();
    seen.add(g.id);
    return g;
  };
  const dockIn = cleanGroup(input.dock.group, "dock") || group("dock");
  dockIn.id = "dock";
  seen.add("dock");

  let columns = [];
  for (const c of input.columns) {
    if (!isObj(c) || !Array.isArray(c.groups)) continue;
    const groups = c.groups.map((g, i) => cleanGroup(g, newGroupId())).filter(Boolean)
      .filter((g) => g.tabs.length).map(uniq);
    if (groups.length) columns.push({ size: num(c.size, 1), groups });
  }
  if (!columns.length) columns = [{ size: 1, groups: [uniq(group(null))] }];

  // The cap: fold the last groups of the last columns into their predecessor.
  while (columns.reduce((n, c) => n + c.groups.length, 0) + 1 > MAX_GROUPS) {
    const col = columns[columns.length - 1];
    if (col.groups.length > 1) {
      const g = col.groups.pop();
      col.groups[col.groups.length - 1].tabs.push(...g.tabs);
    } else if (columns.length > 1) {
      const c = columns.pop();
      const prev = columns[columns.length - 1];
      prev.groups[prev.groups.length - 1].tabs.push(...c.groups[0].tabs);
    } else break;
  }
  share(columns);
  for (const c of columns) share(c.groups);
  for (const g of allGroups({ columns, dock: { group: dockIn } }))
    if (g.active >= g.tabs.length) g.active = Math.max(0, g.tabs.length - 1);

  const side = DOCK_SIDES.includes(input.dock.side) ? input.dock.side : "bottom";
  const size = Math.min(0.9, Math.max(0.1, num(input.dock.size, DOCK_DEFAULT_SIZE)));
  const dock = { side, size, collapsed: !!input.dock.collapsed, group: dockIn };
  const layout = { v: 2, columns, dock, focused: null, maximized: null };
  const ids = allGroups(layout).map((g) => g.id);
  layout.focused = ids.includes(input.focused) ? input.focused : columns[0].groups[0].id;
  layout.maximized = ids.includes(input.maximized) ? input.maximized : null;
  // a folded group is never the maximized one: nothing would be on screen
  if (layout.maximized === "dock" && dock.collapsed) layout.maximized = null;
  else if (layout.maximized && findGroup(layout, layout.maximized).collapsed) layout.maximized = null;
  return layout;
}

// The pre-2026-09 session record: a row of panes, tabs by pane index, and the
// terminal panel's own list. Every field was optional in practice.
//   { tabs:[{path, kind, pane}], panes:[grow…], paneActive:[path|null],
//     active, terms:[{sid,name}|sid], activeTerm, termOpen }
export function migrateV1(s) {
  if (!isObj(s)) return null;
  const grows = Array.isArray(s.panes) && s.panes.length ? s.panes : [1];
  const columns = grows.slice(0, 6).map((g) => ({ size: num(g, 1), groups: [group(null)] }));
  const tabs = Array.isArray(s.tabs) ? s.tabs : [];
  for (const t of tabs) {
    if (!isObj(t) || typeof t.path !== "string" || !t.path) continue;
    const kind = ["doc", "artifact", "secret"].includes(t.kind) ? t.kind : "doc";
    const i = Number.isInteger(t.pane) && t.pane >= 0 ? Math.min(t.pane, columns.length - 1) : 0;
    columns[i].groups[0].tabs.push({ kind, path: t.path });
  }
  const paneActive = Array.isArray(s.paneActive) ? s.paneActive : [];
  columns.forEach((c, i) => {
    const g = c.groups[0];
    const want = paneActive[i];
    const j = g.tabs.findIndex((t) => t.path === want);
    g.active = j >= 0 ? j : Math.max(0, g.tabs.length - 1);
  });
  const terms = (Array.isArray(s.terms) ? s.terms : []).map((o) =>
    typeof o === "string" ? { sid: o } : o).filter((o) => isObj(o) && typeof o.sid === "string" && o.sid);
  const dockTabs = terms.map((o) => ({ kind: "term", sid: o.sid, name: typeof o.name === "string" ? o.name : undefined }));
  const at = dockTabs.findIndex((t) => t.sid === s.activeTerm);
  const termOpen = s.termOpen === undefined ? dockTabs.length > 0 : !!s.termOpen;
  let focused = columns[0].groups[0].id;
  if (typeof s.active === "string") {
    for (const c of columns) for (const g of c.groups)
      if (g.tabs.some((t) => t.path === s.active)) focused = g.id;
  }
  return normalize({
    v: 2, columns,
    dock: { side: "bottom", size: DOCK_DEFAULT_SIZE, collapsed: !termOpen,
            group: { id: "dock", active: at >= 0 ? at : 0, tabs: dockTabs } },
    focused, maximized: null,
  });
}

// The v1 fields, derived from a layout, written NEXT TO it. The hazard of a
// new record is old code reading it: a cached bundle restores `(s.tabs||[])`
// — nothing — and then saves that nothing back. With the shadow it restores
// the tabs and the terminals and loses only the stacking.
export function shadowV1(layout) {
  const groups = workspaceGroups(layout);
  const tabs = [], paneActive = [];
  groups.forEach((g, i) => {
    let act = null;
    g.tabs.forEach((t, j) => {
      if (t.kind === "term" || typeof t.path !== "string") return;
      tabs.push({ path: t.path, kind: t.kind, pane: i });
      if (j === g.active) act = t.path;
    });
    paneActive.push(act);
  });
  const dockTerms = layout.dock.group.tabs.filter((t) => t.kind === "term");
  const activeTerm = dockTerms[layout.dock.group.active];
  return {
    tabs, panes: groups.map((g) => g.size), paneActive,
    terms: dockTerms.map((t) => ({ sid: t.sid, name: t.name })),
    activeTerm: activeTerm ? activeTerm.sid : null,
    termOpen: !layout.dock.collapsed,
  };
}

export function serialize(layout, active) {
  return { ...shadowV1(layout), active: active || null, ...layout };
}

// Read a session record of either version. Never throws, never returns
// nothing: garbage is the default layout.
export function parse(json) {
  let s;
  try { s = typeof json === "string" ? JSON.parse(json) : json; } catch (e) { return null; }
  if (!isObj(s)) return null;
  if (s.v === 2) return normalize(s) || migrateV1(s) || defaultLayout();
  return migrateV1(s) || defaultLayout();
}

// How a screen shape renders a layout. The layout is never rewritten for the
// screen: a phone stacks what a desktop built, the dock becomes a sheet, and
// a desktop gets it all back. This decides only what CSS cannot.
export function plan(layout, vp) {
  const width = num(vp && vp.width, 1280), height = num(vp && vp.height, 800);
  const coarse = !!(vp && vp.coarse);
  const phone = width <= PHONE_MAX_WIDTH;
  const landscapePhone = coarse && height <= LANDSCAPE_MAX_HEIGHT;
  const dockHasTabs = layout.dock.group.tabs.length > 0;
  let dockMode;
  if (layout.dock.collapsed) dockMode = "hidden";
  else if (phone) dockMode = landscapePhone ? "sheet-full" : "sheet";   // "sheet": half or full per the remembered mode
  else dockMode = layout.dock.side === "bottom" ? "band" : "column";
  const maximized = layout.maximized && findGroup(layout, layout.maximized) ? layout.maximized : null;
  return {
    phone, stack: phone, landscapePhone,
    dockSide: phone ? "bottom" : layout.dock.side,   // the sheet is always at the bottom
    dockMode, dockHasTabs,
    maximized,
    keybar: coarse,
    columnsFit: Math.max(1, Math.floor(width / MIN_COL_PX)),
  };
}

// Would a new column fit? The rule is "refuse now, squeeze later": creating a
// column that cannot give every column MIN_COL_PX is refused with a toast;
// a window that later shrinks squeezes what exists, as it always did.
export function canAddColumn(layout, workspaceWidth) {
  if (countGroups(layout) >= MAX_GROUPS) return { ok: false, why: "At most " + MAX_GROUPS + " groups" };
  if (num(workspaceWidth, 0) && (layout.columns.length + 1) * MIN_COL_PX > workspaceWidth)
    return { ok: false, why: "No room for another column at this width" };
  return { ok: true };
}
export function canAddGroup(layout) {
  if (countGroups(layout) >= MAX_GROUPS) return { ok: false, why: "At most " + MAX_GROUPS + " groups" };
  return { ok: true };
}
