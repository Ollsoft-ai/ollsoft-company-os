// Text that reaches an open document from OUTSIDE the editor — an agent, vim,
// a script, anything that writes the file — blooms in where it lands and then
// keeps a soft wash and an underline, so nothing silently appears on the page.
// The glow waits for you: it starts to fade only once you are in the document
// (its cursor, or a click or tap on it), and is gone FADE_MS later. A removal
// leaves a thin tick where the text was, on the same clock.
//
// Who counts as outside: syncd merges a file's disk edits into the live doc as
// a CRDT client of its own, and that client has no presence. So an insert is
// external when its author has no awareness state — a colleague's typing is
// not marked, their caret is already there to watch. A deletion carries no
// author in Yjs at all; it is marked only when it travels with an external
// insert, or when nobody else has the document open to have made it.
//
// The bloom and the fade are driven from JS through custom properties on the
// editor, never a CSS animation: CodeMirror redraws a line's DOM whenever it
// likes (a keystroke in it, scrolling out and back), and every redraw would
// restart a keyframe from the top.
import { StateField, StateEffect } from "@codemirror/state";
import { EditorView, Decoration, WidgetType, ViewPlugin } from "@codemirror/view";

const FLASH_MS = 1100;    // the arrival bloom
const FADE_MS = 15000;    // from the moment you are in the document to gone
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)");

const addBatch = StateEffect.define();    // { id, spans: [[from, to]], cuts: [pos] }
const dropBatch = StateEffect.define();   // id

// Each arrival reads its own pair of properties, so two arrivals a few
// seconds apart fade on their own clocks. Unset, they mean "lit, no bloom".
const vars = (id) => `--a:var(--kbx-a${id},1);--f:var(--kbx-f${id},0)`;

class Cut extends WidgetType {
  constructor(id) { super(); this.id = id; }
  eq(o) { return o.id === this.id; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-ext-cut";
    s.setAttribute("style", vars(this.id));
    s.title = "Removed outside the editor";
    return s;
  }
}

// syncd trims an edit to the character, so "2025" -> "2026" arrives as "6"
// and "two" -> "three" as "hree". Glow whole words; a tick that lands inside
// one moves to its start.
const WORD = /[\p{L}\p{N}_]/u;
function toWords(doc, f, t) {
  const w = (a) => a >= 0 && a < doc.length && WORD.test(doc.sliceString(a, a + 1));
  while (w(f - 1) && w(f)) f--;
  while (w(t) && w(t - 1)) t++;
  return [f, t];
}

const extField = StateField.define({
  create: () => Decoration.none,
  update(deco, tr) {
    deco = deco.map(tr.changes);
    for (const e of tr.effects) {
      if (e.is(addBatch)) {
        const { id } = e.value, doc = tr.state.doc;
        const spans = e.value.spans.filter(([f, t]) => f < t && t <= doc.length).map(([f, t]) => toWords(doc, f, t));
        const cuts = e.value.cuts.filter((p) => p <= doc.length)
          .map((p) => (spans.find(([f, t]) => f < p && p < t) || [p])[0]);
        const mark = Decoration.mark({ class: "cm-ext", attributes: { style: vars(id) }, batch: id });
        const cut = Decoration.widget({ widget: new Cut(id), batch: id });
        deco = deco.update({ sort: true, add: [
          ...spans.map(([f, t]) => mark.range(f, t)),
          ...cuts.map((p) => cut.range(p)),
        ] });
      } else if (e.is(dropBatch)) {
        deco = deco.update({ filter: (_f, _t, v) => v.spec.batch !== e.value });
      }
    }
    return deco;
  },
  provide: (f) => EditorView.decorations.from(f),
});

// The clock. An arrival blooms the first time it is actually on screen — a
// background tab is display:none, a minimised window is document.hidden — and
// starts fading the first time you are in the document after that.
const fader = ViewPlugin.fromClass(class {
  constructor(view) {
    this.view = view;
    this.batches = new Map();   // id -> { shown, attend }: performance.now() stamps, null = not yet
    this.raf = 0;
    this.poked = false;
    this.wake = () => this.kick();
    document.addEventListener("visibilitychange", this.wake);
    this.io = new IntersectionObserver(this.wake);   // how a tab's first showing is noticed
    this.io.observe(view.dom);
  }
  update(u) {
    let added = false;
    for (const tr of u.transactions) {
      for (const e of tr.effects) {
        if (e.is(addBatch)) { this.batches.set(e.value.id, { shown: null, attend: null }); added = true; }
      }
    }
    if (added || u.focusChanged) this.kick();
  }
  kick() {
    if (!this.raf && this.batches.size) this.raf = requestAnimationFrame(() => this.frame());
  }
  frame() {
    this.raf = 0;
    const now = performance.now(), style = this.view.dom.style;
    const visible = !document.hidden && this.view.dom.getClientRects().length > 0;
    const looking = visible && (this.view.hasFocus || this.poked);
    this.poked = false;
    let busy = false;
    const gone = [];
    for (const [id, b] of this.batches) {
      if (b.shown === null) {
        if (!visible) continue;
        b.shown = now;
      }
      if (b.attend === null && looking) b.attend = now;
      const bloom = REDUCED.matches ? 0 : Math.max(0, 1 - (now - b.shown) / FLASH_MS);
      const fade = b.attend === null ? 0 : (now - b.attend) / FADE_MS;
      if (fade >= 1) { gone.push(id); continue; }
      // ease out of the bloom; hold the glow most of the fade, then let it go
      style.setProperty("--kbx-f" + id, (bloom * bloom).toFixed(3));
      style.setProperty("--kbx-a" + id, (1 - fade * fade).toFixed(3));
      if (bloom > 0 || b.attend !== null) busy = true;
    }
    if (gone.length) {
      for (const id of gone) {
        this.batches.delete(id);
        style.removeProperty("--kbx-f" + id);
        style.removeProperty("--kbx-a" + id);
      }
      this.view.dispatch({ effects: gone.map((id) => dropBatch.of(id)) });
    }
    if (busy) this.kick();
  }
  destroy() {
    cancelAnimationFrame(this.raf);
    this.io.disconnect();
    document.removeEventListener("visibilitychange", this.wake);
  }
}, {
  // a read-only document never takes focus: a click or a tap on it is the look
  eventHandlers: { pointerdown() { this.poked = true; this.kick(); } },
});

export function externalChanges() {
  return [extField, fader];
}

let seq = 0;

// Watch `ytext` for edits that came from outside the editor and mark them in
// `view`. Register it AFTER the view exists: yCollab's own observer, added as
// the view is built, must have applied the change before positions are read.
// `ready()` is false until the first full sync — that sync delivers the whole
// document as one remote change, and none of it is news.
export function watchExternal(view, ytext, awareness, ready) {
  const present = (client) => !!awareness.getStates().get(client)?.user;
  ytext.observe((ev, tr) => {
    if (tr.local || !ready()) return;
    // Which clients wrote in this transaction. Deletions advance nobody's
    // clock, so `wrote` empty means it was nothing but deletions.
    let wrote = false, outside = false;
    tr.afterState.forEach((clock, client) => {
      if (clock === (tr.beforeState.get(client) || 0)) return;
      wrote = true;
      if (!present(client)) outside = true;
    });
    const spans = [];
    if (outside) {
      let pos = 0;
      for (let it = ytext._start; it; it = it.right) {
        if (it.deleted || !it.countable) continue;
        if (ev.adds(it) && !present(it.id.client)) {
          const last = spans[spans.length - 1];
          if (last && last[1] === pos) last[1] += it.length;
          else spans.push([pos, pos + it.length]);
        }
        pos += it.length;
      }
    }
    const alone = ![...awareness.getStates()].some(([id, s]) => id !== awareness.clientID && s.user);
    const cuts = [];
    if (outside || (!wrote && alone)) {
      let pos = 0;   // in the NEW text: inserts move it, deletions do not
      for (const d of ev.delta) {
        if (d.retain) pos += d.retain;
        else if (d.insert != null) pos += typeof d.insert === "string" ? d.insert.length : 1;
        else if (d.delete) {
          // a like-for-like replacement says enough with its new text glowing;
          // a tick only where more went than came
          const s = spans.find(([f, t]) => f <= pos && pos <= t);
          if (!s || d.delete > s[1] - s[0]) cuts.push(pos);
        }
      }
    }
    if (spans.length || cuts.length) view.dispatch({ effects: addBatch.of({ id: ++seq, spans, cuts }) });
  });
}
