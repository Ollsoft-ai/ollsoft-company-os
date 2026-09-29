// The rich markdown view: what a document looks like while you are writing it.
//
// Widgets (checkboxes, bullets, rules, copy buttons, media, tables), the table
// state field, the live-preview decorations that hide the markup you are not
// standing in, the hanging indent that keeps a wrapped list item under its own
// first word, @mention autocomplete and highlighting, and the highlight style.
// Everything here is about TEXT and CodeMirror; nothing in it knows about
// tabs, the CRDT, the file tree or a session.
//
// That separation is the point. `app.js` mounts this with Yjs behind it and
// the public-link service mounts the same thing with a plain save, so a
// stranger reading a shared document sees what a colleague sees and a fix to
// the writing surface lands in both at once. Whatever the two hosts differ on
// — how to toast, how to ask, how a menu is drawn, how a relative path becomes
// a URL, who may be @mentioned — arrives through `init()`.
import { StateField, StateEffect } from "@codemirror/state";
import { EditorView, Decoration, WidgetType, ViewPlugin } from "@codemirror/view";
import { undo, redo } from "@codemirror/commands";
import { HighlightStyle, syntaxTree, forceParsing } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";

const svgIcon = (paths) =>
  '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  paths + "</svg>";

// What a host that says nothing does. Every one of these is replaceable, and
// the defaults are chosen so a bare mount still WORKS rather than throwing.
let host = {
  toast: (msg) => console.warn("richview:", msg),      // eslint-disable-line no-console
  confirm: async (msg) => window.confirm(msg),
  prompt: async (msg, value) => window.prompt(msg, value || ""),
  openPath: (rel) => { window.location.href = "/" + rel; },
  // a path as written in the document -> a URL this surface can actually GET
  mediaUrl: (path) => "/api/attachment?path=" + encodeURIComponent(path),
  principals: async () => [],       // who an @mention may name; nobody by default
  menu: (items, x, y) => defaultMenu(items, x, y),
  icons: {
    plus: svgIcon('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'),
    trash: svgIcon('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
    pencil: svgIcon('<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>'),
  },
};
export function init(h) { host = { ...host, ...h, icons: { ...host.icons, ...(h.icons || {}) } }; }

// A context menu for a host that has none of its own. Same markup and classes
// as the app's, because both load the same stylesheet.
let _menu = null;
function closeDefaultMenu() { if (_menu) { _menu.remove(); _menu = null; } }
function defaultMenu(items, x, y) {
  closeDefaultMenu();
  const m = document.createElement("div");
  m.className = "ctx-menu";
  m.setAttribute("data-testid", "ctx-menu");
  for (const it of items) {
    if (it === "-") { m.appendChild(Object.assign(document.createElement("div"), { className: "ctx-sep" })); continue; }
    const b = document.createElement("button");
    b.className = "ctx-item" + (it.danger ? " danger" : "");
    b.innerHTML = (it.icon || "") + "<span></span>";
    b.querySelector("span").textContent = it.label;
    b.addEventListener("click", (e) => { e.stopPropagation(); closeDefaultMenu(); it.fn(); });
    m.appendChild(b);
  }
  document.body.appendChild(m);
  const r = m.getBoundingClientRect();
  m.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  m.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  _menu = m;
  setTimeout(() => {
    document.addEventListener("mousedown", closeDefaultMenu, { once: true });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDefaultMenu(); }, { once: true });
  }, 0);
}

// Markdown source view: content stays ink; the machinery (marks, urls, code)
// recedes. Every colour is a stylesheet token, so a theme change retints the
// editor live — CodeMirror writes these values into its own CSS rules, and a
// var() resolves at paint time.
const mdHighlight = HighlightStyle.define([
  { tag: t.heading, color: "var(--md-heading)", fontWeight: "600" },
  { tag: t.strong, color: "var(--ink)", fontWeight: "600" },
  { tag: t.emphasis, color: "var(--ink)", fontStyle: "italic" },
  { tag: t.strikethrough, color: "var(--muted)", textDecoration: "line-through" },
  { tag: t.link, color: "var(--accent)" },
  { tag: t.url, color: "var(--md-url)" },
  { tag: t.monospace, color: "var(--md-code)" },
  { tag: t.quote, color: "var(--muted)", fontStyle: "italic" },
  { tag: t.meta, color: "var(--faint)" },
  { tag: t.processingInstruction, color: "var(--faint)" },
  { tag: t.contentSeparator, color: "var(--accent)" },
]);

// ═══ Rich markdown: live preview on the SAME text document ══════════════════
// The rendered mode is a decoration layer over the markdown source — never a
// second document model. Headings/bold/links/images/todos render in place and
// each construct's raw syntax reappears exactly while your selection touches
// it (the Obsidian model). Because the markdown stays the single source of
// truth, multiplayer, vim/agent merges, the indexer and todos all keep
// working identically in both modes.

const HIDE = Decoration.replace({});

class CheckboxWidget extends WidgetType {
  constructor(checked) { super(); this.checked = checked; }
  eq(o) { return o.checked === this.checked; }
  toDOM(view) {
    const wrap = document.createElement("span");
    wrap.className = "cm-task";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "cm-task-toggle";
    box.checked = this.checked;
    box.disabled = view.state.readOnly;
    box.addEventListener("mousedown", (e) => e.preventDefault());
    box.addEventListener("click", (e) => {
      e.preventDefault();
      if (view.state.readOnly) return;
      const line = view.state.doc.lineAt(view.posAtDOM(wrap));
      const m = line.text.match(/^(\s*[-*+]\s\[)([ xX])(\])/);
      if (!m) return;
      const at = line.from + m[1].length;
      view.dispatch({ changes: { from: at, to: at + 1, insert: m[2] === " " ? "x" : " " } });
    });
    wrap.appendChild(box);
    return wrap;
  }
  ignoreEvent() { return true; }
}

class BulletWidget extends WidgetType {
  eq() { return true; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-bullet"; s.textContent = "•";
    return s;
  }
}

class HRWidget extends WidgetType {
  eq() { return true; }
  toDOM() {
    const s = document.createElement("span");
    s.className = "cm-hr-line";
    return s;
  }
}

// Floating "copy" affordance on a fenced code block — copies the code between
// the fences, never the fences themselves.
class CopyWidget extends WidgetType {
  constructor(code) { super(); this.code = code; }
  eq(o) { return o.code === this.code; }
  toDOM() {
    const b = document.createElement("button");
    b.className = "cm-code-copy"; b.type = "button";
    b.textContent = "⧉ copy"; b.title = "Copy code block";
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      navigator.clipboard.writeText(this.code).then(() => {
        b.textContent = "✓ copied"; b.classList.add("done");
        setTimeout(() => { b.textContent = "⧉ copy"; b.classList.remove("done"); }, 1300);
      }, () => host.toast("could not copy to clipboard", "err"));
    });
    return b;
  }
  ignoreEvent() { return true; }
}

// The same gesture a fenced block has had, shrunk onto a `code span`. Inline
// code in these documents is almost always the thing you were going to retype
// by hand — a path, a flag, an account name — and retyping is where the typo
// comes from. Deliberately faint until the span is hovered: inline code is
// everywhere, and a solid button on every one of them would be noise.
const INLINE_COPY_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<rect x="9" y="9" width="12" height="12" rx="2"/>' +
  '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const INLINE_DONE_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<polyline points="20 6 9 17 4 12"/></svg>';

class InlineCopyWidget extends WidgetType {
  constructor(text) { super(); this.text = text; }
  eq(o) { return o.text === this.text; }
  toDOM() {
    // A <span role=button>, not a <button>: this sits INSIDE the contenteditable
    // line, where a real button drags the line's baseline around.
    const b = document.createElement("span");
    b.className = "cm-inline-copy";
    b.setAttribute("data-testid", "inline-copy");
    b.setAttribute("role", "button");
    b.setAttribute("aria-label", "Copy " + this.text);
    b.title = "Copy";
    b.innerHTML = INLINE_COPY_SVG;
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      navigator.clipboard.writeText(this.text).then(() => {
        b.innerHTML = INLINE_DONE_SVG; b.classList.add("done");
        setTimeout(() => {
          b.innerHTML = INLINE_COPY_SVG; b.classList.remove("done");
        }, 1200);
      }, () => host.toast("could not copy to clipboard", "err"));
    });
    return b;
  }
  ignoreEvent() { return true; }
}

const VIDEO_EXT = /\.(mp4|webm|ogv|mov|m4v)(\?|#|$)/i;
const AUDIO_EXT = /\.(mp3|wav|m4a|aac|oga|flac)(\?|#|$)/i;
function mediaKind(url) {
  return VIDEO_EXT.test(url) ? "video" : AUDIO_EXT.test(url) ? "audio" : "image";
}

// Find the syntax node of one of `names` that owns a widget's DOM. Positions
// captured when a widget was built go stale after any edit, so actions re-derive
// their range at the moment they run.
function nodeRangeAt(view, dom, names) {
  let pos;
  try { pos = view.posAtDOM(dom); } catch (e) { return null; }
  for (let n = syntaxTree(view.state).resolveInner(pos, 1); n; n = n.parent) {
    if (names.includes(n.name)) return { from: n.from, to: n.to };
  }
  return null;
}

// Images, video and audio render as the real thing — ALWAYS, including when the
// cursor is on them. Clicking a picture used to swap it for `![alt](path)`,
// which re-flowed the page under the pointer; now the embed is atomic and its
// hover bar carries the edit actions instead.
class MediaWidget extends WidgetType {
  constructor(src, alt, kind) {
    super();
    this.src = src; this.alt = alt; this.kind = kind;
  }
  eq(o) { return o.src === this.src && o.alt === this.alt && o.kind === this.kind; }
  get estimatedHeight() { return this.kind === "audio" ? 54 : 180; }
  toDOM(view) {
    const wrap = document.createElement("span");
    wrap.className = "cm-img-embed cm-media-" + this.kind;
    wrap.contentEditable = "false";
    let el;
    if (this.kind === "image") {
      el = new Image();
      el.alt = this.alt || "";
      el.addEventListener("load", () => view.requestMeasure());
    } else {
      el = document.createElement(this.kind);
      el.controls = true;
      el.preload = "metadata";
      el.addEventListener("loadedmetadata", () => view.requestMeasure());
    }
    el.src = this.src;
    el.title = this.alt || "";
    el.addEventListener("error", () => {
      wrap.textContent = "⚠ " + this.kind + " not found: " + (this.alt || this.src);
      wrap.classList.add("cm-img-broken");
    });
    wrap.appendChild(el);

    // A read-only document (someone else's, or a public link that only
    // views) shows the picture and nothing to do to it.
    if (view.state.readOnly) return wrap;

    const bar = document.createElement("span");
    bar.className = "cm-media-bar";
    const act = (label, title, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = label; b.title = title;
      b.addEventListener("mousedown", (e) => e.preventDefault());
      b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); fn(); });
      bar.appendChild(b);
    };
    const range = () => nodeRangeAt(view, wrap, ["Image"]);
    act("✎", "Change the caption / alt text", async () => {
      const r = range();
      if (!r) return;
      const alt = await host.prompt("Caption / alt text:", this.alt || "",
                                 { title: "Describe this " + this.kind });
      if (alt === null) return;
      const cur = view.state.sliceDoc(r.from, r.to);
      const next = cur.replace(/^!\[[^\]]*\]/, "![" + alt.replace(/[[\]]/g, "") + "]");
      view.dispatch({ changes: { from: r.from, to: r.to, insert: next } });
    });
    act("⤓", "Download / open the original", () => {
      window.open(this.src + (this.src.includes("?") ? "&" : "?") + "dl=1", "_blank");
    });
    act("✕", "Remove from the document", async () => {
      const r = range();
      if (!r) return;
      if (!(await host.confirm("Remove this " + this.kind + " from the document?",
                            { title: "Remove", ok: "Remove", danger: true }))) return;
      // take the trailing newline with it, so no blank hole is left behind
      const after = view.state.sliceDoc(r.to, Math.min(r.to + 1, view.state.doc.length));
      view.dispatch({ changes: { from: r.from, to: after === "\n" ? r.to + 1 : r.to } });
      host.toast("Removed (the file itself is untouched)", "ok");
    });
    wrap.appendChild(bar);
    return wrap;
  }
  ignoreEvent() { return true; }   // the embed owns its clicks — no syntax reveal
}

// ---- tables: a real grid you type into, markdown stays the truth ------------
// The source of record is still the GFM table text in the document; the widget
// is a view of it. Cell edits write back that ONE cell (minimal range, so two
// people editing different cells merge); structural edits rewrite the block.
//
// A cell shows its markdown RENDERED until you put the cursor in it, which is
// how the rest of this editor behaves: `**done**` reads as **done**, and the
// asterisks come back the moment the cell is yours to type in.

function splitRow(line, lineFrom) {
  const bars = [];
  for (let i = 0; i < line.length; i++) {
    if (line[i] === "|" && (i === 0 || line[i - 1] !== "\\")) bars.push(i);
  }
  if (!bars.length) return null;
  const cells = [];
  const push = (s, e) => cells.push({ from: lineFrom + s, to: lineFrom + e,
                                      text: line.slice(s, e).trim() });
  if (bars[0] > 0 && line.slice(0, bars[0]).trim()) push(0, bars[0]);
  for (let k = 0; k < bars.length - 1; k++) push(bars[k] + 1, bars[k + 1]);
  const tailFrom = bars[bars.length - 1] + 1;
  if (line.slice(tailFrom).trim()) push(tailFrom, line.length);
  return cells;
}

const ALIGN_RE = /^\s*:?-{1,}:?\s*$/;
function isDelimRow(cells) {
  return cells && cells.length > 0 && cells.every((c) => ALIGN_RE.test(c.text));
}
function alignOf(text) {
  const l = text.trim().startsWith(":"), r = text.trim().endsWith(":");
  return l && r ? "center" : r ? "right" : l ? "left" : "";
}

// Parse the table at `from` into rows of cells with absolute source positions.
function parseTable(md, from) {
  const rows = [];
  let off = 0, align = [];
  for (const line of md.split("\n")) {
    const cells = splitRow(line, from + off);
    off += line.length + 1;
    if (!cells) continue;
    if (isDelimRow(cells) && rows.length) align = cells.map((c) => alignOf(c.text));
    else rows.push(cells);
  }
  return { rows, align };
}

function tableToMarkdown(rows, align) {
  const width = Math.max(...rows.map((r) => r.length));
  const line = (cells) => "| " + Array.from({ length: width },
    (_, i) => (cells[i] == null ? "" : String(cells[i]).trim())).join(" | ") + " |";
  const delim = "| " + Array.from({ length: width }, (_, i) => {
    const a = align[i] || "";
    return a === "center" ? ":---:" : a === "right" ? "---:" : a === "left" ? ":---" : "---";
  }).join(" | ") + " |";
  const out = [line(rows[0] || [])];
  out.push(delim);
  for (let r = 1; r < rows.length; r++) out.push(line(rows[r]));
  return out.join("\n");
}

// A link inside a cell. The rendered cell is not a CodeMirror decoration, so
// the syntax-tree route the body of the document uses is not available here —
// the anchor carries the resolved target itself and the cell opens it.
// Only these ever become a real href. `isExternalUrl` is true of ANY scheme —
// including `javascript:`, which as an anchor in the app's own origin would be
// a document that runs code the moment someone follows a link in a cell.
function safeHref(url) {
  return /^(https?:|mailto:|tel:)/i.test(url) ? url : null;
}

function cellLink(label, url, dir) {
  const a = document.createElement("a");
  a.className = "cm-md-link";
  // inLink: the label is drawn without links of its own. A bare URL used to
  // be its own label, so drawing it found the URL again, made another link,
  // drew ITS label… until the stack overflowed inside TableWidget.toDOM, and
  // CodeMirror stopped drawing the note at that table (2026-09-27: a server
  // README went blank below any table with an https:// cell in it).
  a.appendChild(renderInlineMd(label || url, dir, true));
  if (isExternalUrl(url)) {
    const h = safeHref(url);
    if (h) { a.href = h; a.target = "_blank"; a.rel = "noopener noreferrer"; }
    else a.classList.add("cm-md-dead");     // shown, styled, goes nowhere
  } else {
    const rel = resolveDocPath(dir, url);
    a.href = "#";
    a.dataset.openPath = rel;
    if (rel.split("/").includes("_secrets")) a.classList.add("cm-md-secret");
  }
  a.title = url + "  ·  click opens (click elsewhere in the cell to edit it)";
  return a;
}

function openCellLink(a) {
  const p = a.dataset.openPath;
  if (p) host.openPath(p);
  else if (a.getAttribute("href")) window.open(a.href, "_blank", "noopener");
}

// Inline markdown, as DOM NODES built out of escaped text — a cell can contain
// anything a document contains, and none of it may become markup. Only the
// inline forms: a cell is one line, so headings, lists and fences have no
// meaning in it. `<br>` is the one HTML form GFM leaves people no alternative
// to, so it is understood literally and nothing else is.
function renderInlineMd(text, dir, inLink = false) {
  const frag = document.createDocumentFragment();
  let buf = "", i = 0;
  const flush = () => { if (buf) { frag.appendChild(document.createTextNode(buf)); buf = ""; } };
  const wrap = (tag, inner) => {
    flush();
    const e = document.createElement(tag);
    e.appendChild(renderInlineMd(inner, dir, inLink));
    frag.appendChild(e);
  };
  while (i < text.length) {
    const rest = text.slice(i);
    let m;
    if (text[i] === "\\" && /[\\`*_~[\]()<>!|]/.test(text[i + 1] || "")) {
      buf += text[i + 1]; i += 2; continue;
    }
    if ((m = /^(`+)([^`]+)\1/.exec(rest))) {
      flush();
      const e = document.createElement("code");
      e.textContent = m[2].trim();
      frag.appendChild(e);
    } else if ((m = /^!\[([^\]]*)\]\(\s*([^)\s]+)[^)]*\)/.exec(rest))) {
      flush();
      const img = new Image();
      img.className = "cm-cell-img";
      img.src = resolveMediaUrl(dir, m[2]);
      img.alt = m[1]; img.title = m[1] || m[2];
      frag.appendChild(img);
    } else if (!inLink && (m = /^\[([^\]]*)\]\(\s*([^)\s]+)[^)]*\)/.exec(rest))) {
      flush();
      frag.appendChild(cellLink(m[1], m[2], dir));
    } else if ((m = /^(\*\*|__)(?=\S)([\s\S]+?)\1/.exec(rest))) {
      wrap("strong", m[2]);
    } else if ((m = /^~~(?=\S)([\s\S]+?)~~/.exec(rest))) {
      wrap("s", m[1]);
    } else if ((m = /^([*_])(?=\S)([\s\S]+?)\1/.exec(rest))) {
      wrap("em", m[2]);
    } else if ((m = /^<br\s*\/?>/i.exec(rest))) {
      flush();
      frag.appendChild(document.createElement("br"));
    } else if (!inLink && (m = /^https?:\/\/[^\s<>()]+/.exec(rest))) {
      // GFM's rule: punctuation at the end closes the sentence, not the URL
      // ("https://example.com, and…" must not link to "example.com,")
      m = [m[0].replace(/[.,:;!?'"]+$/, "")];
      flush();
      frag.appendChild(cellLink(m[0], m[0], dir));
    } else {
      buf += text[i++];
      continue;
    }
    i += m[0].length;
  }
  flush();
  return frag;
}

class TableWidget extends WidgetType {
  constructor(md, readOnly, dir) { super(); this.md = md; this.readOnly = readOnly; this.dir = dir; }
  eq(o) { return o.md === this.md && o.readOnly === this.readOnly && o.dir === this.dir; }
  // What CodeMirror assumes for a table it has not drawn yet. Without it a
  // ten-row table counted as one line below the fold, and the page grew under
  // the reader as each one scrolled in. Measured on a desktop: 34px a row
  // (header included, the delimiter row draws nothing) plus the button bar.
  get estimatedHeight() { return 34 * (this.md.split("\n").length - 1) + 38; }
  toDOM(view) {
    const dom = document.createElement("div");
    dom.className = "cm-table-wrap";
    dom.contentEditable = "false";
    this.draw(dom, view);
    return dom;
  }
  updateDOM(dom, view) {
    // a structural edit (add/remove row or column) must always redraw, even
    // though the button press left the caret inside a cell
    if (dom.__force) { dom.__force = false; this.draw(dom, view); return true; }
    if (dom.__md === this.md) return true;                    // our own cell writeback
    if (dom.contains(document.activeElement)) return true;    // don't yank a cell mid-typing
    this.draw(dom, view);
    return true;
  }
  // A widget that throws while CodeMirror draws stops the drawing there, and
  // everything below it stays blank. One table must never cost the note: if
  // the grid cannot be built, the table shows as the markdown it is.
  draw(dom, view) {
    try { this.build(dom, view); }
    catch (e) {
      console.error("table could not be drawn; showing its markdown", e);
      dom.__md = this.md;
      const pre = document.createElement("pre");
      pre.className = "cm-table-raw";
      pre.textContent = this.md;
      dom.replaceChildren(pre);
    }
  }
  ignoreEvent() { return true; }

  build(dom, view) {
    dom.__md = this.md;
    dom.replaceChildren();
    const { rows, align } = parseTable(this.md, 0);
    const table = document.createElement("table");
    table.className = "cm-table";
    const ro = this.readOnly;
    const dir = this.dir;
    // where the caret is, for "insert row below" / "delete this column"
    let focus = { r: 0, c: 0 };

    const model = () => {
      // Re-derive from the CURRENT document — positions move under us.
      const r = nodeRangeAt(view, dom, ["Table"]);
      if (!r) return null;
      const md = view.state.sliceDoc(r.from, r.to);
      return { ...parseTable(md, r.from), from: r.from, to: r.to, md };
    };

    // In the box a line break is a line break; in the file it is `<br>`, the
    // only one GFM allows inside a cell. Translated at the edge, both ways,
    // so nobody editing a cell ever sees or types the tag.
    const toEdit = (md) => md.replace(/<br\s*\/?>/gi, "\n");
    const toMd = (text) => text.replace(/\r?\n/g, "<br>");
    const writeCell = (r, c, text) => {
      const m = model();
      if (!m || !m.rows[r] || !m.rows[r][c]) return;
      const cell = m.rows[r][c];
      const insert = " " + toMd(text).trim().replace(/\|/g, "\\|") + " ";
      if (view.state.sliceDoc(cell.from, cell.to) === insert) return;
      // mark the DOM as already reflecting the result, so the re-render this
      // dispatch triggers keeps the live input (and the caret in it)
      const next = m.md.slice(0, cell.from - m.from) + insert + m.md.slice(cell.to - m.from);
      dom.__md = next;
      view.dispatch({ changes: { from: cell.from, to: cell.to, insert } });
    };

    // Put the cursor back in a cell after a redraw threw the live input away.
    // `__busy` is up from the dispatch until the redraw has settled: an input
    // that loses focus by being REMOVED must not write its old text back,
    // because after a row insert its coordinates mean a different cell.
    const refocus = (want) => requestAnimationFrame(() => {
      dom.__busy = false;
      const td = dom.querySelector(`[data-cell="${want.r},${want.c}"]`) ||
                 dom.querySelector('[data-cell="0,0"]');
      if (td && td.__edit) td.__edit();
    });

    const restructure = (fn, want) => {
      const m = model();
      if (!m) return;
      const grid = m.rows.map((row) => row.map((cell) => cell.text));
      const al = m.align.slice();
      const before = JSON.stringify([grid, al]);
      fn(grid, al);
      if (JSON.stringify([grid, al]) === before) return;   // guarded op declined
      const md = tableToMarkdown(grid, al);
      dom.__force = true;                                  // redraw even with focus inside
      dom.__busy = true;
      const landing = want || { r: focus.r, c: focus.c };
      view.dispatch({ changes: { from: m.from, to: m.to, insert: md } });
      refocus(landing);
    };

    // Undo has to reach the document. Everything typed in a cell is a normal
    // edit of the markdown underneath, but the keystroke happens inside an
    // <input> the editor cannot see — so Ctrl+Z used to undo nothing at all
    // (or, worse, only the browser's own idea of the input's history).
    const undoFromCell = (r, c, inp, isRedo) => {
      clearTimeout(inp._t);
      writeCell(r, c, inp.value);        // land what is pending, then step back
      dom.__force = true;
      dom.__busy = true;
      (isRedo ? redo : undo)(view);
      refocus({ r, c });
    };

    const rowCount = () => (model() || { rows }).rows.length;
    const colCount = () => ((model() || { rows }).rows[0] || []).length;
    const addRow = (at) => restructure((g) => g.splice(at, 0, g[0].map(() => "")),
                                       { r: at, c: 0 });
    const addCol = (at) => restructure((g, al) => {
      g.forEach((row) => row.splice(at, 0, ""));
      al.splice(at, 0, "");
    }, { r: focus.r, c: at });
    const dropRow = (at) => restructure((g) => {
      if (g.length > 2 && at > 0) g.splice(at, 1);
      else host.toast("A table keeps its header row and one body row", "err");
    }, { r: Math.max(1, at - 1), c: focus.c });
    const dropCol = (at) => restructure((g, al) => {
      if ((g[0] || []).length > 1) { g.forEach((row) => row.splice(at, 1)); al.splice(at, 1); }
      else host.toast("A table needs at least one column", "err");
    }, { r: focus.r, c: Math.max(0, at - 1) });

    const cellMenu = (r, c, td) => {
      const items = [{ icon: host.icons.pencil, label: "Edit this cell", fn: () => td.__edit() }, "-"];
      if (r > 0) items.push({ icon: host.icons.plus, label: "Insert row above", fn: () => addRow(r) });
      items.push({ icon: host.icons.plus, label: "Insert row below", fn: () => addRow(r + 1) });
      items.push({ icon: host.icons.plus, label: "Insert column left", fn: () => addCol(c) });
      items.push({ icon: host.icons.plus, label: "Insert column right", fn: () => addCol(c + 1) });
      items.push("-");
      if (r > 0) items.push({ icon: host.icons.trash, label: "Delete this row", danger: true,
                              fn: () => dropRow(r) });
      items.push({ icon: host.icons.trash, label: "Delete this column", danger: true,
                   fn: () => dropCol(c) });
      return items;
    };

    // CodeMirror measures this widget's box to know where every line below it
    // paints. A cell that grew and a height map that did not is how a click
    // lands on the wrong line, so every size change says so.
    const measured = () => { try { view.requestMeasure(); } catch (e) { /* torn down */ } };

    rows.forEach((row, r) => {
      const tr = document.createElement("tr");
      row.forEach((cell, c) => {
        const td = document.createElement(r === 0 ? "th" : "td");
        td.className = "cm-td";
        td.setAttribute("data-cell", r + "," + c);
        if (align[c]) td.style.textAlign = align[c];
        let raw = cell.text.replace(/\\\|/g, "|");
        const shown = document.createElement("span");
        shown.className = "cm-cell-md";
        const paint = () => { shown.replaceChildren(renderInlineMd(raw, dir)); measured(); };
        paint();
        td.appendChild(shown);
        tr.appendChild(td);
        if (ro) return;

        let box = null;                     // the <textarea> while this cell is open
        const grow = () => {
          if (!box) return;
          box.style.height = "auto";
          box.style.height = box.scrollHeight + "px";
          measured();
        };
        const close = () => {
          if (!box) return;
          const el = box;
          box = null;                       // before blur/remove re-enters here
          clearTimeout(el._t);
          if (!dom.__busy) { raw = toMd(el.value); writeCell(r, c, el.value); }
          el.remove();
          td.classList.remove("editing");
          paint();
        };
        // Where a click means, in the RAW text. When the cell is plain the two
        // agree character for character, so the caret lands under the finger;
        // when it carries markup they do not, and the end of the text is the
        // honest answer.
        const caretFromClick = (e) => {
          const plain = shown.textContent;
          // one text node and no marks: the offset under the finger is the
          // offset in the box. Anything richer (bold, a break) lands at the end.
          if (shown.childNodes.length !== 1 || plain !== raw) return toEdit(raw).length;
          let pos = null;
          if (document.caretPositionFromPoint) {
            const cp = document.caretPositionFromPoint(e.clientX, e.clientY);
            if (cp && shown.contains(cp.offsetNode)) pos = cp.offset;
          } else if (document.caretRangeFromPoint) {
            const rg = document.caretRangeFromPoint(e.clientX, e.clientY);
            if (rg && shown.contains(rg.startContainer)) pos = rg.startOffset;
          }
          return pos == null ? raw.length : Math.min(pos, raw.length);
        };
        const edit = (caret) => {
          focus = { r, c };
          if (box) { box.focus(); return; }
          box = document.createElement("textarea");
          box.className = "cm-cell-edit";
          box.rows = 1;
          box.spellcheck = false;
          box.value = toEdit(raw);
          box.setAttribute("data-cell", r + "," + c);
          box.addEventListener("input", () => {
            grow();
            clearTimeout(box._t);
            box._t = setTimeout(() => writeCell(r, c, box.value), 400);
          });
          box.addEventListener("blur", close);
          // keys typed in a cell are the table's business, not the editor's —
          // except the ones that are about the DOCUMENT, which are forwarded
          box.addEventListener("keydown", (e) => {
            e.stopPropagation();
            const mod = e.metaKey || e.ctrlKey;
            if (mod && (e.key === "z" || e.key === "Z" || e.key === "y")) {
              e.preventDefault();
              undoFromCell(r, c, box, e.key === "y" || e.shiftKey);
              return;
            }
            const go = (dr, dc) => {
              const next = dom.querySelector(`[data-cell="${r + dr},${c + dc}"]`);
              if (next && next.__edit) {
                e.preventDefault(); clearTimeout(box._t);
                writeCell(r, c, box.value);
                next.__edit();
              }
              return !!(next && next.__edit);
            };
            const atStart = box.selectionStart === 0 && box.selectionEnd === 0;
            const atEnd = box.selectionStart === box.value.length &&
                          box.selectionEnd === box.value.length;
            if (e.key === "Tab") { if (!go(0, e.shiftKey ? -1 : 1)) go(e.shiftKey ? -1 : 1, 0); }
            // a wrapped cell has lines of its own: leave it only from its edge
            else if (e.key === "ArrowDown" && atEnd) go(1, 0);
            else if (e.key === "ArrowUp" && atStart) go(-1, 0);
            else if (e.key === "Enter" && (e.shiftKey || mod)) {
              // a new line INSIDE the cell — Shift+Enter or Ctrl+Enter, the
              // two spellings people reach for. Stored as <br>, see toMd.
              e.preventDefault();
              const at = box.selectionStart;
              box.value = box.value.slice(0, at) + "\n" + box.value.slice(box.selectionEnd);
              box.selectionStart = box.selectionEnd = at + 1;
              grow();
              clearTimeout(box._t);
              box._t = setTimeout(() => writeCell(r, c, box.value), 400);
            } else if (e.key === "Enter") {
              e.preventDefault();
              clearTimeout(box._t); writeCell(r, c, box.value);
              if (!go(1, 0)) addRow(rowCount());
            } else if (e.key === "Escape") { box.blur(); view.focus(); }
          });
          td.classList.add("editing");
          td.appendChild(box);
          grow();
          box.focus();
          if (caret == null) box.select();
          else box.setSelectionRange(caret, caret);
        };
        td.__edit = edit;

        td.addEventListener("mousedown", (e) => {
          if (e.button !== 0 || box) return;
          const a = e.target.closest("a");
          if (a) { e.preventDefault(); openCellLink(a); return; }
          if (e.target.tagName === "IMG") return;   // let a picture be a picture
          const caret = caretFromClick(e);
          e.preventDefault();                       // no text selection, no caret move
          edit(caret);
        });
        td.addEventListener("contextmenu", (e) => {
          e.preventDefault(); e.stopPropagation();
          focus = { r, c };
          host.menu(cellMenu(r, c, td), e.clientX, e.clientY);
        });
      });
      table.appendChild(tr);
    });
    dom.appendChild(table);

    if (ro) return;
    const bar = document.createElement("div");
    bar.className = "cm-tbl-bar";
    const btn = (label, title, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = label; b.title = title;
      b.addEventListener("mousedown", (e) => e.preventDefault());
      b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); fn(); });
      bar.appendChild(b);
    };
    // The bar works on the END of the table — that is what a row of buttons
    // under a grid looks like it does. Anywhere in the middle is a right-click
    // on the cell you mean.
    const MIDDLE = "  ·  right-click a cell to insert in the middle";
    btn("+ row", "Add a row at the bottom" + MIDDLE, () => addRow(rowCount()));
    btn("+ col", "Add a column at the end" + MIDDLE, () => addCol(colCount()));
    btn("− row", "Remove the last row" + MIDDLE, () => dropRow(rowCount() - 1));
    btn("− col", "Remove the last column" + MIDDLE, () => dropCol(colCount() - 1));
    dom.appendChild(bar);
  }
}

// A table spans whole lines, so its decoration REPLACES line breaks — CodeMirror
// only accepts that from a state field (view plugins may not), which is why
// tables live here instead of inside livePreview().
function buildTableDecos(state, dir) {
  const ranges = [];
  syntaxTree(state).iterate({ enter: (n) => {
    if (n.name !== "Table") return;
    const from = state.doc.lineAt(n.from).from;
    const to = state.doc.lineAt(n.to).to;
    ranges.push(Decoration.replace({
      widget: new TableWidget(state.sliceDoc(from, to), state.readOnly, dir),
      block: true,
    }).range(from, to));
    return false;
  } });
  return Decoration.set(ranges, true);
}

// Takes the document's folder for the same reason livePreview() does: a link
// or a picture in a cell is written relative to the file it lives in.
const tableField = (dir) => StateField.define({
  create: (state) => buildTableDecos(state, dir),
  update(value, tr) {
    // Also rebuild when the SYNTAX TREE changed without the doc changing:
    // CodeMirror parses incrementally, so a table below the fold isn't in the
    // tree yet when a long document opens, and no further edit may ever come.
    if (!tr.docChanged && !tr.reconfigured &&
        syntaxTree(tr.state) === syntaxTree(tr.startState)) return value;
    return buildTableDecos(tr.state, dir);
  },
  provide: (f) => [
    EditorView.decorations.from(f),
    EditorView.atomicRanges.of((view) => view.state.field(f, false) || Decoration.none),
  ],
});

// CommonMark says a link destination containing a space has to be wrapped in
// angle brackets, so "[report](_files/q3 final.xlsx)" is not a link at all —
// it stays literal text, unrendered and unclickable. Dropping a file whose
// name has a space used to write exactly that, and those links are sitting in
// documents already, so parse the form anyway: with the whole "[…](…)" on one
// line and no parentheses inside the destination, it is unambiguous.
//
// Registered BEFORE the built-in Link parser, but it only claims a match the
// built-in parser would refuse: the destination has to contain whitespace, and
// the two forms where a space is already legal — an angle-bracketed
// destination, and a destination followed by a quoted title — are handed back.
const SPACED_LINK = /^(!?)\[([^\[\]\n]*)\]\(([^()\n]*[ \t][^()\n]*)\)/;
const HAS_TITLE = /\s(["']).*\1\s*$/;

const spacedLinks = {
  parseInline: [{
    name: "SpacedLink",
    before: "Link",
    parse(cx, next, pos) {
      if (next !== 91 /* [ */ && next !== 33 /* ! */) return -1;
      const m = SPACED_LINK.exec(cx.text.slice(pos - cx.offset));
      if (!m || m[3].startsWith("<") || HAS_TITLE.test(m[3])) return -1;
      const bang = m[1].length;               // 1 when this is an ![embed]
      const open = pos + bang;                // the "["
      const close = open + 1 + m[2].length;   // the "]"
      const url = close + 2;                  // past "]("
      const end = pos + m[0].length;
      // Same child shape the built-in parser emits — LinkMark "[", "]", "(",
      // URL, ")" — so the live-preview layer needs no special case.
      return cx.addElement(cx.elt(bang ? "Image" : "Link", pos, end, [
        cx.elt("LinkMark", pos, open + 1),
        cx.elt("LinkMark", close, close + 1),
        cx.elt("LinkMark", close + 1, url),
        cx.elt("URL", url, url + m[3].length),
        cx.elt("LinkMark", end - 1, end),
      ]));
    },
  }],
};

// The destination a URL node points at. The angle-bracket form — "[x](<a b.md>)",
// the way CommonMark says to write a target with a space — parses with the
// brackets INSIDE the URL node, and "<a b.md>" is not a path anything can open.
function linkTarget(state, urlN) {
  return state.sliceDoc(urlN.from, urlN.to).replace(/^<|>$/g, "").trim();
}

// A link target that leaves this app: a real scheme (https:, mailto:, …), a
// pure #fragment, or one of our own raw-file endpoints. Everything else names
// something in the knowledgebase.
function isExternalUrl(url) {
  return /^([a-z][a-z0-9+.-]*:|#|\/\/|\/api\/)/i.test(url);
}

// Where a markdown link inside `dir` actually points, as a knowledgebase path:
// percent-decoded, query/fragment dropped, and "." / ".." resolved — a sibling
// link written as "../notes/plan.md" has to end up at the file the tree knows.
function resolveDocPath(dir, url) {
  let raw = url.split("#")[0].split("?")[0];
  try { raw = decodeURIComponent(raw); } catch (e) { /* literal % in the name */ }
  raw = raw.startsWith("/") ? raw.slice(1) : (dir ? dir + "/" : "") + raw;
  const out = [];
  for (const seg of raw.split("/")) {
    if (!seg || seg === ".") continue;
    if (seg === "..") out.pop();
    else out.push(seg);
  }
  return out.join("/");
}

function resolveMediaUrl(dir, url) {
  if (/^(https?:|data:|\/)/.test(url)) return url;
  // Markdown link targets are percent-encoded ("image%20%281%29.png"); the
  // filesystem knows the raw name. Decode BEFORE re-encoding for the query
  // string, or the API looks up a file literally named "image%20%281%29.png".
  try { url = decodeURIComponent(url); } catch (e) { /* literal % in the name */ }
  return host.mediaUrl((dir ? dir + "/" : "") + url);
}

function livePreview(dir) {
  const plugin = ViewPlugin.fromClass(class {
    constructor(view) { this.compute(view); }
    update(u) {
      // The parse runs ahead in the background, and the lines it reaches only
      // become headings, lists and code once it has: without the tree check
      // they stayed raw `##` text until something else moved — forever, in a
      // document drawn whole (drawWhole), where the viewport never changes.
      if (u.docChanged || u.selectionSet || u.viewportChanged ||
          syntaxTree(u.state) !== syntaxTree(u.startState)) this.compute(u.view);
    }
    compute(view) {
      const decos = [], atomics = [];
      const { state } = view;
      const sel = state.selection.main;
      const touches = (from, to) => sel.from <= to && sel.to >= from;
      const hide = (from, to) => {
        if (to > from) { decos.push(HIDE.range(from, to)); atomics.push(HIDE.range(from, to)); }
      };
      const replace = (from, to, widget) => {
        const d = Decoration.replace({ widget });
        decos.push(d.range(from, to)); atomics.push(d.range(from, to));
      };
        // A list line hangs: the marker (and any indent of a nested level)
      // sits in the margin, and the wrapped lines line up under the first
      // word instead of running back to the page's edge. `lead` is how many
      // spaces precede the marker, `mark` the marker's own width in em.
      const hung = new Set();
      const MARKER = /^(\s*)(?:([-*+])|(\d+[.)]))(\s+)/;
      // the width of everything before an item's text, in CSS that resolves
      // against the measured metrics (see calibrateListMetrics)
      const markWidth = (kind, len) =>
        kind === "task" ? "var(--lm-task)"     // the widget swallows the space after it
          : kind === "num" ? "calc(" + len + " * var(--lm-mono) + var(--lm-space))"
            : "calc(var(--lm-bullet) + var(--lm-space))";
      const spaces = (n) => "calc(" + n + " * var(--lm-space))";
      const hangList = (line, lead, kind, len) => {
        if (hung.has(line.from)) return;
        hung.add(line.from);
        decos.push(Decoration.line({ class: "cm-listline",
          attributes: { style: "--list-mark:" + markWidth(kind, len) + ";--list-lead:" + spaces(lead) } })
          .range(line.from));
      };
      // A paragraph wrapped in the FILE is several lines in the editor, and
      // the ones after the marker carry no marker of their own: without this
      // they fall back to the page's edge, which is what makes a list look
      // ragged. They are pushed to where the item's text begins, less the
      // indentation they already print themselves.
      // Continuations are decided at the END of the walk: a line inside a
      // fenced code block belongs to the block's card, not to the item's
      // paragraph, and the block is only seen later in the tree.
      const codeLines = new Set();
      const pendingCont = [];
      const hangCont = (line, kind, len, lead) => {
        if (hung.has(line.from)) return;
        hung.add(line.from);
        pendingCont.push({ line, kind, len, lead });
      };
      const flushCont = () => {
        for (const { line, kind, len, lead } of pendingCont) {
          if (codeLines.has(line.number)) continue;
          const own = (line.text.match(/^\s*/) || [""])[0].length;
          decos.push(Decoration.line({ class: "cm-listcont",
            attributes: { style: "--list-mark:" + markWidth(kind, len) + ";--list-lead:" + spaces(lead) +
                                 ";--own-lead:" + spaces(own) } })
            .range(line.from));
        }
      };
      for (const { from, to } of view.visibleRanges) {
        syntaxTree(state).iterate({ from, to, enter: (n) => {
          const name = n.name;
          if (name.startsWith("ATXHeading")) {
            const level = Math.min(6, +name.slice(10) || 1);
            const line = state.doc.lineAt(n.from);
            decos.push(Decoration.line({ class: "cm-h cm-h" + level }).range(line.from));
            if (!touches(n.from, n.to)) {
              const mark = n.node.getChild("HeaderMark");
              if (mark) hide(mark.from, Math.min(mark.to + 1, line.to));
            }
          } else if (name === "StrongEmphasis" || name === "Emphasis" ||
                     name === "Strikethrough" || name === "InlineCode") {
            const marks = n.node.getChildren(
              name === "InlineCode" ? "CodeMark"
                : name === "Strikethrough" ? "StrikethroughMark" : "EmphasisMark");
            if (!touches(n.from, n.to)) {
              for (const m of marks) hide(m.from, m.to);
            }
            if (name === "InlineCode" && marks.length >= 2) {
              // The content between the backticks, read from the tree so a span
              // written with doubled fences (``a `b` c``) copies what it shows.
              const code = state.sliceDoc(marks[0].to, marks[marks.length - 1].from);
              if (code.trim()) {
                decos.push(Decoration.widget({
                  widget: new InlineCopyWidget(code), side: 1,
                }).range(n.to));
              }
            }
          } else if (name === "Link") {
            const node = n.node;
            const marks = node.getChildren("LinkMark");
            const urlN = node.getChild("URL");
            if (marks.length >= 2 && urlN && !touches(n.from, n.to)) {
              const url = linkTarget(state, urlN);
              hide(n.from, marks[0].to);
              hide(marks[1].from, n.to);
              const secretLink = !/^(https?:|data:)/.test(url) &&
                url.split("/").includes("_secrets");
              decos.push(Decoration.mark({
                class: "cm-md-link" + (secretLink ? " cm-md-secret" : ""),
                attributes: { "data-url": url,
                              title: url + "  ·  Ctrl+click, middle click or double-click opens" },
              }).range(marks[0].to, marks[1].from));
            }
          } else if (name === "Image") {
            // rendered even when the cursor is on it: clicking a picture must
            // never swap it for raw markdown and re-flow the page.
            // Read from the syntax tree, not a regex over the source: a
            // destination with a space in it ("_files/site plan.png") reads
            // back whole here, where a regex stops at the space and embeds a
            // truncated path.
            const node = n.node;
            const marks = node.getChildren("LinkMark");
            const urlN = node.getChild("URL");
            if (marks.length >= 2 && urlN) {
              const alt = state.sliceDoc(marks[0].to, marks[1].from);
              const url = linkTarget(state, urlN);
              if (url) {
                replace(n.from, n.to,
                        new MediaWidget(resolveMediaUrl(dir, url), alt, mediaKind(url)));
              }
            }
          } else if (name === "Table") {
            return false;   // handled by tableField (a block decoration, below)
          } else if (name === "ListItem") {
            const first = state.doc.lineAt(n.from);
            const m = first.text.match(MARKER);
            if (m) {
              const lead = m[1].length;
              const kind = /^\s*[-*+]\s\[[ xX]\]\s/.test(first.text) ? "task" : m[2] ? "bullet" : "num";
              const len = m[3] ? m[3].length : 0;
              hangList(first, lead, kind, len);
              const last = state.doc.lineAt(n.to);
              for (let ln = first.number + 1; ln <= last.number; ln++) {
                const line = state.doc.line(ln);
                if (!line.text.trim()) continue;          // a blank line between paragraphs
                if (MARKER.test(line.text)) continue;     // a nested item: its own shape
                hangCont(line, kind, len, lead);
              }
            }
          } else if (name === "TaskMarker") {
            const line = state.doc.lineAt(n.from);
            const bm = line.text.match(/^(\s*)([-*+])\s\[[ xX]\]\s?/);
            if (bm) {
              const from = line.from + bm[1].length;
              const to = line.from + bm[0].length;
              if (!touches(from, to)) {
                const checked = /[xX]/.test(state.sliceDoc(n.from, n.to));
                replace(from, to, new CheckboxWidget(checked));
              }
            }
            return false;
          } else if (name === "ListMark") {
            const txt = state.sliceDoc(n.from, n.to);
            if (/^[-*+]$/.test(txt)) {
              const after = state.sliceDoc(n.to, Math.min(n.to + 5, state.doc.length));
              if (!/^\s\[[ xX]\]/.test(after) && !touches(n.from, n.to + 1)) {
                replace(n.from, n.to, new BulletWidget());
              }
            } else {
              decos.push(Decoration.mark({ class: "cm-olmark" }).range(n.from, n.to));
            }
          } else if (name === "QuoteMark") {
            const line = state.doc.lineAt(n.from);
            decos.push(Decoration.line({ class: "cm-quoteline" }).range(line.from));
            if (!(sel.from <= line.to && sel.to >= line.from)) {
              hide(n.from, state.sliceDoc(n.to, n.to + 1) === " " ? n.to + 1 : n.to);
            }
          } else if (name === "HorizontalRule") {
            if (!touches(n.from, n.to)) replace(n.from, n.to, new HRWidget());
          } else if (name === "FencedCode") {
            const firstLine = state.doc.lineAt(n.from);
            const lastLine = state.doc.lineAt(n.to);
            for (let ln = firstLine.number; ln <= lastLine.number; ln++) {
              let cls = "cm-codeblock";
              if (ln === firstLine.number) cls += " cm-codeblock-first";
              if (ln === lastLine.number) cls += " cm-codeblock-last";
              codeLines.add(ln);
              decos.push(Decoration.line({ class: cls }).range(state.doc.line(ln).from));
            }
            // copy button on the opening fence, copying the lines between the
            // fences (tolerating a still-unclosed block while you type)
            if (lastLine.number > firstLine.number) {
              const closed = /^\s*(`{3,}|~{3,})\s*$/.test(lastLine.text);
              const from = state.doc.line(firstLine.number + 1).from;
              const to = closed ? Math.max(from, lastLine.from - 1) : lastLine.to;
              decos.push(Decoration.widget({ widget: new CopyWidget(state.sliceDoc(from, to)), side: 1 })
                .range(firstLine.to));
            }
          } else if (name === "CodeMark" || name === "CodeInfo") {
            decos.push(Decoration.mark({ class: "cm-dim" }).range(n.from, n.to));
          }
        } });
      }
      flushCont();
      this.decorations = Decoration.set(decos, true);
      this.atomic = Decoration.set(atomics, true);
    }
  }, {
    decorations: (v) => v.decorations,
    provide: (p) => EditorView.atomicRanges.of(
      (view) => (view.plugin(p) && view.plugin(p).atomic) || Decoration.none),
  });

  // Follow a link at the clicked position. Resolved from the SYNTAX TREE, not
  // the rendered DOM: the click itself moves the cursor into the link, which
  // reveals the raw syntax and tears down the .cm-md-link decoration before
  // the click event lands — a DOM hit test misses its own target.
  const openLinkAt = (view, e) => {
    const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
    if (pos == null) return false;
    for (let n = syntaxTree(view.state).resolveInner(pos, 0); n; n = n.parent) {
      if (n.name === "Link") {
        const u = n.getChild("URL");
        if (!u) return false;
        const url = linkTarget(view.state, u);
        if (isExternalUrl(url)) {
          if (url.startsWith("#")) return true;   // an in-page anchor goes nowhere
          window.open(url, "_blank");
          return true;
        }
        // A link into the knowledgebase opens the way the tree opens it: a
        // document becomes a TAB, an artifact runs, a secret is masked. Only a
        // binary (image, pdf, docx) is handed to the browser — which is what
        // every one of these used to do, so following a link to another note
        // downloaded the markdown instead of opening it.
        const rel = resolveDocPath(dir, url);
        if (rel) host.openPath(rel);
        return true;
      }
    }
    return false;
  };
  // Desktop: Ctrl/Cmd+click, middle click or double-click opens (a plain click
  // places the cursor). Touch (hover:none): a plain tap opens — the
  // Obsidian-mobile model; to edit a link's text, tap beside it and arrow in.
  //
  // Both modifier gestures are taken on MOUSEDOWN, not on click. The mousedown
  // moves the selection into the link, which reveals its raw `[label](url)` —
  // the line re-flows under the pointer, and the coordinates the click then
  // carries resolve to a different place (often past the end of the link), so
  // the open silently did nothing and only the second click of a double-click
  // ever worked. Middle click has a default worth stopping too: on Linux it
  // pastes the X selection into the document.
  const MAC = /Mac|iP(hone|ad|od)/.test(navigator.platform || "");
  const opensLink = (e) =>
    e.button === 1 || (e.button === 0 && (MAC ? e.metaKey : e.ctrlKey || e.metaKey));
  const linkClicks = EditorView.domEventHandlers({
    mousedown(e, view) {
      if (!opensLink(e) || !openLinkAt(view, e)) return false;
      e.preventDefault();          // no caret move, no X-selection paste
      return true;
    },
    auxclick(e) {                  // the middle button's own event, if it lands
      if (e.button !== 1) return false;
      e.preventDefault();
      return true;
    },
    click(e, view) {
      if (window.matchMedia("(hover: none)").matches) return openLinkAt(view, e);
      return false;
    },
    dblclick(e, view) { return openLinkAt(view, e); },
  });
  return [plugin, linkClicks, todoInputRule,
          EditorView.editorAttributes.of({ class: "cm-rich" })];
}

// Typing "[]" (or "[ ]") at the start of a line becomes a todo — the Notion
// shorthand. The canonical "- [ ] " lands in the markdown source, so the
// indexer, the todos artifact and every other reader see a normal GFM task.
const todoInputRule = EditorView.inputHandler.of((view, from, to, text) => {
  if (text !== "]" || from !== to || view.state.readOnly) return false;
  const line = view.state.doc.lineAt(from);
  const m = view.state.sliceDoc(line.from, from).match(/^(\s*)(?:[-*+] )?\[ ?$/);
  if (!m) return false;
  const start = line.from + m[1].length;
  view.dispatch({
    changes: { from: start, to: from, insert: "- [ ] " },
    selection: { anchor: start + 6 },
    userEvent: "input.type",
  });
  return true;
});

// Tab / Shift+Tab: indent or outdent the selected list line(s) by one level
// (two spaces — what nested markdown lists expect). On a plain line, Tab
// still types an indent, so the key is never dead.
function listIndent(view, dir) {
  if (view.state.readOnly) return false;
  const { state } = view;
  const sel = state.selection.main;
  // a line-selection (triple-click) ends after the newline — that trailing
  // position must not drag the next line into the operation
  const endPos = sel.to > sel.from && state.doc.lineAt(sel.to).from === sel.to
    ? sel.to - 1 : sel.to;
  const isBlock = (t) => /^\s*([-*+]\s|\d+\.\s|>\s?)/.test(t);
  let line = state.doc.lineAt(sel.from);
  if (dir > 0 && sel.empty && !isBlock(line.text)) {
    view.dispatch(state.replaceSelection("  "), { userEvent: "input.type" });
    return true;
  }
  const changes = [];
  for (;;) {
    if (dir > 0) {
      if (line.length) changes.push({ from: line.from, insert: "  " });
    } else {
      const m = line.text.match(/^( {1,2}|\t)/);
      if (m) changes.push({ from: line.from, to: line.from + m[1].length });
    }
    if (line.to >= endPos) break;
    line = state.doc.line(line.number + 1);
  }
  if (changes.length) {
    view.dispatch({ changes, userEvent: dir > 0 ? "input.indent" : "delete.dedent" });
  }
  return true;   // Tab always belongs to the editor, never to focus travel
}

// ---- @mention autocomplete: type @ and pick a person -----------------------
let _mentionCache = null, _mentionAt = 0;
// Lower-cased set of the same names, for the editor highlight — a plain array
// scan per match would be O(users) on every visible line.
let _mentionNames = new Set();

async function mentionUsers() {
  if (!_mentionCache || Date.now() - _mentionAt > 60000) {
    try {
      _mentionCache = await host.principals();
      _mentionAt = Date.now();
      const next = new Set(_mentionCache.map((u) => u.toLowerCase()));
      // Only repaint when the roster actually moved: this refreshes every
      // minute, and an unconditional dispatch would churn every open editor.
      if (next.size !== _mentionNames.size ||
          [...next].some((u) => !_mentionNames.has(u))) {
        _mentionNames = next;
        repaintMentions();
      }
    } catch (e) { _mentionCache = _mentionCache || []; }
  }
  return _mentionCache;
}

async function mentionSource(context) {
  const m = context.matchBefore(/@[a-z0-9_]*/i);
  if (!m) return null;
  // the @ must start a word — never fire inside an email address
  if (m.from > 0 && /[\w.@-]/.test(context.state.sliceDoc(m.from - 1, m.from))) return null;
  const users = await mentionUsers();
  if (!users.length) return null;
  return {
    from: m.from + 1,
    options: users.map((u) => ({ label: u, type: "mention" })),
    validFor: /^[a-z0-9_]*$/i,
  };
}

// ---- @mention highlight ----------------------------------------------------
// A tagged person should READ as a person, not as prose that happens to start
// with "@". Markdown has no mention node, so this is a scan over the visible
// lines rather than a syntax-tree walk — but it asks the tree before marking
// anything, so an @ inside code, a link destination or a URL stays plain text.
//
// Only names that belong to a real account light up. Highlighting every @word
// would colour typos and prose ("email me @ noon") as if someone had been
// tagged, which is the one thing a mention colour must not do.
const rosterChanged = StateEffect.define();
const _mentionViews = new Set();

// Both the roster and `window.__kbuser` arrive from their own boot fetch, in no
// fixed order relative to the first document opening. Either one landing has to
// repaint, or whichever lost the race leaves the editor a mention short — or,
// worse, your own name coloured as somebody else's.
function repaintMentions() {
  for (const v of _mentionViews) {
    try { v.dispatch({ effects: rosterChanged.of(null) }); } catch (e) { /* torn down */ }
  }
}

// Character-for-character the indexer's ASSIGNEE_RE (kb_platform/indexer.py):
// the colour has to mean exactly what the to-do index already means by a tag,
// or a highlighted "@bob" that never reaches Bob's to-do list is a worse lie
// than no highlight at all.
const MENTION_RE = /(?:^|\s)@([A-Za-z0-9_][A-Za-z0-9_-]*)/g;
const MENTION_SKIP = new Set(["InlineCode", "FencedCode", "CodeText", "CodeMark",
                              "CodeInfo", "URL", "LinkMark"]);

function inCodeOrUrl(tree, pos) {
  for (let n = tree.resolveInner(pos, 1); n; n = n.parent) {
    if (MENTION_SKIP.has(n.name)) return true;
  }
  return false;
}

function buildMentionDecos(view) {
  if (!_mentionNames.size) return Decoration.none;
  const decos = [];
  const { state } = view;
  const tree = syntaxTree(state);
  for (const range of view.visibleRanges) {
    let pos = range.from;
    while (pos <= range.to) {
      const line = state.doc.lineAt(pos);
      const text = line.text;
      let m;
      MENTION_RE.lastIndex = 0;
      while ((m = MENTION_RE.exec(text))) {
        const name = m[1];
        if (!_mentionNames.has(name.toLowerCase())) continue;
        const from = line.from + m.index + m[0].indexOf("@");
        if (inCodeOrUrl(tree, from + 1)) continue;
        // Being tagged YOURSELF is the one mention you must not scroll past, so
        // it gets its own treatment rather than sharing everyone else's colour.
        const mine = name.toLowerCase() === (window.__kbuser || "").toLowerCase();
        decos.push(Decoration.mark({
          class: "cm-mention" + (mine ? " cm-mention-me" : ""),
          attributes: {
            "data-mention": name,
            ...(mine ? { "data-me": "1" } : {}),
            title: mine ? "@" + name + " — that is you"
                        : "@" + name + " — tagged in this document",
          },
        }).range(from, from + 1 + name.length));
      }
      if (line.to >= range.to) break;
      pos = line.to + 1;
    }
  }
  return Decoration.set(decos, true);
}

function mentionHighlight() {
  return ViewPlugin.fromClass(class {
    constructor(view) {
      this.view = view;
      _mentionViews.add(view);
      // Warm the roster: until it lands nothing is highlighted, and the fetch
      // answers with a `rosterChanged` repaint of every open editor.
      mentionUsers();
      this.decorations = buildMentionDecos(view);
    }
    update(u) {
      if (u.docChanged || u.viewportChanged ||
          u.transactions.some((tr) => tr.effects.some((e) => e.is(rosterChanged)))) {
        this.decorations = buildMentionDecos(u.view);
      }
    }
    destroy() { _mentionViews.delete(this.view); }
  }, { decorations: (v) => v.decorations });
}


// CodeMirror draws the lines on screen and ~1000px either side of them; the
// rest of the document is an empty spacer whose height it GUESSES. On a phone
// both halves of that show. A fling moves the page on the compositor faster
// than a phone's main thread draws the next lines, so what slides into view is
// the spacer — a blank patch, the whole screen at worst. And the guess is poor
// (its yardstick is whichever short line it met first, often a heading), so
// the page is the wrong length and keeps being corrected under your thumb.
// Measured on a phone emulation with a 4–6× slowed CPU: 43 frames with a blank
// band taller than 180px in four flings down a 13 KB note, a first guess 37%
// too tall — and neither once the whole note is drawn.
//
// Drawing the whole document is what CodeMirror itself does to print, and the
// flag it prints with is the only switch it has for it. It is internal, so
// tests/e2e/test_scroll_whole.py fails if an upgrade moves it. The parse is
// pushed to the end first, so the lines below the fold are drawn rich the
// first time rather than as raw `##` that turns into a heading later and
// changes height. Not from inside an update: forceParsing dispatches.
function drawWhole(view, on) {
  const vs = view.viewState;
  if (!vs || vs.printing === on) return;
  if (on) forceParsing(view, view.state.doc.length, 250);
  vs.printing = on;
  view.requestMeasure();
}


// A list's hanging indent is the width of what precedes its text: the
// marker, the space after it, and the spaces of its nesting. Those widths
// belong to the theme's fonts, so they are MEASURED once per font and
// published as --lm-space / --lm-mono; the decorations then do arithmetic
// with them instead of guessing in em (which was out by 3–8px per level).
let _lmSig = "";
function calibrateListMetrics(el) {
  if (!el) return;
  const cs = getComputedStyle(el);
  const sig = cs.fontFamily + "|" + cs.fontSize;
  if (sig === _lmSig) return;
  const probe = document.createElement("div");
  probe.style.cssText = "position:absolute;visibility:hidden;white-space:pre;top:-9999px;left:-9999px;margin:0;padding:0";
  probe.style.font = cs.font || (cs.fontSize + " " + cs.fontFamily);
  probe.textContent = "          ";                     // ten spaces
  document.body.appendChild(probe);
  const space = probe.getBoundingClientRect().width / 10;
  probe.style.fontFamily = cs.getPropertyValue("--mono") ||
    getComputedStyle(document.documentElement).getPropertyValue("--mono");
  probe.textContent = "0000000000";
  const mono = probe.getBoundingClientRect().width / 10;
  probe.remove();
  if (!space || !mono) return;                          // fonts not ready; try again next mount
  _lmSig = sig;
  const r = document.documentElement.style;
  r.setProperty("--lm-space", space.toFixed(2) + "px");
  r.setProperty("--lm-mono", mono.toFixed(2) + "px");
}


export { mdHighlight, calibrateListMetrics, tableField, livePreview, spacedLinks, listIndent, todoInputRule,
         mentionHighlight, mentionSource, repaintMentions, inCodeOrUrl, renderInlineMd, drawWhole,
         resolveMediaUrl, resolveDocPath, linkTarget, isExternalUrl, mediaKind, nodeRangeAt,
         VIDEO_EXT, AUDIO_EXT, HIDE, MENTION_RE,
         CheckboxWidget, BulletWidget, HRWidget, CopyWidget, InlineCopyWidget,
         MediaWidget, TableWidget };
