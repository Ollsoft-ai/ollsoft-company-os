// Who wrote each line: a thin stripe beside it — you, a colleague, or a
// machine. Click the stripe for who, when, and the change itself.
//
// The answer is `git blame` of the committed file (/api/vc/blame), so it costs
// the markdown nothing: authorship lives in the history the knowledgebase
// keeps anyway. A machine is any author that is not a login account — in
// practice kb-syncd, the name a change is committed under when it reached the
// file outside the editor (an agent, a script, a sync).
//
// The editor holds the LIVE text, a few seconds ahead of the last commit, so
// blame and buffer are matched line by line (a Myers diff over whole lines).
// A line in both takes its commit's author; a line you typed since is yours at
// once; anything else — a colleague's or an agent's edit still on its way to a
// commit — stays unstriped until the commit lands and the next fetch names it.
// Blank lines carry no stripe: a reader attributes paragraphs, not the gaps.
import { StateField, StateEffect, RangeSetBuilder } from "@codemirror/state";
import { EditorView, Decoration, ViewPlugin } from "@codemirror/view";

const SETTLE_MS = 6000;   // syncd commits after 0.25 s flush + 4 s quiet; this is that plus slack
const RETRIES = 3;        // lines still unmatched: ask again at 12 s, 24 s, 48 s
const MAX_D = 1500;       // edit distance past which the diff gives up (its trace is O(D²))
const LOCAL = ["input", "delete", "move", "undo", "redo"];

const setBlame = StateEffect.define();

// The commit index rides on the line as data-blame — how a click finds its
// card. -1 = typed here, not in history yet.
function lineDeco(kind, ci) {
  return Decoration.line({ class: "cm-blame cm-blame-" + kind, attributes: { "data-blame": String(ci) } });
}
const MINE = lineDeco("me", -1);

// Lines a transaction changed, as start offsets in the new document. A change
// that begins at a line's end (Enter, deleting the next line) or ends at a
// line's start (a line inserted above) leaves that line as it was, so it
// keeps its author.
function touchedLines(tr) {
  const doc = tr.state.doc, old = tr.startState.doc, out = new Set();
  tr.changes.iterChangedRanges((fa, ta, fb, tb) => {
    let first = doc.lineAt(fb).number, last = doc.lineAt(tb).number;
    const nf = doc.lineAt(fb), of = old.lineAt(fa);
    if (fb === nf.to && fa === of.to && nf.text === of.text) first++;
    const nl = doc.lineAt(tb), ol = old.lineAt(ta);
    if (tb === nl.from && ta === ol.from && nl.text === ol.text) last--;
    for (let n = first; n <= last; n++) out.add(doc.line(n).from);
  });
  return out;
}

const blameField = StateField.define({
  create: () => ({ commits: [], deco: Decoration.none }),
  update(value, tr) {
    let { commits, deco } = value;
    if (tr.docChanged) {
      deco = deco.map(tr.changes);
      // A changed line's old author no longer vouches for it. Your own typing
      // is yours straight away; anything else (a colleague's keystrokes, an
      // agent's write arriving through the CRDT) waits for its commit.
      const touched = touchedLines(tr);
      if (touched.size) {
        const doc = tr.state.doc;
        const starts = [...touched].sort((a, b) => a - b);
        const local = LOCAL.some((e) => tr.isUserEvent(e));
        deco = deco.update({
          filter: (from) => !touched.has(doc.lineAt(from).from),
          filterFrom: starts[0],
          filterTo: doc.lineAt(starts[starts.length - 1]).to,
          add: local ? starts.filter((p) => /\S/.test(doc.lineAt(p).text)).map((p) => MINE.range(p)) : [],
        });
      }
    }
    for (const e of tr.effects) if (e.is(setBlame)) ({ commits, deco } = e.value);
    return { commits, deco };
  },
  provide: (f) => EditorView.decorations.from(f, (v) => v.deco),
});

// For each line of b (the buffer), the index of the line of a (the commit) it
// is, or -1. Common ends first — the usual case, the buffer being the commit
// plus a line or two, never reaches the diff — then Myers' O(ND) over the
// middle (shortest edit script, walked back for its matches).
export function matchLines(a, b) {
  const map = new Int32Array(b.length).fill(-1);
  let s = 0, ea = a.length, eb = b.length;
  while (s < ea && s < eb && a[s] === b[s]) { map[s] = s; s++; }
  while (ea > s && eb > s && a[ea - 1] === b[eb - 1]) { ea--; eb--; map[eb] = ea; }
  const n = ea - s, m = eb - s;
  if (!n || !m) return map;
  const max = Math.min(n + m, MAX_D), off = max + 1;
  const v = new Int32Array(2 * max + 3);
  const trace = [];   // trace[d]: v[k] for k in [-d-1, d+1], as it stood before step d
  let found = -1;
  for (let d = 0; d <= max && found < 0; d++) {
    trace.push(v.slice(off - d - 1, off + d + 2));
    for (let k = -d; k <= d; k += 2) {
      let x = (k === -d || (k !== d && v[off + k - 1] < v[off + k + 1])) ? v[off + k + 1] : v[off + k - 1] + 1;
      let y = x - k;
      while (x < n && y < m && a[s + x] === b[s + y]) { x++; y++; }
      v[off + k] = x;
      if (x >= n && y >= m) { found = d; break; }
    }
  }
  if (found < 0) return map;          // too different: only the common ends are matched
  let x = n, y = m;
  for (let d = found; d >= 0; d--) {
    const t = trace[d], at = (k) => t[k + d + 1], k = x - y;
    const pk = (k === -d || (k !== d && at(k - 1) < at(k + 1))) ? k + 1 : k - 1;
    const px = at(pk), py = px - pk;
    while (x > px && y > py) { x--; y--; map[s + y] = s + x; }
    x = px; y = py;
  }
  return map;
}

// Fetched blame -> decorations for the buffer as it is now.
function build(state, data, me) {
  const doc = state.doc, prev = state.field(blameField).deco;
  const cur = [];
  for (let i = 1; i <= doc.lines; i++) cur.push(doc.line(i).text);
  const match = matchLines(data.lines.map((l) => l[1]), cur);
  const kinds = data.commits.map((c) => (c.machine ? "machine" : c.author === me ? "me" : "other"));
  const decos = new Map();
  const b = new RangeSetBuilder();
  let pending = 0;
  for (let j = 0; j < cur.length; j++) {
    if (!/\S/.test(cur[j])) continue;
    const from = doc.line(j + 1).from;
    if (match[j] >= 0) {
      const ci = data.lines[match[j]][0];
      if (!decos.has(ci)) decos.set(ci, lineDeco(kinds[ci], ci));
      b.add(from, from, decos.get(ci));
      continue;
    }
    pending++;
    // still on its way to a commit: if you typed it, it stays yours meanwhile
    let mine = false;
    prev.between(from, from, (f, _t, v) => { if (f === from && v === MINE) mine = true; });
    if (mine) b.add(from, from, MINE);
  }
  return { deco: b.finish(), pending };
}

function fetcher(opts) {
  return ViewPlugin.fromClass(class {
    constructor(view) {
      this.view = view; this.timer = 0; this.tries = 0; this.seq = 0; this.loaded = false;
      this.onVis = () => { if (!document.hidden && this.stale) this.schedule(0); };
      document.addEventListener("visibilitychange", this.onVis);
      this.schedule(0);
    }
    update(u) {
      if (!u.docChanged) return;
      this.tries = 0;
      // before the first answer (the CRDT's initial sync arrives as a change)
      // ask at once; after it, wait for the commit this edit is becoming
      this.schedule(this.loaded ? SETTLE_MS : 300);
    }
    schedule(ms) { clearTimeout(this.timer); this.timer = setTimeout(() => this.load(), ms); }
    async load() {
      if (!this.view.state.doc.length) return;          // not synced yet, or empty: nothing to credit
      if (document.hidden) { this.stale = true; return; }
      this.stale = false;
      const seq = ++this.seq;
      let data;
      try {
        const r = await fetch("/api/vc/blame?path=" + encodeURIComponent(opts.path()));
        if (!r.ok) return;        // not versioned, or a hub without blame: no stripes, no noise
        data = await r.json();
      } catch (e) { return; }
      if (seq !== this.seq || this.dead) return;
      this.loaded = true;
      const { deco, pending } = build(this.view.state, data, opts.me());
      this.view.dispatch({ effects: setBlame.of({ commits: data.commits, deco }) });
      if (pending && this.tries < RETRIES) this.schedule(SETTLE_MS * 2 ** ++this.tries);
    }
    destroy() {
      this.dead = true;
      clearTimeout(this.timer);
      document.removeEventListener("visibilitychange", this.onVis);
    }
  });
}

// ---- the card a click on a stripe opens --------------------------------------
const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
function ago(ts) {
  let d = ts - Date.now() / 1000;
  for (const [n, unit] of [[60, "second"], [60, "minute"], [24, "hour"], [7, "day"], [4.35, "week"], [12, "month"]]) {
    if (Math.abs(d) < n) return rtf.format(Math.round(d), unit);
    d /= n;
  }
  return rtf.format(Math.round(d), "year");
}

let pop = null;
function closePop() {
  if (!pop) return;
  pop.remove(); pop = null;
  document.removeEventListener("mousedown", onOutside, true);
  document.removeEventListener("keydown", onKey, true);
}
function onOutside(e) { if (pop && !pop.contains(e.target)) closePop(); }
function onKey(e) { if (e.key === "Escape") closePop(); }

function el(cls, text) {
  const d = document.createElement("div");
  d.className = cls;
  if (text) d.textContent = text;
  return d;
}

function showCard(c, x, y, opts) {
  closePop();
  const me = opts.me();
  const kind = !c ? "me" : c.machine ? "machine" : c.author === me ? "me" : "other";
  pop = el("ctx-menu blame-pop cm-blame-" + kind);
  pop.setAttribute("data-testid", "blame-pop");
  const head = pop.appendChild(el("bp-head"));
  const who = head.appendChild(el("bp-who"));
  who.append(el("bp-dot"), !c ? "You" : kind === "me" ? `You (${c.author})` : c.author);
  if (!c) {
    head.appendChild(el("bp-when", "Just now — not in history yet"));
  } else {
    const when = new Date(c.ts * 1000);
    head.appendChild(el("bp-when", `${when.toLocaleDateString()} ${when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · ${ago(c.ts)}`));
    if (c.machine) head.appendChild(el("bp-what", "A machine: changed outside the editor — an agent, a script or a sync."));
    const sep = pop.appendChild(el("ctx-sep"));
    sep.setAttribute("role", "separator");
    const btn = document.createElement("button");
    btn.className = "ctx-item";
    btn.setAttribute("data-testid", "blame-show-change");
    btn.innerHTML = "<span></span>";
    btn.querySelector("span").textContent = "Show this change";
    btn.addEventListener("click", () => { closePop(); opts.showChange(c.rev); });
    pop.appendChild(btn);
  }
  document.body.appendChild(pop);
  const r = pop.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(x + 10, innerWidth - r.width - 8)) + "px";
  pop.style.top = Math.max(8, Math.min(y + 10, innerHeight - r.height - 8)) + "px";
  document.addEventListener("mousedown", onOutside, true);
  document.addEventListener("keydown", onKey, true);
}

// The stripe is a ::before in the line's padding, so a click on it lands on
// the line element itself: claim it only when it is within the stripe's box.
function clicks(opts) {
  return EditorView.domEventHandlers({
    mousedown(e, view) {
      const line = e.target;
      if (e.button !== 0 || !(line instanceof HTMLElement) || !line.classList.contains("cm-blame")) return false;
      const st = getComputedStyle(line, "::before");
      const x0 = line.getBoundingClientRect().left + line.clientLeft + parseFloat(st.left);
      const w = parseFloat(st.width) + parseFloat(st.paddingLeft) + parseFloat(st.paddingRight);
      if (!(e.clientX >= x0 - 2 && e.clientX <= x0 + w + 2)) return false;
      e.preventDefault();
      const ci = Number(line.getAttribute("data-blame"));
      showCard(ci < 0 ? null : view.state.field(blameField).commits[ci] || null, e.clientX, e.clientY, opts);
      return true;
    },
  });
}

// opts: path() — the document's path now (a tab can be renamed under it);
// me() — who is looking; showChange(rev) — open that version's diff.
export function blame(opts) {
  return [blameField, fetcher(opts), clicks(opts)];
}
