// node --test tests/js — undo in a live-synced note (frontend/src/collabundo.js):
// what Yjs applies (the note loading, other people's edits) is not yours to
// undo. The sync is played the way yCollab does it: an observer on the Y.Text
// that dispatches the delta as a CodeMirror change.
import { test } from "node:test";
import assert from "node:assert/strict";

// the ESM builds, the same module instances collabundo.js resolves to
const nm = (p) => new URL("../../frontend/node_modules/" + p, import.meta.url).href;
const { EditorState } = await import(nm("@codemirror/state/dist/index.js"));
const { history, undo } = await import(nm("@codemirror/commands/dist/index.js"));
const Y = await import(nm("yjs/dist/yjs.mjs"));
const { notUndoableFromYjs } = await import("../../frontend/src/collabundo.js");

// An editor bound to a Y.Text: `state` follows every Yjs change, and a local
// edit goes the other way (as yCollab's update does, tagged as its own).
function editor(guard) {
  const ydoc = new Y.Doc();
  const ytext = ydoc.getText("content");
  const LOCAL = {};
  const ed = { ydoc, ytext };
  ed.state = EditorState.create({ doc: "", extensions: [history(), ...(guard ? [notUndoableFromYjs(ydoc)] : [])] });
  ytext.observe((event, ytr) => {
    if (ytr.origin === LOCAL) return;
    const changes = [];
    let pos = 0;
    for (const d of event.delta) {
      if (d.insert != null) { changes.push({ from: pos, to: pos, insert: d.insert }); }
      else if (d.delete != null) { changes.push({ from: pos, to: pos + d.delete }); pos += d.delete; }
      else pos += d.retain;
    }
    ed.state = ed.state.update({ changes }).state;
  });
  ed.local = (spec) => {
    const tr = ed.state.update(spec);
    ed.state = tr.state;
    ydoc.transact(() => {
      tr.changes.iterChanges((fa, ta, fb, tb, ins) => {
        if (ta > fa) ytext.delete(fb, ta - fa);
        if (ins.length) ytext.insert(fb, ins.toString());
      }, true);
    }, LOCAL);
  };
  ed.undo = () => undo({ state: ed.state, dispatch: (tr) => ed.local(tr) });
  return ed;
}

// what another client sends: a Yjs update made elsewhere
function remote(ed, fn) {
  const other = new Y.Doc();
  Y.applyUpdate(other, Y.encodeStateAsUpdate(ed.ydoc));
  const before = Y.encodeStateVector(other);
  fn(other.getText("content"));
  Y.applyUpdate(ed.ydoc, Y.encodeStateAsUpdate(other, before));
}

const NOTE = "| Task | Who |\n| --- | --- |\n| Ship it | alice |\n";
const at = (s) => NOTE.indexOf(s);

test("without the guard, an edit right after the load joins it, and undo empties the note", () => {
  const ed = editor(false);
  remote(ed, (t) => t.insert(0, NOTE));                        // the note arrives
  ed.local({ changes: { from: at("alice"), to: at("alice") + 5, insert: "peter" } });
  ed.undo();
  assert.equal(ed.state.doc.toString(), "", "this is the bug the guard is for");
});

test("an edit right after the load undoes to the loaded note", () => {
  const ed = editor(true);
  remote(ed, (t) => t.insert(0, NOTE));
  ed.local({ changes: { from: at("alice"), to: at("alice") + 5, insert: "peter" } });
  assert.match(ed.state.doc.toString(), /\| Ship it \| peter \|/);
  ed.undo();
  assert.equal(ed.state.doc.toString(), NOTE);
  assert.equal(ed.ytext.toString(), NOTE, "the live session got the undo too");
  ed.undo();
  assert.equal(ed.state.doc.toString(), NOTE, "nothing of yours is left to undo; the load stays");
});

test("undo leaves somebody else's edit alone, and still finds yours behind it", () => {
  const ed = editor(true);
  remote(ed, (t) => t.insert(0, "one\n"));
  ed.local({ changes: { from: 4, insert: "mine\n" } });
  remote(ed, (t) => t.insert(t.length, "theirs\n"));
  ed.undo();
  assert.equal(ed.state.doc.toString(), "one\ntheirs\n");
});
