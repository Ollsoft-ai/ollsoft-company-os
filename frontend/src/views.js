// The registry of VIEW KINDS — what a tab can show — and the two small
// extension points beside it: commands (the palette and the shortcut sheet)
// and slots (places in the chrome a module may put a button).
//
// The shell (app.js) knows how to arrange tabs in groups, drag them, persist
// them and give them focus; it does not know what is inside one. A kind
// registers itself once — documents, artifacts, secrets and terminals from
// app.js, the agent chat from chat.js — and from then on `openView(kind,
// spec)` opens it, the layout persists its spec, and a reload restores it
// through the kind's own `restore`. Adding a kind is one registration and no
// change to the shell; that is the whole point.
//
// A kind:
//   {
//     label: "Terminal",                 // singular, for menus and toasts
//     icon(t) -> svg string,             // the tab's icon
//     title(t) -> string,                // the tab's label
//     tooltip(t) -> string,              // hover text (a path, a session…)
//     key(spec) -> string | null,        // identity: opening the same key twice
//                                        //   activates the existing tab
//     restore(spec) -> spec | null,      // validate a persisted spec at boot;
//                                        //   null drops it (never a crash)
//     serialize(t) -> spec,              // what to persist with the layout
//     async open(t, spec),               // mount into t.el; t is the shell's
//                                        //   tab record (id, kind, el, paneId…)
//     close(t),                          // tear down sockets/editors; the shell
//                                        //   removes t.el afterwards
//     activate(t), deactivate(t),        // optional: shown / hidden
//     resize(t),                         // optional: the group changed size
//     focus(t),                          // optional: put the caret there
//     isActiveDocument: false,           // true: activating it sets the global
//                                        //   `active` the header describes
//     placement: { target: "active" | "dock" | "side", side: "right", size: 0.3 },
//   }
const KINDS = new Map();

export function registerView(kind, def) {
  if (!kind || typeof kind !== "string") throw new Error("registerView: kind must be a string");
  if (typeof def.open !== "function") throw new Error("registerView(" + kind + "): open() is required");
  const full = {
    label: kind, icon: () => "", title: (t) => t.name || kind, tooltip: (t) => t.name || kind,
    key: (spec) => (spec && typeof spec.path === "string" ? spec.path : null),
    restore: (spec) => spec, serialize: (t) => ({ kind }),
    close: () => {}, activate: () => {}, deactivate: () => {}, resize: () => {}, focus: () => {},
    isActiveDocument: false, placement: { target: "active" },
    ...def, kind,
  };
  KINDS.set(kind, full);
  return full;
}
export const viewKind = (kind) => KINDS.get(kind) || null;
export const viewKinds = () => [...KINDS.values()];

// Commands: what the palette lists and the shortcut sheet documents. app.js
// seeds its own; a module adds its own the same way. `keys` use the same
// combo grammar as app.js's BINDINGS ("Alt+Z", "Mod+Shift+P"); `term: true`
// means the combo also fires inside a terminal; `when()` hides it.
const COMMANDS = [];
const commandListeners = new Set();
export function registerCommand(cmd) {
  if (!cmd || !cmd.id || typeof cmd.run !== "function") throw new Error("registerCommand: id and run() are required");
  const i = COMMANDS.findIndex((c) => c.id === cmd.id);
  if (i >= 0) COMMANDS.splice(i, 1, cmd); else COMMANDS.push(cmd);
  for (const fn of commandListeners) fn(cmd);
  return cmd;
}
export const commands = () => COMMANDS.slice();
export function onCommand(fn) { commandListeners.add(fn); return () => commandListeners.delete(fn); }

// Slots: named places in the chrome. A module asks for one and appends its
// element; the shell decides where the slot renders (a phone folds the
// topbar's actions into the drawer, and a slot moves with it).
const SLOTS = new Map();
export function defineSlot(name, el) { SLOTS.set(name, el); return el; }
export function slot(name) {
  const el = SLOTS.get(name);
  if (!el) throw new Error("no slot named " + name);
  return el;
}
export const slotNames = () => [...SLOTS.keys()];
