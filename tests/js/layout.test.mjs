// node --test tests/js — the layout model has rules, and rules get tests
// that need no browser. Run from the repo root.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  defaultLayout, normalize, migrateV1, parse, serialize, shadowV1, plan,
  canAddColumn, canAddGroup, countGroups, workspaceGroups, MAX_GROUPS,
} from "../../frontend/src/layout.js";

const doc = (path) => ({ kind: "doc", path });
const term = (sid, name) => ({ kind: "term", sid, name });

test("the default is one empty group and a collapsed bottom dock", () => {
  const l = defaultLayout();
  assert.equal(l.columns.length, 1);
  assert.equal(l.columns[0].groups.length, 1);
  assert.deepEqual(l.columns[0].groups[0].tabs, []);
  assert.equal(l.dock.side, "bottom");
  assert.equal(l.dock.collapsed, true);
  assert.equal(l.focused, l.columns[0].groups[0].id);
  assert.equal(normalize(l).columns.length, 1);
});

test("normalize drops empty groups but keeps the last workspace group", () => {
  const l = normalize({
    v: 2,
    columns: [
      { size: 1, groups: [{ id: "a", tabs: [] }, { id: "b", tabs: [doc("x.md")] }] },
      { size: 1, groups: [{ id: "c", tabs: [] }] },
    ],
    dock: { side: "bottom", size: 0.4, collapsed: true, group: { id: "dock", tabs: [] } },
  });
  assert.equal(l.columns.length, 1);
  assert.deepEqual(l.columns[0].groups.map((g) => g.id), ["b"]);
  const empty = normalize({ v: 2, columns: [{ groups: [{ id: "a", tabs: [] }] }], dock: { group: {} } });
  assert.equal(empty.columns.length, 1);
  assert.equal(empty.columns[0].groups.length, 1, "the last workspace group may be empty");
});

test("normalize makes sizes fractions, ids unique and the dock always present", () => {
  const l = normalize({
    v: 2,
    columns: [
      { size: 3, groups: [{ id: "a", size: 2, tabs: [doc("1.md")] }, { id: "a", size: 2, tabs: [doc("2.md")] }] },
      { size: 1, groups: [{ id: "b", tabs: [doc("3.md")] }] },
    ],
    dock: { side: "sideways", size: 99, collapsed: false, group: { id: "not-dock", tabs: [term("s1")] } },
    focused: "nope", maximized: "nope",
  });
  assert.equal(l.columns[0].size + l.columns[1].size, 1);
  assert.equal(l.columns[0].size, 0.75);
  const g = l.columns[0].groups;
  assert.equal(g[0].size, 0.5);
  assert.notEqual(g[0].id, g[1].id, "a duplicate id is renamed");
  assert.equal(l.dock.side, "bottom");
  assert.equal(l.dock.size, 0.9, "the dock is clamped");
  assert.equal(l.dock.group.id, "dock");
  assert.equal(l.dock.group.tabs.length, 1);
  assert.equal(l.focused, "a", "focused falls back to the first group");
  assert.equal(l.maximized, null);
});

test("normalize folds groups over the cap into their neighbour, losing no tab", () => {
  const columns = [];
  for (let i = 0; i < 5; i++)
    columns.push({ size: 1, groups: [{ id: "c" + i + "a", tabs: [doc(i + "a.md")] }, { id: "c" + i + "b", tabs: [doc(i + "b.md")] }] });
  const l = normalize({ v: 2, columns, dock: { group: { tabs: [term("x")] } } });
  assert.ok(countGroups(l) <= MAX_GROUPS);
  const paths = workspaceGroups(l).flatMap((g) => g.tabs.map((t) => t.path)).sort();
  assert.equal(paths.length, 10, "every tab survives");
});

test("normalize rejects what is not a layout", () => {
  assert.equal(normalize(null), null);
  assert.equal(normalize({ v: 2 }), null);
  assert.equal(normalize({ v: 2, columns: "x", dock: {} }), null);
  assert.equal(normalize({ v: 2, columns: [], dock: "x" }), null);
});

test("a v1 record becomes a row of groups with the terminals in the dock", () => {
  const v1 = {
    tabs: [{ path: "company/a.md", kind: "doc", pane: 0 }, { path: "company/b.html", kind: "artifact", pane: 1 },
           { path: "company/c.md", kind: "doc", pane: 1 }, { path: "users/me/_secrets/k", kind: "secret", pane: 7 }],
    panes: [1, 3], paneActive: ["company/a.md", "company/c.md"], active: "company/c.md",
    terms: ["abc", { sid: "def", name: "claude" }], activeTerm: "def", termOpen: true,
  };
  const l = migrateV1(v1);
  assert.equal(l.columns.length, 2);
  assert.equal(l.columns[0].size, 0.25);
  assert.deepEqual(l.columns[0].groups[0].tabs, [doc("company/a.md")]);
  assert.equal(l.columns[1].groups[0].tabs.length, 3, "a pane index past the row lands in the last column");
  assert.equal(l.columns[1].groups[0].active, 1, "paneActive picks the visible tab");
  assert.equal(l.focused, l.columns[1].groups[0].id, "focused is the group of the active document");
  assert.deepEqual(l.dock.group.tabs, [{ kind: "term", sid: "abc", name: undefined }, term("def", "claude")]);
  assert.equal(l.dock.group.active, 1);
  assert.equal(l.dock.collapsed, false);
});

test("a v1 record with nothing in it is still a layout", () => {
  const l = migrateV1({});
  assert.equal(l.columns.length, 1);
  assert.equal(l.dock.collapsed, true, "no terminals → the dock starts collapsed");
  assert.equal(migrateV1({ terms: [{ sid: "x" }] }).dock.collapsed, false, "terminals without termOpen → open (the old default)");
  assert.equal(migrateV1({ terms: [{ sid: "x" }], termOpen: false }).dock.collapsed, true);
  assert.equal(migrateV1("junk"), null);
});

test("parse reads either version and never returns nothing", () => {
  assert.equal(parse("{not json"), null);
  assert.equal(parse('"x"'), null);
  assert.equal(parse("{}").columns.length, 1);
  const v2 = serialize(defaultLayout(), null);
  assert.equal(parse(JSON.stringify(v2)).v, 2);
  const broken = { v: 2, columns: "x", dock: {} , tabs: [{ path: "a.md", kind: "doc", pane: 0 }] };
  assert.equal(parse(broken).columns[0].groups[0].tabs[0].path, "a.md", "a broken v2 falls back to its own shadow");
});

test("the v1 shadow is derived from the layout so old bundles still restore", () => {
  const l = normalize({
    v: 2,
    columns: [
      { size: 1, groups: [{ id: "a", active: 1, tabs: [doc("1.md"), doc("2.md"), term("t1", "shell")] }] },
      { size: 1, groups: [{ id: "b", tabs: [{ kind: "artifact", path: "x.html" }] }, { id: "c", tabs: [doc("3.md")] }] },
    ],
    dock: { side: "right", size: 0.3, collapsed: false, group: { id: "dock", active: 0, tabs: [term("d1", "bash 1")] } },
    focused: "b",
  });
  const s = shadowV1(l);
  assert.deepEqual(s.tabs, [
    { path: "1.md", kind: "doc", pane: 0 }, { path: "2.md", kind: "doc", pane: 0 },
    { path: "x.html", kind: "artifact", pane: 1 }, { path: "3.md", kind: "doc", pane: 2 },
  ]);
  assert.deepEqual(s.paneActive, ["2.md", "x.html", "3.md"]);
  assert.equal(s.panes.length, 3);
  assert.deepEqual(s.terms, [{ sid: "d1", name: "bash 1" }], "only the dock's terminals — old code has one panel");
  assert.equal(s.activeTerm, "d1");
  assert.equal(s.termOpen, true);
  const full = serialize(l, "x.html");
  assert.equal(full.active, "x.html");
  assert.equal(full.v, 2);
  assert.equal(parse(JSON.stringify(full)).dock.side, "right");
  // and the shadow round-trips through the v1 migration with every tab intact
  const back = migrateV1(full);
  assert.equal(workspaceGroups(back).flatMap((g) => g.tabs).length, 4);
});

test("plan: a phone stacks and sheets, a desktop renders the layout, a landscape phone goes full", () => {
  const l = normalize({ v: 2, columns: [{ groups: [{ id: "a", tabs: [doc("1.md")] }] }],
    dock: { side: "right", size: 0.3, collapsed: false, group: { id: "dock", tabs: [term("x")] } } });
  const desk = plan(l, { width: 1920, height: 1080, coarse: false });
  assert.equal(desk.phone, false);
  assert.equal(desk.dockMode, "column");
  assert.equal(desk.dockSide, "right");
  assert.equal(desk.keybar, false);
  const phone = plan(l, { width: 390, height: 844, coarse: true });
  assert.equal(phone.phone, true);
  assert.equal(phone.stack, true);
  assert.equal(phone.dockMode, "sheet");
  assert.equal(phone.dockSide, "bottom", "the sheet is always at the bottom, whatever the side says");
  assert.equal(phone.keybar, true);
  const land = plan(l, { width: 844, height: 390, coarse: true });
  assert.equal(land.dockMode, "sheet-full");
  const shortDesk = plan(l, { width: 1200, height: 480, coarse: false });
  assert.equal(shortDesk.landscapePhone, false, "a short desktop window is not a phone");
  l.dock.collapsed = true;
  assert.equal(plan(l, { width: 390, height: 844, coarse: true }).dockMode, "hidden");
  const fold = plan(l, { width: 841, height: 701, coarse: true });
  assert.equal(fold.phone, true, "an unfolded foldable sits under the breakpoint");
  assert.equal(plan(l, { width: 1024, height: 768, coarse: true }).phone, false, "a landscape tablet renders the grid");
});

test("caps: columns need their minimum width now; groups have one cap", () => {
  const l = defaultLayout();
  assert.equal(canAddColumn(l, 1000).ok, true);
  assert.equal(canAddColumn(l, 300).ok, false);
  assert.equal(canAddColumn(l, 0).ok, true, "an unknown width does not refuse");
  const columns = [];
  for (let i = 0; i < 7; i++) columns.push({ groups: [{ id: "g" + i, tabs: [doc(i + ".md")] }] });
  const full = normalize({ v: 2, columns, dock: { group: { tabs: [] } } });
  assert.equal(countGroups(full), 8);
  assert.equal(canAddGroup(full).ok, false);
  assert.equal(canAddColumn(full, 5000).ok, false);
});

test("a folded group is never the maximized one", () => {
  const l = normalize({
    v: 2,
    columns: [
      { groups: [{ id: "a", tabs: [doc("1.md")], collapsed: true }] },
      { groups: [{ id: "b", tabs: [doc("2.md")] }] },
    ],
    dock: { group: { tabs: [] } },
    maximized: "a",
  });
  assert.equal(l.columns[0].groups[0].collapsed, true, "the fold is kept");
  assert.equal(l.maximized, null, "a maximized fold would show nothing");
  const ok = normalize({ v: 2, columns: [{ groups: [{ id: "b", tabs: [doc("2.md")] }] }], dock: { group: { tabs: [] } }, maximized: "b" });
  assert.equal(ok.maximized, "b");
});
