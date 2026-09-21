import * as Y from "yjs";
import { WebsocketProvider } from "y-websocket";
import { EditorState, Compartment, StateField, StateEffect } from "@codemirror/state";
import { EditorView, keymap, lineNumbers, highlightActiveLine,
         ViewPlugin, Decoration, WidgetType, dropCursor, drawSelection } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { autocompletion } from "@codemirror/autocomplete";
import { search as cmSearch, searchKeymap, openSearchPanel,
         highlightSelectionMatches } from "@codemirror/search";
import { markdown } from "@codemirror/lang-markdown";
import { Strikethrough, TaskList, Table } from "@lezer/markdown";
import { HighlightStyle, syntaxHighlighting, syntaxTree } from "@codemirror/language";
import { tags as t } from "@lezer/highlight";
import { yCollab } from "y-codemirror.next";
// xterm is NOT imported here: it is a quarter of the bundle and lives in its
// own chunk, loaded by warmTerminal() the first time a terminal is wanted.
import { initDictation, toggleDictation, dictationReady, retryDictation,
         releaseMicNow, listRecordings, recordingBlob, deleteRecording,
         transcribeRecording } from "./dictation.js";
import { settings } from "./settings.js";
import { connectEvents } from "./events.js";
import { parse as parseLayout, serialize as serializeLayout, findGroup as findLayoutGroup,
         canAddColumn, canAddGroup, newGroupId, MIN_COL_PX, MIN_GROUP_PX } from "./layout.js";
import { registerView, viewKind, registerCommand, commands as registeredCommands,
         defineSlot, slot } from "./views.js";

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

// The terminal's look comes from the theme's --term-* tokens: xterm takes
// real colour strings, so they are read from the computed style when a
// terminal opens and again on every theme change (retintTerminals).
const TERM_KEYS = {
  background: "bg", foreground: "fg", cursor: "cursor", cursorAccent: "bg",
  black: "black", red: "red", green: "green", yellow: "yellow",
  blue: "blue", magenta: "magenta", cyan: "cyan", white: "white",
  brightBlack: "bblack", brightRed: "bred", brightGreen: "bgreen", brightYellow: "byellow",
  brightBlue: "bblue", brightMagenta: "bmagenta", brightCyan: "bcyan", brightWhite: "bwhite",
};
function termTheme() {
  const cs = getComputedStyle(document.documentElement);
  const th = {};
  for (const [k, v] of Object.entries(TERM_KEYS)) th[k] = cs.getPropertyValue("--term-" + v).trim();
  th.selectionBackground = th.cursor + "4D";      // the cursor colour at 30%
  return th;
}
// the terminal's font is the theme's --mono (read like the colours)
function termFont() {
  return getComputedStyle(document.documentElement).getPropertyValue("--mono").trim()
         || '"IBM Plex Mono", ui-monospace, monospace';
}

// ═══ Rich markdown: live preview on the SAME text document ══════════════════
// The rendered mode is a decoration layer over the markdown source — never a
// second document model. Headings/bold/links/images/todos render in place and
// each construct's raw syntax reappears exactly while your selection touches
// it (the Obsidian model). Because the Y.Text markdown stays the single source
// of truth, multiplayer, vim/agent merges, the indexer and todos all keep
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
      }, () => kbToast("could not copy to clipboard", "err"));
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
      }, () => kbToast("could not copy to clipboard", "err"));
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
      const alt = await kbPrompt("Caption / alt text:", this.alt || "",
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
      if (!(await kbConfirm("Remove this " + this.kind + " from the document?",
                            { title: "Remove", ok: "Remove", danger: true }))) return;
      // take the trailing newline with it, so no blank hole is left behind
      const after = view.state.sliceDoc(r.to, Math.min(r.to + 1, view.state.doc.length));
      view.dispatch({ changes: { from: r.from, to: after === "\n" ? r.to + 1 : r.to } });
      kbToast("Removed (the file itself is untouched)", "ok");
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

class TableWidget extends WidgetType {
  constructor(md, readOnly) { super(); this.md = md; this.readOnly = readOnly; }
  eq(o) { return o.md === this.md && o.readOnly === this.readOnly; }
  toDOM(view) {
    const dom = document.createElement("div");
    dom.className = "cm-table-wrap";
    dom.contentEditable = "false";
    this.build(dom, view);
    return dom;
  }
  updateDOM(dom, view) {
    // a structural edit (add/remove row or column) must always redraw, even
    // though the button press left the caret inside a cell
    if (dom.__force) { dom.__force = false; this.build(dom, view); return true; }
    if (dom.__md === this.md) return true;                    // our own cell writeback
    if (dom.contains(document.activeElement)) return true;    // don't yank a cell mid-typing
    this.build(dom, view);
    return true;
  }
  ignoreEvent() { return true; }

  build(dom, view) {
    dom.__md = this.md;
    dom.replaceChildren();
    const { rows, align } = parseTable(this.md, 0);
    const table = document.createElement("table");
    table.className = "cm-table";
    const ro = this.readOnly;
    // where the caret is, for "insert row below" / "delete this column"
    let focus = { r: 0, c: 0 };

    const model = () => {
      // Re-derive from the CURRENT document — positions move under us.
      const r = nodeRangeAt(view, dom, ["Table"]);
      if (!r) return null;
      const md = view.state.sliceDoc(r.from, r.to);
      return { ...parseTable(md, r.from), from: r.from, to: r.to, md };
    };

    const writeCell = (r, c, text) => {
      const m = model();
      if (!m || !m.rows[r] || !m.rows[r][c]) return;
      const cell = m.rows[r][c];
      const insert = " " + text.trim().replace(/\|/g, "\\|") + " ";
      if (view.state.sliceDoc(cell.from, cell.to) === insert) return;
      // mark the DOM as already reflecting the result, so the re-render this
      // dispatch triggers keeps the live input (and the caret in it)
      const next = m.md.slice(0, cell.from - m.from) + insert + m.md.slice(cell.to - m.from);
      dom.__md = next;
      view.dispatch({ changes: { from: cell.from, to: cell.to, insert } });
    };

    const restructure = (fn) => {
      const m = model();
      if (!m) return;
      const grid = m.rows.map((row) => row.map((cell) => cell.text));
      const al = m.align.slice();
      const before = JSON.stringify([grid, al]);
      fn(grid, al);
      if (JSON.stringify([grid, al]) === before) return;   // guarded op declined
      const md = tableToMarkdown(grid, al);
      dom.__force = true;                                  // redraw even with focus inside
      const want = { r: focus.r, c: focus.c };
      view.dispatch({ changes: { from: m.from, to: m.to, insert: md } });
      // put the caret back where the user was working
      requestAnimationFrame(() => {
        const sel = dom.querySelector(`input[data-cell="${want.r},${want.c}"]`) ||
                    dom.querySelector('input[data-cell="0,0"]');
        if (sel) sel.focus();
      });
    };

    rows.forEach((row, r) => {
      const tr = document.createElement("tr");
      row.forEach((cell, c) => {
        const td = document.createElement(r === 0 ? "th" : "td");
        if (align[c]) td.style.textAlign = align[c];
        if (ro) {
          td.textContent = cell.text;
        } else {
          const inp = document.createElement("input");
          inp.type = "text";
          inp.value = cell.text.replace(/\\\|/g, "|");
          inp.size = Math.max(6, Math.min(40, inp.value.length + 1));
          inp.setAttribute("data-cell", r + "," + c);
          inp.addEventListener("focus", () => { focus = { r, c }; });
          inp.addEventListener("input", () => {
            inp.size = Math.max(6, Math.min(40, inp.value.length + 1));
            clearTimeout(inp._t);
            inp._t = setTimeout(() => writeCell(r, c, inp.value), 400);
          });
          inp.addEventListener("blur", () => {
            clearTimeout(inp._t); writeCell(r, c, inp.value);
          });
          // keys typed in a cell are the table's business, not the editor's
          inp.addEventListener("keydown", (e) => {
            e.stopPropagation();
            const go = (dr, dc) => {
              const sel = dom.querySelector(
                `input[data-cell="${r + dr},${c + dc}"]`);
              if (sel) { e.preventDefault(); clearTimeout(inp._t);
                         writeCell(r, c, inp.value); sel.focus(); sel.select(); }
              return !!sel;
            };
            if (e.key === "Tab") { if (!go(0, e.shiftKey ? -1 : 1)) go(e.shiftKey ? -1 : 1, 0); }
            else if (e.key === "ArrowDown") go(1, 0);
            else if (e.key === "ArrowUp") go(-1, 0);
            else if (e.key === "Enter") {
              e.preventDefault();
              clearTimeout(inp._t); writeCell(r, c, inp.value);
              if (!go(1, 0)) restructure((g) => g.push(g[0].map(() => "")));
            } else if (e.key === "Escape") { inp.blur(); view.focus(); }
          });
          td.appendChild(inp);
        }
        tr.appendChild(td);
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
    btn("+ row", "Insert a row below the one you're in",
        () => restructure((g) => g.splice(Math.max(1, focus.r + 1), 0, g[0].map(() => ""))));
    btn("+ col", "Insert a column to the right", () => restructure((g, al) => {
      g.forEach((row) => row.splice(focus.c + 1, 0, ""));
      al.splice(focus.c + 1, 0, "");
    }));
    btn("− row", "Delete the row you're in (never the header)", () => restructure((g) => {
      if (g.length > 2 && focus.r > 0) g.splice(focus.r, 1);
      else kbToast("A table keeps its header row and one body row", "err");
    }));
    btn("− col", "Delete the column you're in", () => restructure((g, al) => {
      if ((g[0] || []).length > 1) { g.forEach((row) => row.splice(focus.c, 1)); al.splice(focus.c, 1); }
      else kbToast("A table needs at least one column", "err");
    }));
    dom.appendChild(bar);
  }
}

// A table spans whole lines, so its decoration REPLACES line breaks — CodeMirror
// only accepts that from a state field (view plugins may not), which is why
// tables live here instead of inside livePreview().
function buildTableDecos(state) {
  const ranges = [];
  syntaxTree(state).iterate({ enter: (n) => {
    if (n.name !== "Table") return;
    const from = state.doc.lineAt(n.from).from;
    const to = state.doc.lineAt(n.to).to;
    ranges.push(Decoration.replace({
      widget: new TableWidget(state.sliceDoc(from, to), state.readOnly),
      block: true,
    }).range(from, to));
    return false;
  } });
  return Decoration.set(ranges, true);
}

const tableField = StateField.define({
  create: (state) => buildTableDecos(state),
  update(value, tr) {
    // Also rebuild when the SYNTAX TREE changed without the doc changing:
    // CodeMirror parses incrementally, so a table below the fold isn't in the
    // tree yet when a long document opens, and no further edit may ever come.
    if (!tr.docChanged && !tr.reconfigured &&
        syntaxTree(tr.state) === syntaxTree(tr.startState)) return value;
    return buildTableDecos(tr.state);
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
  return "/api/attachment?path=" + encodeURIComponent((dir ? dir + "/" : "") + url);
}

function livePreview(dir) {
  const plugin = ViewPlugin.fromClass(class {
    constructor(view) { this.compute(view); }
    update(u) {
      if (u.docChanged || u.selectionSet || u.viewportChanged) this.compute(u.view);
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
                attributes: { "data-url": url, title: url + "  ·  Ctrl+click or double-click opens" },
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
        if (rel) openDeepLink(rel);
        return true;
      }
    }
    return false;
  };
  // Desktop: Ctrl/Cmd+click or double-click opens (plain click places the
  // cursor). Touch (hover:none): a plain tap opens — the Obsidian-mobile model;
  // to edit a link's text, tap beside it and arrow in.
  const linkClicks = EditorView.domEventHandlers({
    click(e, view) {
      if (e.ctrlKey || e.metaKey || window.matchMedia("(hover: none)").matches) {
        return openLinkAt(view, e);
      }
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
      _mentionCache = (await (await fetch("/api/principals")).json()).users || [];
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

// ---- media drops & screenshot pastes (both modes) --------------------------
// The one-request attachment post, for a backend that has not restarted into
// the chunked endpoints yet.
async function uploadAttachmentSingleShot(dir, file, name) {
  const fd = new FormData();
  fd.append("file", file, name);
  const r = await fetch("/api/upload?dir=" + encodeURIComponent(dir),
                        { method: "POST", body: fd });
  if (r.status === 413) return { error: name + " is larger than this server's single-request limit" };
  return await r.json().catch(() => ({ error: "upload failed" }));
}

async function uploadAndInsert(view, tab, files, pos) {
  if (view.state.readOnly) { kbToast("This document is read-only", "err"); return; }
  for (const f of files) {
    // images, video and audio all EMBED (image syntax renders a player for
    // media); anything else becomes a plain link
    const isImg = /^image\//.test(f.type) ||
      /^(video|audio)\//.test(f.type) ||
      VIDEO_EXT.test(f.name || "") || AUDIO_EXT.test(f.name || "");
    let name = f.name || "";
    // pasted screenshots arrive as a generic "image.png" — make them unique and
    // give the embed a friendly alt instead of the timestamp filename.
    let alt = name;
    if (!name || name === "image.png") {
      name = "pasted-" + new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19) +
             (f.type === "image/jpeg" ? ".jpg" : ".png");
      alt = "screenshot";
    }
    // Dropping a 2 GB video into a document used to look like nothing at all
    // happening for several minutes. It reports in the tray now — live
    // percentage, and a ✕ that really does stop it.
    const dir = dirName(tab.path) || "company";
    const reg = {};
    const tid = trayAdd(name, () => { reg.cancelled = true; if (reg.xhr) reg.xhr.abort(); });
    let j;
    try {
      j = await uploadChunked("/api/upload", dir, f, name,
                              (pct) => trayProgress(tid, pct), { reg, files: true });
      if (j === null) j = await uploadAttachmentSingleShot(dir, f, name);
    } finally {
      trayDone(tid);
    }
    if (!j || !j.ok) {
      if (!(j && j.aborted)) kbToast((j && j.error) || "upload failed", "err");
      continue;
    }
    // The server hands back the raw path ("_files/q3 final (v2).xlsx"). Encode
    // it per segment — same as relLink — so a name with a space or a bracket
    // produces a link markdown can actually parse, and strip brackets out of
    // the label so they can't close the link text early.
    const url = j.link.split("/").map(encodeURIComponent).join("/");
    let snippet = (isImg ? "!" : "") + "[" + alt.replace(/[[\]]/g, "") + "](" + url + ")";
    let at = Math.min(pos, view.state.doc.length);
    // An image reads best as its own block: if we're mid-line, break before it,
    // and always leave a blank line after so following text isn't swallowed.
    if (isImg) {
      const line = view.state.doc.lineAt(at);
      if (at > line.from) snippet = "\n\n" + snippet;
      const after = view.state.sliceDoc(at, Math.min(at + 1, view.state.doc.length));
      snippet += after === "\n" ? "\n" : "\n\n";
    }
    view.dispatch({ changes: { from: at, insert: snippet },
                    selection: { anchor: at + snippet.length } });
    pos = at + snippet.length;
  }
}

// ---- dragging a tree row into a document = link it -------------------------
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|avif|bmp|svg)(\?|#|$)/i;

// A link from a document in `fromDir` to `target`, both knowledgebase paths.
// RELATIVE, not absolute: an image embed is resolved by resolveMediaUrl, which
// only understands a relative target — "/users/…/pic.png" would be handed to
// the browser as a literal src and render broken.
function relLink(fromDir, target) {
  const from = fromDir ? fromDir.split("/") : [];
  const to = target.split("/");
  let i = 0;                                        // shared prefix, never the file itself
  while (i < from.length && i < to.length - 1 && from[i] === to[i]) i++;
  return "../".repeat(from.length - i) +
         to.slice(i).map(encodeURIComponent).join("/");
}

// Insert a markdown link to a knowledgebase path at `pos`. Images, video and
// audio embed (same rule as an upload); everything else — documents, folders,
// spreadsheets — becomes a plain link, so a dropped .md opens as a tab and a
// dropped folder reveals itself in the tree.
function insertPathLink(view, tab, path, pos) {
  if (view.state.readOnly) { kbToast("This document is read-only", "err"); return; }
  if (path === tab.path) { kbToast("That is this document", "err"); return; }
  const name = baseName(path);
  const url = relLink(dirName(tab.path), path);
  const embed = IMAGE_EXT.test(name) || VIDEO_EXT.test(name) || AUDIO_EXT.test(name);
  // A note reads as its title, not its filename; an attachment keeps its
  // extension, which is half of what tells you what it is.
  let snippet = (embed ? "![" : "[") + name.replace(/\.md$/i, "") + "](" + url + ")";
  const at = Math.min(pos, view.state.doc.length);
  if (embed) {
    const line = view.state.doc.lineAt(at);
    if (at > line.from) snippet = "\n\n" + snippet;
    snippet += view.state.sliceDoc(at, Math.min(at + 1, view.state.doc.length)) === "\n" ? "\n" : "\n\n";
  }
  view.dispatch({ changes: { from: at, insert: snippet },
                  selection: { anchor: at + snippet.length } });
  view.focus();
}

function mediaExtension(tab) {
  return EditorView.domEventHandlers({
    drop(e, view) {
      // A row dragged out of the tree carries its path: dropping it into text
      // LINKS the file (dropping it on a folder still moves it — see setupDrop).
      const kbPath = e.dataTransfer ? e.dataTransfer.getData("application/x-kb-path") : "";
      if (kbPath) {
        e.preventDefault();
        const p = view.posAtCoords({ x: e.clientX, y: e.clientY });
        insertPathLink(view, tab, kbPath, p == null ? view.state.selection.main.head : p);
        return true;
      }
      const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
      if (!files.length) return false;
      // A folder dropped into the text would upload as a 0-byte stand-in file.
      // Folders belong in the tree, so say where to drop them instead.
      const dirs = [...((e.dataTransfer && e.dataTransfer.items) || [])]
        .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null))
        .some((en) => en && en.isDirectory);
      if (dirs) {
        e.preventDefault();
        kbToast("Drop a folder onto a folder in the sidebar, not into a document", "err");
        return true;
      }
      e.preventDefault();
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
      uploadAndInsert(view, tab, files, pos == null ? view.state.selection.main.head : pos);
      return true;
    },
    paste(e, view) {
      const items = [...((e.clipboardData && e.clipboardData.items) || [])]
        .filter((i) => i.kind === "file");
      const files = items.map((i) => i.getAsFile()).filter(Boolean);
      // a plain-text paste is either a URL worth linking, or CodeMirror's job
      if (!files.length) return pasteAsLink(e, view);
      e.preventDefault();
      uploadAndInsert(view, tab, files, view.state.selection.main.head);
      return true;
    },
  });
}

// ---- pasting a URL ---------------------------------------------------------
// A bare URL is NOT a link in this dialect — GFM autolinking is not among the
// markdown extensions we load — so a pasted address used to sit there as dead
// text that rendered as dead text. Pasting one now writes the markdown:
// over a selection it becomes that selection's link, which is the gesture
// everyone already has muscle memory for, and on its own it links to itself so
// it is at least clickable.
const PASTED_URL = /^(https?:\/\/|mailto:)[^\s<>]+$/i;

// A destination that survives what people actually paste. CommonMark allows
// balanced parentheses unwrapped, and leaving them alone keeps the source
// readable (Wikipedia's "…_(disambiguation)"); anything else goes in angle
// brackets, with the two characters that would close them percent-encoded.
function mdDestination(url) {
  let depth = 0;
  for (const ch of url) {
    if (ch === "(") depth++;
    else if (ch === ")" && --depth < 0) break;
  }
  if (depth === 0 && !/[<>]/.test(url)) return url;
  return "<" + url.replace(/</g, "%3C").replace(/>/g, "%3E") + ">";
}

function pasteAsLink(e, view) {
  const raw = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
  const url = raw.trim();
  if (!PASTED_URL.test(url)) return false;
  const sel = view.state.selection.main;
  // Inside a code span, a fenced block or an existing link destination, a URL
  // is content and must land exactly as typed — the same rule the mention
  // highlight follows, asked of the same syntax tree.
  if (inCodeOrUrl(syntaxTree(view.state), sel.from)) return false;
  const label = view.state.sliceDoc(sel.from, sel.to);
  // A label carrying a bracket or a newline cannot be a link label; pasting
  // over it plainly is better than writing markdown that will not parse.
  if (/[\[\]\n]/.test(label)) return false;
  const insert = "[" + (label || url) + "](" + mdDestination(url) + ")";
  e.preventDefault();
  view.dispatch({
    changes: { from: sel.from, to: sel.to, insert },
    selection: { anchor: sel.from + insert.length },
    userEvent: "input.paste",
    scrollIntoView: true,
  });
  return true;
}

// ---- editing mode (rich | source) ------------------------------------------
function modeExts(tab) {
  return tab.mode === "rich"
    ? [livePreview(dirName(tab.path)), tableField]
    : [lineNumbers(), highlightActiveLine()];
}

function setMode(m) {
  if (!active || active.kind !== "doc") return;
  active.mode = m;
  try { localStorage.setItem("kbEditMode", m); } catch (e) { /* private mode */ }
  if (active.view && active.modeComp) {
    active.view.dispatch({ effects: active.modeComp.reconfigure(modeExts(active)) });
  }
  updateModeUI();
  if (active.view) active.view.focus();
}

function updateModeUI() {
  const sw = $("#modeswitch"), bar = $("#mdbar");
  const isDoc = !!(active && active.kind === "doc");
  // version history exists for documents and artifacts (what git tracks)
  $("#doc-history").hidden = !(active && (active.kind === "doc" || active.kind === "artifact"));
  sw.hidden = !isDoc;
  if (isDoc) {
    sw.querySelector("#mode-rich").classList.toggle("active", active.mode === "rich");
    sw.querySelector("#mode-source").classList.toggle("active", active.mode !== "rich");
  }
  bar.hidden = !(isDoc && active.mode === "rich" &&
                 active.access && active.access.write);
  document.body.classList.toggle("mdbar-on", !bar.hidden);   // room under the last line
}

// ---- markdown toolbar ------------------------------------------------------
function activeDocView() {
  return active && active.kind === "doc" && active.view && !active.view.state.readOnly
    ? active.view : null;
}

// ---- dictation: where do the spoken words go? ------------------------------
// There is no single "focused pane" in this app — `active` is the active TAB and
// `activeTerm` the active TERMINAL, and neither means "has focus". So ask the
// DOM, using the same probes the keyboard dispatcher and activateTerm() use.
//
// Resolved when recording STARTS, then stashed: the round trip to the server is
// a second or more, and by the time the transcript lands the user may have
// clicked somewhere else entirely. Re-validated at insert time, because the
// target can also disappear in that window.
// Sticky pane memory. On touch, *getting to* the mic button destroys the focus
// evidence: tapping the ⋯ menu blurs the terminal, so by the time recording
// starts, activeElement is a menu button and the resolver used to fall through
// to the open document — spoken words landed in the markdown file instead of
// the shell. Remember the last pane the user MEANINGFULLY interacted with
// (typing, tapping into it); opening menus and tapping toolbar buttons must
// not count, which is exactly why this cannot be document.activeElement.
let lastPane = null;   // {kind:"term", t} | "doc" | {kind:"field", el}
// the terminal tab an element sits in, if any
function termTabOf(el) {
  const host = el && el.closest ? el.closest(".tab-content.term") : null;
  return host ? tabs.find((x) => x.el === host) || null : null;
}
document.addEventListener("focusin", (e) => {
  const el = e.target;
  if (!el || !el.closest) return;
  const tt = termTabOf(el);
  if (tt) { lastPane = { kind: "term", t: tt }; return; }
  if (el.tagName === "TEXTAREA" ||
      (el.tagName === "INPUT" && /^(text|search|url|tel|email)$/.test(el.type))) {
    // Fields inside transient chrome (menus, dialogs) are real dictation
    // targets while open — but must not linger as the sticky pane after the
    // chrome closes. Track them live, revalidate at use.
    lastPane = { kind: "field", el };
    return;
  }
  if (el.closest(".cm-editor")) { lastPane = "doc"; return; }
  // buttons, menus, tabs: leave lastPane alone — that's the whole point
});

function dictationTarget() {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;

  let target = null;
  const inTermTab = el && termTabOf(el);
  if (inTermTab && inTermTab.term)
    target = { kind: "term", t: inTermTab };
  else if (el && (el.tagName === "TEXTAREA" ||
             (el.tagName === "INPUT" && /^(text|search|url|tel|email)$/.test(el.type))))
    target = { kind: "field", el };

  // Focus is on chrome (a menu button, the body): fall back to the last pane
  // the user actually worked in, not to "whatever document happens to be open".
  if (!target && lastPane && lastPane.kind === "term" && terms.includes(lastPane.t) &&
      isDisplayed(lastPane.t))
    target = { kind: "term", t: lastPane.t };
  if (!target && lastPane && lastPane.kind === "field" &&
      document.contains(lastPane.el) && !lastPane.el.disabled &&
      lastPane.el.offsetParent !== null)
    target = { kind: "field", el: lastPane.el };

  if (!target) {
    const v = activeDocView();
    if (v) target = { kind: "doc", view: v };
  }
  // Nothing focused and no writable document: the terminal if it is showing,
  // otherwise nowhere — and "nowhere" is a message, not a silent drop.
  if (!target) {
    const shown = (activeTerm && isDisplayed(activeTerm)) ? activeTerm : terms.find(isDisplayed);
    if (shown) target = { kind: "term", t: shown };
  }

  // Say where the words will land while they can still be stopped: the pill
  // shows "→ terminal" for the whole recording, so a misroute is visible
  // before the text lands instead of after.
  const ind = $("#ptt-target");
  if (ind) ind.textContent =
    target ? { term: "→ terminal", doc: "→ document", field: "→ field" }[target.kind] : "";
  return target;
}

function insertDictation(target, text, audioId) {
  if (!text) { kbToast("Nothing was said", "err"); return; }
  // Keep the transcript BEFORE routing it anywhere. Even a transcript that has
  // nowhere to land — or lands somewhere and gets deleted by a stray swipe — is
  // recoverable from the history for a day.
  dictHistAdd(text, audioId);
  if (!target) {
    kbToast("Nowhere to put that — click into a document or the terminal first", "err");
    return;
  }

  if (target.kind === "term") {
    const t = target.t;
    if (!terms.includes(t) || !t.ws || t.ws.readyState !== 1) {
      kbToast("That terminal isn't connected", "err");
      return;
    }
    // NEVER introduce an implicit Enter. A mis-transcribed command that runs
    // itself has no undo; the human presses Return. `[\r\n]` and not `\r?\n`,
    // because a LONE \r is Enter as far as the PTY is concerned — sanitize() in
    // dictation.js already folds those, and this is the second layer.
    const oneLine = text.replace(/[\r\n]+/g, " ").replace(/[ \t]{2,}/g, " ").trim();
    // term.paste(), not rawSend(): paste() normalizes newlines to \r, adds
    // bracketed-paste framing only when the foreground app actually enabled
    // DECSET 2004, and rewrites any embedded ESC to U+241B — which matters here
    // because the transcript is third-party text. We strip control characters
    // in dictation.js as well; this is the second layer, not the only one.
    t.term.paste(t.term.modes && t.term.modes.bracketedPasteMode ? text : oneLine);
    t.term.focus();
    return;
  }

  if (target.kind === "field") {
    const el = target.el;
    if (!el.isConnected) { kbToast("That field is gone", "err"); return; }
    el.focus();
    // execCommand is deprecated but is still the only thing that preserves the
    // native undo buffer in an input; setRangeText fires no event and pushes no
    // undo entry, so it is the fallback, with the event synthesized by hand.
    try { if (document.execCommand("insertText", false, text)) return; } catch (e) { /* below */ }
    const from = el.selectionStart == null ? el.value.length : el.selectionStart;
    const to = el.selectionEnd == null ? from : el.selectionEnd;
    el.setRangeText(text, from, to, "end");
    el.dispatchEvent(new InputEvent("input",
      { bubbles: true, inputType: "insertText", data: text }));
    return;
  }

  const v = target.view;
  if (!v || v.state.readOnly) { kbToast("That document is read-only", "err"); return; }
  v.focus();
  v.dispatch({
    ...v.state.replaceSelection(text),
    scrollIntoView: true,
    // "input.paste", not "input.type": adjacent input.type transactions get
    // merged into one undo step, so a dictation followed by typing would
    // collapse together and one Ctrl+Z would eat both.
    userEvent: "input.paste",
    effects: EditorView.announce.of("Inserted " + text.length + " characters"),
  });
}

// ---- dictation history -----------------------------------------------------
// A day's worth of transcripts, kept in this browser's localStorage. This is
// the safety net for the two ways a transcript dies young: it landed somewhere
// and got deleted by accident, or recording was cut short (backgrounding the
// app on a phone finishes the recording — see the visibilitychange guard in
// dictation.js) and the text went somewhere unexpected. Text in localStorage
// (blobs would blow its quota); the AUDIO lives in dictation.js's IndexedDB
// vault, and `a` on an entry is the vault id that produced it — while that
// recording is still vaulted, the entry offers a download of the original audio.
const DICT_HIST_KEY = "kbDictHistory";
const DICT_HIST_TTL = 24 * 3600 * 1000;
const DICT_HIST_MAX = 200;                // a chatty day, not an unbounded log

function dictHistLoad() {
  let arr;
  try { arr = JSON.parse(localStorage.getItem(DICT_HIST_KEY) || "[]"); }
  catch (e) { return []; }
  if (!Array.isArray(arr)) return [];
  const cut = Date.now() - DICT_HIST_TTL;
  return arr.filter((e) => e && typeof e.text === "string" && e.t > cut);
}

function dictHistAdd(text, audioId) {
  const arr = dictHistLoad();               // load() already expired the old ones
  arr.unshift(audioId ? { t: Date.now(), text, a: audioId } : { t: Date.now(), text });
  if (arr.length > DICT_HIST_MAX) arr.length = DICT_HIST_MAX;
  try { localStorage.setItem(DICT_HIST_KEY, JSON.stringify(arr)); }
  catch (e) { /* quota or private mode — dictation itself still works */ }
}

async function dictAudioDownload(id, t, mime) {
  const blob = await recordingBlob(id);
  if (!blob) { kbToast("That recording is gone", "err"); return; }
  const stamp = new Date(t).toISOString().slice(0, 19).replace(/[:T]/g, "-");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "dictation-" + stamp + ((mime || "").includes("ogg") ? ".ogg" : ".webm");
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Not immediately: the click starts the download, revoking too early aborts it.
  setTimeout(() => URL.revokeObjectURL(a.href), 30_000);
}

function dictHistAgo(t) {
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  return Math.round(s / 3600) + " h ago";
}

async function openDictHistory() {
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div");
  card.className = "modal-card";
  card.innerHTML = `
    <div class="modal-head"><b>Dictation history</b>
      <span class="muted">this browser only</span>
      <button class="modal-x" title="Close">×</button></div>
    <div class="dh-list" data-testid="dh-list"></div>
    <div class="modal-foot"><button class="modal-close">Close</button></div>`;
  ov.appendChild(card);
  document.body.appendChild(ov);
  card.querySelector(".modal-x").addEventListener("click", () => ov.remove());
  card.querySelector(".modal-close").addEventListener("click", () => ov.remove());

  const list = card.querySelector(".dh-list");
  const recs = await listRecordings();          // the audio vault (dictation.js)
  const entries = dictHistLoad();               // the transcripts (localStorage)
  const vaulted = new Set(recs.map((r) => r.id));

  // Recordings that never became a transcript come FIRST — they are the ones
  // that need an action (transcribe again, download, or discard), and burying
  // them under a day of successful dictations would hide exactly the thing
  // this history exists to rescue.
  const stranded = recs.filter((r) => r.status !== "done");
  if (!stranded.length && !entries.length) {
    list.innerHTML = '<div class="muted">Nothing yet — every dictation is kept here: '
      + 'the transcript for 24 hours, and the audio of anything that failed to '
      + 'transcribe for 7 days, ready to download or try again.</div>';
    return;
  }
  const fmtLen = (r) => {
    const secs = r.ms ? Math.round(r.ms / 1000) : 0;
    const dur = secs ? Math.floor(secs / 60) + ":" + String(secs % 60).padStart(2, "0") + ", " : "";
    return dur + (r.bytes >= 1024 ? Math.round(r.bytes / 1024) + " KB" : r.bytes + " B");
  };
  for (const r of stranded) {
    const row = document.createElement("div");
    row.className = "dh-item dh-rec";
    row.innerHTML = `<div class="dh-text"></div>
      <span class="dh-when">${dictHistAgo(r.t)}</span>
      <span class="dh-actions">
        <button class="mini dh-transcribe" data-testid="dh-transcribe"
                title="Send this recording for transcription again">transcribe</button>
        <button class="mini dh-download" data-testid="dh-download"
                title="Download the audio">download</button>
        <button class="mini dh-delete" title="Discard this recording">✕</button>
      </span>`;
    row.querySelector(".dh-text").textContent =
      (r.status === "interrupted" ? "Recording interrupted — not transcribed"
                                  : "Recording not transcribed") + " (" + fmtLen(r) + ")";
    row.querySelector(".dh-transcribe").addEventListener("click", () => {
      ov.remove();               // so the words land where the user was working
      transcribeRecording(r.id);
    });
    row.querySelector(".dh-download").addEventListener("click",
      () => dictAudioDownload(r.id, r.t, r.mime));
    row.querySelector(".dh-delete").addEventListener("click", () => {
      deleteRecording(r.id);
      row.remove();
    });
    list.appendChild(row);
  }
  for (const e of entries) {
    const row = document.createElement("div");
    row.className = "dh-item";
    const hasAudio = e.a && vaulted.has(e.a);
    row.innerHTML = `<div class="dh-text"></div>
      <span class="dh-when">${dictHistAgo(e.t)}</span>
      <span class="dh-actions">
        ${hasAudio ? '<button class="mini dh-download" title="Download the original audio">audio</button>' : ""}
        <button class="mini dh-copy" title="Copy this transcript">copy</button>
      </span>`;
    row.querySelector(".dh-text").textContent = e.text;
    row.querySelector(".dh-copy").addEventListener("click", () => {
      navigator.clipboard.writeText(e.text)
        .then(() => kbToast("Copied", "ok"), () => kbToast("Clipboard blocked", "err"));
    });
    if (hasAudio) {
      const r = recs.find((x) => x.id === e.a);
      row.querySelector(".dh-download").addEventListener("click",
        () => dictAudioDownload(r.id, r.t, r.mime));
    }
    list.appendChild(row);
  }
}

function tbInline(mark) {
  const v = activeDocView(); if (!v) return;
  const { from, to } = v.state.selection.main;
  const L = mark.length;
  const sel = v.state.sliceDoc(from, to);
  const before = v.state.sliceDoc(Math.max(0, from - L), from);
  const after = v.state.sliceDoc(to, Math.min(v.state.doc.length, to + L));
  if (sel.length >= 2 * L && sel.startsWith(mark) && sel.endsWith(mark)) {
    v.dispatch({ changes: { from, to, insert: sel.slice(L, sel.length - L) },
                 selection: { anchor: from, head: to - 2 * L } });
  } else if (before === mark && after === mark) {
    v.dispatch({ changes: [{ from: from - L, to: from }, { from: to, to: to + L }],
                 selection: { anchor: from - L, head: to - L } });
  } else {
    v.dispatch({ changes: [{ from, insert: mark }, { from: to, insert: mark }],
                 selection: { anchor: from + L, head: to + L } });
  }
  v.focus();
}

function tbLines(fn) {
  const v = activeDocView(); if (!v) return;
  let { from, to } = v.state.selection.main;
  // a double/triple-click line selection includes the trailing newline; that
  // position alone must not pull the NEXT line into a line-wise operation
  // (heading on one selected line used to turn two lines into headings)
  if (to > from && v.state.doc.lineAt(to).from === to) to -= 1;
  const changes = [];
  let line = v.state.doc.lineAt(from);
  for (;;) {
    const c = fn(line);
    if (c) changes.push(c);
    if (line.to >= to) break;
    line = v.state.doc.lineAt(line.to + 1);
  }
  if (changes.length) v.dispatch({ changes });
  v.focus();
}

function tbHeading(n) {
  const want = "#".repeat(n) + " ";
  tbLines((line) => {
    const m = line.text.match(/^(#{1,6})\s+/);
    if (m && m[1].length === n) return { from: line.from, to: line.from + m[0].length, insert: "" };
    if (m) return { from: line.from, to: line.from + m[0].length, insert: want };
    return { from: line.from, insert: want };
  });
}

function tbListPrefix(prefix, matchRe) {
  tbLines((line) => {
    const m = line.text.match(matchRe);
    if (m) return { from: line.from + (m[1] || "").length,
                    to: line.from + m[0].length, insert: "" };
    const ind = (line.text.match(/^\s*/) || [""])[0].length;
    if (!line.text.trim()) return { from: line.from + ind, insert: prefix };
    return { from: line.from + ind, insert: prefix };
  });
}

async function tbLink() {
  const v = activeDocView(); if (!v) return;
  // capture the selection before the dialog takes focus
  const { from, to } = v.state.selection.main;
  const url = await kbPrompt("Link URL:", "https://", { title: "Insert link", ok: "Insert" });
  if (!url) { v.focus(); return; }
  const text = v.state.sliceDoc(from, to) || "link";
  const snippet = "[" + text + "](" + url + ")";
  v.dispatch({ changes: { from, to, insert: snippet },
               selection: { anchor: from + 1, head: from + 1 + text.length } });
  v.focus();
}

function tbHr() {
  const v = activeDocView(); if (!v) return;
  const line = v.state.doc.lineAt(v.state.selection.main.head);
  v.dispatch({ changes: { from: line.to, insert: "\n\n---\n" },
               selection: { anchor: line.to + 6 } });
  v.focus();
}

// Insert a starter grid. It lands as ordinary GFM markdown, which the live
// preview immediately renders as an editable table.
function tbTable() {
  const v = activeDocView(); if (!v) return;
  if (v.state.readOnly) { kbToast("This document is read-only", "err"); return; }
  const line = v.state.doc.lineAt(v.state.selection.main.head);
  const md = "| Column | Column |\n| --- | --- |\n|  |  |\n|  |  |";
  const at = line.to;
  v.dispatch({ changes: { from: at, insert: (line.text.trim() ? "\n\n" : "\n") + md + "\n" } });
  // focus the first cell of the table we just made
  setTimeout(() => {
    const inp = v.dom.querySelector('.cm-table-wrap input[data-cell="0,0"]');
    if (inp) { inp.focus(); inp.select(); }
  }, 40);
}

function wireMdBar() {
  $("#mode-rich").addEventListener("click", () => setMode("rich"));
  $("#mode-source").addEventListener("click", () => setMode("source"));
  const mediaInput = $("#up-media"), fileInput = $("#up-file");
  const pickInto = (input) => {
    const v = activeDocView(); if (!v) return;
    input.onchange = () => {
      if (input.files.length) {
        uploadAndInsert(v, active, [...input.files], v.state.selection.main.head);
      }
      input.value = "";
    };
    input.click();
  };
  const bar = $("#mdbar");
  // On touch the dock is a keyboard accessory row: the most-used first, the
  // mic at the thumb's end, ⋯ for the rest. The markup order is the desktop
  // order; here the buttons are re-seated once for this device.
  if (window.matchMedia("(pointer: coarse)").matches) {
    const first = ["mic", "bold", "italic", "h1", "photo", "attach", "ul", "task", "code"];
    const rest = ["h2", "link", "h3", "strike", "ol", "quote", "table", "hr"];
    const by = (k) => bar.querySelector(`button[data-md="${k}"]`);
    const more = document.createElement("button");
    more.type = "button"; more.dataset.md = "more"; more.className = "mdb-txt";
    more.title = "More formatting"; more.textContent = "⋯";
    for (const k of first) { const b = by(k); if (b) bar.appendChild(b); }
    bar.appendChild(more);
    for (const k of rest) { const b = by(k); if (b) { b.classList.add("mdb-2"); bar.appendChild(b); } }
  }
  // On touch the row belongs to the keyboard. Focus alone lies: Android's back
  // button hides the keyboard and leaves the editor focused, so a focus-keyed
  // row stayed up over nothing. The keyboard itself is measurable — the visual
  // viewport loses a keyboard's worth of height when it opens and gets it back
  // when it closes, on Android (the page resizes) and on iOS (the keyboard
  // overlays, the visual viewport still shrinks). So: focus in the document AND
  // a viewport shorter, by more than any browser chrome, than the tallest it
  // has been at this width. The same numbers lift the row above an overlaying
  // keyboard (iOS), where bottom:0 of the layout would sit beneath it. A tap on
  // the row never blurs the editor (pointerdown below), so using it keeps it.
  // The class is set everywhere; only the touch stylesheet reads it.
  const vv = window.visualViewport;
  const inDoc = (el) => !!(el && el.closest && el.closest(".cm-content"));
  let tallest = 0, tallestW = 0;
  function keyboardGap() {
    // A pinch shrinks the visible viewport exactly as a keyboard does, and
    // it is not one: while the page is zoomed, nothing here applies.
    if (vv && vv.scale > 1.01) return 0;
    const h = vv ? vv.height : window.innerHeight, w = vv ? vv.width : window.innerWidth;
    if (w !== tallestW) { tallestW = w; tallest = 0; }        // rotated: measure afresh
    tallest = Math.max(tallest, h);
    const gap = tallest - h;
    return gap > 150 ? gap : 0;                                // the URL bar is ~60, a keyboard 250+
  }
  function syncKb() {
    const up = inDoc(document.activeElement) && keyboardGap() > 0;
    bar.classList.toggle("kb", up);
    document.body.classList.toggle("kb-up", up);
    const lift = vv ? Math.max(0, window.innerHeight - vv.height - vv.offsetTop) : 0;
    bar.style.setProperty("--kb-lift", up ? lift + "px" : "0px");
  }
  document.addEventListener("focusin", (e) => { if (inDoc(e.target)) { syncKb(); setTimeout(syncKb, 350); } });
  document.addEventListener("focusout", (e) => { if (inDoc(e.target)) setTimeout(syncKb, 60); });
  if (vv) { vv.addEventListener("resize", syncKb); vv.addEventListener("scroll", syncKb); }
  window.addEventListener("resize", syncKb);
  // Quiet while you type: a keystroke in the document dims the dock, the next
  // mouse move brings it back (touch: opacity is pinned to 1 in the stylesheet).
  document.addEventListener("keydown", (e) => {
    if (e.target && e.target.closest && e.target.closest(".cm-content")) bar.classList.add("typing");
  }, true);
  document.addEventListener("mousemove", () => {
    if (bar.classList.contains("typing")) bar.classList.remove("typing");
  }, { passive: true });
  // Like the terminal keybar: pressing a toolbar button must not take focus.
  // Without this a phone tap on B / mic / any button blurs the editor, which
  // drops the visible selection and closes the soft keyboard mid-edit.
  bar.addEventListener("pointerdown", (e) => e.preventDefault());
  bar.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-md]");
    if (!b) return;
    switch (b.dataset.md) {
      case "more": bar.classList.toggle("more"); break;
      case "h1": tbHeading(1); break;
      case "h2": tbHeading(2); break;
      case "h3": tbHeading(3); break;
      case "bold": tbInline("**"); break;
      case "italic": tbInline("*"); break;
      case "strike": tbInline("~~"); break;
      case "code": tbInline("`"); break;
      case "ul": tbListPrefix("- ", /^(\s*)[-*+]\s+(?!\[)/); break;
      case "ol": tbListPrefix("1. ", /^(\s*)\d+\.\s+/); break;
      case "task": tbListPrefix("- [ ] ", /^(\s*)[-*+]\s+\[[ xX]\]\s+/); break;
      case "quote": tbListPrefix("> ", /^(\s*)>\s+/); break;
      case "hr": tbHr(); break;
      case "link": tbLink(); break;
      case "table": tbTable(); break;
      case "photo": pickInto(mediaInput); break;     // touch: camera / library
      case "attach": pickInto(fileInput); break;
    }
  });
}

const $ = (s) => document.querySelector(s);
const wsBase = () => (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host;
const isMobile = () => window.matchMedia("(max-width: 880px)").matches;
const COARSE_PRIMARY = window.matchMedia("(pointer: coarse)").matches;
// A phone: narrow AND touched. A desktop window dragged narrow renders the
// phone layout but keeps its record — the sheet's automatic "full" and its
// remembered mode are a phone's, and would leak into the widened window.
const isPhone = () => isMobile() && COARSE_PRIMARY;

// ═══ Icons ══════════════════════════════════════════════════════════════════
// One consistent stroke family (outline, 24-grid) instead of the mixed
// glyph/emoji set — same visual weight everywhere, color only where it carries
// meaning (artifact = accent, secret = amber).
const svgIcon = (paths) =>
  '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + "</svg>";
const I = {
  chevron: svgIcon('<polyline points="9 18 15 12 9 6"/>'),
  chevronDown: svgIcon('<polyline points="6 9 12 15 18 9"/>'),
  doc: svgIcon('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'),
  file: svgIcon('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>'),
  artifact: svgIcon('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'),
  lock: svgIcon('<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>'),
  term: svgIcon('<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>'),
  chat: svgIcon('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
  plus: svgIcon('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'),
  folderPlus: svgIcon('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="10" x2="12" y2="16"/><line x1="9" y1="13" x2="15" y2="13"/>'),
  upload: svgIcon('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>'),
  folderUp: svgIcon('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><polyline points="9.5 14 12 11.5 14.5 14"/><line x1="12" y1="11.5" x2="12" y2="17"/>'),
  download: svgIcon('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>'),
  mention: svgIcon('<circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8"/>'),
  share: svgIcon('<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/>'),
  trash: svgIcon('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
  more: svgIcon('<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>'),
  pencil: svgIcon('<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>'),
  pin: svgIcon('<path d="M9 4h6l-1 6 4 3v2H6v-2l4-3z"/><line x1="12" y1="15" x2="12" y2="21"/>'),
  eye: svgIcon('<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/>'),
  copy: svgIcon('<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
  paste: svgIcon('<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>'),
  move: svgIcon('<polyline points="15 14 20 9 15 4"/><path d="M4 20v-7a4 4 0 0 1 4-4h12"/>'),
  open: svgIcon('<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>'),
  history: svgIcon('<path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/>'),
  folder: svgIcon('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'),
  command: svgIcon('<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>'),
  keyboard: svgIcon('<rect x="2" y="6" width="20" height="12" rx="2"/><line x1="6" y1="10" x2="6" y2="10"/><line x1="10" y1="10" x2="10" y2="10"/><line x1="14" y1="10" x2="14" y2="10"/><line x1="18" y1="10" x2="18" y2="10"/><line x1="8" y1="14" x2="16" y2="14"/>'),
  mic: svgIcon('<path d="M12 2a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3z"/><path d="M19 10v1a7 7 0 0 1-14 0v-1"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="8.5" y1="22" x2="15.5" y2="22"/>'),
};

function treeIcon(n) {
  if (isSecretPath(n.path)) return I.lock;
  if (n.dir) return n.name === "_secrets" ? I.lock : "";
  return n.kind === "artifact" ? I.artifact : n.kind === "md" ? I.doc : I.file;
}

// ---- session restore: reopen where you left off ---------------------------
// Open tabs, the active one, terminal session ids and panel state persist in
// localStorage; on boot everything reopens — terminals REATTACH to their
// still-running shells (the backend keeps them alive across disconnects).
let _restoring = false;

// The layout as layout.js understands it, read off the DOM-side records.
function currentLayout() {
  const specOf = (t) => viewKind(t.kind).serialize(t);
  const groupOf = (p) => {
    const list = paneTabs(p);
    return { id: p.gid, size: p.grow, active: Math.max(0, list.indexOf(p.active)), tabs: list.map(specOf),
             collapsed: !!p.collapsed };
  };
  const focused = focusedPane();
  const max = maximizedPaneId ? paneById(maximizedPaneId) : null;
  return {
    v: 2,
    columns: columns.map((c) => ({ size: c.grow, groups: c.panes.map(groupOf) })),
    dock: { side: dock.side, size: dock.size, collapsed: dockHidden(),
            group: dockPane ? groupOf(dockPane) : { id: "dock", size: 1, active: 0, tabs: [] } },
    focused: focused ? focused.gid : null,
    maximized: max ? max.gid : null,
  };
}

function saveSession() {
  if (_restoring) return;
  try {
    // v2 (the layout) beside every v1 field derived from it — a browser still
    // holding the previous bundle restores the tabs and the terminals from the
    // shadow and loses only the stacking (see layout.js).
    localStorage.setItem("kbOpen",
      JSON.stringify(serializeLayout(currentLayout(), active ? active.path : null)));
  } catch (e) { /* private mode */ }
}

// Restoring the last session is two phases, because its two halves wait on
// different things — and used to wait on each other's.
//
// Phase 1, at t=0: the grid, its groups and the document tabs, from
// localStorage alone. openView registers a tab and draws its row synchronously
// before its first await, so the whole tab bar — the right tab active — is on
// screen in the first frame; only the documents' contents are still on their
// way. A terminal tab is drawn too, as a "⟳ name" placeholder in its group,
// and comes alive in phase 2. Nothing here needs to know who you are: an
// expired session bounces on the first 401 whichever request it is, and your
// name reaches the collaboration session the moment whoami answers
// (t.announce, from loadWhoami).
function restoreTabs() {
  let raw, rec;
  try { raw = localStorage.getItem("kbOpen"); rec = JSON.parse(raw || "null"); } catch (e) { return null; }
  if (!raw || !rec) return null;
  const layout = parseLayout(rec);
  if (!layout) return null;
  _restoring = true;
  const gmap = buildLayout(layout);   // group id → pane
  const mounts = [], pending = [];
  const openInto = (p, specs) => {
    for (const spec of specs) {
      const K = viewKind(spec.kind);
      const clean = K ? K.restore(spec) : null;
      if (!clean) continue;
      if (spec.kind === "term") { pending.push(pendingTerminal(p, clean)); continue; }
      // Open every saved tab CONCURRENTLY: mapping over the list preserves
      // order while firing all the props/epoch/websocket round-trips at once.
      mounts.push(openView(spec.kind, clean, p.id));
    }
  };
  for (const [gid, p] of gmap) {
    const g = gid === "dock" ? layout.dock.group : findLayoutGroup(layout, gid);
    if (g) openInto(p, g.tabs);
  }
  renderTabBar();
  // The tab you were on, from the first frame — not the last one opened.
  const first = typeof rec.active === "string" && tabs.find((x) => x.path === rec.active && !paneOf(x).collapsed);
  if (first) activateTab(first);
  // A restore that will want a terminal starts fetching its chunk now, so the
  // wait for whoami and the wait for xterm overlap instead of adding up.
  if (pending.length) warmTerminal().catch(() => { /* reported on use */ });
  return { layout, rec, mounts: Promise.allSettled(mounts), pending, gmap };
}

// The grid the layout describes, replacing the empty boot pane. Returns the
// map from the layout's group ids to the panes made for them.
function buildLayout(layout) {
  const chrome = [document.querySelector(".doc-title-bar"), $("#mdbar")].filter(Boolean);
  for (const el of chrome) el.remove();          // kept aside while the boot pane goes
  for (const c of columns.slice()) c.el.remove();
  columns.length = 0; panes.length = 0;
  activePaneId = focusedPaneId = null; maximizedPaneId = null;
  const gmap = new Map();
  layout.columns.forEach((col, i) => {
    const c = makeColumn(i);
    c.grow = col.size;
    col.groups.forEach((g, j) => {
      const p = insertPaneAt(c, j);
      p.grow = g.size; p.gid = g.id;
      if (g.collapsed) { p.collapsed = true; p.el.hidden = true; }
      gmap.set(g.id, p);
    });
  });
  gmap.set("dock", dockPane);
  dock.side = layout.dock.side; dock.size = layout.dock.size;
  applyDockSide();
  dockPane.collapsed = layout.dock.collapsed;
  dockPane.el.hidden = layout.dock.collapsed;
  if (!layout.dock.collapsed && isPhone()) setTermMax(preferredTermMax());
  keepOneOpen();
  const fp = gmap.get(layout.focused);
  const open = panes.find((p) => !p.collapsed) || panes[0];
  focusedPaneId = fp && !fp.collapsed ? fp.id : open.id;
  activePaneId = open.id;
  if (layout.maximized) { const mp = gmap.get(layout.maximized); if (mp && !mp.collapsed) maximizedPaneId = mp.id; }
  normalizeSplits();
  const home = panes[0];
  for (const el of chrome) { if (el.id === "mdbar") home.el.appendChild(el); else home.el.insertBefore(el, home.hostEl); }
  return gmap;
}

// Phase 2, once whoami has answered (canShell, the pty protocol version) and
// the terminal UI is wired: the terminals — straight away, not after every
// document's websocket has connected, which is what they used to queue behind
// and never needed. Then the tabs' contents, and the tidy-up that depends on
// them (a group whose files all vanished collapses, the saved per-group
// selection comes back).
async function restoreRest(h) {
  if (!h) return;
  const { layout, rec, mounts, pending, gmap } = h;
  try {
    if (pending.length) {
      if (!canShell) { for (const t of pending) dropTab(t); }
      else {
        for (const t of pending) {
          if (!tabs.includes(t)) continue;
          let ok = false;
          try { ok = await attachTerminal(t, undefined); } catch (e) { ok = false; }
          if (!ok) { for (const x of pending) if (tabs.includes(x) && x.pending) dropTab(x); break; }
        }
      }
    }
    await mounts;
    for (const p of panes.slice()) if (!paneTabs(p).length) paneEmptied(p, true);
    keepOneOpen();
    if (!paneTabs(dockPane).length && !dockHidden()) hideTerminalPanel(true);
    // The active document first — one in view: a document in a folded
    // group is never it, and new documents open beside one that is shown.
    const inView = (t) => !paneOf(t).collapsed;
    const act = typeof rec.active === "string" && tabs.find((x) => x.path === rec.active && inView(x));
    if (act) activateTab(act);
    else if (tabs.length) activateTab(firstDocTab(paneTabs(panes[0]).filter(inView)) || firstDocTab(tabs.filter(inView)) || null);
    if (activePane().collapsed) activePaneId = (panes.find((p) => !p.collapsed) || panes[0]).id;
    // Then each group shows the tab it showed — after, because activating
    // the document pulls its group's view onto it, and a document read
    // behind a terminal in its own group belongs behind it.
    for (const [gid, p] of gmap) {
      if (!paneById(p.id)) continue;
      const g = gid === "dock" ? layout.dock.group : findLayoutGroup(layout, gid);
      const want = g && paneTabs(p)[g.active];
      if (want) p.active = want;
    }
    if (isPhone() && paneTabs(dockPane).length) liftPanelOntoPhone();
    showEachPanesTab();
    const fp = gmap.get(layout.focused);
    if (fp && paneById(fp.id) && !fp.collapsed) focusedPaneId = fp.id;
    const dockTerm = dockPane.active && dockPane.active.kind === "term" ? dockPane.active : null;
    const shownTerm = dockTerm || terms.find(isDisplayed) || null;
    if (shownTerm && shownTerm.term) noteActiveTerm(shownTerm);
    normalizeSplits();
    renderTabBar();
    refitDisplayedTerminals();
  } finally {
    _restoring = false;
    saveSession();
  }
}

// ---- in-app dialogs & toasts ----------------------------------------------
// Native alert/confirm/prompt look foreign and block the whole tab; these are
// promise-based drop-ins on the app's own modal system. Enter confirms,
// Escape (or the scrim) cancels.
function kbDialog(opts) {
  return new Promise((resolve) => {
    const ov = document.createElement("div");
    ov.className = "modal-overlay dlg-overlay";
    const card = document.createElement("div");
    card.className = "modal-card dlg-card";
    card.setAttribute("data-testid", "dlg");
    if (opts.title) {
      const h = document.createElement("div");
      h.className = "dlg-title"; h.textContent = opts.title;
      card.appendChild(h);
    }
    if (opts.message) {
      const m = document.createElement("div");
      m.className = "dlg-msg"; m.textContent = opts.message;
      card.appendChild(m);
    }
    let input = null;
    if (opts.input) {
      input = document.createElement("input");
      input.className = "dlg-input";
      input.setAttribute("data-testid", "dlg-input");
      input.value = opts.input.value || "";
      input.placeholder = opts.input.placeholder || "";
      input.spellcheck = false;
      card.appendChild(input);
    }
    const foot = document.createElement("div");
    foot.className = "modal-foot";
    const done = (val) => {
      document.removeEventListener("keydown", onKey, true);
      ov.remove();
      resolve(val);
    };
    if (opts.cancel !== null) {
      const c = document.createElement("button");
      c.textContent = opts.cancel || "Cancel";
      c.setAttribute("data-testid", "dlg-cancel");
      c.addEventListener("click", () => done(null));
      foot.appendChild(c);
    }
    if (opts.alt) {
      const al = document.createElement("button");
      al.textContent = opts.alt;
      al.setAttribute("data-testid", "dlg-alt");
      al.addEventListener("click", () => done(DLG_ALT));
      foot.appendChild(al);
    }
    const ok = document.createElement("button");
    ok.className = "primary" + (opts.danger ? " danger" : "");
    ok.textContent = opts.ok || "OK";
    ok.setAttribute("data-testid", "dlg-ok");
    ok.addEventListener("click", () => done(input ? input.value : true));
    foot.appendChild(ok);
    card.appendChild(foot);
    ov.appendChild(card);
    document.body.appendChild(ov);
    ov.addEventListener("click", (e) => { if (e.target === ov) done(null); });
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(null); }
      else if (e.key === "Enter" && (!input || document.activeElement === input)) {
        e.preventDefault(); e.stopPropagation(); done(input ? input.value : true);
      }
    };
    document.addEventListener("keydown", onKey, true);
    if (input) { input.focus(); input.select(); } else ok.focus();
  });
}
const kbAlert = (message, title) =>
  kbDialog({ title: title || "Notice", message, cancel: null }).then(() => undefined);
// Three-way ask. Needed where the middle option is not "no": moving something
// into a folder with a different audience is a choice between two real moves.
const DLG_ALT = Object.freeze({ alt: true });
const kbChoose = (message, o) =>
  kbDialog({ title: (o && o.title) || "Confirm", message, ok: o && o.ok,
             alt: o && o.alt, danger: !!(o && o.danger) })
    .then((v) => (v === DLG_ALT ? "alt" : v !== null ? "ok" : null));
const kbConfirm = (message, o) =>
  kbDialog({ title: (o && o.title) || "Confirm", message, ok: o && o.ok,
             danger: !!(o && o.danger) }).then((v) => v !== null);
const kbPrompt = (message, value, o) =>
  kbDialog({ title: o && o.title, message, ok: o && o.ok,
             input: { value, placeholder: o && o.placeholder } });

function toastHost() {
  let host = document.getElementById("toasts");
  if (!host) {
    host = document.createElement("div");
    host.id = "toasts";
    document.body.appendChild(host);
  }
  return host;
}

// ---- the upload tray -------------------------------------------------------
// An upload into the TREE haunts the folder it is landing in (a ghost row with
// a live percentage). An upload started from inside a DOCUMENT — a dropped
// video, a pasted screenshot — has no row to haunt, and for a big file that
// meant a long silence in which nothing on screen said anything was happening.
// It reports here instead: one live bar per file, in the corner, cancellable.
const _tray = new Map();     // id -> {name, pct, cancel}
let _traySeq = 0;

function trayHost() {
  let host = document.getElementById("uptray");
  if (!host) {
    host = document.createElement("div");
    host.id = "uptray";
    host.setAttribute("data-testid", "upload-tray");
    // inside the toast column, so the two never fight over the same corner
    toastHost().prepend(host);
  }
  return host;
}

function trayRender() {
  const host = trayHost();
  host.textContent = "";
  for (const [id, u] of _tray) {
    const row = document.createElement("div");
    row.className = "uprow";
    row.dataset.upload = String(id);
    row.style.setProperty("--pct", (u.pct || 0) + "%");
    const spin = document.createElement("span");
    spin.className = "upspin";
    const name = document.createElement("span");
    name.className = "upname";
    name.textContent = u.name;
    const pct = document.createElement("span");
    pct.className = "upct";
    pct.textContent = u.pct == null ? "waiting…" : u.pct + "%";
    row.append(spin, name, pct);
    if (u.cancel) {
      const x = document.createElement("button");
      x.className = "upcancel";
      x.title = "Cancel this upload";
      x.setAttribute("aria-label", "Cancel upload of " + u.name);
      x.textContent = "✕";
      x.onclick = () => u.cancel();
      row.appendChild(x);
    }
    host.appendChild(row);
  }
  host.classList.toggle("on", _tray.size > 0);
}

function trayAdd(name, cancel) {
  const id = ++_traySeq;
  _tray.set(id, { name, pct: null, cancel });
  trayRender();
  return id;
}

// the hot path: touch the two nodes that change, never the whole tray
function trayProgress(id, pct) {
  const u = _tray.get(id);
  if (!u) return;
  u.pct = pct;
  const row = trayHost().querySelector('.uprow[data-upload="' + id + '"]');
  if (!row) return trayRender();
  row.style.setProperty("--pct", pct + "%");
  const el = row.querySelector(".upct");
  if (el) el.textContent = pct + "%";
}

function trayDone(id) {
  _tray.delete(id);
  trayRender();
}

function kbToast(msg, kind, action) {
  const host = toastHost();
  const t = document.createElement("div");
  t.className = "toast" + (kind ? " " + kind : "");
  t.setAttribute("data-testid", "toast");
  t.textContent = msg;
  // one action, for the thing you just did and might not have meant (Undo).
  // It lives in the toast because a dialog before every delete is the tax
  // this replaces.
  if (action && action.label) {
    const b = document.createElement("button");
    b.className = "toast-act"; b.type = "button"; b.textContent = action.label;
    b.setAttribute("data-testid", "toast-act");
    b.addEventListener("click", () => { t.remove(); action.fn(); });
    t.appendChild(b);
  }
  host.appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 350); }, action ? 8000 : 4200);
}

// ---- chrome: the mobile file-tree drawer, and the user menu (bottom left) ---
// Same DOM on every screen size — CSS turns the sidebar into a drawer and the
// action buttons into a dropdown below 880px, so nothing here forks by device.
function closeNav() { document.body.classList.remove("nav-open"); }
function wireNav() {
  // ☰: the drawer on a phone (opened onto the file you are in, centred — not
  // wherever the tree happened to be scrolled last time), the collapsible
  // file panel on a desktop; the corner's ☰ brings the panel back.
  $("#nav-btn").addEventListener("click", toggleSidebar);
  const badge = $("#access-badge");   // the pen / eye: not a decoration — the sharing panel is behind it
  if (badge) {
    badge.addEventListener("click", () => { if (active) openPerms(active.path); });
    badge.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && active) { e.preventDefault(); openPerms(active.path); } });
  }
  const cn = $("#corner-nav");
  if (cn) cn.addEventListener("click", toggleSidebar);
  $("#scrim").addEventListener("click", closeNav);
  // The user menu, bottom left of the sidebar: everything that is not a file
  // or the search lives one click behind the person. Hidden until asked, gone
  // again the moment an action is chosen (each opens its own surface) — and
  // on a phone the drawer goes with it, so the surface is what you see next.
  const menu = $("#user-menu"), who = $("#user-btn");
  const setOpen = (on) => {
    menu.hidden = !on;
    who.setAttribute("aria-expanded", on ? "true" : "false");
    who.classList.toggle("open", on);
  };
  who.addEventListener("click", (e) => { e.stopPropagation(); setOpen(menu.hidden); });
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target)) setOpen(false);
  });
  menu.addEventListener("click", (e) => {
    if (e.target.closest("button, a")) { setOpen(false); closeNav(); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.hidden) { e.stopPropagation(); setOpen(false); who.focus(); }
  }, true);
}

// ---- sidebar width: dragged, and remembered ------------------------------
// Which files you keep open decides how wide the tree needs to be, and that
// does not change between sessions — so the width is a local preference, not
// app state: stored in localStorage, restored before the first paint of the
// layout, and clamped to the window so a width dragged on a 34" screen never
// leaves a laptop with no editor left.
const SBW_KEY = "kbSidebarW";
// Whether the file panel is collapsed (☰ / Alt+B on a desktop) is remembered
// the same way, and applied before the first paint so nothing jumps.
const NAV_KEY = "kbNavHidden";
try {
  if (localStorage.getItem(NAV_KEY) === "1" && window.matchMedia("(min-width: 881px)").matches)
    document.body.classList.add("nav-hidden");
} catch (e) { /* private mode */ }
function setNavHidden(on) {
  document.body.classList.toggle("nav-hidden", on);
  try { localStorage.setItem(NAV_KEY, on ? "1" : "0"); } catch (e) { /* private mode */ }
}
const SBW_DEFAULT = 264, SBW_MIN = 140;
const sbwMax = () => Math.max(SBW_MIN, Math.round(window.innerWidth * 0.6));

function setSidebarWidth(px, persist) {
  const w = Math.round(Math.min(Math.max(px, SBW_MIN), sbwMax()));
  document.documentElement.style.setProperty("--sbw", w + "px");
  if (persist) { try { localStorage.setItem(SBW_KEY, String(w)); } catch (e) { /* private mode */ } }
  return w;
}

function restoreSidebarWidth() {
  let w = SBW_DEFAULT;
  try { w = parseInt(localStorage.getItem(SBW_KEY), 10) || SBW_DEFAULT; } catch (e) { /* ok */ }
  setSidebarWidth(w, false);
}
// Applied at module eval, not in boot(): boot awaits the network, and a tree
// that snaps from 264px to your width after the first fetch is a visible jump.
restoreSidebarWidth();

// The theme is a setting (kb_platform/settings.py: ui.theme), applied at module
// eval from the cached value so the first paint is already in the right colours,
// then kept in step with the server. One theme today; the attribute is the hook
// the theme step fills with CSS.
function applyTheme(t) { document.documentElement.dataset.theme = t || "deep-blue"; }
applyTheme(settings.get("ui.theme"));
settings.subscribe("ui.theme", (t) => { applyTheme(t); retintTerminals(); });

// Per-token overrides on top of the chosen theme (ui.theme.custom): inline
// custom properties on <html>, before first paint from the cache and live
// after. Only the registry's whitelist of base tokens ever lands here; the
// stylesheet derives the rest.
let _overrideKeys = [];
function applyThemeOverrides(map) {
  const root = document.documentElement.style;
  for (const k of _overrideKeys) root.removeProperty("--" + k);
  _overrideKeys = [];
  for (const [k, v] of Object.entries(map || {})) { root.setProperty("--" + k, v); _overrideKeys.push(k); }
}
applyThemeOverrides(settings.get("ui.theme.custom"));
settings.subscribe("ui.theme.custom", (m) => { applyThemeOverrides(m); retintTerminals(); });

// The brand is a company setting too: the product name next to the mark (and
// the tab's title), and the logo — /brand/logo serves the uploaded file or the
// built-in mark, so the markup never changes; a custom one just gets a fresh
// cache-buster and loses the light-theme inversion meant for the white mark.
function applyBrand() {
  const name = settings.get("brand.name") || "Company OS";
  for (const el of document.querySelectorAll('[data-brand="name"]')) el.textContent = name;
  document.title = name;
  const st = settings.state();
  const custom = !!settings.get("brand.logo");
  for (const img of document.querySelectorAll(".logo-word, .login-logo")) {
    img.classList.toggle("custom", custom);
    const want = "/brand/logo" + (custom ? "?v=" + encodeURIComponent((st && st.logoRev) || 0) : "");
    if (img.getAttribute("src") !== want) img.setAttribute("src", want);
  }
}
applyBrand();
settings.subscribe("brand.name", applyBrand);
settings.subscribe("brand.logo", applyBrand);
settings.subscribe("*", (k) => { if (k === null) applyBrand(); });   // a re-upload moves logoRev only

function wireSidebarResize() {
  const bar = $("#sb-resizer");
  if (!bar) return;   // cached older app.html
  let startX = 0, startW = 0;
  const onMove = (e) => setSidebarWidth(startW + (e.clientX - startX), false);
  const onUp = (e) => {
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", onUp);
    document.body.classList.remove("sb-resizing");
    setSidebarWidth(startW + (e.clientX - startX), true);
    // panes size themselves off the editor column, and xterm needs telling
    window.dispatchEvent(new Event("resize"));
  };
  bar.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || isMobile()) return;
    e.preventDefault();
    startX = e.clientX;
    startW = $(".sidebar").getBoundingClientRect().width;
    document.body.classList.add("sb-resizing");
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
  });
  bar.addEventListener("dblclick", () => {
    setSidebarWidth(SBW_DEFAULT, true);
    window.dispatchEvent(new Event("resize"));
  });
  // keyboard: the handle is a real focusable separator, so arrows resize it
  bar.addEventListener("keydown", (e) => {
    const step = e.key === "ArrowLeft" ? -16 : e.key === "ArrowRight" ? 16 : 0;
    if (!step) return;
    e.preventDefault();
    setSidebarWidth($(".sidebar").getBoundingClientRect().width + step, true);
  });
  // a window narrowed after the fact must not keep a tree wider than the app
  window.addEventListener("resize", () => {
    const cur = parseInt(getComputedStyle(document.documentElement)
      .getPropertyValue("--sbw"), 10) || SBW_DEFAULT;
    if (cur > sbwMax()) setSidebarWidth(cur, false);
    // …nor a tab strip frozen at a width the old window justified
    unlockAllTabStrips();
  });
}

// ---- identity -------------------------------------------------------------
let canShell = true;   // false for viewer accounts (no terminal, no cron)

async function loadWhoami() {
  const r = await fetch("/api/whoami");
  const j = await r.json();
  canShell = j.shell !== false;
  // Known BEFORE any terminal is restored — a restore that raced the separate
  // /api/cron answer reattached the old way, a hard reset instead of replay.
  if (typeof j.v === "number") { backendV = j.v; _bvAt = Date.now(); }
  $("#whoami").textContent = j.user + " · uid " + j.uid;
  $("#user-name").textContent = j.user;
  $("#user-avatar").textContent = (j.user || "?").slice(0, 1).toUpperCase();
  whoamiUser = j.user;
  renderTabBar();
  window.__kbuser = j.user;
  // Tabs restored before this answer announced themselves as "user"; tell the
  // collaboration sessions, and the avatar row, who you actually are.
  for (const t of tabs) if (t.announce) t.announce();
  renderPresence();
  repaintMentions();    // "@you" glows; until we know who you are, it cannot
  restoreTreeState();   // before the first loadTree() render (boot awaits us first)
  if (!canShell) {
    $("#toggleterm").hidden = true;
    $("#cron-btn").hidden = true;
  }
}

// ---- file tree ------------------------------------------------------------
let _lastTreeJson = "";
let _lastTreeData = null;

function rerenderTree() {
  if (!_lastTreeData) return;
  const host = $("#tree");
  host.innerHTML = "";
  // Top-level areas (company/projects/users) are never deletable from the UI.
  host.appendChild(renderNodes(_lastTreeData, false));
  if (active) {                                  // re-apply selection highlight after re-render
    const el = host.querySelector('.tree-item[data-path="' + cssEsc(active.path) + '"]');
    if (el) el.classList.add("active");
  }
  updateFoldButton();
  updateTreePresence();
  paintTreeCursor();   // the keyboard cursor survives the 4s refresh
}

let _lastTreePaths = null;   // paths seen in the previous tree, to detect removals

function collectTreePaths(nodes, set) {
  for (const n of nodes || []) {
    set.add(n.path);
    if (n.dir) collectTreePaths(n.children, set);
  }
  return set;
}

// A file another user renamed or deleted disappears from the tree — retire any
// tab still holding it (instead of leaving a dead editor whose edits reach a
// room whose backing file is gone). Only prune paths that were present LAST
// poll and are gone NOW, so a file we just created isn't pruned before it lands.
function pruneVanishedTabs(newPaths) {
  if (!_lastTreePaths) return;
  for (const t of tabs.slice()) {
    if (t.path && t.path.includes("/") && _lastTreePaths.has(t.path) && !newPaths.has(t.path) &&
        !_movingPaths.has(t.path)) {
      const wasActive = active === t;
      closeTab(t);
      if (wasActive) kbToast(baseName(t.path) + " was moved or deleted", "err");
    }
  }
}

// Folders whose name starts with "_" or "." (attachments' _files/, _secrets/,
// .claude/, …) hold machinery, not the notes you came for — start them
// collapsed. Applied ONCE per folder the first time it appears, so re-opening
// one (or the 4s tree poll) never fights the user's choice.
const _seenDirs = new Set();
function applyDefaultCollapse(nodes) {
  for (const n of nodes || []) {
    if (!n.dir) continue;
    if (!_seenDirs.has(n.path)) {
      _seenDirs.add(n.path);
      if (n.name.startsWith("_") || n.name.startsWith(".")) collapsed.add(n.path);
    }
    applyDefaultCollapse(n.children);
  }
}

// The tree's ETag from its last full answer, sent back as If-None-Match. An
// unchanged tree — which is nearly every poll — is then a 304: no 640 KB
// parsed, no re-stringified diff, no Set of every path rebuilt, every 4 s,
// per tab. `cache: "no-store"` keeps the browser's own HTTP cache out of it:
// left in, the browser answers the 304 from ITS copy and hands us a 200 to
// parse all over again. A backend from before the ETag simply answers 200
// every time, exactly as it always did.
let _treeEtag = null;

async function fetchTree(fresh) {
  window.__kbtreefetches = (window.__kbtreefetches || 0) + 1;   // test hook
  const headers = _treeEtag ? { "If-None-Match": _treeEtag } : {};
  const r = await fetch("/api/tree" + (fresh ? "?fresh=1" : ""), { headers, cache: "no-store" });
  if (r.status === 304) return null;              // what we have is current
  if (!r.ok) throw new Error("tree " + r.status);
  _treeEtag = r.headers.get("ETag");
  return r.json();
}

async function loadTree(force) {
  let j;
  try { j = await fetchTree(!!force); } catch (e) { return; }
  applyTree(j, force);
}

// ---- live tree updates (from /api/events; see events.js) -----------------
// A full tree waits for a pause in typing: rebuilding 2,600 rows under a
// keystroke is exactly the stutter this replaces. The document and the
// terminal count; a dialog's input does not — the name you just typed into
// "New file" is the very row you are waiting to see.
let _lastKey = 0, _treeRefreshTimer = null;
document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && t.closest && t.closest(".cm-content, .xterm-helper-textarea")) _lastKey = Date.now();
}, true);
function refreshTreeWhenIdle() {
  const wait = 1500 - (Date.now() - _lastKey);
  if (wait > 0) {
    clearTimeout(_treeRefreshTimer);
    _treeRefreshTimer = setTimeout(refreshTreeWhenIdle, wait);
    return;
  }
  _treeRefreshTimer = null;
  loadTree(false);
}
function findTreeNode(nodes, path) {
  for (const n of nodes || []) {
    if (n.path === path) return n;
    if (n.dir && path.startsWith(n.path + "/")) {
      const hit = findTreeNode(n.children, path);
      if (hit) return hit;
    }
  }
  return null;
}
// Only file timestamps moved (someone typed): patch those rows in place and
// adopt the server's new ETag, so the next catch-up is a 304. No rebuild.
function patchTreeMtimes(changed, etag) {
  for (const c of changed) {
    const node = findTreeNode(_lastTreeData, c.path);
    if (node) node.mtime = c.mtime;
    const el = document.querySelector('.tree-item[data-path="' + cssEsc(c.path) + '"] .tmtime');
    if (el) {
      el.textContent = fmtMtime(c.mtime);
      el.title = "Last modified " + new Date(c.mtime * 1000).toLocaleString();
    }
  }
  if (etag) _treeEtag = etag;
  _lastTreeJson = null;      // the next full tree must repaint, whatever it hashes to
}
let _presencePoll = null;

function applyTree(j, force) {
  if (j === null) {                                // 304, or the fetch failed
    if (force && _lastTreeData) rerenderTree();   // "reload" still repaints
    return;
  }
  const newPaths = collectTreePaths(j.tree, new Set());
  pruneVanishedTabs(newPaths);
  _lastTreePaths = newPaths;
  applyDefaultCollapse(j.tree);
  const sig = JSON.stringify(j.tree);
  if (!force && sig === _lastTreeJson) return;   // nothing changed -> no re-render (no flicker)
  _lastTreeJson = sig;
  _lastTreeData = j.tree;
  rerenderTree();
}

// collapse/expand ALL folders, VS-Code explorer style: one smart button —
// collapses while anything is open, expands once everything is folded
function allDirPaths(nodes, out) {
  for (const n of nodes || []) {
    if (n.dir) { out.push(n.path); allDirPaths(n.children, out); }
  }
  return out;
}

function updateFoldButton() {
  const b = $("#tree-fold");
  if (!b || !_lastTreeData) return;
  const dirs = allDirPaths(_lastTreeData, []);
  const anyOpen = dirs.some((p) => !collapsed.has(p));
  b.textContent = anyOpen ? "⊟" : "⊞";
  b.title = anyOpen ? "Collapse all folders" : "Expand all folders";
}

function toggleFoldAll() {
  if (!_lastTreeData) return;
  const dirs = allDirPaths(_lastTreeData, []);
  if (dirs.some((p) => !collapsed.has(p))) dirs.forEach((p) => collapsed.add(p));
  else collapsed.clear();
  rerenderTree();
}
function cssEsc(s) { return s.replace(/["\\]/g, "\\$&"); }
const collapsed = new Set();  // folder paths the user has collapsed

// ---- persisted tree state ----
// Which folders are open/collapsed survives a reload (per browser, like the
// other kb* prefs). The snapshot is tagged with the login it belongs to: a
// different user on the same browser starts from the defaults instead of
// inheriting — or leaking — the previous user's folder layout.
let _treeSaveTimer = null;
function saveTreeStateSoon() {
  clearTimeout(_treeSaveTimer);
  _treeSaveTimer = setTimeout(() => {
    try {
      localStorage.setItem("kbTreeState", JSON.stringify({
        user: window.__kbuser || "",
        collapsed: [...collapsed].slice(0, 3000),
        seen: [..._seenDirs].slice(0, 3000),
      }));
    } catch (e) { /* private mode */ }
  }, 250);
}
// Every mutation persists — wrapping the Set beats chasing every call site
// (caret clicks, fold-all, reveal-on-open, ghosts, default collapse).
for (const m of ["add", "delete", "clear"]) {
  const orig = collapsed[m].bind(collapsed);
  collapsed[m] = (...a) => { const r = orig(...a); saveTreeStateSoon(); return r; };
}
// Called from loadWhoami — after the username is known, before the first tree
// render. Restoring `seen` too keeps applyDefaultCollapse from re-collapsing
// a _files/ or .claude/ the user deliberately opened in an earlier session.
function restoreTreeState() {
  let s = null;
  try { s = JSON.parse(localStorage.getItem("kbTreeState") || "null"); }
  catch (e) { /* private mode */ }
  if (!s || s.user !== (window.__kbuser || "") ||
      !Array.isArray(s.collapsed) || !Array.isArray(s.seen)) return;
  for (const p of s.seen) if (typeof p === "string") _seenDirs.add(p);
  for (const p of s.collapsed) if (typeof p === "string") collapsed.add(p);
}

function mkBtn(html, title, fn) {
  const b = document.createElement("button");
  b.className = "tbtn"; b.innerHTML = html; b.title = title;
  b.setAttribute("aria-label", title);
  b.addEventListener("click", fn);
  return b;
}

// ---- context menu (right-click / long-press on tree rows) ------------------
let _ctxMenu = null;
let _ctxOpenedAt = 0;      // a menu ignores the scroll its own opening caused
function closeCtxMenu() {
  if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; }
}

function openCtxMenu(items, x, y) {
  closeCtxMenu();
  const m = document.createElement("div");
  m.className = "ctx-menu";
  m.setAttribute("data-testid", "ctx-menu");
  for (const it of items) {
    if (it === "-") {
      const s = document.createElement("div");
      s.className = "ctx-sep";
      m.appendChild(s);
      continue;
    }
    if (it.note) {                       // a line that explains, with nothing to click
      const n = document.createElement("div");
      n.className = "ctx-note";
      n.textContent = it.note;
      m.appendChild(n);
      continue;
    }
    const b = document.createElement("button");
    b.className = "ctx-item" + (it.danger ? " danger" : "");
    b.innerHTML = (it.icon || "") + "<span></span>";
    b.querySelector("span").textContent = it.label;
    b.addEventListener("click", (e) => { e.stopPropagation(); closeCtxMenu(); it.fn(); });
    m.appendChild(b);
  }
  document.body.appendChild(m);
  _ctxOpenedAt = performance.now();
  const r = m.getBoundingClientRect();
  m.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  m.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  m.querySelector(".ctx-item")?.focus();  // Tab/arrows walk the menu; Escape closes it
  _ctxMenu = m;
}

function openTreeMenu(n, parentWritable, x, y) {
  const canW = !!(n.access && n.access.write);
  const items = [];
  if (n.dir) {
    if (canW) {
      items.push({ icon: I.plus, label: "New file", fn: () => newFileIn(n.path) });
      items.push({ icon: I.folderPlus, label: "New folder", fn: () => newFolderIn(n.path) });
      items.push({ icon: I.upload, label: "Upload files", fn: () => uploadInto(n.path) });
      items.push({ icon: I.folderUp, label: "Upload folder", fn: () => uploadFolderInto(n.path) });
      if (fileClipboard) {
        items.push({ icon: I.paste, label: "Paste " + baseName(fileClipboard.path),
                     fn: () => pasteInto(n.path) });
      }
      items.push("-");
    }
    // reading is enough to take a copy away — same bar as downloading one file
    items.push({ icon: I.download, label: "Download as ZIP", fn: () => downloadFolderZip(n) });
    items.push("-");
  } else {
    items.push({ icon: I.open, label: "Open", fn: () => openEntry(n) });
    items.push({ icon: I.download, label: "Download",
                 fn: () => { location.href = "/api/attachment?dl=1&path=" + encodeURIComponent(n.path); } });
    items.push("-");
  }
  const pinned = pinnedTargets().has(n.path);
  items.push({ icon: I.pin, label: pinned ? "Unpin from the sidebar" : "Pin to the sidebar",
               fn: () => (pinned ? unpinPath(n.path) : pinPath(n.path, !!n.dir)) });
  items.push({ icon: I.copy, label: "Copy", fn: () => copyEntry(n) });
  if (parentWritable) {
    items.push({ icon: I.pencil, label: "Rename…", fn: () => renameEntry(n) });
    items.push({ icon: I.move, label: "Move to…", fn: () => moveToEntry(n) });
  }
  items.push("-");
  items.push({ icon: I.share, label: "Share — who can open this", fn: () => openPerms(n.path) });
  if (parentWritable && n.path.includes("/")) {
    items.push("-");
    items.push({ icon: I.trash, label: n.dir ? "Delete folder" : "Delete", danger: true,
                 fn: () => deleteEntry(n) });
  }
  openCtxMenu(items, x, y);
}

// ---- file clipboard + rename / move / copy ---------------------------------
let fileClipboard = null;   // {path, dir}
const _movingPaths = new Set();   // paths mid-move, so the tree-prune poll skips them

// A tab's kind follows its extension/location — the SAME logic the tree uses —
// so a renamed/moved file opens in the right surface (a .md that lands in
// _secrets/ becomes a masked secret, not a dead collaborative editor).
function kindForPath(p) {
  if (isSecretPath(p)) return "secret";
  if (p.endsWith(".html")) return "artifact";
  return "doc";
}

// Ask first, THEN navigate. A browser sent to a download that answers with an
// error JSON navigates away to display it, taking the whole app — open tabs,
// terminals and all — with it. The probe answers the same 403/404/413 without
// building anything, so only a download that will actually arrive is started.
async function downloadFolderZip(n) {
  const q = "/api/folder-zip?path=" + encodeURIComponent(n.path);
  let info = {};
  try {
    const r = await fetch(q + "&probe=1");
    try { info = await r.json(); } catch (e) { info = {}; }
    if (!r.ok) { kbToast(info.error || "that folder cannot be downloaded", "err"); return; }
  } catch (e) {
    kbToast("that folder cannot be downloaded", "err");
    return;
  }
  kbToast("Zipping " + baseName(n.path) + " — the download starts when it is ready");
  location.href = q;
}

function copyEntry(n) {
  fileClipboard = { path: n.path, dir: !!n.dir };
  kbToast("Copied — right-click a folder and Paste", "ok");
}

async function moveEntry(srcPath, dst) {
  dst = dst.replace(/\/{2,}/g, "/");
  if (isSecretPath(srcPath) && !isSecretPath(dst)) {
    if (!await kbConfirm(
      "This moves it out of _secrets/ — from then on it WILL appear in git history and search. Move it anyway?",
      { title: "Leaving secrets", ok: "Move", danger: true })) return false;
  }
  // keep affected editor tabs open, re-homed at the new path
  const affected = tabs.filter((t) => t.path === srcPath || t.path.startsWith(srcPath + "/"));
  const keepPath = active ? active.path : null;
  const reopen = affected.map((t) => ({ old: t.path, pane: t.paneId }));
  affected.forEach((t) => _movingPaths.add(t.path));
  // Ask BEFORE the move when the destination has a different audience. A drag
  // is the one gesture where "publish my private note to the whole team" can
  // happen by accident, and the answer decides what the backend does with the
  // permissions, so it cannot be an after-the-fact toast.
  let audience = "destination";
  let pv = null;
  try {
    const r0 = await fetch("/api/fs/move-preview", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ src: srcPath, dst }),
    });
    if (r0.ok) pv = await r0.json();
  } catch (e) { /* advisory only — the move still works without the preview */ }
  if (pv && pv.changes) {
    const ans = await kbChoose(
      `Now: open to ${shWho(pv.from)}.\n` +
      `${pv.dst_folder}: open to ${shWho(pv.to)}.` +
      (pv.mine ? "" : "\n\nYou don't own this, so its permissions can't be changed — it will move as it is."),
      { title: "This changes who can open it",
        ok: pv.mine ? "Move & hand over" : "Move",
        alt: pv.mine ? "Move, keep as it is" : null });
    if (ans === null) { affected.forEach((t) => _movingPaths.delete(t.path)); return false; }
    if (ans === "alt") audience = "keep";
  }
  let j, r;
  try {
    r = await fetch("/api/fs/rename", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ src: srcPath, dst, audience }),
    });
    j = await r.json().catch(() => ({}));
  } catch (e) {
    affected.forEach((t) => _movingPaths.delete(t.path));
    kbToast("could not reach the server — is your session still open?", "err");
    return false;
  }
  if (!r.ok) {
    affected.forEach((t) => _movingPaths.delete(t.path));
    kbToast(r.status === 404 ? "move needs an updated backend — reload the page"
                             : (j.error || "could not move"), "err");
    return false;
  }
  const remap = (p) => dst + p.slice(srcPath.length);
  for (const t of affected) closeTab(t);
  for (const it of reopen) {
    const np = remap(it.old);
    try { await openPath(np, kindForPath(np), it.pane); } catch (e) { /* gone */ }
  }
  affected.forEach((t) => _movingPaths.delete(t.path));
  // restore whatever tab was active before (re-homed if it was one that moved)
  if (keepPath) {
    const want = affected.some((t) => t.path === keepPath) ? remap(keepPath) : keepPath;
    const t = tabs.find((x) => x.path === want);
    if (t) activateTab(t);
  }
  loadTree(true);
  await repointPins(srcPath, dst);
  return true;
}

// A pin points at a path, so moving or renaming what it points at must carry
// the pin with it — krystof moved a pinned file into company/ and had to pin
// it again by hand (2026-09-21). `to` null means it is gone: drop the pin.
// Your own list always; the company list only if you may write it.
async function repointPins(from, to) {
  const remap = (b) => {
    if (b.kind === "term") return b;
    if (b.target === from) return to === null ? null : { ...b, target: to };
    if (b.target.startsWith(from + "/"))
      return to === null ? null : { ...b, target: to + b.target.slice(from.length) };
    return b;
  };
  for (const scope of ["mine", "company"]) {
    if (scope === "company" && !isAdmin) continue;
    const list = (scope === "company" ? launchers.company : launchers.mine) || [];
    const next = list.map(remap).filter(Boolean);
    if (next.length === list.length && next.every((b, i) => b.target === list[i].target)) continue;
    await savePins(scope, next);
  }
}

async function renameEntry(n) {
  const cur = baseName(n.path);
  const name = await kbPrompt("New name:", cur, { title: "Rename " + cur, ok: "Rename" });
  if (!name || !name.trim() || name.trim() === cur) return;
  if (name.includes("/")) { kbToast("a name cannot contain / — use Move to… instead", "err"); return; }
  const dir = dirName(n.path);
  await moveEntry(n.path, (dir ? dir + "/" : "") + name.trim());
}

// ---- choosing a path, over the tree the panel already holds ----------------
// The palette searches everything (names AND contents) and OPENS what you
// pick. This is the other half: pick a path and hand it back. The chat uses
// it for "add a file" and for a chat's working folder.
function pickPath(opts = {}) {
  const dirs = opts.kind === "dir";
  const entries = [];
  const walk = (nodes) => {
    for (const n of nodes || []) {
      // the dot-dirs the tree hides stay hidden here too (`.*` shows them)
      if (!showHidden && n.name.startsWith(".")) continue;
      // …and a secret is findable in the tree, never through a list like this
      // one, exactly as the palette has it
      if (isSecretPath(n.path)) continue;
      // moving a folder: itself and everything under it are not destinations
      if (opts.exclude && (n.path === opts.exclude || n.path.startsWith(opts.exclude + "/"))) continue;
      if (n.dir) { if (dirs) entries.push(n.path); walk(n.children); }
      else if (!dirs) entries.push(n.path);
    }
  };
  walk(_lastTreeData || []);
  if (dirs) entries.unshift("");                    // the knowledgebase itself
  return new Promise((resolve) => {
    let sel = 0, shown = entries, done = false;
    const ov = document.createElement("div");
    ov.className = "modal-overlay palette-overlay";
    ov.setAttribute("data-testid", "pickpath");
    const card = document.createElement("div");
    card.className = "modal-card palette-card pick-card";
    const head = document.createElement("div");
    head.className = "palette-head";
    const kind = document.createElement("span");
    kind.className = "palette-kind";
    kind.innerHTML = dirs ? I.folder : I.file;
    const input = document.createElement("input");
    input.className = "palette-input";
    input.setAttribute("data-testid", "pickpath-input");
    input.placeholder = opts.placeholder || (dirs ? "Filter folders…" : "Filter files…");
    input.spellcheck = false; input.autocomplete = "off";
    const x = document.createElement("button");
    x.className = "modal-x palette-x"; x.textContent = "×"; x.title = "Close";
    x.setAttribute("aria-label", "Close");
    head.append(kind, input, x);
    const list = document.createElement("div");
    list.className = "palette-list";
    list.setAttribute("data-testid", "pickpath-list");
    const foot = document.createElement("div");
    foot.className = "palette-foot";
    foot.innerHTML = "<span>↑↓ move · ⏎ choose · esc cancel</span>";
    card.append(head, list, foot);
    ov.appendChild(card);
    document.body.appendChild(ov);

    const finish = (v) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey, true);
      ov.remove();
      resolve(v === undefined ? null : v);
    };
    const label = (path) => (path ? baseName(path) : "Company OS");
    const render = () => {
      const q = input.value.trim().toLowerCase();
      shown = (q ? entries.filter((e) => e.toLowerCase().includes(q)) : entries).slice(0, 300);
      if (sel >= shown.length) sel = Math.max(0, shown.length - 1);
      list.innerHTML = "";
      if (!shown.length) {
        const none = document.createElement("div");
        none.className = "palette-empty muted"; none.textContent = "No matches";
        list.appendChild(none);
        return;
      }
      shown.forEach((path, i) => {
        const row = document.createElement("div");
        row.className = "palette-item" + (i === sel ? " sel" : "");
        row.setAttribute("data-testid", "pickpath-item");
        row.dataset.path = path;
        const ic = document.createElement("span");
        ic.className = "pi-icon"; ic.innerHTML = dirs ? I.folder : I.file;
        const body = document.createElement("span");
        body.className = "pi-body";
        const main = document.createElement("span");
        main.className = "pi-main"; main.textContent = label(path);
        body.appendChild(main);
        const parent = dirName(path);
        if (parent || !path) {
          const sub = document.createElement("span");
          sub.className = "pi-sub"; sub.textContent = path ? parent : "the whole knowledgebase";
          body.appendChild(sub);
        }
        row.append(ic, body);
        row.addEventListener("click", () => finish(path));
        list.appendChild(row);
      });
      const cur = list.children[sel];
      if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: "nearest" });
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(null); return; }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        sel = Math.max(0, Math.min(shown.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
        render();
        return;
      }
      if (e.key === "Enter") { e.preventDefault(); if (shown.length) finish(shown[sel]); }
    };
    input.addEventListener("input", () => { sel = 0; render(); });
    x.addEventListener("click", () => finish(null));
    ov.addEventListener("click", (e) => { if (e.target === ov) finish(null); });
    document.addEventListener("keydown", onKey, true);
    render();
    input.focus();
  });
}

async function moveToEntry(n) {
  // Typing a destination path was the worst input in the app: you had to know
  // the folder by heart and spell it. Pick it from the folders that exist.
  const dest = await pickPath({ kind: "dir", exclude: n.dir ? n.path : null,
                                placeholder: "Move “" + baseName(n.path) + "” to…" });
  if (dest === null) return;
  const d = dest.replace(/^\/+|\/+$/g, "");
  if (d === dirName(n.path)) return;
  await moveEntry(n.path, (d ? d + "/" : "") + baseName(n.path));
}

async function pasteInto(folder) {
  if (!fileClipboard) return;
  const base = baseName(fileClipboard.path);
  // only split off an extension for files — a folder name is taken whole
  const dot = fileClipboard.dir ? -1 : base.lastIndexOf(".");
  const stem = dot > 0 ? base.slice(0, dot) : base;
  const ext = dot > 0 ? base.slice(dot) : "";
  const names = [base];
  for (let i = 1; i <= 8; i++) names.push(stem + " copy" + (i > 1 ? " " + i : "") + ext);
  for (const cand of names) {
    if (folder + "/" + cand === fileClipboard.path) continue;  // pasting beside the original
    let r, j;
    try {
      r = await fetch("/api/fs/copy", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ src: fileClipboard.path, dst: folder + "/" + cand }),
      });
    } catch (e) { kbToast("could not reach the server", "err"); return; }
    if (r.status === 409) continue;   // taken — try the next "copy" name
    j = await r.json().catch(() => ({}));
    if (!r.ok) {
      kbToast(r.status === 404 ? "paste needs an updated backend — reload the page"
                               : (j.error || "could not paste"), "err");
      return;
    }
    kbToast("Pasted as " + cand, "ok");
    loadTree(true);
    return;
  }
  kbToast("too many copies of that name here already", "err");
}

// ---- uploads into the tree, with visible progress -------------------------
// While a file uploads, its target folder shows a GHOST ROW: semi-transparent,
// spinner, live percentage, and a progress fill. The registry (dir -> uploads)
// survives the 4s tree re-render; the ghost leaves when the real row lands.
const _pendingUploads = new Map();   // dir -> Map(id -> {name, pct})
let _upSeq = 0;

function renderGhostRows(dir) {
  const frag = document.createDocumentFragment();
  const pend = _pendingUploads.get(dir);
  if (!pend) return frag;
  for (const [id, u] of pend) {
    const row = document.createElement("div");
    row.className = "tree-item file uploading";
    row.dataset.upload = id;
    if (u.pct != null) row.style.setProperty("--pct", u.pct + "%");
    const spin = document.createElement("span");
    spin.className = "upspin";
    const label = document.createElement("span");
    label.className = "tlabel"; label.textContent = u.name;
    const pct = document.createElement("span");
    pct.className = "upct";
    pct.textContent = u.pct == null ? "waiting…" : u.pct + "%";
    row.append(spin, label, pct);
    frag.appendChild(row);
  }
  return frag;
}

function updateGhostRow(id, pct) {
  for (const m of _pendingUploads.values()) {
    const u = m.get(id);
    if (u) u.pct = pct;
  }
  for (const el of document.querySelectorAll(
    '.tree-item.uploading[data-upload="' + id + '"]')) {
    el.style.setProperty("--pct", pct + "%");
    const p = el.querySelector(".upct");
    if (p) p.textContent = pct + "%";
  }
}

// ---- the wire ---------------------------------------------------------------
// fetch() can't report request-body progress — XHR is still the only way, and
// progress is the whole point here.
function xhrPost(url, body, opts = {}) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    if (opts.json) xhr.setRequestHeader("content-type", "application/json");
    if (opts.onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) opts.onProgress(e.loaded, e.total);
      };
    }
    const parsed = () => {
      try { return JSON.parse(xhr.responseText) || {}; } catch (e) { return {}; }
    };
    xhr.onload = () => resolve({ status: xhr.status, json: parsed() });
    xhr.onerror = () => resolve({ status: 0, json: {}, network: true });
    xhr.ontimeout = () => resolve({ status: 0, json: {}, network: true });
    xhr.onabort = () => resolve({ status: 0, json: {}, aborted: true });
    if (opts.reg) opts.reg.xhr = xhr;
    if (opts.reg && opts.reg.cancelled) return resolve({ status: 0, json: {}, aborted: true });
    xhr.send(body);
  });
}

// ---- chunked uploads --------------------------------------------------------
// One POST cannot carry a big file: the tunnel in front of this box rejects a
// request body over 100 MB, which is what "larger than the server's upload
// limit" always was — a limit no server here had set. So the file is sliced and
// sent a chunk at a time. Every chunk carries its byte offset, which makes a
// retry harmless (the server pwrites it to the same place), so a dropped
// connection costs one chunk instead of the upload.
//
// `base` is "/fs/upload" (into a folder, via the hub) or "/api/upload" (an
// attachment in the document's _files/, as the user). Resolves to the finish
// payload on success, or {error} — and returns null if the server has no
// chunked endpoints at all, which is the caller's cue to post it in one piece.
const UPLOAD_CHUNK_TRIES = 5;

async function uploadChunked(base, dir, file, name, onPct, opts = {}) {
  const reg = opts.reg || {};
  const begin = await xhrPost(base + "/begin", JSON.stringify(
    { dir, name, size: file.size, files: opts.files !== false }), { json: true, reg });
  if (begin.status === 404) return null;          // server predates chunking
  if (begin.status < 200 || begin.status >= 300 || !begin.json.id) {
    return { error: begin.json.error || (begin.aborted ? "cancelled" : "upload could not start"),
             status: begin.status, aborted: begin.aborted };
  }
  const id = begin.json.id;
  const abort = () => xhrPost(base + "/abort", JSON.stringify({ id }), { json: true });
  const size = Math.max(1, file.size);            // an empty file is still 100% done
  const chunk = Math.min(Math.max(begin.json.chunk_size || 8 << 20, 1 << 18),
                         begin.json.max_chunk || 32 << 20);
  let offset = begin.json.offset || 0;
  let tries = 0;
  while (offset < file.size) {
    const end = Math.min(offset + chunk, file.size);
    const at = offset;
    const r = await xhrPost(
      base + "/chunk?id=" + encodeURIComponent(id) + "&offset=" + at,
      file.slice(at, end),
      { reg, onProgress: (loaded) => onPct(Math.min(99, Math.floor((100 * (at + loaded)) / size))) });
    if (r.status >= 200 && r.status < 300) {
      offset = typeof r.json.offset === "number" ? r.json.offset : end;
      tries = 0;
      onPct(Math.min(99, Math.floor((100 * offset) / size)));
      continue;
    }
    if (r.aborted) { abort(); return { error: "cancelled", aborted: true }; }
    // 409 means the server and we disagree about how far we got — it wins.
    const resumable = (r.network || r.status >= 500 || r.status === 409) &&
                      r.status !== 507;
    if (typeof r.json.offset === "number") offset = Math.min(r.json.offset, file.size);
    if (!resumable || ++tries >= UPLOAD_CHUNK_TRIES) {
      abort();
      return { error: r.json.error || (r.network ? "the connection dropped" : "upload failed"),
               status: r.status };
    }
    await new Promise((res) => setTimeout(res, 400 * tries));   // back off, then resume
  }
  const fin = await xhrPost(base + "/finish", JSON.stringify({ id }), { json: true, reg });
  if (fin.status < 200 || fin.status >= 300 || !fin.json.ok) {
    abort();
    return { error: fin.json.error || (fin.aborted ? "cancelled" : "upload failed"),
             status: fin.status, aborted: fin.aborted };
  }
  onPct(100);
  return fin.json;
}

// The legacy single-shot post, kept as the fallback for a server that has not
// been redeployed yet (the browser reloads its bundle well before every
// backend on the box has restarted).
function uploadSingleShot(folder, file, onPct, reg) {
  const fd = new FormData();
  fd.append("file", file, file.name);
  return xhrPost("/fs/upload?dir=" + encodeURIComponent(folder), fd, {
    reg,
    onProgress: (loaded, total) => onPct(Math.round((100 * loaded) / total)),
  }).then((r) => ({ status: r.status, error: r.json.error || (r.network ? "network error" : null) }));
}

// Upload one file INTO A FOLDER (the tree's path), reporting 0–100.
async function uploadWithProgress(folder, file, onPct) {
  const r = await uploadChunked("/fs/upload", folder, file, file.name, onPct,
                                { files: false });
  if (r === null) return uploadSingleShot(folder, file, onPct);
  return r.error ? { status: r.status || 0, error: r.error, aborted: r.aborted }
                 : { status: 200, error: null };
}

// ---- folder uploads --------------------------------------------------------
// A FOLDER upload is a list of files each carrying a path relative to the drop
// target ("sub/deep/a.png"). Two things make that work: every folder on those
// paths is created first (so an empty folder arrives too, and so /fs/upload
// always has a real `dir=`), and the per-file POST names the subfolder.
const UPLOAD_MAX_FILES = 2000;    // a mis-dropped home directory is not an upload
const UPLOAD_MAX_DEPTH = 24;
// Machinery, not content: a nested `.git` would become a gitlink in the KB's own
// audit repo, and `.DS_Store`/`Thumbs.db` are pure noise on every Mac/Windows drop.
const UPLOAD_SKIP = new Set([".git", ".DS_Store", "Thumbs.db"]);

// Every ancestor folder of every file, so `a/b/c.md` also asks for `a` and `a/b`.
function ancestorDirs(items) {
  const s = new Set();
  for (const it of items) {
    const parts = it.rel.split("/");
    parts.pop();
    let cur = "";
    for (const p of parts) { cur = cur ? cur + "/" + p : p; s.add(cur); }
  }
  return [...s];
}

// Create the folder skeleton, parents first. An existing folder (409) is a
// success — dropping a folder next to one that is already there must merge, and
// a subfolder we couldn't create is remembered so its files are skipped rather
// than silently landing in the wrong place.
async function ensureFolders(folder, dirs) {
  const failed = new Set();
  const ordered = [...new Set(dirs)].filter(Boolean).sort(
    (a, b) => a.split("/").length - b.split("/").length || a.localeCompare(b));
  for (const d of ordered) {
    const parent = d.includes("/") ? d.slice(0, d.lastIndexOf("/")) : "";
    if (parent && failed.has(parent)) { failed.add(d); continue; }
    const r = await fetch("/api/fs/mkdir", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: folder + "/" + d }),
    });
    if (r.ok || r.status === 409) continue;
    const j = await r.json().catch(() => ({}));
    kbToast(j.error || ("could not create folder " + d), "err");
    failed.add(d);
  }
  return failed;
}

// Shared by drag-drop and the ⇪ picker: register every ghost up front (later
// ones say "waiting…"), then upload one at a time.
// `files` is a list of File (flat, landing directly in `folder`) or of
// {file, rel} for a folder upload, where `rel` is the path under `folder`.
// `extraDirs` carries folders that hold no files at all — otherwise a dropped
// empty folder would vanish.
async function uploadMany(folder, files, extraDirs = []) {
  const items = files.map((f) => (f instanceof File ? { file: f, rel: f.name } : f));
  if (!items.length && !extraDirs.length) return 0;
  const dirs = [...ancestorDirs(items), ...extraDirs];
  const m = _pendingUploads.get(folder) || new Map();
  _pendingUploads.set(folder, m);
  const ids = items.map((it) => {
    const id = ++_upSeq;
    m.set(id, { name: it.rel, pct: null });
    return id;
  });
  collapsed.delete(folder);   // the user should SEE the ghosts appear
  rerenderTree();
  let ok = 0;
  try {
    const failed = dirs.length ? await ensureFolders(folder, dirs) : new Set();
    for (let i = 0; i < items.length; i++) {
      const { file: f, rel } = items[i], id = ids[i];
      const sub = rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : "";
      if (sub && failed.has(sub)) { m.delete(id); continue; }
      updateGhostRow(id, 0);
      const r = await uploadWithProgress(sub ? folder + "/" + sub : folder, f,
                                         (pct) => updateGhostRow(id, pct));
      if (r.status >= 200 && r.status < 300) ok++;
      else if (r.aborted) { /* the user pressed ✕ — they know */ }
      else if (r.status === 413) kbToast(rel + " was refused in one piece — reload the "
                                         + "page to upload it in chunks", "err");
      else kbToast(r.error || "could not upload " + rel, "err");
      m.delete(id);
    }
  } finally {
    ids.forEach((id) => m.delete(id));
    if (!m.size) _pendingUploads.delete(folder);
    await loadTree(true);      // the real rows replace the ghosts in one render
  }
  return ok;
}

// Pick files for a folder — the click-path twin of drag-and-drop (and the only
// path on touch devices, which cannot drag files onto the tree).
function uploadInto(folder) {
  const input = $("#up-tree");
  input.onchange = async () => {
    const files = [...input.files];
    input.value = "";
    const ok = await uploadMany(folder, files);
    if (ok) kbToast("Uploaded " + ok + (ok > 1 ? " files" : " file") + " to " + folder, "ok");
  };
  input.click();
}

// Pick a whole FOLDER — `webkitdirectory` hands us every file inside it with a
// `webkitRelativePath`, i.e. the tree to recreate. (The browser asks for
// confirmation itself, so there is no second dialog here.) Empty subfolders are
// invisible to the picker; the drag-drop path does carry them.
function uploadFolderInto(folder) {
  const input = $("#up-tree-dir");
  input.onchange = async () => {
    const picked = [...input.files];
    input.value = "";
    const items = picked
      .map((f) => ({ file: f, rel: f.webkitRelativePath || f.name }))
      .filter((it) => !it.rel.split("/").some((seg) => UPLOAD_SKIP.has(seg)));
    if (!items.length) { kbToast("That folder has nothing to upload", "err"); return; }
    if (items.length > UPLOAD_MAX_FILES) {
      kbToast("That folder has " + items.length + " files — the limit is " +
              UPLOAD_MAX_FILES + " per upload", "err");
      return;
    }
    const top = items[0].rel.split("/")[0];
    const ok = await uploadMany(folder, items);
    if (ok) kbToast("Uploaded " + top + " (" + ok + (ok > 1 ? " files" : " file") +
                    ") to " + folder, "ok");
  };
  input.click();
}

// ---- dropped folders -------------------------------------------------------
// `dataTransfer.files` cannot describe a folder (a dropped directory shows up
// as a useless zero-byte entry), so walk the webkitGetAsEntry() tree instead.
function _entryFile(entry) {
  return new Promise((res) => entry.file((f) => res(f), () => res(null)));
}
// readEntries() returns a batch at a time (~100 in Chromium) and signals the
// end with an empty one — a single call silently truncates a big folder.
function _readAllEntries(reader) {
  return new Promise((res, rej) => {
    const out = [];
    const step = () => reader.readEntries((batch) => {
      if (!batch.length) { res(out); return; }
      out.push(...batch);
      step();
    }, rej);
    step();
  });
}
async function _walkEntry(entry, prefix, acc, depth) {
  if (acc.files.length >= UPLOAD_MAX_FILES || UPLOAD_SKIP.has(entry.name)) return;
  if (entry.isFile) {
    const f = await _entryFile(entry);
    if (f) acc.files.push({ file: f, rel: prefix + entry.name });
    return;
  }
  if (!entry.isDirectory || depth >= UPLOAD_MAX_DEPTH) return;
  const dir = prefix + entry.name;
  acc.dirs.push(dir);
  let kids;
  try { kids = await _readAllEntries(entry.createReader()); }
  catch (e) { kbToast("could not read " + dir, "err"); return; }
  for (const k of kids) await _walkEntry(k, dir + "/", acc, depth + 1);
}
// Returns {files: [{file, rel}], dirs: [rel]} for a drop of any mix of files and
// folders. The entry list MUST be taken synchronously: `dataTransfer.items` is
// emptied as soon as the drop event's task ends, so nothing may be awaited first.
async function collectDropped(dt) {
  const entries = [...((dt && dt.items) || [])]
    .filter((i) => i.kind === "file")
    .map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null));
  const plain = [...((dt && dt.files) || [])];
  if (!entries.some(Boolean)) {    // no entries API — flat files are all we get
    return { files: plain.map((f) => ({ file: f, rel: f.name })), dirs: [] };
  }
  const acc = { files: [], dirs: [] };
  for (const e of entries) if (e) await _walkEntry(e, "", acc, 0);
  return acc;
}

// hidden (dot-prefixed) entries are filtered client-side; the sidebar's `.*`
// toggle reveals them (default off — machinery like .claude and artifacts'
// .ll/ working folders stay out of sight)
let showHidden = false;
try { showHidden = localStorage.getItem("kbShowHidden") === "1"; } catch (e) { /* private mode */ }

// The last-modified stamp on a file row. Short enough to sit beside a name
// without pushing it out — the exact timestamp is on hover. Folders have no
// stamp: a folder's own mtime tracks its listing, not the work inside it.
function fmtMtime(sec) {
  const d = new Date(sec * 1000), now = new Date();
  if (d.toDateString() === now.toDateString())
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  return d.toLocaleDateString([], d.getFullYear() === now.getFullYear()
    ? { day: "numeric", month: "short" }
    : { day: "numeric", month: "short", year: "2-digit" });
}

function renderNodes(nodes, parentWritable) {
  const frag = document.createDocumentFragment();
  for (const n of nodes) {
    if (!showHidden && n.name.startsWith(".")) continue;
    const row = document.createElement("div");
    row.className = "tree-item " + (n.dir ? "dir" : "file") +
      (isSecretPath(n.path) ? " secretpath" :
        n.kind === "artifact" ? " artifact" : n.kind === "file" ? " generic" : "");
    row.dataset.path = n.path;
    const label = document.createElement("span");
    label.className = "tlabel"; label.textContent = n.name;
    const iconHtml = treeIcon(n);
    const icon = document.createElement("span");
    icon.className = "ticon";
    if (iconHtml) icon.innerHTML = iconHtml;
    // A quiet mark when this row's audience differs from the folder it sits in.
    // Deliberately absent for everything that just follows its folder — a badge
    // on every row is a badge on none.
    const aud = n.aud ? Object.assign(document.createElement("span"), {
      className: "taud taud-" + n.aud,
      title: { solo: "Only you can open this",
               custom: "Shared with different people than this folder",
               open: "Readable more widely than this folder" }[n.aud] || "",
    }) : null;
    // when this file was last written — the tree already orders files by it,
    // so the date is what makes that order legible
    const when = !n.dir && n.mtime ? Object.assign(document.createElement("span"), {
      className: "tmtime", textContent: fmtMtime(n.mtime),
      title: "Last modified " + new Date(n.mtime * 1000).toLocaleString(),
    }) : null;
    // who has this open right now (filled by the presence poll)
    const pres = document.createElement("span");
    pres.className = "tpresence";
    const actions = document.createElement("span");
    actions.className = "tactions";
    if (n.dir && n.access && n.access.write) {
      actions.append(
        mkBtn(I.plus, "New file here", (e) => { e.stopPropagation(); newFileIn(n.path); }),
        mkBtn(I.folderPlus, "New folder here", (e) => { e.stopPropagation(); newFolderIn(n.path); }),
        mkBtn(I.upload, "Upload files here", (e) => { e.stopPropagation(); uploadInto(n.path); }));
    }
    actions.appendChild(mkBtn(I.share, "Who can open this", (e) => { e.stopPropagation(); openPerms(n.path); }));
    // Deleting an entry needs write on its PARENT — which we know right here,
    // so the button only appears where the kernel could say yes.
    if (parentWritable) {
      const del = mkBtn(I.trash, "Delete" + (n.dir ? " folder (and contents)" : ""),
        (e) => { e.stopPropagation(); deleteEntry(n); });
      del.classList.add("danger");
      actions.appendChild(del);
    }
    // Touch screens have no hover and no right-click — ⋯ opens the full verb
    // menu (rename/copy/move/… — everything the desktop context menu has).
    const more = mkBtn(I.more, "Actions", (e) => {
      e.stopPropagation();
      const r = e.currentTarget.getBoundingClientRect();
      openTreeMenu(n, parentWritable, r.right, r.bottom);
    });
    more.classList.add("tmore");

    // right-click (or long-press) menu — the full VS-Code-style verb set
    row.addEventListener("contextmenu", (e) => {
      e.preventDefault(); e.stopPropagation();
      openTreeMenu(n, parentWritable, e.clientX, e.clientY);
    });
    // dragging a row moves the file/folder — or, dropped into a document,
    // links it; top-level areas stay fixed
    if (n.path.includes("/")) {
      row.draggable = true;
      row.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("application/x-kb-path", n.path);
        // text/plain is the fallback for everything that is not a folder row or
        // an editor — a terminal, say, where the path is what you wanted anyway.
        e.dataTransfer.setData("text/plain", n.path);
        e.dataTransfer.effectAllowed = "copyMove";
      });
    }

    if (n.dir) {
      const caret = document.createElement("span");
      caret.className = "caret" + (collapsed.has(n.path) ? "" : " open");
      caret.innerHTML = I.chevron;
      row.append(caret, icon, label, pres, actions, more, ...(aud ? [aud] : []));
      const kids = document.createElement("div");
      kids.className = "tree-children" + (collapsed.has(n.path) ? " collapsed" : "");
      kids.appendChild(renderNodes(n.children || [], !!(n.access && n.access.write)));
      kids.appendChild(renderGhostRows(n.path));   // in-flight uploads, if any
      row.addEventListener("click", () => {
        const nowCollapsed = kids.classList.toggle("collapsed");
        caret.classList.toggle("open", !nowCollapsed);
        if (nowCollapsed) collapsed.add(n.path); else collapsed.delete(n.path);
        updateFoldButton();
      });
      setupDrop(row, n.path);
      frag.append(row, kids);
    } else {
      row.append(icon, label, ...(when ? [when] : []), pres, actions, more,
                 ...(aud ? [aud] : []));
      row.addEventListener("click", () => openEntry(n));
      frag.appendChild(row);
    }
  }
  return frag;
}

// ---- presence in the tree: who is looking at what --------------------------
let _treePresence = {};

async function loadPresence() {
  let j;
  try {
    const r = await fetch("/api/presence");
    if (!r.ok) return;         // older hub/syncd — the feature lights up after restart
    j = await r.json();
  } catch (e) { return; }
  _treePresence = j.presence || {};
  updateTreePresence();
}

function updateTreePresence() {
  const me = window.__kbuser;
  document.querySelectorAll("#tree .tree-item").forEach((row) => {
    const host = row.querySelector(".tpresence");
    if (!host) return;
    const users = (_treePresence[row.dataset.path] || []).filter((u) => u !== me);
    const sig = users.join(",");
    if (host.dataset.sig === sig) return;
    host.dataset.sig = sig;
    host.textContent = "";
    for (const u of users.slice(0, 3)) {
      const a = document.createElement("span");
      a.className = "tp-avatar";
      a.style.background = userColors(u).color;
      a.textContent = u.slice(0, 2).toUpperCase();
      a.title = u + " has this open right now";
      host.appendChild(a);
    }
    if (users.length > 3) {
      const x = document.createElement("span");
      x.className = "tp-avatar tp-over";
      x.textContent = "+" + (users.length - 3);
      x.title = users.join(", ");
      host.appendChild(x);
    }
  });
}

function openEntry(n) {
  if (isSecretPath(n.path)) openPath(n.path, "secret");
  else if (n.kind === "artifact") openPath(n.path, "artifact");
  else if (n.kind === "file") window.open("/api/attachment?path=" + encodeURIComponent(n.path), "_blank");
  else openPath(n.path, "doc");
}

// ---- editor panes: the split view ------------------------------------------
// ---- groups, columns and the dock: the layout -----------------------------
// The editor area is a WORKSPACE of COLUMNS, each column a vertical stack of
// GROUPS ("panes" here: a tab strip, an editor host and one visible tab), plus
// the DOCK — the group that lives outside the workspace as the bottom panel
// (or a side column) and holds the terminals by default. Every kind of view
// is a tab in a group; a terminal is a tab like a document, only its group is
// the dock until you drag it elsewhere. The rules — what a valid layout is,
// how an old session maps onto it, how a phone renders it — live in
// layout.js; this is the DOM side.
//
// The flat `tabs` array stays the single list of every open tab (order = strip
// order); a tab's `paneId` says which group it lives in. Everything that looked
// a tab up by path still works untouched — a terminal tab simply has no path.
const columns = [];      // [{id, el, grow, panes: […]}] left → right
const panes = [];        // workspace panes in reading order (column by column)
let paneSeq = 0, colSeq = 0;
let activePaneId = null;   // the pane whose visible tab is `active` — where documents open
let focusedPaneId = null;  // the pane the keyboard is in, whatever it shows
let dockPane = null;       // the pane that is #terminal-panel
const dock = { side: "bottom", size: 0.38 };   // collapsed ⇔ #terminal-panel.hidden
let maximizedPaneId = null;
let _dragTab = null;     // the tab currently being dragged, if any

const allPanes = () => (dockPane ? [...panes, dockPane] : panes.slice());
const paneById = (id) => allPanes().find((p) => p.id === id) || null;
const paneOf = (t) => paneById(t.paneId) || panes[0];
const paneTabs = (p) => tabs.filter((t) => t.paneId === p.id);
const activePane = () => paneById(activePaneId) || panes[0];
const focusedPane = () => paneById(focusedPaneId) || activePane();
const colById = (id) => columns.find((c) => c.id === id) || null;
const colOf = (p) => colById(p.colId);
const isDock = (p) => !!p && p === dockPane;
const dockHidden = () => !dockPane || dockPane.el.hidden;
// the first tab in a list that the header can describe (a document, not a terminal)
const firstDocTab = (list) => list.find((t) => viewKind(t.kind).isActiveDocument) || null;
// Is this tab on screen — its group's visible tab, in a group that is shown?
function isDisplayed(t) {
  const p = t && paneOf(t);
  if (!p || p.active !== t || p.el.hidden) return false;
  if (maximizedPaneId && maximizedPaneId !== p.id) return false;
  return true;
}

function makePane() {
  const el = document.createElement("div");
  el.className = "pane";
  const barEl = document.createElement("div");
  barEl.className = "tabbar";
  barEl.hidden = true;
  const hostEl = document.createElement("div");
  hostEl.className = "editor";
  const dropEl = document.createElement("div");
  dropEl.className = "pane-drop";
  dropEl.appendChild(Object.assign(document.createElement("div"), { className: "pane-drop-ind" }));
  el.append(barEl, hostEl, dropEl);
  return finishPane({ id: ++paneSeq, el, barEl, hostEl, dropEl, active: null, grow: 1,
                      colId: null, role: "group", gid: newGroupId(), collapsed: false });
}

// A group's collapsed form: one line naming what it holds; a tap opens it
// again. The same for a group in a column and for the dock.
function makeHandle(p) {
  const h = document.createElement("button");
  h.type = "button"; h.className = "pane-handle";
  h.setAttribute("data-testid", "pane-handle");
  h.title = "Open this group (middle-click closes the tab it shows)";
  h.addEventListener("click", () => { expandPane(p); if (p.active) activateTab(p.active); });
  // middle-click closes the tab the group shows, as it does on a tab
  h.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); });
  h.addEventListener("auxclick", (e) => { if (e.button === 1 && p.active) { e.preventDefault(); closeTab(p.active); } });
  // a tab dropped on the handle goes into the group, which opens to show it
  h.addEventListener("dragover", (e) => {
    if (!_dragTab) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "move";
    h.classList.add("drop-over");
  });
  h.addEventListener("dragleave", () => h.classList.remove("drop-over"));
  h.addEventListener("drop", (e) => {
    if (!_dragTab) return;
    e.preventDefault(); e.stopPropagation();
    const t = _dragTab;
    endTabDrag();
    moveTabToPane(t, p, paneTabs(p).length);
  });
  p.handleEl = h;
  return h;
}

function finishPane(p) {
  p.el.dataset.pane = String(p.id);
  p.barEl.setAttribute("role", "tablist");
  // The streak is over when the MOUSE leaves the strip — that is the moment
  // Chrome springs the tabs back. Only the mouse: a touch pointer ceases to
  // exist the instant the finger lifts, so pointerleave fires after every tap,
  // and releasing there would re-flow the strip between taps — exactly what
  // the freeze exists to prevent. For touch the end of the streak is the next
  // touch somewhere else (see wireTabStrip).
  p.barEl.addEventListener("pointerleave", (e) => {
    if (e.pointerType === "mouse") unlockTabStrip(p.barEl);
  });
  // A group that changes size refits the terminal it shows (a document
  // reflows by itself). One observer, many hosts.
  _fitObserver.observe(p.hostEl);
  makeHandle(p);
  wirePane(p);
  return p;
}
const _fitObserver = new ResizeObserver((entries) => {
  for (const e of entries) {
    const p = allPanes().find((x) => x.hostEl === e.target);
    if (!p) continue;
    // a tight group has no room for the floating formatting toolbar (a
    // touch screen's keyboard row is not that toolbar); a narrow strip
    // keeps its tab names and gives up ＋ and ▾ first
    p.el.classList.toggle("tight", !COARSE_PRIMARY && (e.contentRect.width < 420 || e.contentRect.height < 240));
    p.el.classList.toggle("narrow", e.contentRect.width < 240);
    if (p.active && p.active.kind === "term") fitTerm(p.active);
  }
});

// The dock is the terminal panel's own markup adopted as a pane: same ids,
// same header row, same test hooks — only now it is a group like the others.
function adoptDock() {
  const el = $("#terminal-panel");
  const dropEl = document.createElement("div");
  dropEl.className = "pane-drop";
  dropEl.appendChild(Object.assign(document.createElement("div"), { className: "pane-drop-ind" }));
  el.appendChild(dropEl);
  dockPane = finishPane({ id: ++paneSeq, el, barEl: $("#term-tabs"), hostEl: $("#terminal"), dropEl,
                          active: null, grow: 1, colId: null, role: "dock", gid: "dock", collapsed: true });
  // The dock's handle lives at the bottom of the editor area whatever side
  // the dock is on: "▴ Terminal · bash 1 · notes.md" (Ctrl+` opens it too).
  dockPane.handleEl.id = "dock-handle";
  dockPane.handleEl.title = "Open the terminal panel (Ctrl+`)";
  document.querySelector(".editor-wrap").appendChild(dockPane.handleEl);
  return dockPane;
}

function makeColumn(index) {
  const el = document.createElement("div");
  el.className = "col";
  // an equal share of the row, whatever shares the others hold (after a
  // reload they are fractions, and a flat 1 would dwarf them)
  const share = columns.length ? columns.reduce((a, x) => a + x.grow, 0) / columns.length : 1;
  const c = { id: ++colSeq, el, grow: share, panes: [] };
  columns.splice(index, 0, c);
  const host = $("#panes");
  const before = columns[index + 1];
  host.insertBefore(el, before ? before.el : null);
  return c;
}

// A new column at `index`, holding one new pane. (The historic name — a pane
// used to BE a column.)
function insertPane(index) {
  return insertPaneAt(makeColumn(index), 0);
}

// A new pane inside column `c` at row `rowIndex`.
function insertPaneAt(c, rowIndex) {
  const p = makePane();
  p.colId = c.id;
  p.grow = c.panes.length ? c.panes.reduce((a, x) => a + x.grow, 0) / c.panes.length : 1;
  c.panes.splice(rowIndex, 0, p);
  const before = c.panes[rowIndex + 1];
  c.el.insertBefore(p.el, before ? (before.handleEl.parentElement === c.el ? before.handleEl : before.el) : null);
  c.el.insertBefore(p.handleEl, p.el);
  // Split handles are disposable, panes are NOT: re-inserting a pane element
  // reloads any artifact iframe inside it, so only the handles get rebuilt.
  normalizeSplits();
  if (activePaneId === null) activePaneId = p.id;
  if (focusedPaneId === null) focusedPaneId = p.id;
  return p;
}

function removePane(p) {
  if (isDock(p)) return;                       // the dock collapses; it never leaves
  const c = colOf(p);
  const i = panes.indexOf(p);
  if (i < 0 || panes.length < 2 || !c) return;   // the last pane always stays
  if (p.el.querySelector(":scope > .doc-title-bar, :scope > #mdbar"))
    seatDocChrome(panes.find((x) => x !== p && !x.collapsed) || panes.find((x) => x !== p));
  _fitObserver.unobserve(p.hostEl);
  p.el.remove();
  if (p.handleEl) p.handleEl.remove();
  c.panes.splice(c.panes.indexOf(p), 1);
  if (!c.panes.length) { c.el.remove(); columns.splice(columns.indexOf(c), 1); }
  if (maximizedPaneId === p.id) maximizedPaneId = null;
  normalizeSplits();
  if (keepOneOpen()) normalizeSplits();   // the neighbour it leaves behind may be folded
  const next = panes[i] || panes[i - 1];
  if (activePaneId === p.id) activePaneId = next.id;
  if (focusedPaneId === p.id) focusedPaneId = next.id;
}

// A group whose last tab just left: the dock folds away, a group in a split
// goes, and the last group of all stays — open, so the empty screen shows
// (folded and empty it would be a blank workspace with no handle to click).
function paneEmptied(p, quiet) {
  if (isDock(p)) { hideTerminalPanel(quiet); return; }
  if (panes.length > 1) { removePane(p); return; }
  if (p.collapsed) { p.collapsed = false; p.el.hidden = false; normalizeSplits(); }
}

// The workspace never shows nothing: with every group folded (a record from
// a build that allowed it, or the open group's files all gone) the first
// one opens.
function keepOneOpen() {
  if (!panes.length || panes.some((p) => !p.collapsed)) return false;
  panes[0].collapsed = false; panes[0].el.hidden = false;
  return true;
}

function syncPaneOrder() {
  panes.length = 0;
  for (const c of columns) panes.push(...c.panes);
}

function normalizeSplits() {
  const host = $("#panes");
  host.querySelectorAll(":scope > .pane-split, .row-split").forEach((x) => x.remove());
  const folded = (c) => c.panes.every((p) => p.collapsed);
  columns.forEach((c, i) => {
    // every group folded: the column is a rail of handles and its width goes
    // to the neighbours (tmux break-pane, VS Code's collapsed side panel)
    c.el.classList.toggle("folded", folded(c));
    if (i) {
      const sp = makeSplit(columns[i - 1], c, "col");
      sp.classList.toggle("fold-hidden", folded(columns[i - 1]) || folded(c));
      host.insertBefore(sp, c.el);
    }
    c.panes.forEach((p, j) => {
      if (j) {
        const sp = makeSplit(c.panes[j - 1], p, "row");
        // no handle beside a collapsed group: there is nothing to size
        sp.classList.toggle("fold-hidden", !!(c.panes[j - 1].collapsed || p.collapsed));
        c.el.insertBefore(sp, p.handleEl);
      }
    });
  });
  syncPaneOrder();
  panes.forEach((p, i) => {
    // Single-pane selectors — the tests', and anything pasted into a console —
    // must keep resolving, so the first pane carries the historic ids.
    if (i === 0) { p.barEl.id = "tabbar"; p.hostEl.id = "editor"; }
    else { p.barEl.removeAttribute("id"); p.hostEl.removeAttribute("id"); }
    p.barEl.dataset.testid = "tabbar";
    p.hostEl.dataset.testid = "editor";
  });
  document.body.classList.toggle("split", panes.length > 1);
  applyMaximize();
}

// Maximized: one pane fills the editor area and nothing else — dock
// included — is on screen. Render state, not a change to the layout.
function applyMaximize() {
  const on = !!maximizedPaneId && !!paneById(maximizedPaneId);
  if (!on) maximizedPaneId = null;
  document.body.classList.toggle("maximized", on);
  for (const c of columns)
    c.el.classList.toggle("max-hidden", on && !c.panes.some((p) => p.id === maximizedPaneId));
  for (const p of allPanes()) {
    p.el.classList.toggle("maximized", on && p.id === maximizedPaneId);
    p.el.classList.toggle("max-hidden", on && p.id !== maximizedPaneId);
  }
  document.querySelectorAll("#panes .pane-split, #panes .row-split, #term-resizer")
    .forEach((x) => x.classList.toggle("max-hidden", on));
  // the phone's full-screen terminal is the dock, maximized — the old class
  // stays as an alias for the stylesheet's scrim and drawer rules
  document.body.classList.toggle("term-max", on && !!dockPane && maximizedPaneId === dockPane.id && isMobile());
  document.querySelector(".editor-wrap").classList.toggle("max-hidden", on && !!dockPane && maximizedPaneId === dockPane.id);
  applySizes();
}

// Sizes reach the stylesheet from the VISIBLE items only. A share is a
// fraction of 1 in the saved record, and flex hands out only sum(flex-grow)
// of the space when the sum is below 1 — so shares applied verbatim leave a
// gap the moment a sibling folds, is hidden by a maximize, or leaves. Each
// visible item gets its share of the visible sum, scaled to average 1
// (tmux rebalances the same way when a pane is zoomed or killed). One
// place, for the columns, the groups in each, and the workspace / dock split.
function applySizes() {
  const shown = (x) => !x.el.hidden && !x.el.classList.contains("max-hidden") && !x.el.classList.contains("folded");
  const scale = (items) => {
    const vis = items.filter(shown);
    const sum = vis.reduce((a, x) => a + x.grow, 0) || 1;
    for (const x of items) { x.el.style.flexGrow = String(x.grow / sum * (vis.length || 1)); x.el.style.flexBasis = "0"; }
  };
  scale(columns);
  for (const c of columns) scale(c.panes);
  if (_workspace.el && dockPane) scale([_workspace, dockPane]);
}

function setMaximized(paneId) {
  maximizedPaneId = paneId || null;
  applyMaximize();
  renderTabBar();   // the strip shows the state (and the way back)
  refocus();
  requestAnimationFrame(refitDisplayedTerminals);
  saveSession();
}

function toggleMaximize(p) {
  p = p || keyPane();
  if (!p || p.el.hidden) return;
  // the phone's sheet: full or half, and the choice is remembered
  if (isDock(p) && isPhone()) { setTermMax(maximizedPaneId !== p.id); return; }
  setMaximized(maximizedPaneId === p.id ? null : p.id);
}

// Dragging the handle moves size between exactly two neighbours; the rest of
// the row (or column) keeps its share, so a three-pane layout doesn't
// reshuffle itself. `axis` "col": widths between columns; "row": heights
// between the groups stacked in one column.
function makeSplit(a, b, axis, onChange, minFrac) {
  const s = document.createElement("div");
  const vertical = axis === "row";
  s.className = vertical ? "row-split" : "pane-split";
  s.title = "Drag to resize";
  s.setAttribute("role", "separator");
  s.setAttribute("aria-orientation", vertical ? "horizontal" : "vertical");
  s.setAttribute("aria-label", vertical ? "Resize the groups above and below" : "Resize the columns");
  s.tabIndex = 0;
  const MIN = vertical ? MIN_GROUP_PX : MIN_COL_PX;
  // a space too small for two minimums splits in half at most — never a
  // negative share, which flex ignores and the saved layout would keep
  // …and `minFrac`, if given, keeps either side at least that share of the
  // pair (the dock's record clamps to [0.1, 0.9]; the drag agrees, so the
  // release never jumps)
  const clamp = (v, total) => {
    const lo = Math.min(Math.max(MIN, (minFrac || 0) * total), total / 2);
    return Math.min(Math.max(v, lo), total - lo);
  };
  const size = (el) => el.getBoundingClientRect()[vertical ? "height" : "width"];
  const apply = (aw, total, sum) => {
    a.grow = sum * (aw / total);
    b.grow = sum - a.grow;
    applySizes();
  };
  s.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    // Capture: an artifact iframe under the cursor would otherwise swallow
    // the move and the release, and the drag would "stick" until a click.
    try { s.setPointerCapture(e.pointerId); } catch (err) { /* synthetic events */ }
    const x0 = vertical ? e.clientY : e.clientX;
    const aw0 = size(a.el), total = aw0 + size(b.el), sum = a.grow + b.grow;
    document.body.classList.add(vertical ? "row-resizing" : "pane-resizing");
    const move = (ev) => {
      const d = (vertical ? ev.clientY : ev.clientX) - x0;
      apply(clamp(aw0 + d, total), total, sum);
    };
    const up = () => {
      s.removeEventListener("pointermove", move);
      s.removeEventListener("pointerup", up);
      s.removeEventListener("pointercancel", up);
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.body.classList.remove(vertical ? "row-resizing" : "pane-resizing");
      if (onChange) onChange();
      refitDisplayedTerminals();
      saveSession();
    };
    s.addEventListener("pointermove", move);
    s.addEventListener("pointerup", up);
    s.addEventListener("pointercancel", up);
    document.addEventListener("pointermove", move);   // for the tests' synthetic events, which capture cannot route
    document.addEventListener("pointerup", up);
  });
  // The keyboard's way: arrows move the handle by 24px.
  s.addEventListener("keydown", (e) => {
    const dec = vertical ? e.key === "ArrowUp" : e.key === "ArrowLeft";
    const inc = vertical ? e.key === "ArrowDown" : e.key === "ArrowRight";
    if (!dec && !inc) return;
    e.preventDefault();
    const aw0 = size(a.el), total = aw0 + size(b.el), sum = a.grow + b.grow;
    apply(clamp(aw0 + (inc ? 24 : -24), total), total, sum);
    if (onChange) onChange();
    refitDisplayedTerminals();
    saveSession();
  });
  return s;
}

// ---- collapse: any group folds to a one-line handle and unfolds again ----
function collapsePane(p, quiet) {
  if (!p || p.collapsed) return;
  // The workspace always shows a group: the last open one cannot fold (tmux
  // keeps its last pane, VS Code its last editor group). The dock always can.
  if (!isDock(p) && !panes.some((x) => x !== p && !x.collapsed)) {
    if (!quiet) kbToast("The last open group stays open", "err");
    return;
  }
  p.collapsed = true;
  p.el.hidden = true;
  if (maximizedPaneId === p.id) { maximizedPaneId = null; applyMaximize(); }
  // A folded tab cannot be the one the header describes, the one documents
  // open beside, or the one the keyboard sits in.
  const fallback = panes.find((x) => x !== p && !x.collapsed) || panes.find((x) => x !== p) || panes[0];
  if (focusedPaneId === p.id && fallback) focusedPaneId = fallback.id;
  if (activePaneId === p.id && fallback) activePaneId = fallback.id;
  if (isDock(p)) document.body.classList.remove("term-max");
  normalizeSplits();
  if (!quiet && active && paneOf(active) === p) {
    const outside = tabs.filter((x) => paneOf(x) !== p && !paneOf(x).collapsed);
    activateTab(firstDocTab(paneTabs(focusedPane())) || firstDocTab(outside) || null);
  } else {
    renderTabBar();
    saveSession();
  }
  if (!quiet) refocus();
}

function expandPane(p) {
  if (!p) return;
  const was = p.collapsed;
  p.collapsed = false;
  if (isDock(p) && !paneTabs(p).length) { p.collapsed = true; p.el.hidden = true; renderTabBar(); return; }   // an empty dock stays out of the way
  p.el.hidden = false;
  if (isDock(p)) {
    closeNav();   // the drawer would cover the panel on a phone
    if (maximizedPaneId && maximizedPaneId !== p.id) setMaximized(null);
    if (isPhone()) setTermMax(preferredTermMax());
  }
  normalizeSplits();
  renderTabBar();
  requestAnimationFrame(refitDisplayedTerminals);
  if (was) saveSession();
}

function togglePaneFold(p) {
  p = p || keyPane();
  if (!p) return;
  if (p.collapsed || p.el.hidden) expandPane(p); else collapsePane(p);
}

// Clicking anywhere in a pane puts the keyboard there. If what it shows is a
// document, that document becomes the active one the header and toolbar
// describe — as in VS Code. If it shows a terminal, the terminal gets the
// focus and the header keeps describing the document you were reading.
// The keyboard never lands on <body>: when what had it is gone (a closed
// tab, a removed group, a folded group's editor), the focused group's tab
// takes it — i3 focuses whatever took the space.
function refocus() {
  const ae = document.activeElement;
  if (ae && ae !== document.body) return;
  const p = focusedPane();
  const t = p && p.active;
  if (!t || p.el.hidden || p.el.classList.contains("max-hidden")) return;
  if (t.kind === "term") { if (t.term) t.term.focus(); return; }
  viewKind(t.kind).focus(t);
}

function focusPane(p) {
  const was = focusedPaneId;
  focusedPaneId = p.id;
  const cur = p.active;
  if (cur && !viewKind(cur.kind).isActiveDocument) {
    if (cur.kind === "term") noteActiveTerm(cur);
    if (was !== p.id) { renderTabBar(); saveSession(); }
    return;
  }
  if (activePaneId === p.id) { if (was !== p.id) renderTabBar(); return; }
  activePaneId = p.id;
  if (cur) activateTab(cur);
  else { renderTabBar(); saveSession(); }
}

function wirePane(p) {
  // Clicking into a pane focuses it — EXCEPT on its tab strip, where the tab's
  // own click decides. focusPane re-renders the strip, and a strip rebuilt
  // between pointerdown and click swallows the click that caused it: the tab
  // you aimed at is gone by the time the browser looks for it.
  p.el.addEventListener("pointerdown", (e) => {
    // …nor on the strip's action buttons: focusPane re-renders the strip,
    // and a button rebuilt between pointerdown and click never gets the click
    if (!e.target.closest(".tab, .grp-actions")) focusPane(p);
  }, true);
  // Double-clicking the strip's empty space maximizes the group (the panel
  // title convention); double-clicking again restores.
  p.barEl.addEventListener("dblclick", (e) => {
    if (e.target === p.barEl) toggleMaximize(p);
  });
  // — the tab strip: drop here to move the tab into this pane at this position
  p.barEl.addEventListener("dragover", (e) => {
    if (!_dragTab) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    markBarInsert(p, e.clientX);
  });
  p.barEl.addEventListener("dragleave", (e) => {
    if (!p.barEl.contains(e.relatedTarget)) clearDropMarks();
  });
  p.barEl.addEventListener("drop", (e) => {
    if (!_dragTab) return;
    e.preventDefault(); e.stopPropagation();
    const t = _dragTab;
    endTabDrag();
    moveTabToPane(t, p, barInsertIndex(p, e.clientX));
  });
  // — the body: middle = move into this pane, an edge = split off a new one.
  // The overlay covers the strip too, so a drop there is the strip's: a
  // position among its tabs.
  const overBar = (e) => !p.barEl.hidden && e.clientY <= p.barEl.getBoundingClientRect().bottom;
  p.dropEl.addEventListener("dragover", (e) => {
    if (!_dragTab) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (overBar(e)) { p.dropEl.classList.remove("over"); markBarInsert(p, e.clientX); return; }
    clearDropMarks();
    const side = dropSide(p, e.clientX, e.clientY);
    if ((side === "in" || isDock(p)) && paneOf(_dragTab) === p) return;   // its own group: nothing would move
    paintPaneHint(p, side);
  });
  p.dropEl.addEventListener("dragleave", () => p.dropEl.classList.remove("over"));
  p.dropEl.addEventListener("drop", (e) => {
    if (!_dragTab) return;
    e.preventDefault(); e.stopPropagation();
    const t = _dragTab;
    if (overBar(e)) { const at = barInsertIndex(p, e.clientX); endTabDrag(); moveTabToPane(t, p, at); return; }
    const side = dropSide(p, e.clientX, e.clientY);
    endTabDrag();
    dropTabOn(t, p, side);
  });
}

// Where a tab lands when dropped on a pane's body: the middle moves it in, an
// edge splits a new group off — left / right a new column beside this one,
// top / bottom a new group above or below it in the same column. The dock
// takes only "in": it is a place, not something to split.
// Would a new group fit in column `c`? "Refuse now, squeeze later", like
// canAddColumn: a column that cannot give every open group MIN_GROUP_PX is
// refused with a toast; a window that later shrinks squeezes what exists.
// Under the phone breakpoint the whole workspace is one stack.
function roomForGroup(c) {
  const ok = canAddGroup(currentLayout());
  if (!ok.ok) return ok;
  const stack = isMobile();
  const h = (stack ? $("#panes") : c.el).getBoundingClientRect().height;
  const n = (stack ? panes : c.panes).filter((p) => !p.collapsed).length;
  if (h && (n + 1) * MIN_GROUP_PX > h) return { ok: false, why: "No room for another group at this height" };
  return { ok: true };
}

function dropTabOn(t, p, side) {
  if ((side === "in" || isDock(p)) && paneOf(t) === p) return;   // its own group: nothing to move
  if (side === "in" || isDock(p)) { moveTabToPane(t, p, paneTabs(p).length); return; }
  // Splitting a pane's only tab off that same pane would just delete the pane
  // it came from and rebuild it — nothing moves, so don't pretend it did.
  if (paneOf(t) === p && paneTabs(p).length === 1) return;
  const c = colOf(p);
  if (side === "left" || side === "right") {
    const ok = canAddColumn(currentLayout(), $("#panes").getBoundingClientRect().width);
    if (!ok.ok) { kbToast(ok.why, "err"); return; }
    moveTabToPane(t, insertPane(columns.indexOf(c) + (side === "right" ? 1 : 0)), 0);
    return;
  }
  const ok = roomForGroup(c);
  if (!ok.ok) { kbToast(ok.why, "err"); return; }
  moveTabToPane(t, insertPaneAt(c, c.panes.indexOf(p) + (side === "bottom" ? 1 : 0)), 0);
}

function dropSide(p, x, y) {
  if (isDock(p)) return "in";
  const r = p.dropEl.getBoundingClientRect();
  const fx = (x - r.left) / r.width, fy = (y - r.top) / r.height;
  // under the phone breakpoint groups stack: above, below, or in
  if (isMobile()) return fy < 0.3 ? "top" : fy > 0.7 ? "bottom" : "in";
  // The nearest edge wins, within its outer band; the middle is "in".
  const d = { left: fx, right: 1 - fx, top: fy, bottom: 1 - fy };
  const m = Math.min(d.left, d.right, d.top, d.bottom);
  if (m > 0.28) return "in";
  return ["left", "right", "top", "bottom"].find((k) => d[k] === m);
}

function paintPaneHint(p, side) {
  const ind = p.dropEl.querySelector(".pane-drop-ind");
  ind.style.top = side === "bottom" ? "50%" : "4px";
  ind.style.bottom = side === "top" ? "50%" : "4px";
  ind.style.left = side === "right" ? "50%" : "4px";
  ind.style.right = side === "left" ? "50%" : "4px";
  p.dropEl.classList.add("over");
}

function clearDropMarks() {
  document.querySelectorAll(".tab.drop-before, .tab.drop-after")
    .forEach((e) => e.classList.remove("drop-before", "drop-after"));
  document.querySelectorAll(".tabbar.drop-end").forEach((e) => e.classList.remove("drop-end"));
  for (const p of allPanes()) { p.dropEl.classList.remove("over"); p.handleEl.classList.remove("drop-over"); }
}

function barInsertIndex(p, x) {
  const els = [...p.barEl.querySelectorAll(".tab")];
  for (let i = 0; i < els.length; i++) {
    const r = els[i].getBoundingClientRect();
    if (x < r.left + r.width / 2) return i;
  }
  return els.length;
}

function markBarInsert(p, x) {
  clearDropMarks();
  const els = [...p.barEl.querySelectorAll(".tab")];
  const i = barInsertIndex(p, x);
  if (i < els.length) els[i].classList.add("drop-before");
  else if (els.length) els[els.length - 1].classList.add("drop-after");
  else p.barEl.classList.add("drop-end");
}

function endTabDrag() {
  _dragTab = null;
  document.body.classList.remove("dragging-tab", "touch-drag");
  document.querySelectorAll(".tab.dragging").forEach((e) => e.classList.remove("dragging"));
  clearDropMarks();
}

// ---- touch: hold a tab to lift it, carry it, let go where it should be -----
// No HTML5 drag on a finger (Android has none, iOS has its own). A tab held
// still for a third of a second lifts: a ghost follows the finger, the strips
// and groups show where it would land — between two tabs, into a group,
// above or below one, into the panel — and letting go puts it there. Moving
// the finger before the hold ends is a scroll, and stays one: nothing here
// touches the gesture until the tab is lifted, and from then on the page
// does not pan under it.
function wireTouchTabDrag() {
  const HOLD = 320, SLOP = 8;
  let press = null;   // {t, el, x, y, id, timer}
  let drag = null;    // {t, id, ghost, dx, dy, raf, x, y}
  const cancelPress = () => { if (press) { clearTimeout(press.timer); press = null; } };
  const lift = () => {
    const { t, el, x, y, id } = press;
    press = null;
    const r = el.getBoundingClientRect();
    const ghost = el.cloneNode(true);
    ghost.className = "tab tab-ghost";
    ghost.style.width = r.width + "px";
    document.body.appendChild(ghost);
    drag = { t, el, id, ghost, dx: x - r.left, dy: y - r.top, raf: 0, x, y };
    _dragTab = t;
    document.body.classList.add("dragging-tab", "touch-drag");
    el.classList.add("dragging");
    try { if (navigator.vibrate) navigator.vibrate(8); } catch (e) { /* not everywhere */ }
    place();
  };
  const place = () => {
    if (!drag) return;
    drag.raf = 0;
    drag.ghost.style.transform = "translate3d(" + (drag.x - drag.dx) + "px," + (drag.y - drag.dy) + "px,0) scale(1.04)";
    const hit = touchTarget(drag.x, drag.y);
    clearDropMarks();
    if (!hit) return;
    if (hit.kind === "bar") markBarInsert(hit.p, drag.x);
    else if (hit.p.el.hidden) hit.p.handleEl.classList.add("drop-over");   // a folded group: its handle lights up
    else if (!((hit.side === "in" || isDock(hit.p)) && paneOf(drag.t) === hit.p)) paintPaneHint(hit.p, hit.side);
  };
  document.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "touch" || drag || !e.isPrimary) return;
    const el = e.target && e.target.closest ? e.target.closest(".tab") : null;
    if (!el || !el._tab || e.target.closest(".tab-x")) return;
    cancelPress();
    press = { t: el._tab, el, x: e.clientX, y: e.clientY, id: e.pointerId, timer: setTimeout(lift, HOLD) };
  }, { passive: true });
  document.addEventListener("pointermove", (e) => {
    if (press && !drag) {
      if (Math.hypot(e.clientX - press.x, e.clientY - press.y) > SLOP) cancelPress();   // a scroll, not a hold
      return;
    }
    if (!drag || e.pointerType !== "touch") return;
    drag.x = e.clientX; drag.y = e.clientY;
    if (!drag.raf) drag.raf = requestAnimationFrame(place);
  }, { passive: true });
  const end = (e) => {
    if (press && (e.type === "pointerup" || e.type === "pointercancel")) cancelPress();
    if (!drag || e.pointerType !== "touch") return;
    const d = drag;
    drag = null;
    if (d.raf) cancelAnimationFrame(d.raf);
    const hit = e.type === "pointerup" ? touchTarget(e.clientX, e.clientY) : null;
    d.ghost.classList.add("drop");
    setTimeout(() => d.ghost.remove(), 140);
    endTabDrag();
    if (!hit) return;
    if (hit.kind === "bar") moveTabToPane(d.t, hit.p, hit.index);
    else dropTabOn(d.t, hit.p, hit.side);
  };
  document.addEventListener("pointerup", end);
  document.addEventListener("pointercancel", end);
  // A lifted tab must not scroll the page under itself, and the hold must
  // not pop the context menu or select text (the stylesheet handles the rest).
  document.addEventListener("touchmove", (e) => { if (drag) e.preventDefault(); }, { passive: false });
  document.addEventListener("contextmenu", (e) => { if (press || drag) e.preventDefault(); });
}

// Once, the first time a phone has two tabs: the gesture nobody would find.
function hintHoldToMove() {
  if (!COARSE_PRIMARY || _restoring || tabs.length !== 2) return;
  try {
    if (localStorage.getItem("kbHintHold")) return;
    localStorage.setItem("kbHintHold", "1");
  } catch (e) { return; }
  kbToast("Tip: hold a tab to move it — above or below another" + (isPhone() ? "" : ", or into the panel"), "ok");
}

// Where a finger is: over a strip (a place among its tabs) or over a group
// (a side, or the middle). Rectangles, not elementFromPoint: the drop
// overlays cover everything while a tab is in flight. On a phone the groups
// stack, so a group's sides are above and below; a desktop's are all four.
function touchTarget(x, y) {
  for (const p of allPanes()) {
    if (p.el.hidden) {   // folded: its handle is the target, and takes the tab in
      if (p.handleEl.classList.contains("on")) {
        const h = p.handleEl.getBoundingClientRect();
        if (x >= h.left && x <= h.right && y >= h.top && y <= h.bottom) return { kind: "body", p, side: "in" };
      }
      continue;
    }
    if (p.el.classList.contains("max-hidden")) continue;
    if (!p.barEl.hidden) {
      const b = p.barEl.getBoundingClientRect();
      if (x >= b.left && x <= b.right && y >= b.top - 8 && y <= b.bottom + 4)
        return { kind: "bar", p, index: barInsertIndex(p, x) };
    }
    const r = p.el.getBoundingClientRect();
    if (x < r.left || x > r.right || y < r.top || y > r.bottom) continue;
    if (isDock(p)) return { kind: "body", p, side: "in" };
    const fy = (y - r.top) / r.height;
    if (isMobile()) return { kind: "body", p, side: fy < 0.3 ? "top" : fy > 0.7 ? "bottom" : "in" };
    return { kind: "body", p, side: dropSide(p, x, y) };
  }
  return null;
}

// Move a tab into `target` at position `index` of that pane's strip. The tab's
// live mount travels with it: CodeMirror and xterm survive re-parenting
// untouched, and an artifact iframe reloads — which is what re-parenting an
// iframe means, and is fine, since an artifact re-runs from its own source
// either way.
function moveTabToPane(t, target, index) {
  const from = paneOf(t);
  // On a phone the panel is the terminal sheet — a document in it would sit
  // under the keybar with its bar at the wrong end. It stays a terminal sheet.
  if (isDock(target) && from !== target && isMobile() && t.kind !== "term") {
    kbToast("On a phone the panel holds terminals — drop it above the panel", "err");
    return;
  }
  const cur = tabs.indexOf(t);
  if (cur >= 0) tabs.splice(cur, 1);
  t.paneId = target.id;
  if (from !== target) target.hostEl.appendChild(t.el);
  // Splice back into the flat list at the spot that yields `index` in the strip.
  const list = tabs.filter((x) => x.paneId === target.id);
  const at = !list.length ? tabs.length
    : index >= list.length ? tabs.indexOf(list[list.length - 1]) + 1
      : tabs.indexOf(list[index]);
  tabs.splice(at, 0, t);
  if (from !== target) {
    if (!paneTabs(from).length) paneEmptied(from, true);
    else if (from.active === t) from.active = paneTabs(from)[0] || null;
    if (isDock(target)) showDock();
  }
  if (viewKind(t.kind).isActiveDocument) activePaneId = target.id;
  activateTab(t);
  if (t.kind === "term") requestAnimationFrame(() => fitTerm(t));
  else viewKind(t.kind).resize(t);
}

// Split the focused group's visible tab off into a new group: `dir` 1 / -1 a
// new column to the right / left, "down" / "up" a new group below / above in
// the same column. From the dock, "right" means the workspace's far column.
function splitActiveTab(dir) {
  const p = keyPane();
  const t = p && p.active;
  if (!t) return;
  if (paneTabs(p).length < 2) return;   // nothing left behind = not a split
  if (isMobile() && (dir === 1 || dir === -1)) dir = dir > 0 ? "down" : "up";   // groups stack on a phone
  if (dir === "down" || dir === "up") {
    const c = colOf(p) || columns[columns.length - 1];
    const ok = roomForGroup(c);
    if (!ok.ok) { kbToast(ok.why, "err"); return; }
    const at = isDock(p) ? c.panes.length : c.panes.indexOf(p) + (dir === "down" ? 1 : 0);
    moveTabToPane(t, insertPaneAt(c, at), 0);
    return;
  }
  const ok = canAddColumn(currentLayout(), $("#panes").getBoundingClientRect().width);
  if (!ok.ok) { kbToast(ok.why, "err"); return; }
  const at = isDock(p) ? (dir > 0 ? columns.length : 0)
    : columns.indexOf(colOf(p)) + (dir > 0 ? 1 : 0);
  moveTabToPane(t, insertPane(at), 0);
}

// Move the focused group's visible tab: "left" / "right" into the neighbouring
// column (a new one at the edge), "up" / "down" into the neighbouring group of
// its column (a new one at the top or bottom), "dock" into the dock. The
// palette's path to what the mouse does by dragging — and the accessible one.
function moveFocusedTab(where) {
  const p = keyPane();
  const t = p && p.active;
  if (!t) return;
  if (isMobile() && (where === "left" || where === "right")) where = where === "right" ? "down" : "up";   // groups stack on a phone
  if (where === "dock") { if (isDock(p)) return; moveTabToPane(t, dockPane, paneTabs(dockPane).length); return; }
  const c = colOf(p);
  if (!c) {   // from the dock: into the workspace's first or last column
    const target = where === "left" ? columns[0] : columns[columns.length - 1];
    moveTabToPane(t, target.panes[0], paneTabs(target.panes[0]).length);
    return;
  }
  if (where === "left" || where === "right") {
    const i = columns.indexOf(c) + (where === "right" ? 1 : -1);
    const alone = paneTabs(p).length === 1 && c.panes.length === 1;
    if (columns[i]) { moveTabToPane(t, columns[i].panes[0], paneTabs(columns[i].panes[0]).length); return; }
    if (alone) return;   // already at the edge, on its own
    const ok = canAddColumn(currentLayout(), $("#panes").getBoundingClientRect().width);
    if (!ok.ok) { kbToast(ok.why, "err"); return; }
    moveTabToPane(t, insertPane(where === "right" ? columns.length : 0), 0);
    return;
  }
  const j = c.panes.indexOf(p) + (where === "down" ? 1 : -1);
  if (c.panes[j]) { moveTabToPane(t, c.panes[j], paneTabs(c.panes[j]).length); return; }
  if (paneTabs(p).length === 1) return;
  const ok = roomForGroup(c);
  if (!ok.ok) { kbToast(ok.why, "err"); return; }
  moveTabToPane(t, insertPaneAt(c, where === "down" ? c.panes.length : 0), 0);
}

// The keyboard's way between groups: next / previous in reading order, the
// dock last. Focusing a group focuses what it shows.
function focusNextPane(d) {
  // a folded group is in the round: focusing it is focusing its handle
  // (Enter opens it, Alt+W closes the tab it names)
  const all = allPanes().filter((p) => (!p.el.hidden || p.handleEl.classList.contains("on"))
    && (!maximizedPaneId || p.id === maximizedPaneId));
  if (all.length < 2) return;
  const i = all.indexOf(keyPane());
  const p = all[((i < 0 ? 0 : i) + d + all.length) % all.length];
  if (p.el.hidden) { p.handleEl.focus(); return; }
  if (p.active) { activateTab(p.active); if (viewKind(p.active.kind).isActiveDocument) viewKind(p.active.kind).focus(p.active); }
  else focusPane(p);
}

// The dock's side and size: a bottom band by default, a column on the right
// or left for a wide monitor. Layout state, not a setting — it is a choice
// per screen, and it travels with the session record of that browser.
const _workspace = { el: null, grow: 1 };   // the editor area as one side of the dock's split
function applyDockSide() {
  const mc = document.querySelector(".main-col");
  if (!mc || !dockPane) return;
  const ws = document.querySelector(".editor-wrap");
  _workspace.el = ws;
  const el = dockPane.el;
  el.style.height = ""; el.style.width = "";
  // On a phone the dock is the sheet at the bottom and the stylesheet owns
  // its size (half, or maximized); the desktop side and size come back when
  // the window grows.
  mc.dataset.dock = isMobile() ? "bottom" : dock.side;
  _workspace.grow = 1 - dock.size; dockPane.grow = dock.size;
  // the split between the workspace and the dock: the same handle as between
  // any two groups (row-wise for a bottom dock, column-wise for a side one).
  // The record keeps the dock within [0.1, 0.9] (layout.js), so the drag
  // does too — the screen never differs from what a reload would show.
  const old = $("#term-resizer");
  if (old) old.remove();
  const onDock = () => {
    dock.size = Math.min(0.9, Math.max(0.1, dockPane.grow / (dockPane.grow + _workspace.grow)));
    _workspace.grow = 1 - dock.size; dockPane.grow = dock.size;
    applySizes();
  };
  // a left dock comes first in a row-reverse: the handle's "a" is whatever
  // is on the left, or dragging it right would shrink what it should grow
  const h = dock.side === "left" ? makeSplit(dockPane, _workspace, "col", onDock, 0.1)
    : makeSplit(_workspace, dockPane, dock.side === "bottom" ? "row" : "col", onDock, 0.1);
  h.id = "term-resizer";
  mc.insertBefore(h, el);
  h.classList.toggle("fold-hidden", el.hidden);
  applyMaximize();   // the rebuilt handle hides while something is maximized; sizes follow
}

function setDockSide(side) {
  if (!["bottom", "right", "left"].includes(side) || side === dock.side) return;
  dock.side = side;
  applyDockSide();
  requestAnimationFrame(refitDisplayedTerminals);
  saveSession();
}

// ---- editor tabs (VS-Code style) ------------------------------------------
// Every open document or artifact is a tab. Each tab keeps its own live mount
// (CodeMirror view + Yjs provider, or sandboxed iframe) in a hidden container,
// so switching tabs is instant and background artifacts keep running.
let tabSeq = 0;
const tabs = [];      // {id, path, kind:'doc'|'artifact', name, el, view, provider, ydoc, frame, access, synced}
let active = null;    // the active tab (or null)

function baseName(p) { return p.includes("/") ? p.slice(p.lastIndexOf("/") + 1) : p; }
function dirName(p) { return p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : ""; }
// files inside a _secrets/ folder: kernel-shared like any file, but excluded
// from git history, the search index and the CRDT relay, and opened in the
// masked secret viewer instead of the collaborative editor
function isSecretPath(p) { return p.split("/").includes("_secrets"); }

// ---- the close-streak lock (Chrome's, and for Chrome's reason) -------------
// Closing a tab with the pointer widens the ones that remain, which slides the
// next × out from under the cursor — so closing four tabs means four separate
// aim-and-click trips. Chrome answers by freezing the strip's layout for the
// duration of the streak: the tabs keep the width they had, leaving a gap at
// the right, and only spring back once the pointer leaves the strip.
//
// The freeze is per strip (a split has one each) and is a single CSS custom
// property — pinning --tab-max to the width the tabs currently have caps them
// there, and dropping it lets the transition glide them back out.
function lockTabStrip(barEl) {
  if (!barEl) return;
  const first = barEl.querySelector(".tab");
  if (!first) return;
  // Measured, not computed from the tab count: the strip may be scrolled, in a
  // split pane, or already at its minimum, and the only width that keeps the
  // next × under the cursor is the one actually on screen.
  barEl.style.setProperty("--tab-max", first.getBoundingClientRect().width + "px");
  barEl.classList.add("tabs-locked");
}

function unlockTabStrip(barEl) {
  if (!barEl || !barEl.classList.contains("tabs-locked")) return;
  barEl.classList.remove("tabs-locked");
  barEl.style.removeProperty("--tab-max");
}

function unlockAllTabStrips() {
  document.querySelectorAll(".tabbar.tabs-locked").forEach(unlockTabStrip);
}

function wireTabStrip() {
  // Touching anything that is not a locked strip ends the streak. This is what
  // releases the freeze on a touchscreen, where there is no pointer to leave,
  // and it also covers a mouse that clicks straight into the editor without
  // ever crossing the strip's edge.
  document.addEventListener("pointerdown", (e) => {
    const el = e.target instanceof Element ? e.target : null;
    if (!el || !el.closest(".tabbar.tabs-locked")) unlockAllTabStrips();
  }, true);
}

function renderHandles() {
  for (const p of allPanes()) {
    const list = paneTabs(p);
    const on = p.el.hidden && list.length > 0 && !(maximizedPaneId && maximizedPaneId !== p.id);
    p.handleEl.classList.toggle("on", on);
    if (on) p.handleEl.textContent = "▴ " + (isDock(p) ? "Terminal · " : "") + list.map((t) => viewKind(t.kind).title(t)).join(" · ");
  }
  const r = $("#term-resizer");
  if (r && dockPane) r.classList.toggle("fold-hidden", dockPane.el.hidden);
  applySizes();   // whatever just folded or came back, the rest fills the space
  // the touch keybar belongs to whichever group the keyboard is in, if it shows a terminal
  const fp = focusedPane();
  document.body.classList.toggle("kb-term", !!(fp && fp.active && fp.active.kind === "term" && !fp.el.hidden));
}

// The actions every group offers, at the right end of its strip: a new
// terminal here, maximize / restore, fold. The dock's carry the historic ids.
function groupActions(p) {
  const box = document.createElement("div");
  box.className = "grp-actions";
  const mk = (act, text, title, testid) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "grp-btn"; b.dataset.act = act;
    b.textContent = text; b.title = title; b.setAttribute("aria-label", title);
    if (testid) b.setAttribute("data-testid", testid);
    b.addEventListener("pointerdown", (e) => e.preventDefault());   // never steal focus from the terminal
    box.appendChild(b);
    return b;
  };
  const dock = isDock(p);
  // (no ＋ here: a terminal comes from Ctrl+` / Ctrl+Shift+`, the person's
  // menu or the palette — a button per strip was noise)
  const max = maximizedPaneId === p.id;
  // Maximize is only offered when something else is on screen to step aside
  // — with one group it does nothing. The phone's sheet keeps its half /
  // full toggle, which is a different thing wearing the same icon.
  const others = allPanes().some((x) => x !== p && paneTabs(x).length);
  if (others || max || (dock && isPhone())) {
    const bm = mk("max", max ? "⤡" : "⤢", max ? "Restore (Alt+Z)" : "Maximize this group (Alt+Z)", dock ? "term-max" : "grp-max");
    if (dock) { bm.id = "term-max"; bm.dataset.label = max ? "half" : "full"; }
    bm.addEventListener("click", () => toggleMaximize(p));
  }
  // fold: not offered for the last open group of the workspace — there
  // would be nothing left to look at (the dock always folds)
  if (dock || panes.some((x) => x !== p && !x.collapsed)) {
    const bf = mk("fold", "▾", "Fold away — its tabs stay open" + (dock ? " (Ctrl+`)" : ""), dock ? "term-hide" : "grp-fold");
    if (dock) bf.id = "term-hide";
    bf.addEventListener("click", () => collapsePane(p));
  }
  return box;
}

function renderTabBar() {
  renderHandles();
  // the chat shows what you have open as context chips: the set changed
  if (_chatMod) _chatMod.then((m) => m.docsChanged && m.docsChanged()).catch(() => {});
  const dup = {};
  tabs.forEach((t) => { dup[t.name] = (dup[t.name] || 0) + 1; });
  for (const p of allPanes()) {
    const list = paneTabs(p);
    p.barEl.innerHTML = "";
    // A lone empty pane keeps the placeholder screen; an empty strip in a split
    // cannot happen (a pane is removed when its last tab leaves).
    p.barEl.hidden = list.length === 0;
    p.el.classList.toggle("focused", p.id === focusedPaneId);
    for (const t of list) p.barEl.appendChild(tabEl(t, dup));
    if (list.length) p.barEl.appendChild(groupActions(p));
    // The strip's own scroll needs no saving across this: emptying and
    // refilling happens in one task, so layout never runs while the bar has no
    // children and scrollLeft is never clamped to zero. Measured, not assumed.
    revealCurrentTab(p);
  }
  syncChatRows();
}

// Chrome keeps the tab you are looking at on screen: switch to one that is
// scrolled out of the strip and the strip follows. Written against the bar's
// own scrollLeft rather than scrollIntoView(), which also walks up and scrolls
// ancestors — and skipped entirely mid-streak, where moving the strip is the
// one thing we are trying to prevent.
function revealCurrentTab(p) {
  if (p.barEl.classList.contains("tabs-locked")) return;
  const cur = p.barEl.querySelector(".tab.current");
  if (!cur) return;
  const bar = p.barEl.getBoundingClientRect();
  // the group's actions sit over the strip's right end: a tab under them is
  // not in view
  const acts = p.barEl.querySelector(".grp-actions");
  const right = bar.right - (acts ? acts.getBoundingClientRect().width : 0);
  const r = cur.getBoundingClientRect();
  if (r.left < bar.left) p.barEl.scrollLeft -= bar.left - r.left;
  else if (r.right > right) p.barEl.scrollLeft += r.right - right;
}

function tabEl(t, dup) {
  const K = viewKind(t.kind);
  const el = document.createElement("div");
  // .current = the tab this pane is showing; .active = the one the header, the
  // toolbar and every shortcut act on. In a split they are different tabs, and
  // an unfocused pane still has to say which of its files you are looking at.
  // A terminal is never .active: the header keeps describing the document.
  el.className = "tab" + (t === paneOf(t).active ? " current" : "") + (t === active ? " active" : "")
    + (t.kind === "term" ? " term-tab" : "");
  el.setAttribute("role", "tab");
  el.setAttribute("aria-selected", t === paneOf(t).active ? "true" : "false");
  if (t.attention) el.classList.add("attention");
  el._tab = t;
  el.title = K.tooltip(t);
  el.dataset.kind = t.kind;
  if (t.path) el.dataset.path = t.path;
  if (t.sid) el.dataset.sid = t.sid;
  const icon = document.createElement("span");
  icon.className = "tab-icon";
  icon.innerHTML = K.icon(t);
  const name = document.createElement("span");
  name.className = "tab-name";
  name.textContent = K.title(t);
  el.append(icon, name);
  if (t.path && dup[t.name] > 1 && dirName(t.path)) {   // disambiguate same-named files
    const d = document.createElement("span");
    d.className = "tab-dir";
    d.textContent = dirName(t.path);
    el.appendChild(d);
  }
  const x = document.createElement("button");
  x.className = "tab-x" + (t.kind === "term" ? " term-x" : "");
  x.title = t.kind === "term" ? "Kill terminal" : "Close"; x.textContent = "×";
  x.addEventListener("click", (e) => { e.stopPropagation(); closeTab(t, true); });
  el.appendChild(x);
  el.addEventListener("click", () => {
    activateTab(t);
    if (t.kind === "term" && t.connected === false && t.term) retryTermsNow();   // a stalled tab retries now
  });
  el.addEventListener("auxclick", (e) => { if (e.button === 1) { e.preventDefault(); closeTab(t, true); } });
  // Drag to reorder, to move into another pane, or onto a pane's edge to split.
  // Deliberately NO text/plain payload: the tree's folder rows and the editor
  // both accept dropped text, and a tab is not a path being pasted somewhere.
  el.draggable = !COARSE_PRIMARY;
  el.addEventListener("dragstart", (e) => {
    _dragTab = t;
    document.body.classList.add("dragging-tab");
    el.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("application/x-kb-tab", String(t.id)); } catch (err) { /* ok */ }
  });
  el.addEventListener("dragend", endTabDrag);
  return el;
}

// ---- deep links: the open document IS the URL ------------------------------
// /company/notes.md in the address bar opens that file, so a doc's URL can be
// pasted to a colleague; switching tabs keeps the URL in step.
function pathFromUrl() {
  const segs = location.pathname.split("/").filter(Boolean).map((s) => {
    try { return decodeURIComponent(s); } catch (e) { return s; }
  });
  if (!segs.length || !["company", "projects", "users"].includes(segs[0])) return null;
  return segs.join("/");
}

// Set at boot: does the hub actually route /company/… back to the app? Until
// it does (old hub still running), rewriting the URL would make F5 a 404.
let _deepLinksOk = false;

// True until boot has finished settling the address bar. Opening the deep link
// you arrived on is not a navigation you can go Back from — without this it
// pushed an entry, so Back landed on the same app and looked stuck.
let _settling = true;

function syncUrl(replace) {
  if (!_deepLinksOk) return;
  const want = active
    ? "/" + active.path.split("/").map(encodeURIComponent).join("/")
    : "/";
  if (location.pathname === want) return;
  const st = { path: active ? active.path : null };
  // NB: `history` in this module is CodeMirror's history extension (imported
  // from @codemirror/commands) — always reach the browser API via window.history.
  try {
    if (_restoring || _settling || replace) window.history.replaceState(st, "", want);
    else window.history.pushState(st, "", want);
  } catch (e) { /* about:blank in tests */ }
}

async function openDeepLink(p) {
  const existing = tabs.find((t) => t.path === p);
  // Already open (e.g. restored): don't navigate away. Already the active
  // document: leave its group showing what it showed — a reload's URL names
  // the document the app itself wrote there, and its group may rightly be
  // showing a terminal in front of it.
  if (existing) { if (existing !== active) activateTab(existing); return; }
  if (isSecretPath(p)) await openPath(p, "secret");
  else if (p.endsWith(".html")) await openPath(p, "artifact");
  else if (p.endsWith(".md")) await openPath(p, "doc");
  else if (baseName(p).includes(".")) {
    // a plain attachment link (image, pdf, docx): open it in its own tab so the
    // app itself is never replaced by the raw file
    window.open("/api/attachment?path=" + encodeURIComponent(p), "_blank");
    if (active) syncUrl(true); else window.history.replaceState({}, "", "/");
  }
  // An extension-less path is a folder: show it in the tree — which on a phone
  // means opening the drawer, since the tree is not otherwise on screen.
  else revealFolder(p);
}

function syncTestHooks() {
  // Test hooks (harmless in prod): always describe the ACTIVE tab. __kbpath is
  // set for every tab kind (an active artifact is not "nothing open");
  // __kbview/__kbydoc stay doc-only, and __kbkind disambiguates.
  window.__kbview = active && active.view ? active.view : null;
  window.__kbydoc = active && active.ydoc ? active.ydoc : null;
  window.__kbpath = active ? active.path : null;
  window.__kbkind = active ? active.kind : null;
  window.__kbsynced = !!(active && active.synced);
}

// The path bar as breadcrumbs: segments muted, the file itself in ink, and a
// small flag when the open thing is an artifact executing as the viewer.
function renderDocTitle(t) {
  const host = $("#doc-title");
  host.textContent = "";
  if (!t) { host.textContent = "No document open"; return; }
  t.path.split("/").forEach((seg, i, arr) => {
    if (i) {
      const sep = document.createElement("span");
      sep.className = "crumb-sep"; sep.textContent = "/";
      host.appendChild(sep);
    }
    const el = document.createElement("span");
    el.className = "crumb" + (i === arr.length - 1 ? " last" : "");
    el.textContent = seg;
    host.appendChild(el);
  });
  if (t.kind === "artifact") {
    const flag = document.createElement("span");
    flag.className = "doc-flag"; flag.textContent = "artifact · runs as you";
    host.appendChild(flag);
  } else if (t.kind === "secret") {
    const flag = document.createElement("span");
    flag.className = "doc-flag secret"; flag.textContent = "secret";
    host.appendChild(flag);
  }
}

// A spinner over a tab's pane until its content is actually there. Scoped to
// the tab (not the editor column) so switching away from a slow-opening
// document doesn't drag its spinner onto the one you switched to. The CSS
// holds it invisible for 180ms, so a fast open never flashes it.
function showTabLoading(t, label) {
  if (!t.el || t.el.querySelector(".kb-loading")) return;
  const el = document.createElement("div");
  el.className = "kb-loading";
  el.setAttribute("role", "status");
  el.setAttribute("data-testid", "tab-loading");
  const spin = document.createElement("span");
  spin.className = "upspin";
  const txt = document.createElement("span");
  txt.textContent = label;
  el.append(spin, txt);
  t.el.appendChild(el);
}

function clearTabLoading(t) {
  if (!t || !t.el) return;
  for (const el of t.el.querySelectorAll(".kb-loading")) el.remove();
}

function activateTab(t) {
  if (t && !viewKind(t.kind).isActiveDocument) { showTab(t, true); return; }
  active = t;
  if (t && paneOf(t).collapsed && !_restoring) expandPane(paneOf(t));   // a restore keeps folded groups folded
  // …and one hidden behind another group's maximize brings the layout back:
  // what you asked for is what you see
  if (t && maximizedPaneId && maximizedPaneId !== t.paneId && !_restoring) { maximizedPaneId = null; applyMaximize(); }
  if (t) { activePaneId = t.paneId; focusedPaneId = t.paneId; paneOf(t).active = t; }
  showEachPanesTab();
  // The formatting dock floats over the active document's own group — not
  // over the whole editor area, where it would sit on a side column's chat
  // or on a terminal stacked below. (On a phone it is the keyboard row,
  // fixed to the viewport, and does not care where it lives.)
  if (t) seatDocChrome(paneOf(t));
  renderDocTitle(t);
  setAccessBadge(t ? t.access : null);
  document.querySelectorAll(".tree-item").forEach((e) =>
    e.classList.toggle("active", !!t && e.dataset.path === t.path));
  // Keep the highlight real on a tree that is on screen: expand down to the
  // file and follow it. A phone's drawer is closed right now — it reveals on
  // open instead, so switching tabs never pops the drawer.
  if (t && !isMobile() && !document.body.classList.contains("nav-hidden"))
    revealActiveInTree(false);
  syncTestHooks();
  updateModeUI();
  renderPresence();
  renderSyncBadge();
  renderTabBar();
  saveSession();
  syncUrl();
}

// The document bar (path, history, presence, mode, badge) and the formatting
// dock belong to the active document, so they live inside its group: the
// bar right under the strip (a phone puts it at the group's bottom), the
// dock floating over the text. Both move when the active document does.
function seatDocChrome(p) {
  if (!p) return;
  const bar = document.querySelector(".doc-title-bar");
  if (bar && bar.parentElement !== p.el) p.el.insertBefore(bar, p.hostEl);
  const md = $("#mdbar");
  if (md && md.parentElement !== p.el) p.el.appendChild(md);
}

// Each pane shows its OWN visible tab — switching panes must not blank the
// one you just came from. Only the global `active` follows the click.
function showEachPanesTab() {
  for (const p of allPanes()) {
    const list = paneTabs(p);
    if (!list.includes(p.active)) p.active = list[list.length - 1] || null;
    for (const o of list) o.el.style.display = o === p.active ? "" : "none";
    // The document bar and the formatting dock belong to a document: a group
    // showing a terminal or a chat hides them (an empty group keeps the bar —
    // it is the "No document open" line).
    p.el.classList.toggle("showing-doc", !p.active || viewKind(p.active.kind).isActiveDocument);
  }
  const fp = focusedPane();
  document.body.classList.toggle("kb-term", !!(fp && fp.active && fp.active.kind === "term" && !fp.el.hidden));
}

// Make `t` the tab its group shows, for a kind the header does not describe
// (a terminal): the keyboard goes there and `active` stays what it was.
function showTab(t, focus) {
  const p = paneOf(t);
  if (p.collapsed && !_restoring) expandPane(p);
  if (maximizedPaneId && maximizedPaneId !== p.id && !_restoring) { maximizedPaneId = null; applyMaximize(); }
  p.active = t;
  focusedPaneId = p.id;
  showEachPanesTab();
  t.attention = false;
  if (t.kind === "term") { activateTerm(t, focus); return; }
  renderTabBar();
  saveSession();
  viewKind(t.kind).activate(t);
  if (focus) viewKind(t.kind).focus(t);
}

// `fromPointer` is true only for a close the MOUSE performed on the strip (the
// × or a middle-click). A keyboard close, or a tab retired because its file was
// deleted or moved, has no cursor to keep the next × under and must leave the
// strip free to re-flow.
function closeTab(t, fromPointer) {
  if (t.kind === "term" && t.term) { killTerminal(t, fromPointer); return; }
  const i = tabs.indexOf(t);
  if (i < 0) return;
  const p = paneOf(t);
  if (fromPointer) lockTabStrip(p.barEl); else unlockTabStrip(p.barEl);
  const inPane = paneTabs(p);
  const j = inPane.indexOf(t);
  tabs.splice(i, 1);
  viewKind(t.kind).close(t);
  t.el.remove();
  // The tab you get next is this pane's neighbour, not some other column's.
  const rest = paneTabs(p);
  if (p.active === t) p.active = rest[j] || rest[j - 1] || null;
  const wasActive = active === t;
  if (!rest.length) paneEmptied(p, true);
  if (wasActive) activateTab(nextActiveAfter(p));
  else { renderTabBar(); saveSession(); }
  refocus();
}

// After the active document closes: this pane's neighbour if it is a document,
// else the first document in the pane documents open into, else the first
// document anywhere, else nothing.
function nextActiveAfter(p) {
  const own = p.active && viewKind(p.active.kind).isActiveDocument ? p.active : null;
  const inView = (t) => !paneOf(t).collapsed;   // never a document behind a fold — that would open it
  return own || firstDocTab(paneTabs(activePane()).filter(inView)) || firstDocTab(tabs.filter(inView)) || null;
}

// A tab that never came alive (a restored terminal whose shell could not be
// reached): nothing to tear down, just take it off the strip.
function dropTab(t) {
  const i = tabs.indexOf(t);
  if (i < 0) return;
  const p = paneOf(t);
  tabs.splice(i, 1);
  t.el.remove();
  const rest = paneTabs(p);
  if (p.active === t) p.active = rest[rest.length - 1] || null;
  if (!rest.length) paneEmptied(p, true);
  if (activeTerm === t) { activeTerm = null; window.__kbterm = null; }
  renderTabBar(); saveSession();
}

// Full teardown of a document mount: the provider does NOT destroy its
// awareness (which keeps a heartbeat interval) or the Y.Doc — without these,
// every closed doc tab leaks an interval + document forever.
function closeDocMount(t) {
  if (t.view) t.view.destroy();
  if (t.provider) { t.provider.awareness.destroy(); t.provider.destroy(); }
  if (t.ydoc) t.ydoc.destroy();
  t.view = t.provider = t.ydoc = t.frame = null;
}

async function openPath(path, kind, paneId) {
  return openView(kind, { path }, paneId);
}

// Open a view of any registered kind (views.js): a document, an artifact, a
// secret, a terminal, an agent chat… The shell makes the tab and its element,
// puts it in a group, and hands the element to the kind to fill.
async function openView(kind, spec, paneId) {
  const K = viewKind(kind);
  if (!K) { kbToast("Nothing here can show a " + kind, "err"); return null; }
  closeNav();   // on mobile the drawer covers the editor — opening something is leaving it
  const key = K.key(spec);
  const existing = key === null ? null
    : tabs.find((x) => (x.kind === kind ? K.key(K.serialize(x)) === key : (!!x.path && x.path === spec.path)));
  if (existing) { activateTab(existing); return existing; }
  const pane = placePane(K, paneId);
  const el = document.createElement("div");
  el.className = "tab-content" + (K.contentClass ? " " + K.contentClass : "");
  pane.hostEl.appendChild(el);
  const t = { id: ++tabSeq, kind, path: typeof spec.path === "string" ? spec.path : null,
              name: "", el, paneId: pane.id,
              view: null, provider: null, ydoc: null, frame: null,
              access: null, synced: false,
              mode: localStorage.getItem("kbEditMode") || "rich", modeComp: null };
  t.name = t.path ? baseName(t.path) : K.label;
  if (K.init) K.init(t, spec);
  tabs.push(t);
  unlockTabStrip(pane.barEl);   // a new tab re-flows the strip; the streak is over
  if (t.path) noteRecent(t.path);
  activateTab(t);
  hintHoldToMove();
  try { await K.open(t, spec); }
  catch (e) {
    if (!e || !e.quiet) { console.error(e); kbToast("Could not open " + t.name, "err"); }
    if (tabs.includes(t)) closeTab(t);
    return null;
  }
  return tabs.includes(t) ? t : null;
}

// Which pane a new tab goes to: the one asked for, else the kind's placement —
// "active" (documents: the group of the active document), "dock" (terminals),
// "side" (a column on the right or left, made if missing, at the kind's size;
// on a phone, where columns stack, the active group instead).
function placePane(K, paneId) {
  const asked = paneId && paneById(paneId);
  if (asked) return asked;
  const pl = K.placement || { target: "active" };
  // A phone has no terminal panel: the screen holds one group, so a
  // terminal is a tab in it like everything else. The panel (the dock) is
  // a desktop idea — a band under the documents you can size and fold.
  if (pl.target === "dock" && dockPane) return isPhone() ? activePane() : dockPane;
  // While a group is maximized it is the only one in view, and a document
  // opens there (VS Code's rule) instead of into a hidden group.
  const maxed = maximizedPaneId ? paneById(maximizedPaneId) : null;
  if (maxed && !isDock(maxed) && pl.target !== "side") return maxed;
  if (pl.target === "side") {
    const own = tabs.find((x) => x.kind === K.kind);
    if (own) return paneOf(own);
    // nothing open at all: the chat takes the empty group, not a third of
    // the width beside "No document open"; the first document then opens
    // in a column on the other side (below)
    if (!tabs.length && panes.length === 1) return panes[0];
    const ok = canAddColumn(currentLayout(), $("#panes").getBoundingClientRect().width);
    if (!ok.ok || isMobile()) return activePane();
    const side = pl.side === "left" ? "left" : "right";
    const p = insertPane(side === "right" ? columns.length : 0);
    const size = Math.min(0.5, Math.max(0.15, pl.size || 0.3));
    const nc = colOf(p);
    const total = columns.filter((x) => x !== nc).reduce((a, x) => a + x.grow, 0) || 1;
    nc.grow = (size / (1 - size)) * total;
    normalizeSplits();
    return p;
  }
  // The only group holds side-kind tabs alone (a chat that opened first): a
  // document opens in a new column on the other side, at the workspace's
  // share, and the chat keeps the column it would have asked for.
  const ap = activePane();
  const apTabs = paneTabs(ap);
  const sideOnly = apTabs.length && apTabs.every((x) => ((viewKind(x.kind) || {}).placement || {}).target === "side");
  if (sideOnly && panes.length === 1 && !isMobile()) {
    const spl = viewKind(apTabs[0].kind).placement;
    const ok = canAddColumn(currentLayout(), $("#panes").getBoundingClientRect().width);
    if (ok.ok) {
      const p = insertPane(spl.side === "left" ? columns.length : 0);
      const size = Math.min(0.5, Math.max(0.15, spl.size || 0.3));
      colOf(p).grow = 1 - size; colOf(ap).grow = size;
      normalizeSplits();
      return p;
    }
  }
  return activePane();
}

// ---- the built-in view kinds ------------------------------------------------
registerView("doc", {
  label: "Document",
  icon: () => I.doc,
  tooltip: (t) => t.path,
  key: (spec) => (typeof spec.path === "string" ? spec.path : null),
  restore: (spec) => (typeof spec.path === "string" && spec.path ? { kind: "doc", path: spec.path } : null),
  serialize: (t) => ({ kind: "doc", path: t.path }),
  open: (t) => mountDoc(t),
  close: closeDocMount,
  focus: (t) => { if (t.view) t.view.focus(); },
  isActiveDocument: true,
});
registerView("artifact", {
  label: "Artifact",
  icon: () => I.artifact,
  tooltip: (t) => t.path,
  key: (spec) => (typeof spec.path === "string" ? spec.path : null),
  restore: (spec) => (typeof spec.path === "string" && spec.path ? { kind: "artifact", path: spec.path } : null),
  serialize: (t) => ({ kind: "artifact", path: t.path }),
  open: (t) => { mountArtifact(t); },
  close: closeDocMount,
  isActiveDocument: true,
});
// ---- the agent chat: a view kind whose code arrives on first use ----------
// The chat talks to an ACP agent (Claude Code, Codex, Gemini CLI…) through
// the person's backend; see chat.js and kb_platform/acp.py. Like the terminal
// chunk, nobody who never opens a chat downloads it.
let _chatMod = null;
function warmChat() {
  if (!_chatMod) _chatMod = import("./chat.js").then((m) => { m.init(chatShell); return m; })
    .catch((e) => { _chatMod = null; throw e; });
  return _chatMod;
}
let _repoRoot = "";
// What the chat module may use of the shell — the whole contract, in one place.
const chatShell = {
  wsBase, toast: kbToast, prompt: (m, v, o) => kbPrompt(m, v, o),
  isMobile, isAdmin: () => !$("#admin-btn").hidden,
  repoRoot: () => _repoRoot,
  setRepoRoot: (p) => { if (p) _repoRoot = p; },
  relPath: (abs) => (_repoRoot && abs && abs.startsWith(_repoRoot + "/") ? abs.slice(_repoRoot.length + 1) : abs),
  openAbs: (abs) => {
    const rel = chatShell.relPath(abs);
    if (rel && rel !== abs) openDeepLink(rel);
    else kbToast(abs + " is outside the knowledgebase", "err");
  },
  openTermWith,
  openChat: (spec) => openChat(spec),
  agentAvatar, chatsChanged: () => loadChats(), userName: () => whoamiUser,
  dictation: { ready: dictationReady, toggle: toggleDictation },
  renameTab: (t, name) => { t.name = name || t.name; renderTabBar(); saveSession(); },
  attention: (t) => { if (paneOf(t).active !== t || !document.hasFocus()) { t.attention = true; renderTabBar(); } },
  saveSession,
  agentName: (id) => { const e = settings.entry("ai.agent"); return e && e.labels ? e.labels[id] : null; },
  settings,
  // the chat's context: what the window has open (the visible tab of every
  // group, the focused one first), and a way to pick anything else
  openDocs: () => {
    const seen = new Set(), out = [];
    for (const t of [active, ...allPanes().map((p) => p.active)]) {
      // a _secrets/ file never rides along by itself: it is out of the index,
      // out of git and out of the palette for the same reason
      if (!t || !t.path || isSecretPath(t.path) || seen.has(t.path)) continue;
      seen.add(t.path);
      out.push({ path: t.path, name: t.name || baseName(t.path) });
    }
    return out;
  },
  pickPath: (o) => pickPath(o),
  knowsTree: () => !!(_lastTreePaths && _lastTreePaths.size),
  hasPath: (rel) => !!(_lastTreePaths && _lastTreePaths.has(rel)),
  abs: (rel) => (rel ? (_repoRoot ? _repoRoot + "/" + rel : rel) : _repoRoot),
  baseName,
  openPath: (rel) => openDeepLink(rel),
};
registerView("chat", {
  label: "Agent chat",
  icon: () => I.chat,
  title: (t) => t.name,
  tooltip: (t) => (t.chat && t.chat.sessionId ? t.name + " · " + t.chat.sessionId : t.name),
  key: (spec) => (spec && spec.sessionId ? "chat:" + spec.sessionId : null),
  contentClass: "chat",
  placement: { target: "side", side: "right", size: 0.34 },
  init: (t, spec) => {
    t.path = null;
    t.chat = { agent: spec.agent || defaultChatAgent(), sessionId: spec.sessionId || null,
               title: spec.title || "", ctx: spec.ctx || null };
    t.name = spec.title ? spec.title.slice(0, 40) : (spec.sessionId ? (chatShell.agentName(t.chat.agent) || "Chat") : "New chat");
  },
  restore: (spec) => ({ kind: "chat", agent: typeof spec.agent === "string" ? spec.agent : undefined,
                        sessionId: typeof spec.sessionId === "string" ? spec.sessionId : undefined,
                        title: typeof spec.title === "string" ? spec.title : undefined,
                        ctx: spec.ctx && typeof spec.ctx === "object" ? spec.ctx : undefined }),
  serialize: (t) => ({ kind: "chat", agent: t.chat.agent, sessionId: t.chat.sessionId, title: t.chat.title,
                       ctx: t.chat.ctx || undefined }),
  open: async (t, spec) => (await warmChat()).open(t, spec),
  close: (t) => { if (_chatMod) _chatMod.then((m) => m.close(t)).catch(() => {}); },
  activate: (t) => { t.attention = false; if (_chatMod) _chatMod.then((m) => m.activate(t)).catch(() => {}); },
  focus: (t) => { if (_chatMod) _chatMod.then((m) => m.focus(t)).catch(() => {}); },
});

// Which agent a new chat opens with: your own setting when you made one,
// else the agent you last used on this device, else the company's default.
function defaultChatAgent() {
  let device = null;
  try { device = localStorage.getItem("kbChatAgent"); } catch (e) { /* private mode */ }
  const src = settings.source ? settings.source("ai.agent") : null;
  if (src === "user") return settings.get("ai.agent") || device || "claude";
  return device || settings.get("ai.agent") || "claude";
}

// The chat button, Alt+C and the palette: a NEW chat with your agent, every
// time — in the side column the kind asks for (beside the chats you have).
function openChat(spec) {
  openView("chat", spec || {});
}

// An agent's mark: a tinted square with a glyph, the same in the sidebar's
// chat rows, the composer's chip and the empty screen. Claude's is the
// spark its terminal prints; the others take a letter.
const AGENT_MARK = { claude: "✻", codex: "C", gemini: "G", copilot: "Cp", grok: "Gr", qwen: "Q", deepseek: "D", opencode: "O", echo: "E" };
function agentAvatar(id, size) {
  const e = document.createElement("span");
  e.className = "av av-" + (AGENT_MARK[id] ? id : "other") + (size ? " av-" + size : "");
  e.textContent = AGENT_MARK[id] || (id || "?").slice(0, 1).toUpperCase();
  e.setAttribute("aria-hidden", "true");
  return e;
}

// ---- the inbox: what happened while you were elsewhere ---------------------
// Events are written by whoever saw them — syncd when a document you were
// @named in is committed, the hub when something is shared with you — into
// your own `users/<you>/.os/inbox.jsonl`. The app only reads that file and
// marks lines read. A dot on the person, a count in their menu, a list.
let inboxUnread = 0;

async function loadInbox() {
  try {
    const r = await fetch("/api/inbox");
    if (!r.ok) return null;
    const j = await r.json();
    inboxUnread = j.unread || 0;
    renderInboxCount();
    return j;
  } catch (e) { return null; }
}
function renderInboxCount() {
  const n = document.querySelector("#inbox-btn .inbox-n");
  if (n) n.textContent = inboxUnread ? String(inboxUnread) : "";
  const btn = $("#user-btn");
  if (btn) btn.classList.toggle("has-news", !!inboxUnread);
}
function inboxChanged() {
  loadInbox().then((j) => { for (const t of tabs.filter((x) => x.kind === "inbox")) paintInbox(t, j); });
}
async function markInbox(body) {
  try {
    const r = await fetch("/api/inbox/read", { method: "POST", headers: { "content-type": "application/json" },
                                               body: JSON.stringify(body || {}) });
    if (!r.ok) return false;
  } catch (e) { return false; }
  inboxChanged();
  return true;
}
function openInbox() { openView("inbox", {}); }

// "kbt_p12_alice" and "tomas_vargosko" both want to read as a person
const shortName = (u) => String(u || "").replace(/^kbt_[a-z0-9]+_/, "").split(/[._-]+/)[0];

const INBOX_WORD = {
  mention: "mentioned you in",
  share: "shared",
  agent: "finished in",
};

function paintInbox(t, data) {
  const host = t.el.querySelector(".inbox-list");
  if (!host) return;
  host.textContent = "";
  const events = (data && data.events) || [];
  const clear = t.el.querySelector(".inbox-clear");
  if (clear) clear.hidden = !events.length;
  if (!events.length) {
    host.append(el2("div", "trash-empty muted", "Nothing new. Mentions and things shared with you land here."));
    return;
  }
  for (const e of events) {
    const row = el2("button", "inbox-item" + (e.read ? "" : " unread"));
    row.type = "button";
    row.setAttribute("data-testid", "inbox-item");
    row.dataset.id = e.id;
    const ic = el2("span", "inbox-ic");
    ic.innerHTML = e.kind === "share" ? I.share : e.kind === "agent" ? I.chat : I.mention;
    const body = el2("div", "inbox-body");
    const who = e.actor ? shortName(e.actor) : "Someone";
    body.append(el2("div", "inbox-line", who + " " + (INBOX_WORD[e.kind] || "touched") + " " + baseName(e.path)));
    if (e.text) body.append(el2("div", "inbox-quote", e.text));
    body.append(el2("div", "inbox-when", e.path + " · " + agoShort(e.at)));
    row.append(ic, body);
    row.addEventListener("click", async () => {
      await markInbox({ ids: [e.id] });
      if (e.path) openAtLine(e.path, e.line || 0);
    });
    host.append(row);
  }
}

registerView("inbox", {
  label: "Inbox",
  icon: () => I.mention,
  title: () => "Inbox",
  tooltip: () => "Mentions, and what was shared with you",
  key: () => "inbox",
  contentClass: "trash-view",
  restore: () => ({ kind: "inbox" }),
  serialize: () => ({ kind: "inbox" }),
  init: (t) => { t.path = null; t.name = "Inbox"; },
  open: async (t) => {
    t.el.textContent = "";
    const head = el2("div", "trash-head");
    head.append(el2("h2", "trash-title", "Inbox"),
                el2("div", "trash-note muted", "Mentions, and what people shared with you."));
    const clear = el2("button", "mini inbox-clear", "Mark all read");
    clear.type = "button"; clear.hidden = true;
    clear.addEventListener("click", () => markInbox({}));
    head.append(clear);
    const list = el2("div", "inbox-list");
    list.setAttribute("data-testid", "inbox-list");
    t.el.append(head, list);
    paintInbox(t, await loadInbox());
  },
  activate: (t) => { loadInbox().then((j) => paintInbox(t, j)); },
});

// ---- the trash: deleting is a move, so it can come back ---------------------
// `.trash/` inside the folder that owns the audience (company/, a project, a
// person's own folder). It is a dot-directory, so the tree hides it, the
// index skips it and search never turns up a deleted document — while an
// agent with a shell reads it like any other folder. Nothing red anywhere:
// deleting says "Moved to the trash" with an Undo, and this view is a list.
let trashCount = 0;

async function loadTrash() {
  try {
    const r = await fetch("/api/fs/trash");
    if (!r.ok) return null;
    const j = await r.json();
    trashCount = (j.entries || []).length;
    renderTrashRow();
    return j;
  } catch (e) { return null; }
}
function trashChanged() {
  loadTrash().then((j) => { for (const t of tabs.filter((x) => x.kind === "trash")) paintTrash(t, j); });
}
async function restoreTrash(path) {
  let j = {};
  try {
    const r = await fetch("/api/fs/restore", { method: "POST", headers: { "content-type": "application/json" },
                                               body: JSON.stringify({ path }) });
    j = await r.json().catch(() => ({}));
    if (!r.ok) { kbToast(j.error || "could not restore it", "err"); return null; }
  } catch (e) { kbToast("could not reach the server", "err"); return null; }
  await loadTree(true);
  trashChanged();
  kbToast(j.renamed ? "Restored as “" + baseName(j.path) + "”" : "Restored “" + baseName(j.path) + "”", "ok");
  return j.path;
}
async function purgeTrash(body, what) {
  try {
    const r = await fetch("/api/fs/trash-purge", { method: "POST", headers: { "content-type": "application/json" },
                                                   body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { kbToast(j.error || "could not empty the trash", "err"); return false; }
  } catch (e) { kbToast("could not reach the server", "err"); return false; }
  kbToast(what, "ok");
  trashChanged();
  return true;
}

// The trash lives behind the person's ⋯, beside Settings — a rare action
// does not need a row of the sidebar. What it does need is to say that it
// holds something, so the menu item carries the count.
function renderTrashRow() {
  const n = document.querySelector("#trash-btn .trash-row-n");
  if (n) n.textContent = trashCount ? String(trashCount) : "";
}

function openTrash() { openView("trash", {}); }

function paintTrash(t, data) {
  const host = t.el.querySelector(".trash-list");
  if (!host) return;
  const note = t.el.querySelector(".trash-note");
  if (!data) { host.textContent = ""; host.append(el2("div", "trash-empty muted", "Could not read the trash.")); return; }
  if (note) note.textContent = "Deleted things wait in a .trash folder beside where they lived, until you empty it.";
  const empty = t.el.querySelector(".trash-empty-btn");
  if (empty) empty.hidden = !(data.entries || []).length;
  host.textContent = "";
  if (!(data.entries || []).length) {
    host.append(el2("div", "trash-empty muted", "Nothing in the trash."));
    return;
  }
  for (const e of data.entries) {
    const row = el2("div", "trash-item");
    row.setAttribute("data-testid", "trash-item");
    row.dataset.path = e.path;
    const ic = el2("span", "trash-ic");
    ic.innerHTML = e.dir ? I.folder : e.name.endsWith(".html") ? I.artifact : e.name.endsWith(".md") ? I.doc : I.file;
    const body = el2("div", "trash-body");
    body.append(el2("div", "trash-name", e.name),
                el2("div", "trash-from", "in " + (e.folder || "/") + " · " + agoShort(e.at)));
    const acts = el2("div", "trash-acts");
    const back = el2("button", "mini", "Put back"); back.type = "button";
    back.setAttribute("data-testid", "trash-restore");
    back.addEventListener("click", () => restoreTrash(e.path));
    const gone = el2("button", "mini trash-forget", "Delete for good"); gone.type = "button";
    gone.addEventListener("click", async () => {
      if (!await kbConfirm("Delete “" + e.name + "” for good?", { title: "Delete for good", ok: "Delete", danger: true })) return;
      purgeTrash({ path: e.path }, "Gone for good");
    });
    acts.append(back, gone);
    row.append(ic, body, acts);
    host.append(row);
  }
}

function el2(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

registerView("trash", {
  label: "Trash",
  icon: () => I.trash,
  title: () => "Trash",
  tooltip: () => "Deleted files, and the way back",
  key: () => "trash",
  contentClass: "trash-view",
  restore: () => ({ kind: "trash" }),
  serialize: () => ({ kind: "trash" }),
  init: (t) => { t.path = null; t.name = "Trash"; },
  open: async (t) => {
    t.el.textContent = "";
    const head = el2("div", "trash-head");
    head.append(el2("h2", "trash-title", "Trash"), el2("div", "trash-note muted", ""));
    const empty = el2("button", "mini trash-empty-btn", "Empty the trash");
    empty.type = "button"; empty.hidden = true;
    empty.addEventListener("click", async () => {
      if (!await kbConfirm("Delete everything in the trash for good?",
                           { title: "Empty the trash", ok: "Empty it", danger: true })) return;
      purgeTrash({ all: true }, "The trash is empty");
    });
    head.append(empty);
    const list = el2("div", "trash-list");
    list.setAttribute("data-testid", "trash-list");
    t.el.append(head, list);
    paintTrash(t, await loadTrash());
  },
  activate: (t) => { loadTrash().then((j) => paintTrash(t, j)); },
});

// ---- the sidebar's Chats: new one, recent ones ------------------------------
// The index the backend keeps (`/api/acp/chats`): every agent's sessions with
// their titles and last activity. The eight most recent are rows beside the
// files; the rest are one click away in the chat's Recent chats.
let _chats = [];
async function loadChats() {
  try { _chats = (await (await fetch("/api/acp/chats")).json()).chats || []; }
  catch (e) { return; }
  renderChats();
}
const CHATS_SHOWN = 5;          // …then ⋯ for the rest
let _chatsOpen = false;
function renderChats() {
  const host = $("#chats");
  if (!host) return;
  // a pinned chat stays at the top and is never hidden behind the ⋯
  const all = _chats.filter((c) => c.title);
  const pinned = all.filter((c) => c.pinned);
  const rest = all.filter((c) => !c.pinned);
  const list = _chatsOpen ? all.slice(0, 40) : pinned.concat(rest.slice(0, Math.max(0, CHATS_SHOWN - pinned.length)));
  host.innerHTML = "";
  host.hidden = !all.length;
  for (const c of list) {
    const row = document.createElement("div");
    row.className = "chat-row" + (c.pinned ? " pinned" : ""); row.dataset.sid = c.id;
    row.tabIndex = 0; row.setAttribute("role", "button");
    row.title = c.title + " · " + (chatShell.agentName(c.agent) || c.agent);
    const t = document.createElement("span"); t.className = "chat-row-title"; t.textContent = c.title;
    const w = document.createElement("span"); w.className = "chat-row-time"; w.textContent = agoShort(c.updatedAt);
    const open = () => { closeNav(); openChat({ agent: c.agent, sessionId: c.id, title: c.title }); };
    row.append(agentAvatar(c.agent), t);
    if (c.pinned) { const p = document.createElement("span"); p.className = "chat-row-pin"; p.innerHTML = I.pin; p.title = "Pinned"; row.append(p); }
    row.append(w);
    // the same menu on a pointer and on a finger: right-click, or the ⋯
    const more = document.createElement("button");
    more.type = "button"; more.className = "tbtn chat-row-more-btn"; more.textContent = "⋯";
    more.title = "Pin, rename, delete"; more.setAttribute("aria-label", "Chat actions");
    more.addEventListener("click", (e) => { e.stopPropagation(); const r = more.getBoundingClientRect(); openChatMenu(c, r.right, r.bottom + 4); });
    row.append(more);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    row.addEventListener("contextmenu", (e) => { e.preventDefault(); openChatMenu(c, e.clientX, e.clientY); });
    host.append(row);
  }
  // ⋯ opens the rest in place; when they are all here it closes them again
  if (all.length > CHATS_SHOWN) {
    const more = document.createElement("button");
    more.type = "button"; more.className = "chat-row chat-row-more"; more.dataset.testid = "chats-more";
    more.textContent = _chatsOpen ? "···" : "···";
    more.title = _chatsOpen ? "Show fewer chats" : "Show all " + all.length + " chats";
    more.setAttribute("aria-label", more.title);
    more.setAttribute("aria-expanded", _chatsOpen ? "true" : "false");
    more.addEventListener("click", () => { _chatsOpen = !_chatsOpen; renderChats(); });
    host.append(more);
  } else if (_chatsOpen) _chatsOpen = false;
  syncChatRows();
}
// What you can do to a chat from the list — the same set the chat's own ⋯
// offers, plus the pin and the delete a list needs.
function openChatMenu(c, x, y) {
  const items = [
    { icon: I.chat, label: "Open", fn: () => { closeNav(); openChat({ agent: c.agent, sessionId: c.id, title: c.title }); } },
    { icon: I.pin, label: c.pinned ? "Unpin" : "Pin to the top",
      fn: () => pinChat(c, !c.pinned) },
    { icon: I.pencil, label: "Rename…", fn: () => renameChat(c) },
    "-",
    { icon: I.trash, label: "Delete", danger: true, fn: () => deleteChat(c) },
  ];
  openCtxMenu(items, x, y);
}

async function chatAction(url, body, fail) {
  try {
    const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" },
                                 body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) { kbToast(j.error || fail, "err"); return null; }
    return j;
  } catch (e) { kbToast(fail, "err"); return null; }
}

// After a change, take the list from the server rather than patching the
// row in place: a background refresh may have replaced these objects, and
// the file is the truth anyway.
async function pinChat(c, on) {
  if (!await chatAction("/api/acp/pin", { id: c.id, pinned: on }, "could not pin this chat")) return;
  await loadChats();
}

async function renameChat(c) {
  const name = await kbPrompt("Name this chat", c.title || "", { title: "Rename", ok: "Rename" });
  if (name === null || name === undefined) return;
  const clean = String(name).trim();
  if (!clean) return;
  const j = await chatAction("/api/acp/rename", { id: c.id, title: clean }, "could not rename this chat");
  if (!j) return;
  const title = j.title || clean;
  for (const t of tabs) if (t.kind === "chat" && t.chat && t.chat.sessionId === c.id) {
    t.chat.title = title;
    if (t.chatView) t.chatView.title = title;
    chatShell.renameTab(t, title.slice(0, 40));
  }
  await loadChats();
}

async function deleteChat(c) {
  const ok = await kbConfirm("Delete “" + (c.title || "this chat") + "” from your list? "
                             + "The conversation stays with the agent; this takes it off your list.",
                             { title: "Delete chat", ok: "Delete", danger: true });
  if (!ok) return;
  if (!await chatAction("/api/acp/forget", { id: c.id }, "could not delete this chat")) return;
  await loadChats();
  kbToast("Deleted from your chats", "ok");
}

function agoShort(t) {
  if (!t) return "";
  const s = Math.max(0, Date.now() / 1000 - t);
  if (s < 60) return "now";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  if (s < 7 * 86400) return Math.floor(s / 86400) + "d";
  return new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
// the row of the chat in view is marked, as the tree marks the open file
function syncChatRows() {
  const shown = new Set(tabs.filter((t) => t.kind === "chat" && t.chat && t.chat.sessionId && paneOf(t).active === t).map((t) => t.chat.sessionId));
  document.querySelectorAll("#chats .chat-row[data-sid]").forEach((r) => r.classList.toggle("active", shown.has(r.dataset.sid)));
}

registerView("secret", {
  label: "Secret",
  icon: () => I.lock,
  tooltip: (t) => t.path,
  key: (spec) => (typeof spec.path === "string" ? spec.path : null),
  restore: (spec) => (typeof spec.path === "string" && spec.path ? { kind: "secret", path: spec.path } : null),
  serialize: (t) => ({ kind: "secret", path: t.path }),
  open: (t) => mountSecret(t),
  close: closeDocMount,
  isActiveDocument: true,
});

// ---- secret viewer: masked, reveal/copy/edit — never the collab editor ----
async function mountSecret(t) {
  showTabLoading(t, "Opening " + baseName(t.path) + "…");
  const r = await fetch("/api/file?path=" + encodeURIComponent(t.path));
  const j = await r.json().catch(() => ({}));
  if (!tabs.includes(t)) return;
  clearTabLoading(t);   // el.innerHTML below would strand it anyway
  refreshTabBadge(t);
  const el = t.el;
  el.className = "tab-content secret-view";
  if (!r.ok) {
    el.innerHTML = '<div class="secret-card"><div class="secret-denied">🔒 ' +
      escapeHtml(j.error === "forbidden" ? "You don't have access to this secret." : (j.error || "cannot read")) +
      "</div></div>";
    return;
  }
  let content = j.content || "";
  el.innerHTML = `
    <div class="secret-card">
      <div class="secret-head">
        <span class="secret-name">🔒 ${escapeHtml(baseName(t.path))}</span>
        <span class="muted small">never in git history, search, or the live-doc relay</span>
      </div>
      <pre class="secret-body masked" data-testid="secret-body"></pre>
      <textarea class="secret-edit" data-testid="secret-edit" spellcheck="false" hidden></textarea>
      <div class="secret-actions">
        <button data-s="reveal" data-testid="secret-reveal">Reveal</button>
        <button data-s="copy">Copy</button>
        <button data-s="edit">Edit</button>
        <button data-s="save" class="primary" hidden>Save</button>
        <button data-s="cancel" hidden>Cancel</button>
      </div>
    </div>`;
  const body = el.querySelector(".secret-body");
  const ta = el.querySelector(".secret-edit");
  const btn = (k) => el.querySelector(`[data-s="${k}"]`);
  let revealed = false;
  const paint = () => {
    body.textContent = revealed ? content
      : content.replace(/[^\n]/g, "•").split("\n").map((l) => l.slice(0, 24)).join("\n");
    body.classList.toggle("masked", !revealed);
    btn("reveal").textContent = revealed ? "Hide" : "Reveal";
  };
  paint();
  btn("reveal").addEventListener("click", () => { revealed = !revealed; paint(); });
  btn("copy").addEventListener("click", () =>
    navigator.clipboard.writeText(content).then(
      () => kbToast("Secret copied to clipboard", "ok"),
      () => kbToast("could not copy", "err")));
  const editMode = (on) => {
    ta.hidden = !on; body.hidden = on;
    btn("save").hidden = btn("cancel").hidden = !on;
    btn("edit").hidden = btn("reveal").hidden = btn("copy").hidden = on;
    if (on) { ta.value = content; ta.focus(); }
  };
  btn("edit").addEventListener("click", () => editMode(true));
  btn("cancel").addEventListener("click", () => editMode(false));
  btn("save").addEventListener("click", async () => {
    const rr = await fetch("/api/artifact/write", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: t.path, content: ta.value }),
    });
    const jj = await rr.json().catch(() => ({}));
    if (!rr.ok || jj.error) { kbToast(jj.error || "could not save", "err"); return; }
    content = ta.value;
    editMode(false); paint();
    kbToast("Secret saved", "ok");
  });
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

async function mountDoc(t) {
  // Until the CRDT session syncs there is nothing in the pane — not an empty
  // document, just nothing yet. Say so, or a slow open is indistinguishable
  // from a blank file.
  showTabLoading(t, "Opening " + baseName(t.path) + "…");
  // Both round trips start NOW, in parallel — they are independent, and over a
  // tunnel each costs a full RTT; serializing them was a visible chunk of the
  // time-to-first-keystroke on every doc open.
  const propsP = fetch("/fs/props?path=" + encodeURIComponent(t.path));
  // the doc's lineage id: presenting it is what admits us to the live session
  // (a tab holding an older lineage would re-merge stale history as duplicated
  // text — the relay refuses those instead)
  const epochP = fetch("/api/doc-epoch?path=" + encodeURIComponent(t.path))
    .then((r) => r.json()).then((j) => j.epoch || "")
    .catch(() => "");  // offline; the connect will just be refused
  // Decide editable-ness up front so the editor opens in the right mode. A
  // read-only file still opens in the live session (you see it + updates), just
  // not editable — the daemon also refuses to persist edits from a read-only join.
  let access = { read: true, write: true };
  try {
    const rr = await propsP;
    const pr = await rr.json();
    // gone (404), or present-but-unreadable (hub returns 200 with read:false) —
    // either way there is nothing to edit, so say so instead of mounting a dead,
    // never-syncing editor. A bad/paste-shared deep link lands here.
    if (rr.status === 404 || rr.status === 403 || (pr.access && !pr.access.read)) {
      kbToast(baseName(t.path) + ": " + (pr.error || "you don't have access"), "err");
      closeTab(t);
      return;
    }
    if (pr.access) access = pr.access;
  } catch (e) { /* default to editable; the daemon is the real gate */ }
  if (!tabs.includes(t)) return;   // tab closed while we were fetching
  t.access = access;
  if (active === t) setAccessBadge(access);
  const readOnly = !access.write;

  const epoch = await epochP;
  if (!tabs.includes(t)) return;
  const ydoc = new Y.Doc();
  const provider = new WebsocketProvider(wsBase() + "/ws/doc", t.path, ydoc,
                                         { params: { e: epoch } });
  const ytext = ydoc.getText("content");
  const undoManager = new Y.UndoManager(ytext);

  t.modeComp = new Compartment();
  const extensions = [
    history(),
    keymap.of([
      { key: "Mod-b", run: () => { tbInline("**"); return true; } },
      { key: "Mod-i", run: () => { tbInline("*"); return true; } },
      { key: "Mod-Shift-k", run: () => { tbLink(); return true; } },
      { key: "Tab", run: (v) => listIndent(v, 1), shift: (v) => listIndent(v, -1) },
      ...searchKeymap, ...defaultKeymap, ...historyKeymap,
    ]),
    // find/replace inside the open document (Ctrl+F), with the panel on top so
    // it never hides behind the terminal
    cmSearch({ top: true }),
    highlightSelectionMatches(),
    autocompletion({ override: [mentionSource], icons: false }),
    // GFM task lists + strikethrough + tables, so the tree exposes TaskMarker /
    // Strikethrough / Table nodes the live-preview layer decorates.
    markdown({ extensions: [TaskList, Strikethrough, Table, spacedLinks] }),
    syntaxHighlighting(mdHighlight),
    mentionHighlight(),
    yCollab(ytext, provider.awareness, { undoManager }),
    EditorView.lineWrapping,
    // draw our own caret/selection: the native caret is near-invisible on some
    // mobile browsers against the dark chassis
    drawSelection(),
    dropCursor(),
    mediaExtension(t),
    t.modeComp.of(modeExts(t)),
  ];
  if (readOnly) {
    extensions.push(EditorState.readOnly.of(true), EditorView.editable.of(false));
  }
  t.ydoc = ydoc;
  t.provider = provider;
  t.view = new EditorView({
    state: EditorState.create({ doc: ytext.toString(), extensions }),
    parent: t.el,
  });
  calibrateListMetrics(t.view.contentDOM);
  // Sync visibility: an editor that is NOT live-syncing must say so — the one
  // thing worse than a broken connection is a silently broken one (you type,
  // your colleague sees nothing). Also self-heal the stale-lineage case: if the
  // relay refuses us and the doc's epoch has moved on, rebuild this tab fresh.
  t.conn = "connecting";
  provider.on("status", (e) => {
    t.conn = e.status;
    if (active === t) renderSyncBadge();
  });
  provider.on("connection-close", async () => {
    if (t._relineaged || !tabs.includes(t)) return;
    try {
      const now = (await (await fetch("/api/doc-epoch?path=" + encodeURIComponent(t.path))).json()).epoch;
      if (now && now !== epoch) {
        t._relineaged = true;
        const path = t.path, kind = t.kind;
        closeTab(t);
        await openPath(path, kind);
        kbToast("Document session was out of date — reloaded it fresh", "ok");
      }
    } catch (e) { /* stay disconnected; the badge shows it */ }
  });
  // Read at every announce, never captured: a tab restored before whoami has
  // answered would otherwise introduce you to your colleagues as "user" for
  // as long as it stayed open. loadWhoami re-announces every open tab.
  const me = () => window.__kbuser || "user";
  const setMe = () => provider.awareness.setLocalStateField("user", { name: me(), ...userColors(me()) });
  t.announce = setMe;
  setMe();
  // Presence + remote cursors both ride on awareness: repaint the avatar row
  // whenever anyone joins, leaves, or moves — for the tab that's showing. The
  // relay doesn't replay existing presence to a late joiner, so when a NEW peer
  // appears we re-announce ourselves — they learn we're here immediately instead
  // of waiting for the ~30s awareness heartbeat. (Re-announcing emits 'updated'
  // for our own id, never 'added', so this converges and can't loop.)
  provider.awareness.on("change", ({ added }) => {
    if (added && added.length) setMe();
    if (active === t) renderPresence();
  });
  provider.on("sync", (isSynced) => {
    t.synced = isSynced;
    if (isSynced) clearTabLoading(t);   // the text is really here now
    if (active === t) { window.__kbsynced = isSynced; renderSyncBadge(); }
  });
  // Never spin forever. If the relay is unreachable the doc genuinely has no
  // content to show, but an endless spinner claims it is still coming — drop
  // to the empty editor and let the "not syncing" badge tell the true story.
  setTimeout(() => {
    if (tabs.includes(t) && !t.synced) clearTabLoading(t);
  }, 10000);
  if (active === t) { syncTestHooks(); updateModeUI(); renderPresence(); }
}

// A stable, distinct color per username (so every collaborator's cursor + avatar
// is their own). Vivid enough to read on the dark chassis.
function userColors(name) {
  let h = 5381;
  for (let i = 0; i < name.length; i++) h = ((h << 5) + h + name.charCodeAt(i)) >>> 0;
  const hue = h % 360;
  const sat = 62 + ((h >>> 9) % 22);   // parenthesised: % binds tighter than >> in JS
  return { color: `hsl(${hue} ${sat}% 62%)`, colorLight: `hsl(${hue} ${sat}% 62% / .30)` };
}

// One badge in the title bar for two facts. The icon: a pen when you may
// write here, an eye when you may only read. The colour: primary while the
// live session is connected and synced, the danger colour while it is not
// (a silently dead connection once made a whole pairing session look broken).
// The words live in the tooltip.
function renderDocBadge(accessOverride) {
  const b = $("#access-badge");
  const t = active;
  const access = accessOverride !== undefined ? accessOverride : (t ? t.access : null);
  if (!t || !access) { b.hidden = true; return; }
  const write = !!access.write;
  const state = t.provider ? (t.conn === "connected" && t.synced ? "live" : "off") : "";
  b.innerHTML = write ? I.pencil : I.eye;
  b.className = "access-badge " + (write ? "rw" : "ro") + (state ? " " + state : "");
  const what = write ? "You can read and write this file"
             : access.read ? "Read-only: you can see this file, not change it" : "No access";
  const how = state === "live" ? "live — everyone sees your edits in real time"
            : state === "off" ? "NOT connected to the live session — your edits are not reaching others. Reopen the tab if this persists."
            : "";
  b.title = (how ? `${what} · ${how}` : what) + " · click: who can open this (Alt+S)";
  b.setAttribute("aria-label", b.title);
  b.setAttribute("role", "button"); b.tabIndex = 0;
  b.hidden = false;
}
function renderSyncBadge() { renderDocBadge(); }

let _presenceSig = "";

// The "who's here" row: one avatar per distinct user with the doc open (dedup by
// name across their tabs/windows), yourself marked. Reads live awareness state.
function renderPresence() {
  const host = $("#presence");
  const t = active;
  if (!t || t.kind !== "doc" || !t.provider) { host.hidden = true; host.textContent = ""; _presenceSig = ""; return; }
  const states = t.provider.awareness.getStates();
  const selfId = t.provider.awareness.clientID;
  const seen = new Map();
  for (const [id, st] of states) {
    const u = st.user;
    if (!u || !u.name) continue;
    const cur = seen.get(u.name) || { name: u.name, color: u.color || "var(--accent)", self: false };
    if (id === selfId) cur.self = true;
    seen.set(u.name, cur);
  }
  const users = [...seen.values()].sort((a, b) => (b.self ? 1 : 0) - (a.self ? 1 : 0));
  // awareness 'change' also fires on every remote cursor move — only touch the
  // DOM when the actual set of people (name/color/self) changed.
  const sig = t.path + "|" + users.map((u) => u.name + u.color + u.self).join(",");
  if (sig === _presenceSig) return;
  _presenceSig = sig;
  host.textContent = "";
  host.hidden = users.length === 0;
  for (const u of users) {
    const a = document.createElement("span");
    a.className = "presence-avatar" + (u.self ? " self" : "");
    a.style.background = u.color;
    a.textContent = u.name.slice(0, 2).toUpperCase();
    a.title = u.name + (u.self ? " (you)" : "") + " · editing now";
    host.appendChild(a);
  }
}

// ---- artifacts (sandboxed, query-as-viewer) -------------------------------
function mountArtifact(t) {
  // An artifact is a whole page fetched into a sandboxed frame — the slowest
  // thing to open, and until it paints the frame is plain white.
  showTabLoading(t, "Opening " + baseName(t.path) + "…");
  const frame = document.createElement("iframe");
  frame.className = "artifact-frame";
  // 'load' fires for error responses too, which is what we want: either way
  // the frame is showing its own content now and the spinner would be a lie.
  frame.addEventListener("load", () => clearTabLoading(t));
  // Two independent walls: (1) sandbox="allow-scripts" WITHOUT allow-same-origin
  // gives the artifact an opaque origin — it cannot touch the app's DOM, cookies,
  // or session. (2) The /api/artifact/raw response carries a strict CSP
  // (connect-src 'none', etc.) so the artifact cannot reach the network to
  // exfiltrate data. Its only capability is the postMessage query bridge.
  frame.setAttribute("sandbox", "allow-scripts");
  frame.src = "/api/artifact/raw?path=" + encodeURIComponent(t.path);
  t.el.appendChild(frame);
  t.frame = frame;
  refreshTabBadge(t);
}

async function refreshTabBadge(t) {
  try {
    const p = await (await fetch("/fs/props?path=" + encodeURIComponent(t.path))).json();
    t.access = p.access || null;
  } catch (e) { t.access = null; }
  if (active === t) setAccessBadge(t.access);
}

// An artifact reading binary gets the bytes buffered in the browser first, so
// cap it: past this a tab would stall or die, and the honest answer is "link to
// it instead". Blob storage can spill to disk, so this is generous.
const MAX_ARTIFACT_BYTES = 512 * 1024 * 1024;

// A kb-read/kb-write path is allowed only within the requesting artifact's own
// folder (confused-deputy containment) — checked against THAT tab's path, not
// whichever tab happens to be focused.
function inArtifactScope(tab, p) {
  if (typeof p !== "string" || p.includes("..")) return false;
  const dir = dirName(tab.path);
  if (dir === "") return false;               // artifact at repo root: allow nothing
  return p === dir || p.startsWith(dir + "/");
}

// The bridge: forward an artifact's SQL to the per-user backend, which runs it as
// the viewer. The message is matched to the open artifact tab it came from —
// background artifact tabs stay live (dashboards keep refreshing).
window.addEventListener("message", async (ev) => {
  const tab = tabs.find((t) => t.frame && ev.source === t.frame.contentWindow);
  if (!tab) return;
  const msg = ev.data || {};
  let result;
  try {
    if (msg.type === "kb-query") {
      const r = await fetch("/api/artifact/query", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ sql: msg.sql, params: msg.params || [] }),
      });
      result = await r.json();
    } else if (msg.type === "kb-toggle") {
      // Flip a task checkbox. Forwarded to the toggle endpoint, which edits the
      // source file AS the viewer (kernel-checked).
      const r = await fetch("/api/tasks/toggle", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: msg.path, line: msg.line }),
      });
      result = await r.json();
    } else if (msg.type === "kb-read") {
      // Read a file AS the viewer (kernel enforces access), scoped to the
      // artifact's own folder so a hostile artifact can't read the viewer's
      // private files elsewhere (confused-deputy containment).
      if (!inArtifactScope(tab, msg.path)) {
        result = { error: "path outside this artifact's folder" };
      } else {
        const r = await fetch("/api/artifact/read", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ path: msg.path }),
        });
        result = await r.json();
      }
    } else if (msg.type === "kb-read-bytes") {
      // Binary sibling of kb-read: hand the artifact the file's BYTES so it can
      // show a video/image/PDF that lives next to it. The host (authenticated)
      // fetches as the viewer; the Blob crosses the sandbox boundary by
      // structured clone — no base64 sidecars, no copy — and the artifact makes
      // its own blob: URL. blob: is a local memory handle, so this displays
      // bytes without giving the sandbox any way to send them out.
      if (!inArtifactScope(tab, msg.path)) {
        result = { error: "path outside this artifact's folder" };
      } else {
        const r = await fetch("/api/attachment?path=" + encodeURIComponent(msg.path));
        if (!r.ok) {
          result = { error: r.status === 403 ? "forbidden" : "not found" };
        } else {
          // The hub strips content-length, so this pre-flight only fires when
          // it survives; blob.size below is the check that always holds.
          const len = Number(r.headers.get("content-length") || 0);
          const tooBig = (n) => ({ error: "file is too large to load into an artifact (" +
                                          Math.round(n / 1048576) + " MB)" });
          if (len > MAX_ARTIFACT_BYTES) {
            result = tooBig(len);
          } else {
            const blob = await r.blob();
            result = blob.size > MAX_ARTIFACT_BYTES ? tooBig(blob.size)
              : { ok: true, blob, size: blob.size, content_type: blob.type };
          }
        }
      }
    } else if (msg.type === "kb-write") {
      // Write a file AS the viewer, scoped to the artifact's own folder so it
      // can't overwrite/poison files elsewhere (e.g. shared docs or config).
      if (!inArtifactScope(tab, msg.path)) {
        result = { error: "path outside this artifact's folder" };
      } else {
        const r = await fetch("/api/artifact/write", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ path: msg.path, content: msg.content }),
        });
        result = await r.json();
        // a write can also CREATE, so a new file shows up in the tree at once
        if (result.ok && !(_lastTreePaths && _lastTreePaths.has(result.path))) loadTree(true);
      }
    } else if (msg.type === "kb-list") {
      // List a folder at or under the artifact's own — the same folder scope as
      // kb-read, so an artifact can browse its own tree and nothing else.
      if (!inArtifactScope(tab, msg.path)) {
        result = { error: "path outside this artifact's folder" };
      } else {
        const r = await fetch("/api/artifact/list", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ artifact: tab.path, path: msg.path, depth: msg.depth || 1 }),
        });
        result = await r.json();
      }
    } else if (msg.type === "kb-mkdir") {
      // Create a subfolder AS the viewer, inside the artifact's own folder.
      if (!inArtifactScope(tab, msg.path)) {
        result = { error: "path outside this artifact's folder" };
      } else {
        const r = await fetch("/api/artifact/mkdir", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ artifact: tab.path, path: msg.path }),
        });
        result = await r.json();
        if (result.ok) loadTree(true);
      }
    } else if (msg.type === "kb-delete") {
      // Delete a file or folder AS the viewer, inside the artifact's own folder.
      // The backend refuses the folder itself and the artifact's own file, and
      // wants an explicit `recursive` before it takes a non-empty subtree.
      if (!inArtifactScope(tab, msg.path)) {
        result = { error: "path outside this artifact's folder" };
      } else {
        const r = await fetch("/api/artifact/delete", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ artifact: tab.path, path: msg.path,
                                 recursive: !!msg.recursive }),
        });
        result = await r.json();
        if (result.ok) loadTree(true);
      }
    } else if (msg.type === "kb-fetch") {
      // Network for artifacts — via the hub's egress proxy only. The hub
      // enforces the admin allowlist for THIS artifact (path taken from the
      // tab, never from the iframe) and injects `secret:` refs server-side.
      const r = await fetch("/egress", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ artifact: tab.path, url: msg.url, method: msg.method,
                               headers: msg.headers, body: msg.body, json: msg.json,
                               form: msg.form }),
      });
      result = await r.json().catch(() => ({ error: "egress failed" }));
    } else if (msg.type === "kb-upload") {
      // Binary into the artifact's own _files/ (as the viewer, kernel-checked).
      if (typeof msg.name !== "string" || typeof msg.b64 !== "string" || !dirName(tab.path)) {
        result = { error: "kb-upload needs {name, b64}" };
      } else {
        const bin = Uint8Array.from(atob(msg.b64), (c) => c.charCodeAt(0));
        const fd = new FormData();
        fd.append("file", new Blob([bin]), msg.name.replace(/[/\\]/g, "_"));
        const r = await fetch("/api/upload?dir=" + encodeURIComponent(dirName(tab.path)),
                              { method: "POST", body: fd });
        result = await r.json().catch(() => ({ error: "upload failed" }));
      }
    } else if (msg.type === "kb-save-as") {
      // The artifact suggests, the USER chooses the destination in trusted
      // chrome — that's why writes outside the artifact's folder are ok here.
      const path = await kbPrompt("Save to:", msg.suggested || "company/untitled.md",
                                  { title: "Save from artifact", ok: "Save" });
      if (!path) {
        result = { cancelled: true };
      } else {
        const rel = path.trim().replace(/^\/+/, "");
        const cr = await fetch("/api/file", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ path: rel }),
        });
        if (!cr.ok && cr.status !== 409) {
          result = await cr.json().catch(() => ({ error: "could not create" }));
        } else if (cr.status === 409 &&
                   !(await kbConfirm(`"${rel}" exists — overwrite it?`,
                                     { title: "Overwrite", ok: "Overwrite", danger: true }))) {
          result = { cancelled: true };
        } else {
          const wr = await fetch("/api/artifact/write", {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ path: rel, content: String(msg.content || "") }),
          });
          const wj = await wr.json().catch(() => ({}));
          result = wr.ok && !wj.error ? { ok: true, path: rel } : { error: wj.error || "could not save" };
          if (result.ok) { kbToast("Saved to " + rel, "ok"); loadTree(true); }
        }
      }
    } else if (msg.type === "kb-clipboard") {
      try {
        await navigator.clipboard.writeText(String(msg.text || ""));
        result = { ok: true };
        kbToast("Copied to clipboard", "ok");
      } catch (e) { result = { error: "clipboard unavailable" }; }
    } else {
      return;
    }
  } catch (e) { result = { error: String(e) }; }
  if (tab.frame && tab.frame.contentWindow) {
    tab.frame.contentWindow.postMessage({ type: "kb-result", id: msg.id, ...result }, "*");
  }
});

// ---- launcher buttons ------------------------------------------------------
// One-tap shortcuts in the bar under the topbar: open a file/artifact, or open
// a terminal that runs a command (as you — it is literally typed into your
// shell). Two scopes: company-wide buttons (blue, written by admins via the
// hub) and personal buttons (violet, stored in your private users/<you>/ dir).
let launchers = { company: [], mine: [] };
let isAdmin = false;

async function loadLaunchers() {
  try { launchers = await (await fetch("/api/launchers")).json(); }
  catch (e) { return; }
  renderLaunchbar();
}

function launcherIcon(b) {
  if (b.kind === "term") return "$";
  if (b.kind === "folder") return I.folder;
  return b.target.endsWith(".html") ? I.artifact : b.target.endsWith(".md") ? I.doc : I.file;
}

function runLauncher(b) {
  if (b.kind === "term") { openTermWith(b.target); return; }
  if (b.kind === "folder") { closeNav(); revealFolder(b.target.replace(/^\/+/, "")); return; }
  const p = b.target.replace(/^\/+/, "");
  if (isSecretPath(p)) openPath(p, "secret");
  else if (p.endsWith(".html")) openPath(p, "artifact");
  else if (p.endsWith(".md")) openPath(p, "doc");
  else window.open("/api/attachment?path=" + encodeURIComponent(p), "_blank");
}

function launcherChip(b, cls) {
  const chip = document.createElement("button");
  chip.className = "lchip " + cls;
  chip.title = (cls === "company" ? "Company · " : "Yours · ") +
               (b.kind === "term" ? "runs: " + b.target : "opens: " + b.target);
  const ic = document.createElement("span");
  ic.className = "lchip-ic"; ic.innerHTML = launcherIcon(b);
  const lb = document.createElement("span");
  lb.className = "lchip-label"; lb.textContent = b.label;
  chip.append(ic, lb);
  return chip;
}

// The Pinned section: the ways in you chose, as rows in the same list as
// the chats and the files. A pin is made by right-clicking what you want
// (a file, a folder) — the dialog behind ＋ is for editing them in bulk and
// for the company-wide ones.
function renderLaunchbar() {
  const host = $("#pins");
  if (!host) return;
  host.textContent = "";
  const rows = [];
  for (const [list, scope] of [[launchers.company || [], "company"], [launchers.mine || [], "mine"]]) {
    for (const b of list) {
      if (b.kind === "term" && !canShell) continue;   // viewers have no shell
      rows.push(pinRow(b, scope));
    }
  }
  // nothing pinned, nothing on screen: the section's title goes too. The way
  // in is the tree's right-click, or "Pinned items" in the user menu.
  const title = $("#pins-title");
  if (title) title.hidden = !rows.length;
  host.hidden = !rows.length;
  for (const r of rows) host.appendChild(r);
}

function pinRow(b, scope) {
  const row = document.createElement("div");
  row.className = "pin-row " + scope;
  row.tabIndex = 0; row.setAttribute("role", "button");
  row.dataset.target = b.target; row.dataset.kind = b.kind;
  row.title = b.label + " · " + (b.kind === "term" ? b.target : b.kind === "folder" ? "folder " + b.target : b.target)
    + (scope === "company" ? " · pinned for everyone" : "");
  const ic = document.createElement("span");
  ic.className = "pin-row-ic"; ic.innerHTML = launcherIcon(b);
  const lb = document.createElement("span");
  lb.className = "pin-row-label"; lb.textContent = b.label;
  row.append(ic, lb);
  const more = document.createElement("button");
  more.type = "button"; more.className = "tbtn pin-row-more"; more.textContent = "⋯";
  more.title = "Rename, unpin"; more.setAttribute("aria-label", "Pinned item actions");
  more.addEventListener("click", (e) => { e.stopPropagation(); const r = more.getBoundingClientRect(); openPinMenu(b, scope, r.right, r.bottom + 4); });
  row.append(more);
  row.addEventListener("click", () => runLauncher(b));
  row.addEventListener("keydown", (e) => {
    if (e.target !== row) return;                       // the ⋯ inside answers for itself
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); runLauncher(b); }
  });
  row.addEventListener("contextmenu", (e) => { e.preventDefault(); openPinMenu(b, scope, e.clientX, e.clientY); });
  return row;
}

function openPinMenu(b, scope, x, y) {
  const mine = scope === "mine";
  const editable = mine || isAdmin;
  const ic = b.kind === "term" ? I.open : launcherIcon(b);
  const items = [{ icon: ic, label: b.kind === "term" ? "Run in a terminal" : "Open", fn: () => runLauncher(b) }];
  if (editable) {
    items.push({ icon: I.pencil, label: "Rename…", fn: () => renamePin(b, scope) });
    if (isAdmin)
      items.push(mine
        ? { icon: I.share, label: "Pin for everyone", fn: () => movePin(b, "company") }
        : { icon: I.lock, label: "Keep it just for me", fn: () => movePin(b, "mine") });
    items.push("-");
    items.push({ icon: I.pin, label: mine ? "Unpin" : "Unpin for everyone", danger: !mine,
                 fn: () => unpin(b, scope) });
  } else {
    items.push({ note: "Pinned for everyone by an admin" });
  }
  items.push("-");
  items.push({ icon: I.plus, label: "Manage pinned items…", fn: showLaunchersModal });
  openCtxMenu(items, x, y);
}

// ---- making and unmaking pins, from anywhere -------------------------------
async function savePins(scope, buttons) {
  const url = scope === "company" ? "/admin/launchers" : "/api/launchers";
  try {
    const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" },
                                 body: JSON.stringify({ buttons }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { kbToast(j.error || "could not save the pinned items", "err"); return false; }
  } catch (e) { kbToast("could not save the pinned items", "err"); return false; }
  await loadLaunchers();
  return true;
}

const pinnedTargets = () => new Set([...(launchers.company || []), ...(launchers.mine || [])].map((b) => b.target));

async function pinPath(path, isDir) {
  const label = baseName(path) || path;
  const mine = (launchers.mine || []).slice();
  if (mine.some((b) => b.target === path)) { kbToast("Already pinned", "ok"); return; }
  mine.push({ label: label.slice(0, 48), kind: isDir ? "folder" : "file", target: path });
  if (await savePins("mine", mine)) kbToast("Pinned “" + label + "”", "ok");
}

// unpin whatever is pinned at this path, in whichever list it lives (the
// company's needs an admin; the server says no otherwise)
async function unpinPath(path) {
  for (const scope of ["mine", "company"]) {
    const list = (scope === "company" ? launchers.company : launchers.mine) || [];
    if (list.some((b) => b.target === path)) {
      if (scope === "company" && !isAdmin) { kbToast("An admin pinned this for everyone", "err"); return; }
      const gone = list.find((b) => b.target === path);
      if (await savePins(scope, list.filter((b) => b.target !== path)))
        kbToast("Unpinned “" + gone.label + "”", "ok");
      return;
    }
  }
}

// Promote a pin to the whole company, or take it back to yourself. Written
// in the order that cannot lose it: the destination first, the source only
// once that write came back. A duplicate for one render beats a pin nobody
// has any more.
async function movePin(b, to) {
  const from = to === "company" ? "mine" : "company";
  const same = (x) => x.target === b.target && x.kind === b.kind && x.label === b.label;
  // Both lists are whole-list writes, and the company one is shared: compute
  // from what the server has NOW, not from a copy this tab may have been
  // holding for an hour, or the move quietly restores someone's old list.
  await loadLaunchers();
  const dest = ((to === "company" ? launchers.company : launchers.mine) || []).slice();
  const src = ((from === "company" ? launchers.company : launchers.mine) || []).filter((x) => !same(x));
  if (!dest.some(same)) dest.push({ label: b.label, kind: b.kind, target: b.target });
  if (!(await savePins(to, dest))) return;
  await savePins(from, src);
  kbToast(to === "company" ? "“" + b.label + "” is pinned for everyone"
                           : "“" + b.label + "” is pinned for you only", "ok");
}

async function unpin(b, scope) {
  const list = (scope === "company" ? launchers.company : launchers.mine) || [];
  await savePins(scope, list.filter((x) => !(x.target === b.target && x.label === b.label && x.kind === b.kind)));
}

async function renamePin(b, scope) {
  const name = await kbPrompt("Name this pinned item", b.label, { title: "Rename", ok: "Rename" });
  if (name === null || name === undefined) return;
  const clean = String(name).trim().slice(0, 48);
  if (!clean) return;
  const list = ((scope === "company" ? launchers.company : launchers.mine) || [])
    .map((x) => (x.target === b.target && x.label === b.label && x.kind === b.kind ? { ...x, label: clean } : x));
  await savePins(scope, list);
}

function showLaunchersModal() {
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div");
  card.className = "modal-card launcher-card";
  ov.appendChild(card);
  document.body.appendChild(ov);

  const save = async (scope, buttons) => {
    const url = scope === "company" ? "/admin/launchers" : "/api/launchers";
    const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" },
                                 body: JSON.stringify({ buttons }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { kbToast(j.error || "could not save the pinned items", "err"); return false; }
    await loadLaunchers();
    return true;
  };

  function section(title, scope, buttons, editable, hint) {
    const wrap = document.createElement("div");
    const h = document.createElement("div");
    h.className = "admin-sec-title"; h.textContent = title;
    wrap.appendChild(h);
    if (hint) {
      const p = document.createElement("div");
      p.className = "muted lnch-note"; p.textContent = hint;
      wrap.appendChild(p);
    }
    const list = document.createElement("div");
    list.className = "lchip-list";
    if (!buttons.length) {
      const none = document.createElement("span");
      none.className = "muted"; none.textContent = "nothing pinned yet";
      list.appendChild(none);
    }
    buttons.forEach((b, i) => {
      const chip = launcherChip(b, scope === "company" ? "company" : "mine");
      chip.classList.add("static");
      chip.type = "button";
      if (editable) {
        const x = document.createElement("span");
        x.className = "lx"; x.title = "Remove"; x.textContent = "×";
        x.addEventListener("click", async () => {
          if (await save(scope, buttons.filter((_, j) => j !== i))) render();
        });
        chip.appendChild(x);
      }
      list.appendChild(chip);
    });
    wrap.appendChild(list);
    if (editable) {
      const form = document.createElement("div");
      form.className = "admin-form lnch-form";
      form.innerHTML = `
        <input data-testid="lnch-label-${scope}" placeholder="label" maxlength="48">
        <select data-testid="lnch-kind-${scope}">
          <option value="file">opens a file / artifact</option>
          <option value="folder">reveals a folder in the tree</option>
          ${canShell ? '<option value="term">runs a command in a terminal</option>' : ""}
        </select>
        <input data-testid="lnch-target-${scope}" class="lnch-target" placeholder="path (e.g. company/todos.html)">
        <button data-testid="lnch-browse-${scope}" class="mini lnch-browse" title="Choose from the tree">Browse…</button>
        <button data-testid="lnch-add-${scope}" class="primary">Pin it</button>`;
      const kind = form.querySelector("select");
      const target = form.querySelector(".lnch-target");
      const browse = form.querySelector(".lnch-browse");
      const syncKind = () => {
        const term = kind.value === "term";
        target.placeholder = term ? "command (e.g. claude)" : "path (e.g. company/todos.html)";
        browse.hidden = term;                       // there is nothing to browse for a command
      };
      kind.addEventListener("change", syncKind);
      syncKind();
      browse.addEventListener("click", async () => {
        const p = await pickPath({ kind: kind.value === "folder" ? "dir" : "file" });
        if (p === null) return;
        target.value = p;
        const label = form.querySelector("input");
        if (!label.value.trim()) label.value = (baseName(p) || p).slice(0, 48);
        target.focus();
      });
      // by class, not position: the Browse button comes first in the row now
      form.querySelector("button.primary").addEventListener("click", async () => {
        const label = form.querySelector("input").value.trim();
        const tgt = target.value.trim();
        if (!label || !tgt) { kbToast("label and target are both required", "err"); return; }
        if (await save(scope, buttons.concat([{ label, kind: kind.value, target: tgt }]))) render();
      });
      wrap.appendChild(form);
    }
    return wrap;
  }

  function render() {
    card.innerHTML = `
      <div class="modal-head"><b>Pinned items</b>
        <span class="muted">one click opens a file, reveals a folder, or runs a command</span>
        <button class="modal-x" title="Close">×</button></div>`;
    card.appendChild(section("Company — everyone sees these", "company",
      launchers.company || [], isAdmin,
      isAdmin ? "" : "Set by admins. Ask one to pin something for the whole company."));
    card.appendChild(section("Yours — only you see these", "mine",
      launchers.mine || [], true,
      "Right-click anything in the tree to pin it. A command pin types itself into a fresh shell running as you."));
    const foot = document.createElement("div");
    foot.className = "modal-foot";
    const close = document.createElement("button");
    close.textContent = "Close";
    close.addEventListener("click", () => ov.remove());
    foot.appendChild(close);
    card.appendChild(foot);
    card.querySelector(".modal-x").addEventListener("click", () => ov.remove());
  }
  render();
}

// ---- settings dialog — generated from the registry the backend serves ------
// One row per setting: the control comes from its type, the pill says which
// layer the value came from, × clears that layer. Admins get a Company tab that
// edits the shared layer with the same rows. Nothing here knows any setting by
// name: adding one is a registry entry on the server, and the row appears.
const SET_SOURCE_LABEL = { default: "default", company: "company default", user: "your setting" };
const _openMaps = new Set();   // which "Customize…" panels are open, across re-renders

function openSettings() {
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.setAttribute("data-testid", "settings");
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
  const card = document.createElement("div");
  card.className = "modal-card settings-card";
  card.setAttribute("data-testid", "settings-card");
  ov.appendChild(card);
  document.body.appendChild(ov);
  let tab = "yours";
  // Escape closes any .modal-overlay from outside (closeTopModal), so the
  // subscription lets go by itself once the card is off the page.
  const unsub = settings.subscribe("*", () => { if (ov.isConnected) render(); else unsub(); });
  const close = () => { unsub(); ov.remove(); };
  const tid = (key) => key.replace(/\./g, "-");
  const fail = (r) => { if (r && !r.ok) kbToast(r.error || "could not save the setting", "err"); };

  // The control for one entry. `onChange` receives a typed value — or undefined
  // for "not set", which only the company tab offers.
  function control(e, value, onChange, unsettable) {
    let el;
    const notSet = value === undefined;
    if (e.type === "bool" && unsettable) {
      el = document.createElement("select");
      el.innerHTML = '<option value="">— not set —</option><option value="true">on</option><option value="false">off</option>';
      el.value = notSet ? "" : String(value);
      el.addEventListener("change", () => onChange(el.value === "" ? undefined : el.value === "true"));
    } else if (e.type === "bool") {
      el = document.createElement("input"); el.type = "checkbox"; el.checked = !!value;
      el.addEventListener("change", () => onChange(el.checked));
    } else if (e.type === "enum") {
      el = document.createElement("select");
      if (unsettable) el.appendChild(new Option("— not set —", ""));
      for (const o of e.options) el.appendChild(new Option((e.labels && e.labels[o]) || o, o));
      el.value = notSet ? "" : value;
      el.addEventListener("change", () => onChange(el.value === "" ? undefined : el.value));
    } else if (e.type === "map") {
      // one colour picker per token, starting from what the theme paints now;
      // a change saves the whole map, × on a token drops it, an empty map unsets
      el = document.createElement("div"); el.className = "set-map";
      const cur = value && typeof value === "object" ? value : {};
      const n = Object.keys(cur).length;
      const toggle = document.createElement("button");
      toggle.type = "button"; toggle.className = "mini";
      toggle.textContent = n ? `Customize… (${n})` : "Customize…";
      toggle.setAttribute("data-testid", `set-${tid(e.key)}-${unsettable ? "co" : "input"}`);
      const panelId = `${e.key}:${unsettable ? "company" : "user"}`;
      const panel = document.createElement("div"); panel.className = "set-map-panel";
      panel.hidden = !_openMaps.has(panelId);
      const cs = getComputedStyle(document.documentElement);
      const save = (m) => onChange(Object.keys(m).length ? m : undefined);
      const keys = Array.isArray(e.keys) ? e.keys : Object.keys(e.keys || {});
      const patOf = (k) => (Array.isArray(e.keys) ? e.pattern : e.keys[k]) || "";
      for (const k of keys) {
        const name = document.createElement("span"); name.textContent = k;
        const painted = cs.getPropertyValue("--" + k).trim();
        const isColour = patOf(k) === "#[0-9a-f]{6}";
        const inp = document.createElement("input");
        if (isColour) {
          inp.type = "color";
          inp.value = cur[k] || (/^#[0-9a-f]{6}$/i.test(painted) ? painted.toLowerCase() : "#000000");
          inp.addEventListener("change", () => save({ ...cur, [k]: inp.value.toLowerCase() }));
        } else {
          inp.type = "text"; inp.className = "set-map-text";
          inp.value = cur[k] || ""; inp.placeholder = painted;
          inp.addEventListener("change", () => {
            const v = inp.value.trim();
            if (v === "") { const m = { ...cur }; delete m[k]; save(m); return; }
            if (!new RegExp("^(?:" + patOf(k) + ")$").test(v)) { kbToast(`${k}: not a valid value`, "err"); inp.value = cur[k] || ""; return; }
            save({ ...cur, [k]: v });
          });
        }
        inp.setAttribute("data-testid", `set-${tid(e.key)}-${k}${unsettable ? "-co" : ""}`);
        const clr = document.createElement("button");
        clr.type = "button"; clr.className = "mini set-map-x"; clr.textContent = "×";
        clr.title = "Back to the theme's colour"; clr.style.visibility = k in cur ? "visible" : "hidden";
        clr.addEventListener("click", () => { const m = { ...cur }; delete m[k]; save(m); });
        panel.append(name, inp, clr);
      }
      toggle.addEventListener("click", () => {
        if (_openMaps.has(panelId)) _openMaps.delete(panelId); else _openMaps.add(panelId);
        panel.hidden = !_openMaps.has(panelId);
      });
      el.append(toggle, panel);
    } else if (e.type === "image") {
      // a file, not a value: the control uploads it (admin, company layer)
      el = document.createElement("label");
      el.className = "set-upload";
      el.innerHTML = '<span class="mini">Upload…</span><input type="file" accept=".svg,.png,image/svg+xml,image/png" hidden>';
      const input = el.querySelector("input");
      input.setAttribute("data-testid", `set-${tid(e.key)}-file`);
      input.addEventListener("change", async () => {
        if (!input.files.length) return;
        const fd = new FormData(); fd.append("file", input.files[0]);
        const r = await fetch("/admin/brand/logo", { method: "POST", body: fd });
        const j = await r.json().catch(() => ({}));
        input.value = "";
        if (!r.ok) { kbToast(j.error || "could not upload the logo", "err"); return; }
        await settings.fetch();
      });
      const cur = document.createElement("span");
      cur.className = "set-help";
      cur.textContent = notSet || value === "" ? "the built-in mark" : "custom (" + value + ")";
      el.appendChild(cur);
    } else if (e.type === "int") {
      el = document.createElement("input"); el.type = "number";
      el.min = e.min; el.max = e.max; el.step = 1;
      el.value = notSet ? "" : value;
      el.placeholder = unsettable ? "not set" : "";
      el.addEventListener("change", () => {
        if (el.value === "" && unsettable) { onChange(undefined); return; }
        const n = Math.round(Number(el.value));
        if (!Number.isFinite(n) || n < e.min || n > e.max) {
          kbToast(`${e.label}: ${e.min} to ${e.max}`, "err"); render(); return;
        }
        onChange(n);
      });
    } else {
      el = document.createElement("input"); el.type = "text";
      if (e.maxlen) el.maxLength = e.maxlen;
      el.value = notSet ? "" : value;
      el.placeholder = unsettable ? "not set" : "";
      el.addEventListener("change", () => {
        const v = el.value.trim();
        if (v === "" && unsettable) { onChange(undefined); return; }
        if (e.pattern && !new RegExp("^(?:" + e.pattern + ")$").test(v)) {
          kbToast(`${e.label}: not a valid value`, "err"); render(); return;
        }
        onChange(v);
      });
    }
    el.setAttribute("data-testid", `set-${tid(e.key)}-${unsettable ? "co" : "input"}`);
    return el;
  }

  function row(e, scope) {
    const st = settings.state();
    const r = document.createElement("div");
    r.className = "set-row";
    r.setAttribute("data-testid", `set-${tid(e.key)}${scope === "company" ? "-company" : ""}`);
    const lab = document.createElement("div");
    lab.innerHTML = '<div class="set-label"></div><span class="set-help"></span>';
    lab.querySelector(".set-label").textContent = e.label;
    lab.querySelector(".set-help").textContent = e.help || "";
    r.appendChild(lab);
    const pill = document.createElement("span");
    const x = document.createElement("button");
    x.className = "mini set-reset"; x.type = "button"; x.textContent = "×";
    if (scope === "company") {
      const cur = st.company.values[e.key];
      r.appendChild(control(e, cur, (v) => (v === undefined
        ? settings.unset(e.key, "company") : settings.set(e.key, v, "company")).then(fail), true));
      pill.className = "set-src" + (cur === undefined ? "" : " company");
      pill.setAttribute("data-testid", `set-${tid(e.key)}-co-src`);
      pill.textContent = cur === undefined ? "not set" : "company default";
      x.title = "Clear the company default";
      x.setAttribute("data-testid", `set-${tid(e.key)}-co-reset`);
      x.addEventListener("click", () => {
        if (e.type === "image") {
          fetch("/admin/brand/logo", { method: "POST", headers: { "content-type": "application/json" },
                                       body: JSON.stringify({ reset: true }) })
            .then(async (r) => { if (!r.ok) fail({ ok: false, error: (await r.json().catch(() => ({}))).error });
                                 else settings.fetch(); });
          return;
        }
        settings.unset(e.key, "company").then(fail);
      });
      if (cur !== undefined) r.classList.add("resettable");
    } else {
      const src = settings.source(e.key) || "default";
      r.appendChild(control(e, settings.get(e.key), (v) => (v === undefined
        ? settings.unset(e.key, "user") : settings.set(e.key, v, "user")).then(fail), false));
      pill.className = "set-src " + src;
      pill.setAttribute("data-testid", `set-${tid(e.key)}-src`);
      pill.textContent = SET_SOURCE_LABEL[src] || src;
      x.title = "Back to " + (st.company.values[e.key] !== undefined ? "the company default" : "the default");
      x.setAttribute("data-testid", `set-${tid(e.key)}-reset`);
      x.addEventListener("click", () => settings.unset(e.key, "user").then(fail));
      if (src === "user") r.classList.add("resettable");
    }
    r.appendChild(pill);
    r.appendChild(x);
    return r;
  }

  function render() {
    const st = settings.state();
    card.innerHTML = `
      <div class="modal-head"><b>Settings</b>
        <span class="muted">yours override the company's, the company's override the defaults</span>
        <button class="modal-x" title="Close">×</button></div>`;
    card.querySelector(".modal-x").addEventListener("click", close);
    if (!st) {
      const p = document.createElement("div");
      p.className = "muted";
      p.textContent = "Settings are not available: the backend predates them. Reload once it has been updated.";
      card.appendChild(p);
    } else {
      if (isAdmin) {
        const tabs = document.createElement("div");
        tabs.className = "settings-tabs";
        for (const [id, label] of [["yours", "Yours"], ["company", "Company"]]) {
          const b = document.createElement("button");
          b.type = "button"; b.textContent = label;
          b.setAttribute("data-testid", `settings-tab-${id}`);
          b.classList.toggle("active", tab === id);
          b.addEventListener("click", () => { tab = id; render(); });
          tabs.appendChild(b);
        }
        card.querySelector(".modal-head b").after(tabs);
      }
      // a hand-edited file that went wrong is reported, never silently blanked
      const problems = [];
      if (st.company.error) problems.push("company file: " + st.company.error);
      if (st.user.error) problems.push("your file: " + st.user.error);
      for (const [k, why] of Object.entries(st.company.rejected || {})) problems.push(`company file, ${k}: ${why}`);
      for (const [k, why] of Object.entries(st.user.rejected || {})) problems.push(`your file, ${k}: ${why}`);
      if (problems.length) {
        const n = document.createElement("div");
        n.className = "note"; n.setAttribute("data-testid", "settings-problems");
        n.textContent = "Ignored — " + problems.join("; ");
        card.appendChild(n);
      }
      const scope = tab === "company" ? "company" : "user";
      const entries = st.schema.filter((e) => e.scopes.includes(scope));
      const groups = [];
      for (const e of entries) if (!groups.includes(e.group)) groups.push(e.group);
      if (!entries.length) {
        const p = document.createElement("div");
        p.className = "muted"; p.textContent = "Nothing to set here yet.";
        card.appendChild(p);
      }
      for (const g of groups) {
        const h = document.createElement("div");
        h.className = "admin-sec-title"; h.textContent = g;
        card.appendChild(h);
        for (const e of entries.filter((x) => x.group === g)) card.appendChild(row(e, scope));
      }
      const note = document.createElement("div");
      note.className = "muted lnch-note";
      note.textContent = scope === "company"
        ? "Company defaults apply to everyone who has not chosen their own. Stored in .os/settings.json."
        : "Yours follow you to every device. Stored in users/" + (window.__kbuser || "you") +
          "/.os/settings.json — an agent running as you may edit it too.";
      card.appendChild(note);
    }
    const foot = document.createElement("div");
    foot.className = "modal-foot";
    const cb = document.createElement("button");
    cb.className = "modal-close"; cb.type = "button"; cb.textContent = "Close";
    cb.addEventListener("click", close);
    foot.appendChild(cb);
    card.appendChild(foot);
  }
  render();
  settings.fetch();     // the latest state, in case the tab was asleep for a while
}

// ---- admin panel (user & group management; admins only) -------------------
async function openAdmin() {
  // Admins get the full panel; users with write access to .os/egress.json
  // (the delegation) get the network-access section alone.
  let data = { users: [], groups: [], egressOnly: !isAdmin };
  if (isAdmin) {
    data = await (await fetch("/admin/list")).json();
    if (data.error) { kbToast(data.error, "err"); return; }
    data.egressOnly = false;
  }
  try { data.egress = (await (await fetch("/admin/egress")).json()).entries || {}; }
  catch (e) { data.egress = {}; }
  showAdminModal(data);
}

async function adminPost(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" },
                               body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { kbToast(j.error || "action failed", "err"); return false; }
  return true;
}

function showAdminModal(data) {
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div");
  card.className = "modal-card admin-card";

  const userRows = data.users.map((u) => `
    <tr>
      <td class="mono">${escapeHtml(u.username)}${u.admin ? ' <span class="pill">admin</span>' : ""}${u.shell === false ? ' <span class="pill viewer">viewer</span>' : ""}</td>
      <td>${escapeHtml(u.first)} ${escapeHtml(u.last)}</td>
      <td class="muted">${escapeHtml(u.email)}</td>
      <td class="mono muted">${u.groups.map(escapeHtml).join(", ")}</td>
      <td><button class="mini shell-toggle" data-u="${escapeHtml(u.username)}" data-shell="${u.shell === false ? 0 : 1}"
        title="${u.shell === false ? "Grant shell + cron (make full)" : "Revoke shell + cron (make viewer)"}">${u.shell === false ? "→ full" : "→ viewer"}</button></td>
      <td><button class="del-user" data-u="${escapeHtml(u.username)}" title="Remove user">×</button></td>
    </tr>`).join("");

  const groupBlocks = data.groups.map((g) => `
    <div class="grp">
      <div class="grp-head"><b class="mono">${escapeHtml(g.name)}</b>
        <span class="muted">${g.members.length} member(s)</span>
        <button class="del-user del-grp" data-g="${escapeHtml(g.name)}" title="Delete group">×</button></div>
      <div class="grp-members">
        ${g.members.map((m) => `<span class="chip">${escapeHtml(m)}<button class="rm-mem" data-g="${escapeHtml(g.name)}" data-u="${escapeHtml(m)}">×</button></span>`).join("") || '<span class="muted">none</span>'}
      </div>
      <div class="grp-add">
        <select class="add-user" data-g="${escapeHtml(g.name)}">
          <option value="">add member…</option>
          ${data.users.filter((u) => !g.members.includes(u.username)).map((u) => `<option>${escapeHtml(u.username)}</option>`).join("")}
        </select>
      </div>
    </div>`).join("");

  card.innerHTML = `
    <div class="modal-head"><b>${data.egressOnly ? "Network access" : "Administration"}</b>
      <span class="muted">${data.egressOnly ? "artifact egress allowlist" : "users &amp; groups"}</span>
      <button class="modal-x" title="Close">×</button></div>
    ${data.egressOnly ? "" : `
    <div class="admin-sec-title">Users</div>
    <div class="admin-scroll"><table class="admin-tbl">
      <thead><tr><th>username</th><th>name</th><th>email</th><th>groups</th><th></th><th></th></tr></thead>
      <tbody>${userRows}</tbody></table></div>
    <div class="admin-form">
      <input id="nu-username" placeholder="username" size="10">
      <input id="nu-first" placeholder="first name" size="9">
      <input id="nu-last" placeholder="last name" size="9">
      <input id="nu-email" placeholder="email" size="14">
      <input id="nu-pw" type="password" placeholder="password (12+)" size="12" minlength="12">
      <select id="nu-kind" data-testid="nu-kind" title="Full accounts get a shell and cron; viewers only use the webapp">
        <option value="full">full · shell + cron</option>
        <option value="viewer">viewer · webapp only</option>
      </select>
      <button id="nu-create" class="primary">Create user</button>
    </div>

    <div class="admin-sec-title">Groups</div>
    <div class="admin-scroll">${groupBlocks || '<span class="muted">none</span>'}</div>
    <div class="admin-form">
      <input id="ng-name" placeholder="new group name" size="16">
      <button id="ng-create">Create group</button>
    </div>`}

    <div class="admin-sec-title">Artifact network access</div>
    <div class="muted lnch-note">Artifacts are sandboxed offline. An entry here lets ONE artifact call
      the listed domains through the hub — with <span class="mono">secret:</span> refs resolved
      server-side and every call audit-logged. Editable by admins and anyone with write access to
      <span class="mono">.os/egress.json</span> (grant it via ⚙ on that file).</div>
    <div class="egress-list" data-testid="egress-tbl">${
      Object.entries(data.egress || {}).map(([a, e]) => `
        <div class="egress-item">
          <div class="egress-info">
            <div class="egress-art mono">${escapeHtml(a)}</div>
            <div class="egress-doms mono muted">→ ${(e.domains || []).map(escapeHtml).join(", ")}</div>
          </div>
          <button class="del-user del-egress" data-a="${escapeHtml(a)}" title="Revoke network access">Revoke</button>
        </div>`).join("") ||
      '<div class="muted egress-empty">No artifact has network access yet.</div>'}
    </div>
    <div class="admin-form">
      <input id="eg-artifact" data-testid="eg-artifact" placeholder="artifact path (….html)" size="26">
      <input id="eg-domains" data-testid="eg-domains" placeholder="domains, comma-separated" size="24">
      <button id="eg-set" data-testid="eg-set">Allow</button>
    </div>
    <div class="modal-foot"><button class="modal-close">Close</button></div>`;
  ov.appendChild(card);
  document.body.appendChild(ov);

  const reload = async () => { ov.remove(); openAdmin(); };
  card.querySelector(".modal-x").addEventListener("click", () => ov.remove());
  card.querySelector(".modal-close").addEventListener("click", () => ov.remove());

  if (card.querySelector("#nu-create")) card.querySelector("#nu-create").addEventListener("click", async () => {
    const body = {
      username: card.querySelector("#nu-username").value.trim(),
      first: card.querySelector("#nu-first").value.trim(),
      last: card.querySelector("#nu-last").value.trim(),
      email: card.querySelector("#nu-email").value.trim(),
      password: card.querySelector("#nu-pw").value,
      kind: card.querySelector("#nu-kind").value,
    };
    if (await adminPost("/admin/users", body)) reload();
  });
  card.querySelectorAll(".shell-toggle").forEach((b) => b.addEventListener("click", async () => {
    const toViewer = b.dataset.shell === "1";
    const msg = toViewer
      ? `Make "${b.dataset.u}" a viewer? They lose the terminal, cron and any shell login — the webapp keeps working. Their running processes are stopped.`
      : `Give "${b.dataset.u}" a full account? They gain a shell (terminal, cron, ssh).`;
    if (await kbConfirm(msg, { title: toViewer ? "Make viewer" : "Make full account",
                               ok: toViewer ? "Make viewer" : "Grant shell", danger: toViewer })) {
      if (await adminPost("/admin/users/shell", { username: b.dataset.u, shell: !toViewer })) reload();
    }
  }));
  if (card.querySelector("#ng-create")) card.querySelector("#ng-create").addEventListener("click", async () => {
    const name = card.querySelector("#ng-name").value.trim();
    if (name && await adminPost("/admin/groups", { name })) reload();
  });
  card.querySelector("#eg-set").addEventListener("click", async () => {
    const artifact = card.querySelector("#eg-artifact").value.trim();
    const domains = card.querySelector("#eg-domains").value.split(",")
      .map((s) => s.trim().toLowerCase()).filter(Boolean);
    if (artifact && await adminPost("/admin/egress", { artifact, domains })) reload();
  });
  card.querySelectorAll(".del-egress").forEach((b) => b.addEventListener("click", async () => {
    if (await kbConfirm(`Revoke network access for "${b.dataset.a}"?`,
                        { title: "Revoke network access", ok: "Revoke", danger: true })) {
      if (await adminPost("/admin/egress", { artifact: b.dataset.a, domains: [] })) reload();
    }
  }));
  card.querySelectorAll(".del-user").forEach((b) => b.addEventListener("click", async () => {
    if (await kbConfirm(`Remove user "${b.dataset.u}"? This deletes their account, home, private files, and database schema.`,
                        { title: "Remove user", ok: "Remove user", danger: true })) {
      if (await adminPost("/admin/users/delete", { username: b.dataset.u })) reload();
    }
  }));
  card.querySelectorAll(".del-grp").forEach((b) => b.addEventListener("click", async () => {
    if (await kbConfirm(`Delete group "${b.dataset.g}"? Members keep their accounts; files owned by the group keep its (then-orphaned) id.`,
                        { title: "Delete group", ok: "Delete group", danger: true })) {
      if (await adminPost("/admin/groups/delete", { name: b.dataset.g })) reload();
    }
  }));
  card.querySelectorAll(".rm-mem").forEach((b) => b.addEventListener("click", async () => {
    if (await adminPost("/admin/groups/member", { group: b.dataset.g, username: b.dataset.u, action: "remove" })) reload();
  }));
  card.querySelectorAll(".add-user").forEach((s) => s.addEventListener("change", async () => {
    if (s.value && await adminPost("/admin/groups/member", { group: s.dataset.g, username: s.value, action: "add" })) reload();
  }));
}

// ---- cron panel (the user's own crontab; every user has one) --------------
async function openCron() {
  let data;
  try { data = await (await fetch("/api/cron")).json(); }
  catch (e) { kbToast("could not load crontab", "err"); return; }
  showCronModal(data);
}

const CRON_PRESETS = [
  ["* * * * *", "every minute"],
  ["*/5 * * * *", "every 5 minutes"],
  ["0 * * * *", "every hour"],
  ["0 8 * * *", "daily at 08:00"],
  ["0 8 * * 1", "Mondays at 08:00"],
  ["@reboot", "at boot"],
];

function showCronModal(data) {
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div");
  card.className = "modal-card cron-card";
  ov.appendChild(card);
  document.body.appendChild(ov);

  const post = async (url, body) => {
    const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" },
                                 body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { card.querySelector(".cron-err").textContent = j.error || "action failed"; return null; }
    return j;
  };

  function render(d) {
    card.innerHTML = `
      <div class="modal-head"><b>Scheduled jobs</b>
        <span class="muted">cron · runs as <span class="mono">${escapeHtml(d.user || "")}</span>, even while you're logged out</span>
        <button class="modal-x" title="Close">×</button></div>
      ${d.available ? "" : '<div class="note">cron is not available on this machine.</div>'}
      <div class="admin-scroll"><table class="admin-tbl cron-tbl" data-testid="cron-jobs">
        <thead><tr><th>schedule</th><th>command</th><th></th><th></th></tr></thead>
        <tbody></tbody></table></div>
      <div class="cron-hint muted">schedule = <span class="mono">minute hour day-of-month month day-of-week</span>
        — e.g. <span class="mono">*/5 * * * *</span> is every 5 minutes. Redirect output to a log:
        <span class="mono">… &gt;&gt; ~/logs/job.log 2&gt;&amp;1</span>. In commands, escape % as
        <span class="mono">\%</span> (cron treats a bare % specially).</div>
      <div class="cron-presets">
        ${CRON_PRESETS.map(([v, l]) =>
          `<button class="chip-preset" data-v="${v}" title="${v}">${l}</button>`).join("")}
      </div>
      <div class="admin-form cron-form">
        <input id="cron-schedule" data-testid="cron-schedule" placeholder="* * * * *" size="13">
        <input id="cron-command" data-testid="cron-command" placeholder="command to run" size="34">
        <button id="cron-add" data-testid="cron-add" class="primary" ${d.available ? "" : "disabled"}>Add job</button>
      </div>
      <div class="cron-err err" data-testid="cron-err"></div>
      <div class="modal-foot"><button class="modal-close">Close</button></div>`;

    const tbody = card.querySelector("tbody");
    if (!d.jobs || !d.jobs.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = '<td colspan="4" class="muted">no scheduled jobs</td>';
      tbody.appendChild(tr);
    }
    for (const job of d.jobs || []) {
      const tr = document.createElement("tr");
      tr.className = "cron-row" + (job.paused ? " paused" : "");
      const sched = document.createElement("td");
      sched.className = "mono cron-sched"; sched.textContent = job.schedule;
      const cmd = document.createElement("td");
      cmd.className = "mono cron-cmd"; cmd.textContent = job.command; cmd.title = job.command;
      const tgl = document.createElement("td");
      const tglBtn = document.createElement("button");
      tglBtn.className = "mini"; tglBtn.textContent = job.paused ? "▶" : "⏸";
      tglBtn.title = job.paused ? "Resume" : "Pause (keeps the entry, stops it running)";
      tglBtn.addEventListener("click", async () => {
        const j = await post("/api/cron/toggle", { line: job.line, raw: job.raw });
        if (j) render(j);
      });
      tgl.appendChild(tglBtn);
      const del = document.createElement("td");
      const delBtn = document.createElement("button");
      delBtn.className = "del-user"; delBtn.textContent = "×"; delBtn.title = "Delete job";
      delBtn.addEventListener("click", async () => {
        if (!await kbConfirm("Delete this scheduled job?", { title: "Delete job", ok: "Delete", danger: true })) return;
        const j = await post("/api/cron/remove", { line: job.line, raw: job.raw });
        if (j) render(j);
      });
      del.appendChild(delBtn);
      tr.append(sched, cmd, tgl, del);
      tbody.appendChild(tr);
    }

    card.querySelector(".modal-x").addEventListener("click", () => ov.remove());
    card.querySelector(".modal-close").addEventListener("click", () => ov.remove());
    card.querySelectorAll(".chip-preset").forEach((b) => b.addEventListener("click", () => {
      card.querySelector("#cron-schedule").value = b.dataset.v;
      card.querySelector("#cron-command").focus();
    }));
    card.querySelector("#cron-add").addEventListener("click", async () => {
      const schedule = card.querySelector("#cron-schedule").value.trim();
      const command = card.querySelector("#cron-command").value.trim();
      if (!schedule || !command) {
        card.querySelector(".cron-err").textContent = "schedule and command are both required";
        return;
      }
      const j = await post("/api/cron/add", { schedule, command });
      if (j) render(j);
    });
  }
  render(data);
}

// ---- version history (per-file: versions, diff, restore) -------------------
function renderPatch(host, patch) {
  host.textContent = "";
  if (!patch.trim()) {
    host.innerHTML = '<div class="muted">no textual change in this version</div>';
    return;
  }
  for (const line of patch.split("\n")) {
    const div = document.createElement("div");
    div.className = "vh-line" +
      (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ")
        || line.startsWith("index ") ? " vh-meta"
        : line.startsWith("@@") ? " vh-hunk"
        : line.startsWith("+") ? " vh-add"
        : line.startsWith("-") ? " vh-del" : "");
    div.textContent = line || " ";
    host.appendChild(div);
  }
}

async function openHistory(path) {
  let data;
  try {
    const r = await fetch("/api/vc/log?path=" + encodeURIComponent(path));
    data = await r.json();
    if (!r.ok) { kbToast(data.error || "history unavailable", "err"); return; }
  } catch (e) { kbToast("history unavailable", "err"); return; }

  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div");
  card.className = "modal-card vh-card";
  card.innerHTML = `
    <div class="modal-head"><b>Version history</b>
      <span class="muted mono">${escapeHtml(path)}</span>
      <button class="modal-x" title="Close">×</button></div>
    <div class="vh-body">
      <div class="vh-list" data-testid="vh-list"></div>
      <div class="vh-detail">
        <div class="vh-diff" data-testid="vh-diff"><div class="muted">Pick a version on the left — every save is one entry, with who made it.</div></div>
        <div class="vh-actions">
          <button class="vh-restore primary" data-testid="vh-restore" hidden>Restore this version</button>
        </div>
      </div>
    </div>
    <div class="modal-foot"><button class="modal-close">Close</button></div>`;
  ov.appendChild(card);
  document.body.appendChild(ov);
  card.querySelector(".modal-x").addEventListener("click", () => ov.remove());
  card.querySelector(".modal-close").addEventListener("click", () => ov.remove());

  const list = card.querySelector(".vh-list");
  const diffHost = card.querySelector(".vh-diff");
  const restoreBtn = card.querySelector(".vh-restore");
  let picked = null;

  if (!data.entries.length) {
    list.innerHTML = '<div class="muted vh-none">No versions yet — history starts with the next edit.</div>';
  }
  data.entries.forEach((e, i) => {
    const row = document.createElement("button");
    row.className = "vh-item";
    row.setAttribute("data-testid", "vh-item");
    const when = new Date(e.ts * 1000);
    row.innerHTML = `<span class="vh-when">${when.toLocaleDateString()} ${when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
      <span class="vh-author"></span><span class="vh-cur">${i === 0 ? "current" : ""}</span>`;
    row.querySelector(".vh-author").textContent = e.author;
    row.title = e.subject;
    row.addEventListener("click", async () => {
      list.querySelectorAll(".vh-item").forEach((x) => x.classList.remove("active"));
      row.classList.add("active");
      picked = e;
      diffHost.innerHTML = '<div class="muted">loading…</div>';
      try {
        const rr = await fetch("/api/vc/diff?path=" + encodeURIComponent(path) + "&rev=" + e.rev);
        const dj = await rr.json();
        if (!rr.ok) { diffHost.innerHTML = ""; kbToast(dj.error || "could not load diff", "err"); return; }
        renderPatch(diffHost, dj.patch || "");
      } catch (err) { diffHost.innerHTML = '<div class="muted">could not load the change</div>'; }
      restoreBtn.hidden = i === 0;   // restoring "current" is a no-op
    });
    list.appendChild(row);
  });

  restoreBtn.addEventListener("click", async () => {
    if (!picked) return;
    if (!await kbConfirm(
      `Restore "${baseName(path)}" to the version from ${new Date(picked.ts * 1000).toLocaleString()} (by ${picked.author})? ` +
      "The current content stays in history, so this is safe to undo.",
      { title: "Restore version", ok: "Restore" })) return;
    try {
      const sr = await fetch("/api/vc/show?path=" + encodeURIComponent(path) + "&rev=" + picked.rev);
      const sj = await sr.json();
      if (!sr.ok) { kbToast(sj.error || "could not load that version", "err"); return; }
      // the write happens AS YOU through the normal as-user path — so the
      // restore is itself an attributed edit, and the live doc merges it in
      const wr = await fetch("/api/artifact/write", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ path, content: sj.content }),
      });
      const wj = await wr.json().catch(() => ({}));
      if (!wr.ok || wj.error) { kbToast(wj.error || "could not restore", "err"); return; }
      kbToast("Restored — the editor catches up in a few seconds", "ok");
      ov.remove();
    } catch (e) { kbToast("could not restore", "err"); }
  });
}

// ---- sharing: who can open this -------------------------------------------
// The panel speaks PEOPLE. /fs/share turns that into an owning group, mode bits
// and POSIX ACLs — hub.py picks the mechanism per object so that the everyday
// action, adding or removing one person, stays a gpasswd and never a walk over
// every file. The octal editor this replaced is still here, one disclosure
// down: "advanced" should mean tucked away, not taken away.

const SH_SCOPES = [
  ["inherit", "Same as the folder it's in"],
  ["everyone", "Everyone at Ollsoft"],
  ["people", "Specific people"],
  ["private", "Only me"],
];

function shInitials(s) {
  const p = String(s || "?").trim().split(/[\s_.-]+/).filter(Boolean);
  return ((p[0] || "?")[0] + (p[1] ? p[1][0] : "")).toUpperCase();
}

function shWho(a) {
  if (!a) return "someone";
  if (a.scope === "everyone") return "everyone at Ollsoft";
  if (a.scope === "private") return "its owner only";
  return (a.group || "its group") + (a.people ? ` (${a.people} ${a.people === 1 ? "person" : "people"})` : "");
}

// ---- a link for someone with no account --------------------------------
// The platform mounts the file or folder in front of a separate container
// (see docs/public-sharing.md); this is only the panel that asks for one.
// The URL exists exactly once, in the answer to the request that made it.
async function wirePublicShare(host, s) {
  if (!host) return;
  const secret = !!s.secret;
  // `share.url` from the server is already whole when the box knows its own
  // public address (KB_SHARE_BASE), and a bare path when it does not — either
  // way it is the URL, not a piece of one. Prefixing it produced
  // "https://share.example.orghttps://share.example.org/s/…" the moment the
  // address was configured (2026-09-22).
  const paint = (shares) => {
    host.textContent = "";
    if (secret) {
      host.append(el2("div", "muted", "A secret is never put on the internet."));
      return;
    }
    const mine = (shares || []).filter((x) => x.path === s.path);
    for (const sh of mine) {
      const row = el2("div", "sh-public-row");
      row.setAttribute("data-testid", "sh-public-row");
      const when = sh.expires ? "until " + new Date(sh.expires * 1000).toLocaleDateString() : "no end date";
      row.append(el2("span", "sh-public-what",
                     (sh.mode === "edit" ? "Anyone with the link can edit" : "Anyone with the link can read")
                     + (sh.password ? ", with the password" : "")),
                 el2("span", "sh-public-when", when));
      const off = el2("button", "mini", "Stop sharing"); off.type = "button";
      off.setAttribute("data-testid", "sh-public-off");
      off.addEventListener("click", async () => {
        const r = await fetch("/fs/public/revoke", { method: "POST", headers: { "content-type": "application/json" },
                                                     body: JSON.stringify({ id: sh.id }) });
        if (!r.ok) { kbToast("could not stop that link", "err"); return; }
        kbToast("The link no longer works", "ok");
        load();
      });
      row.append(off);
      host.append(row);
    }
    if (mine.length) return;
    // …no link yet: the form that makes one
    const form = el2("div", "sh-public-form");
    const mode = document.createElement("select");
    mode.setAttribute("data-testid", "sh-public-mode");
    mode.innerHTML = '<option value="view">can read it</option><option value="edit">can read and edit it</option>';
    const days = document.createElement("input");
    days.type = "number"; days.min = "1"; days.max = "90"; days.value = "14";
    days.className = "sh-public-days"; days.setAttribute("data-testid", "sh-public-days");
    days.title = "Days until the link stops working";
    const pw = document.createElement("input");
    pw.type = "password"; pw.placeholder = "password (optional)"; pw.autocomplete = "new-password";
    pw.setAttribute("data-testid", "sh-public-pw");
    const go = el2("button", "mini", "Create a link"); go.type = "button";
    go.setAttribute("data-testid", "sh-public-create");
    go.addEventListener("click", async () => {
      go.disabled = true;
      let j = {};
      try {
        const r = await fetch("/fs/public", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ path: s.path, mode: mode.value, days: Number(days.value) || 14,
                                 password: pw.value }) });
        j = await r.json().catch(() => ({}));
        if (!r.ok) { kbToast(j.error || "could not create the link", "err"); go.disabled = false; return; }
      } catch (e) { kbToast("could not reach the server", "err"); go.disabled = false; return; }
      showLinkOnce(host, j.share.url, load);
    });
    form.append(el2("span", "muted", "Anyone with the link"), mode, days,
                el2("span", "muted", "days"), pw, go);
    host.append(form);
  };
  const load = async () => {
    try {
      const r = await fetch("/fs/public");
      const j = r.ok ? await r.json() : { shares: [] };
      paint(j.shares);
    } catch (e) { paint([]); }
  };
  load();
}

// The token is in the URL and nowhere else: shown once, with a copy button,
// and a plain warning that it will not be shown again.
function showLinkOnce(host, url, done) {
  host.textContent = "";
  const box = el2("div", "sh-public-new");
  const field = document.createElement("input");
  field.readOnly = true; field.value = url; field.className = "sh-public-url";
  field.setAttribute("data-testid", "sh-public-url");
  field.addEventListener("focus", () => field.select());
  const copy = el2("button", "mini", "Copy"); copy.type = "button";
  copy.addEventListener("click", () => navigator.clipboard.writeText(url)
    .then(() => kbToast("Link copied", "ok"), () => kbToast("Clipboard blocked", "err")));
  const ok = el2("button", "mini", "Done"); ok.type = "button";
  ok.addEventListener("click", done);
  box.append(el2("div", "muted", "Copy it now — this is the only time it is shown."),
             field, el2("div", "sh-public-acts", ""));
  box.querySelector(".sh-public-acts").append(copy, ok);
  host.append(box);
  field.focus();
}

async function openPerms(path) {
  const [r, pr] = await Promise.all([
    fetch("/fs/share?path=" + encodeURIComponent(path)),
    fetch("/api/principals").catch(() => null),
  ]);
  const s = await r.json();
  if (!r.ok) { kbToast(s.error || "cannot read who has access", "err"); return; }
  let principals = { users: [], groups: [] };
  try { if (pr && pr.ok) principals = await pr.json(); } catch (e) { /* usernames only */ }
  showShareModal(s, principals);
}

function showShareModal(s, principals) {
  const ro = !s.can_edit;
  const owner = s.people.find((p) => p.role === "owner") || { user: s.owner, name: s.owner_name };
  let people = s.people.filter((p) => p.role !== "owner")
    .map((p) => ({ user: p.user, name: p.name, role: p.role, via: p.via, source: p.source }));
  // A thing that merely follows its folder opens on "same as the folder", so
  // pressing Save without touching anything cannot quietly cut it loose into a
  // per-item access list that stops tracking the folder.
  let scope = (s.inherited && s.parent) ? "inherit" : s.scope;

  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div");
  card.className = "modal-card";
  const scopeOpts = SH_SCOPES.filter(([v]) => v !== "inherit" || s.parent)
    .map(([v, label]) => `<option value="${v}"${v === scope ? " selected" : ""}>${escapeHtml(label)}</option>`)
    .join("");

  card.innerHTML = `
    <div class="modal-head">
      <b>${escapeHtml(s.path.split("/").pop())}</b>
      <span class="muted">${s.is_dir ? "folder" : "file"}</span>
      <button class="modal-x" title="Close">×</button>
    </div>
    ${ro ? `<div class="note">${s.secret
        ? "A secret is its owner's to share: only " + escapeHtml(s.owner_name || s.owner) +
          " can change who can read this one — not even a platform admin."
        : ["root", "daemon", "nobody"].includes(s.owner)
        ? "This belongs to the folder rather than to a person, so only a platform admin can change who has access."
        : "Only " + escapeHtml(s.owner_name || s.owner) + " (or a platform admin) can change who has access."}</div>` : ""}
    ${s.secret ? `<div class="note">Inside <b>_secrets/</b>: whoever you add here can read
       ${s.is_dir ? "every credential in this folder" : "this credential"} in full. It stays out of
       search, out of version history and out of live co-editing either way — that is what
       <b>_secrets/</b> guarantees, not who may open it.</div>` : ""}
    <label class="frow"><span>Access</span>
      <select id="sh-scope" data-testid="sh-scope"${ro ? " disabled" : ""}>${scopeOpts}</select>
    </label>
    <div id="sh-hint" class="sh-hint muted"></div>
    <div class="acl-title">People</div>
    <div id="sh-people" class="sh-people" data-testid="sh-people"></div>
    <div class="acl-add" id="sh-add">
      <select id="sh-who" data-testid="sh-who"><option value="">add someone…</option>${
        (principals.users || []).map((u) => `<option>${escapeHtml(u)}</option>`).join("")}</select>
      <select id="sh-role" data-testid="sh-role">
        <option value="view">can view</option><option value="edit">can edit</option></select>
      <button id="sh-addbtn" data-testid="sh-addbtn">Add</button>
    </div>
    <div class="acl-title">Anyone with a link</div>
    <div id="sh-public" class="sh-public" data-testid="sh-public">…</div>
    <details class="sh-adv"><summary>Advanced — owner, group, mode, raw ACLs</summary>
      <div id="sh-advbody" class="sh-advbody muted">loading…</div>
    </details>
    <div class="modal-foot">
      ${ro ? "" : '<button id="sh-save" class="primary" data-testid="sh-save">Save</button>'}
      <button id="sh-close">Close</button>
    </div>`;
  ov.appendChild(card);
  document.body.appendChild(ov);

  const peopleHost = card.querySelector("#sh-people");
  const hintHost = card.querySelector("#sh-hint");
  const addHost = card.querySelector("#sh-add");
  wirePublicShare(card.querySelector("#sh-public"), s);

  function editable() { return !ro && scope === "people"; }

  // "Same as the folder" describes the FOLDER's people, not this item's — so it
  // needs the folder's list. Fetched once, up front, so switching to it shows
  // the right names immediately instead of an empty list until you reopen.
  let inherited = null;
  async function loadInherited() {
    if (!s.parent) return;
    try {
      const r = await fetch("/fs/share?path=" + encodeURIComponent(s.parent));
      if (!r.ok) return;
      const ps = await r.json();
      inherited = ps.scope === "everyone" ? "everyone"
        : ps.people.map((q) => ({ user: q.user, name: q.name,
                                  role: q.role === "owner" ? "edit" : q.role,
                                  via: "group", source: ps.group }));
    } catch (e) { /* the rows just stay empty */ }
    if (scope === "inherit") renderPeople();
  }

  function renderHint() {
    const t = {
      inherit: `Follows <b>${escapeHtml(s.parent || "")}</b>. Change it there and this follows along.`,
      everyone: "Anyone with an Ollsoft account can open and edit this.",
      people: s.is_dir
        ? "Only the people below. Adding or removing someone later is instant — no re-processing of the files inside."
        : "Only the people below, regardless of who can see the folder it sits in.",
      private: "Only you. It stays in your own search — the indexer keeps read access, and nobody else's search can reach it.",
    }[scope] || "";
    hintHost.innerHTML = t;
  }

  function renderPeople() {
    peopleHost.innerHTML = "";
    const rows = [{ user: owner.user, name: owner.name, role: "owner" }];
    if (scope === "everyone" || (scope === "inherit" && inherited === "everyone")) {
      rows.push({ user: "*", name: "Everyone at Ollsoft", role: "everyone" });
    } else if (scope === "inherit") {
      rows.push(...(Array.isArray(inherited) ? inherited : []).filter((q) => q.user !== owner.user));
    } else if (scope !== "private") {
      rows.push(...people);
    }
    for (const p of rows) {
      const row = document.createElement("div");
      row.className = "sh-row";
      const you = p.role === "owner";
      row.innerHTML = `
        <span class="sh-av${you ? " you" : ""}${p.role === "everyone" ? " all" : ""}">${
          p.role === "everyone" ? "@" : escapeHtml(shInitials(p.name || p.user))}</span>
        <span class="sh-nm"><b>${escapeHtml(p.name || p.user)}</b>${
          p.user === "*" ? "" : `<span>${escapeHtml(p.user)}${
            p.via === "group" && p.source ? " · via " + escapeHtml(p.source) : ""}</span>`}</span>`;
      if (p.role === "owner") {
        row.insertAdjacentHTML("beforeend", '<span class="sh-fixed">owner</span>');
      } else if (p.role === "everyone") {
        row.insertAdjacentHTML("beforeend", '<span class="sh-fixed">can view &amp; edit</span>');
      } else if (!editable()) {
        row.insertAdjacentHTML("beforeend",
          `<span class="sh-fixed">${p.role === "edit" ? "can edit" : "can view"}</span>`);
      } else {
        const sel = document.createElement("select");
        sel.className = "sh-role";
        sel.innerHTML = `<option value="view"${p.role === "view" ? " selected" : ""}>can view</option>
                         <option value="edit"${p.role === "edit" ? " selected" : ""}>can edit</option>`;
        sel.addEventListener("change", () => { p.role = sel.value; });
        row.appendChild(sel);
        row.appendChild(mkBtn("×", "Remove " + p.user, () => {
          people = people.filter((x) => x !== p);
          renderPeople();
        }));
      }
      peopleHost.appendChild(row);
    }
    addHost.hidden = !editable();
  }

  function redraw() { renderHint(); renderPeople(); }
  redraw();
  loadInherited();

  const scopeSel = card.querySelector("#sh-scope");
  if (scopeSel) scopeSel.addEventListener("change", () => { scope = scopeSel.value; redraw(); });

  card.querySelector("#sh-addbtn").addEventListener("click", () => {
    const who = card.querySelector("#sh-who").value.trim();
    if (!who) return;
    if (who === owner.user) { kbToast("They own it — they always have access", "err"); return; }
    const role = card.querySelector("#sh-role").value;
    const cur = people.find((x) => x.user === who);
    if (cur) cur.role = role;
    else people.push({ user: who, name: who, role, via: "person" });
    card.querySelector("#sh-who").value = "";
    renderPeople();
  });

  // The octal view, built on first open so the common case costs nothing.
  const adv = card.querySelector(".sh-adv");
  let advLoaded = false;
  adv.addEventListener("toggle", () => {
    if (!adv.open || advLoaded) return;
    advLoaded = true;
    buildAdvanced(card.querySelector("#sh-advbody"), s.path, ro);
  });

  const close = () => ov.remove();
  card.querySelector(".modal-x").addEventListener("click", close);
  card.querySelector("#sh-close").addEventListener("click", close);
  const saveBtn = card.querySelector("#sh-save");
  if (saveBtn) saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    let r, j;
    try {
      r = await fetch("/fs/share", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({
          path: s.path, scope,
          people: people.map((p) => ({ user: p.user, role: p.role })),
        }),
      });
      j = await r.json().catch(() => ({}));
    } catch (e) {
      saveBtn.disabled = false; saveBtn.textContent = "Save";
      kbToast("could not reach the server", "err");
      return;
    }
    if (!r.ok) {
      saveBtn.disabled = false; saveBtn.textContent = "Save";
      kbToast(j.error || "could not save", "err");
      return;
    }
    const notes = [];
    if (j.forked) notes.push(`This folder now keeps its own list of people, separate from ${s.parent}.`);
    if (j.regrouped) notes.push(`${j.regrouped} item${j.regrouped === 1 ? "" : "s"} inside now follow the new people.`);
    if (j.granted_traverse && j.granted_traverse.length) {
      notes.push("Opened the way in through " + j.granted_traverse.join(", ") +
                 " — pass-through only, they still can't list those folders.");
    }
    if (j.restarted && j.restarted.length) {
      notes.push("Reloaded the session of " + j.restarted.join(", ") +
                 " so the change applies to them right away. Any web terminal they had open was closed.");
    }
    close();
    loadTree(true);
    if (notes.length) kbAlert(notes.join("\n\n"), "Access updated");
    else kbToast("Access updated", "ok");
  });
}

// The raw view: owner, group, mode preset and named ACL entries, straight onto
// /fs/props. Unchanged semantics — it is the escape hatch for the cases the
// people list deliberately cannot express (a one-off group grant, an odd mode).
async function buildAdvanced(host, path, ro) {
  let p;
  try {
    const r = await fetch("/fs/props?path=" + encodeURIComponent(path));
    p = await r.json();
    if (!r.ok) throw new Error(p.error || "cannot read properties");
  } catch (e) {
    host.textContent = String(e.message || e);
    return;
  }
  let principals = { users: [], groups: [] };
  try {
    const pr = await fetch("/api/principals");
    if (pr.ok) principals = await pr.json();
  } catch (e) { /* names only */ }
  const opts = (arr, cur, placeholder) => {
    const list = [...new Set([...(cur ? [cur] : []), ...arr])];
    return (placeholder ? '<option value="">' + placeholder + "</option>" : "") +
      list.map((n) => `<option${n === cur ? " selected" : ""}>${escapeHtml(n)}</option>`).join("");
  };
  const removals = [];
  const additions = [];
  const m = parseInt(p.mode, 8) || 0;
  const cur = (m & 0o004) ? "company" : (m & 0o040) ? "team" : "private";
  const oct = p.is_dir ? { company: "2775", team: "2770", private: "0700" }
                       : { company: "664", team: "660", private: "600" };
  host.classList.remove("muted");
  host.innerHTML = `
    <div class="muted sh-mode">mode ${escapeHtml(p.mode)} · ${escapeHtml(p.path)}</div>
    <label class="frow"><span>Owner</span>${ro
      ? `<input value="${escapeHtml(p.owner)}" disabled>`
      : `<select id="pm-owner">${opts(principals.users, p.owner)}</select>`}</label>
    <label class="frow"><span>Group</span>${ro
      ? `<input value="${escapeHtml(p.group)}" disabled>`
      : `<select id="pm-group">${opts(principals.groups, p.group)}</select>`}</label>
    ${ro ? "" : `<label class="frow"><span>Mode</span>
      <select id="pm-vis" data-testid="pm-vis" title="Plain chmod presets — owner/group/others permission bits">
        <option value="company"${cur === "company" ? " selected" : ""}>company · everyone can read · chmod ${oct.company}</option>
        <option value="team"${cur === "team" ? " selected" : ""}>team · only the group above · chmod ${oct.team}</option>
        <option value="private"${cur === "private" ? " selected" : ""}>private · owner + entries below · chmod ${oct.private}</option>
      </select></label>
      <input type="hidden" id="pm-vis0" value="${cur}">`}
    <div class="acl-title">Named ACL entries</div>
    <div id="pm-acls" class="acl-list"></div>
    ${ro ? "" : `<div class="acl-add">
      <select id="pm-type"><option value="user">user</option><option value="group">group</option></select>
      <select id="pm-name">${opts(principals.users, null, "who…")}</select>
      <select id="pm-perms"><option value="r">read</option><option value="rw">read+write</option><option value="rwx">rwx</option></select>
      <button id="pm-addacl">add</button></div>
      <div class="modal-foot"><button id="pm-save">Apply raw permissions</button></div>`}`;

  const aclHost = host.querySelector("#pm-acls");
  function renderAcls() {
    const live = p.acls.filter((a) => !removals.some((r) => r.type === a.type && r.name === a.name))
      .concat(additions);
    aclHost.innerHTML = "";
    if (!live.length) { aclHost.innerHTML = '<div class="muted">none</div>'; return; }
    for (const a of live) {
      const row = document.createElement("div");
      row.className = "acl-row";
      row.innerHTML = `<span class="acl-kind">${a.type}</span><span class="acl-name">${escapeHtml(a.name)}</span><span class="acl-perms">${a.perms}</span>`;
      if (!ro) {
        row.appendChild(mkBtn("×", "Remove", () => {
          const ai = additions.indexOf(a);
          if (ai >= 0) additions.splice(ai, 1);
          else removals.push({ type: a.type, name: a.name });
          renderAcls();
        }));
      }
      aclHost.appendChild(row);
    }
  }
  renderAcls();
  if (ro) return;

  host.querySelector("#pm-type").addEventListener("change", () => {
    const type = host.querySelector("#pm-type").value;
    host.querySelector("#pm-name").innerHTML =
      opts(type === "group" ? principals.groups : principals.users, null, "who…");
  });
  host.querySelector("#pm-addacl").addEventListener("click", () => {
    const name = host.querySelector("#pm-name").value.trim();
    if (!name) return;
    additions.push({ type: host.querySelector("#pm-type").value, name,
                     perms: host.querySelector("#pm-perms").value });
    renderAcls();
  });
  host.querySelector("#pm-save").addEventListener("click", async () => {
    const body = { path: p.path, acl_add: additions, acl_remove: removals };
    const owner = host.querySelector("#pm-owner").value.trim();
    const group = host.querySelector("#pm-group").value.trim();
    if (owner && owner !== p.owner) body.owner = owner;
    if (group && group !== p.group) body.group = group;
    const vis = host.querySelector("#pm-vis");
    if (vis && vis.value !== host.querySelector("#pm-vis0").value) body.visibility = vis.value;
    const r = await fetch("/fs/props", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) { kbToast(j.error || "could not save", "err"); return; }
    if (j.granted_traverse && j.granted_traverse.length) {
      kbAlert("Also granted traverse (folder pass-through, no listing) on: " +
              j.granted_traverse.join(", ") + " — so they can reach this file.", "Shared");
    } else kbToast("Permissions saved", "ok");
    loadTree();
  });
}

function setAccessBadge(access) { renderDocBadge(access === undefined ? null : access); }

// "meeting notes" → "meeting notes.md". Typing the extension is a chore and
// .md is what nearly every new file here is; a name that already carries one
// (.html for an artifact, .csv, …) is taken exactly as typed, and so is a
// name whose dot is part of the word ("plan v1.2" keeps its own shape only
// if you also give it an extension — that is the price of one simple rule).
function withDefaultExt(path) {
  const name = baseName(path);
  return name && !name.includes(".") ? path + ".md" : path;
}

async function createAndOpen(rawPath) {
  const path = withDefaultExt(rawPath);
  const r = await fetch("/fs/newfile", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ path }),
  });
  const j = await r.json();
  if (r.ok) { await loadTree(); openPath(path, path.endsWith(".html") ? "artifact" : "doc"); }
  else kbToast(j.error || "could not create file", "err");
}
async function newFileIn(folder) {
  const name = await kbPrompt("Name — a plain name becomes a document, .html an artifact:",
                              "note", { title: "New file in " + folder, ok: "Create" });
  if (name) createAndOpen(folder + "/" + name.trim());
}

async function newFolderIn(folder) {
  const name = await kbPrompt("Folder name:", "folder",
                              { title: "New folder in " + folder, ok: "Create" });
  if (!name) return;
  const r = await fetch("/api/fs/mkdir", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ path: folder + "/" + name.trim() }),
  });
  const j = await r.json().catch(() => ({}));
  if (r.ok) loadTree(true);
  else kbToast(j.error || "could not create folder", "err");
}

async function deleteEntry(n) {
  const what = n.dir ? `folder "${n.path}" and everything inside it` : `"${n.path}"`;
  // A secret is never copied aside, and something already in the trash has
  // nowhere further to go: those two ask the old, red question.
  const forGood = isSecretPath(n.path) || n.path.split("/").includes(".trash");
  const ok = forGood
    ? await kbConfirm(`Delete ${what} for good? Secrets are not kept in the trash.`,
                      { title: "Delete for good", ok: "Delete", danger: true })
    : await kbConfirm(`Move ${what} to the trash?`, { title: "Move to trash", ok: "Move to trash" });
  if (!ok) return;
  const r = await fetch("/api/fs/delete", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ path: n.path, permanent: forGood }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { kbToast(j.error || "could not delete", "err"); return; }
  if (j.trashed) {
    kbToast("Moved to the trash", "ok", { label: "Undo", fn: () => restoreTrash(j.trashed) });
    trashChanged();
  }
  // Retire any tabs that were showing the deleted path (or anything under it).
  for (const t of tabs.filter((t) => t.path === n.path || t.path.startsWith(n.path + "/"))) {
    closeTab(t);
  }
  loadTree(true);
  await repointPins(n.path, null);      // a pin to something deleted is a dead end
}

function setupDrop(row, folder) {
  row.addEventListener("dragover", (e) => { e.preventDefault(); row.classList.add("drop-hover"); });
  row.addEventListener("dragleave", () => row.classList.remove("drop-hover"));
  row.addEventListener("drop", async (e) => {
    e.preventDefault(); e.stopPropagation();
    row.classList.remove("drop-hover");
    // a tree row dropped on a folder = move (rename); OS files = upload
    const internal = e.dataTransfer ? e.dataTransfer.getData("application/x-kb-path") : "";
    if (internal) {
      if (internal === folder || folder.startsWith(internal + "/") ||
          dirName(internal) === folder) return;
      await moveEntry(internal, folder + "/" + baseName(internal));
      return;
    }
    // Folders come as entries, not as files — collectDropped walks them into
    // {rel} paths, so a whole tree can be dropped onto a folder.
    const { files, dirs } = await collectDropped(e.dataTransfer);
    if (!files.length && !dirs.length) return;
    if (files.length >= UPLOAD_MAX_FILES) {
      kbToast("That is more than " + UPLOAD_MAX_FILES + " files — upload it in parts", "err");
      return;
    }
    // Unlike the folder picker, a drop gets no confirmation from the browser —
    // and a mis-aimed drop can be a lot of files, so ask once with the count.
    if (dirs.length) {
      const tops = dirs.filter((d) => !d.includes("/"));   // what was actually dropped
      const what = tops.length === 1 ? '"' + tops[0] + '"' : tops.length + " folders";
      if (!await kbConfirm("Upload " + what + " with " + files.length +
                           (files.length === 1 ? " file" : " files") + " into " + folder + "?",
                           { title: "Upload folder", ok: "Upload" })) return;
    }
    const ok = await uploadMany(folder, files, dirs);   // ghosts, then the real tree
    if (ok && dirs.length) {
      kbToast("Uploaded " + ok + (ok > 1 ? " files" : " file") + " to " + folder, "ok");
    }
  });
}

// ---- fuzzy matching --------------------------------------------------------
// One scorer, mirroring the server's _name_score (user_server.py), so a file
// ranks the same whether it came from the tree we already hold or from the API.
// Diacritics are folded, so `lekarska` finds `lékařská zpráva.png`.
function foldText(s) {
  return s.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
}

// Are the query's characters present, in order — each one either CONSECUTIVE
// to the previous match or starting a word? Consecutive runs and word starts
// score higher (that's what makes `apl` rank projects/acme/plan.md first);
// letters plucked from the middles of unrelated words ("tomas" out of
// "terraform.tfvars") are not a match at all.
function subseqMatch(q, hay) {
  let i = 0, score = 0, run = 0, last = 0;
  const hits = [];
  for (let pos = 0; pos < hay.length && i < q.length; pos++) {
    if (hay[pos] !== q[i]) { run = 0; continue; }
    const boundary = pos === 0 || " -_./".includes(hay[pos - 1]);
    if (run === 0 && !boundary) continue;   // mid-word starts don't count
    run++;
    score += 2 + run;
    if (boundary) score += 4;
    hits.push(pos);
    last = pos;
    i++;
  }
  if (i < q.length) return null;
  return { score: Math.max(score - (last >> 3), 1), hits };
}

// Rank a path against a query, best tier first: exact basename, basename
// prefix, basename substring, path substring, then fuzzy over the basename and
// finally over the whole path. `on` says which string the hit indexes refer to.
function pathScore(q, path) {
  const fq = foldText(q);
  if (!fq) return null;
  const name = path.slice(path.lastIndexOf("/") + 1);
  const fname = foldText(name), fpath = foldText(path);
  const stem = fname.replace(/\.[^.]+$/, "");
  const span = (at, len) => Array.from({ length: len }, (_, k) => at + k);
  if (fq === fname || fq === stem) return { score: 1000, hits: span(0, fname.length), on: "name" };
  if (fname.startsWith(fq)) return { score: 900 - Math.min(fname.length, 99), hits: span(0, fq.length), on: "name" };
  let at = fname.indexOf(fq);
  if (at >= 0) return { score: 800 - Math.min(at, 99), hits: span(at, fq.length), on: "name" };
  at = fpath.indexOf(fq);
  if (at >= 0) return { score: 700 - Math.min(at, 99), hits: span(at, fq.length), on: "path" };
  // every word somewhere in the path, in any order: "transcriptor readme"
  const toks = fq.split(/\s+/).filter(Boolean);
  if (toks.length > 1) {
    const hits = [];
    let bonus = 0, ok = true;
    for (const tk of toks) {
      const k = fpath.indexOf(tk);
      if (k < 0) { ok = false; break; }
      hits.push(...span(k, tk.length));
      bonus += Math.max(0, 40 - k);
    }
    if (ok) return { score: 600 + Math.min(bonus, 90), hits, on: "path" };
  }
  let m = subseqMatch(fq, fname);
  if (m) return { score: 400 + m.score, hits: m.hits, on: "name" };
  m = subseqMatch(fq, fpath);
  if (m) return { score: 200 + m.score, hits: m.hits, on: "path" };
  return null;
}

// Command labels are prose, not paths — "Go to file / search everything" has a
// slash in it, and pathScore would treat everything after it as the basename
// and return hit indexes into the wrong string. Same ladder, one string.
function labelScore(q, label) {
  const fq = foldText(q), fl = foldText(label);
  const span = (at, len) => Array.from({ length: len }, (_, k) => at + k);
  if (!fq) return { score: 1, hits: [] };
  if (fl.startsWith(fq)) return { score: 900, hits: span(0, fq.length) };
  const at = fl.indexOf(fq);
  if (at >= 0) return { score: 800 - Math.min(at, 99), hits: span(at, fq.length) };
  const toks = fq.split(/\s+/).filter(Boolean);
  if (toks.length > 1 && toks.every((t) => fl.includes(t))) {
    const hits = [];
    for (const t of toks) hits.push(...span(fl.indexOf(t), t.length));
    return { score: 600, hits };
  }
  const m = subseqMatch(fq, fl);
  return m ? { score: 200 + m.score, hits: m.hits } : null;
}

// Render text with the matched characters emphasised. Folding can change a
// string's length (ligatures), so highlight only when the indexes still line up.
function markHits(text, hits) {
  const frag = document.createDocumentFragment();
  const set = new Set(hits || []);
  if (!set.size || foldText(text).length !== text.length) {
    frag.appendChild(document.createTextNode(text));
    return frag;
  }
  let buf = "", on = false;
  const flush = () => {
    if (!buf) return;
    if (on) { const b = document.createElement("b"); b.textContent = buf; frag.appendChild(b); }
    else frag.appendChild(document.createTextNode(buf));
    buf = "";
  };
  for (let i = 0; i < text.length; i++) {
    const hit = set.has(i);
    if (hit !== on) { flush(); on = hit; }
    buf += text[i];
  }
  flush();
  return frag;
}

// Everything in the tree, flat — the palette's file corpus. The tree is built
// by the backend running AS this user, so it already contains exactly the files
// the kernel lets them see: matching over it can never leak a path. Cached
// against the tree signature, so typing doesn't re-walk it on every keystroke.
let _flatCache = { sig: null, list: [] };
function flatEntries() {
  if (_flatCache.sig === _lastTreeJson) return _flatCache.list;
  const out = [];
  (function walk(nodes) {
    for (const n of nodes || []) { out.push(n); if (n.children) walk(n.children); }
  })(_lastTreeData || []);
  _flatCache = { sig: _lastTreeJson, list: out };
  return out;
}

// Recently opened paths, so an empty palette is still useful and repeat visits
// rank first.
let recents = [];
try { recents = JSON.parse(localStorage.getItem("kbRecent") || "[]"); } catch (e) { /* private mode */ }
function noteRecent(path) {
  recents = [path, ...recents.filter((p) => p !== path)].slice(0, 40);
  try { localStorage.setItem("kbRecent", JSON.stringify(recents)); } catch (e) { /* ok */ }
}

// ---- keyboard bindings: ONE table drives the dispatcher, the command palette
// and the help sheet, so a shortcut can never disagree with its documentation.
// Combos use `Mod` (⌘ on a Mac, Ctrl elsewhere) and are matched on `event.code`
// so they survive alternative layouts — and Mac's Option key, which turns
// Alt+P into "π" in `event.key` but leaves `code` as "KeyP".
//   term:true  — still fires while a terminal has focus. Everything else is
//                handed straight to the shell, so Ctrl+K, Ctrl+P, Ctrl+R and
//                Alt+B stay readline's — a web app that eats those is unusable.
const IS_APPLE = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

// `Mod` is ⌘ on a Mac and Ctrl everywhere else; a literal `Ctrl` means the
// physical Ctrl key on every platform (⌘+Shift+digit is a macOS screenshot, so
// the digit shortcuts have to use Ctrl even there).
function comboMatches(e, combo) {
  const parts = combo.split("+");
  const key = parts.pop();
  const wantCtrl = parts.includes("Ctrl") || (!IS_APPLE && parts.includes("Mod"));
  const wantMeta = IS_APPLE && parts.includes("Mod");
  if (e.ctrlKey !== wantCtrl || e.metaKey !== wantMeta) return false;
  if (e.altKey !== parts.includes("Alt")) return false;
  // "?" already needs Shift on most layouts, so don't demand it twice
  if (key !== "?" && e.shiftKey !== parts.includes("Shift")) return false;
  // Match on `code`, not `key`: `key` is the CHARACTER, which becomes "π" under
  // Option on a Mac and is a dead key for ` on German/French/Czech layouts.
  if (/^[A-Z]$/.test(key)) return e.code === "Key" + key;
  if (/^[0-9]$/.test(key)) return e.code === "Digit" + key || e.code === "Numpad" + key;
  if (key === "?") return e.key === "?";
  return e.code === key || e.key === key;
}

// How a combo is written for humans, per platform.
function comboLabel(combo) {
  const map = IS_APPLE ? { Mod: "⌘", Ctrl: "⌃", Alt: "⌥", Shift: "⇧" }
                       : { Mod: "Ctrl", Ctrl: "Ctrl", Alt: "Alt", Shift: "Shift" };
  const pretty = { Backquote: "`", BracketLeft: "[", BracketRight: "]", Slash: "/",
                   Backslash: "\\",
                   Escape: "Esc", Enter: "⏎", ArrowUp: "↑", ArrowDown: "↓",
                   ArrowLeft: "←", ArrowRight: "→", Delete: "Del" };
  return combo.split("+").map((p) => map[p] || pretty[p] || p).join(IS_APPLE ? "" : "+");
}

const hasDoc = () => !!(active && active.kind === "doc");
const hasTab = () => !!active;
// The group the keyboard's tab commands act on: the focused group — or,
// while the keyboard sits on a folded group's handle, that group: Alt+W
// closes the tab it names, Alt+] moves it to the next one (which opens the
// group), and nothing maximizes it.
function keyPane() {
  const h = document.activeElement;
  if (h && h.classList && h.classList.contains("pane-handle")) {
    const p = allPanes().find((x) => x.handleEl === h);
    if (p) return p;
  }
  return focusedPane();
}
// the group has a tab (a terminal counts): what Alt+W, Alt+] act on
const hasFocusedTab = () => !!(keyPane() && keyPane().active);

// group: null keeps an entry out of the help sheet (it is a mode of another one)
const BINDINGS = [
  // — Navigate —
  { id: "palette", keys: ["Mod+P", "Mod+K"], group: "Navigate",
    label: "Go to file / search everything", run: () => openPalette("find") },
  { id: "commands", keys: ["Mod+Shift+P"], group: "Navigate", term: true,
    label: "Command palette", hint: "or type > in Go to file",
    run: () => openPalette("commands") },
  { id: "nexttab", keys: ["Alt+BracketRight"], group: "Navigate", term: true,
    label: "Next tab", run: () => cycleTab(1) },
  { id: "prevtab", keys: ["Alt+BracketLeft"], group: "Navigate", term: true,
    label: "Previous tab", run: () => cycleTab(-1) },
  { id: "gototab", keys: ["Alt+1"], group: "Navigate", term: true,
    label: "Go to tab 1…9", labelKeys: ["Alt+1…9", "Ctrl+Shift+1…9"], run: () => gotoTab(0) },
  { id: "closetab", keys: ["Alt+W"], group: "Navigate", term: true,
    label: "Close tab", when: hasFocusedTab,
    run: () => { const t = keyPane().active; if (t) closeTab(t); } },
  { id: "splitright", keys: ["Alt+Backslash"], group: "Navigate", term: true,
    when: () => hasFocusedTab() && paneTabs(keyPane()).length > 1,
    label: "Split the editor right", hint: "or drag a tab to a group's edge",
    run: () => splitActiveTab(1) },
  { id: "splitdown", keys: ["Alt+Shift+Backslash"], group: "Navigate", term: true,
    when: () => hasFocusedTab() && paneTabs(keyPane()).length > 1,
    label: "Split the editor below", hint: "or drag a tab to a group's top or bottom edge",
    run: () => splitActiveTab("down") },
  { id: "maximize", keys: ["Alt+Z"], group: "Navigate", term: true,
    when: hasFocusedTab, label: "Maximize / restore the focused group",
    hint: "or double-click the empty part of its tab strip",
    run: () => toggleMaximize() },
  { id: "focustree", keys: ["Mod+Shift+E", "Alt+E"], group: "Navigate",
    label: "Focus the file tree", run: focusTree },
  { id: "sidebar", keys: ["Alt+B"], group: "Navigate",
    label: "Show / hide the file tree", run: toggleSidebar },
  { id: "reveal", keys: ["Alt+R"], group: "Navigate", when: hasTab,
    label: "Reveal the open file in the tree", run: revealActive },

  // — Documents —
  { id: "newdoc", keys: ["Alt+N"], group: "Documents", label: "New document",
    run: newDocument },
  { id: "mode", keys: ["Alt+M"], group: "Documents", when: hasDoc,
    label: "Switch Rich ⇄ Source", run: () => setMode(active.mode === "rich" ? "source" : "rich") },
  { id: "find", keys: ["Mod+F"], group: "Documents", when: hasDoc,
    label: "Find in this document", run: findInDoc },
  { id: "history", keys: ["Alt+H"], group: "Documents", when: hasTab,
    label: "Version history", run: () => { if (active) openHistory(active.path); } },
  { id: "perms", keys: ["Alt+S"], group: "Documents", when: hasTab,
    label: "Share — who can open this", run: () => { if (active) openPerms(active.path); } },
  { id: "link", keys: ["Mod+Shift+K"], group: "Documents", when: hasDoc,
    label: "Insert link", run: tbLink },
  { id: "save", keys: ["Mod+S"], group: "Documents",
    label: "Save (already continuous)", run: saveNow },

  // — Terminal —
  // physical Ctrl on every platform: ⌘+` is "cycle windows" on macOS, and this
  // is the one binding users already had
  { id: "term", keys: ["Ctrl+Backquote"], group: "Terminal", term: true,
    label: "Show / hide the terminal", when: () => canShell, run: toggleTerminalPanel },
  { id: "newterm", keys: ["Ctrl+Shift+Backquote"], group: "Terminal", term: true,
    label: "New terminal", when: () => canShell, run: () => openTermWith() },

  // — Agents —
  { id: "chat", keys: ["Alt+C"], group: "Agents", term: true,
    label: "Agent chat", hint: "Claude Code, Codex, Gemini… in a side column",
    run: () => openChat() },

  // — Dictation —
  // F9 is the one function key no browser has claimed (F1 help, F3 find, F5
  // reload, F6 toolbar, F7 caret browsing, F10 menu, F11 fullscreen, F12
  // devtools are all spoken for), it has no default GNOME/KDE binding, and no
  // IME conflict. Alt+K is the alias for anyone whose window manager grabs
  // F-keys — the only Alt+letter this app hadn't already claimed.
  //
  // dictation.js handles the actual keydown/keyup in the capture phase (it needs
  // e.repeat and it needs a keyup, neither of which the dispatcher has). This
  // entry is what puts the key in the help sheet and the palette, and — via
  // term:true — what stops xterm from sending \x1b[20~ to the shell instead.
  // The cost, stated plainly: F9 no longer reaches the terminal, so mc's menu
  // key is gone while dictation is available. `when` makes that conditional.
  { id: "dictate", keys: ["F9", "Alt+K"], group: "Dictation", term: true,
    label: "Dictate — speak, and the text lands where you were typing",
    hint: "hold to talk, or tap once and tap again when you're done",
    when: dictationReady, run: toggleDictation },

  // — Help —
  { id: "help", keys: ["?", "F1", "Mod+Slash"], group: "Help",
    label: "Keyboard shortcuts", run: openShortcuts },
];

// Commands that have no key of their own but belong in the palette.
const EXTRA_COMMANDS = [
  { id: "newfolder", label: "New folder in company", run: () => newFolderIn("company") },
  { id: "upload", label: "Upload files into company", run: () => uploadInto("company") },
  { id: "uploaddir", label: "Upload a folder into company", run: () => uploadFolderInto("company") },
  { id: "hidden", label: "Show / hide dot-files", run: () => $("#hidden-toggle").click() },
  { id: "foldall", label: "Collapse / expand all folders", run: toggleFoldAll },
  { id: "cron", label: "Scheduled jobs (cron)", when: () => canShell, run: openCron },
  { id: "admin", label: "Admin — users, groups, network", when: () => !$("#admin-btn").hidden,
    run: openAdmin },
  { id: "settings", label: "Settings — theme and preferences", run: () => openSettings() },
  { id: "pinopen", label: "Pin / unpin the open file in the sidebar", when: hasTab,
    run: () => { if (!active) return;
      pinnedTargets().has(active.path) ? unpinPath(active.path) : pinPath(active.path, false); } },
  { id: "copypath", label: "Copy the open file's path", when: hasTab,
    run: () => { if (!active) return;
      navigator.clipboard.writeText(active.path)
        .then(() => kbToast("Path copied", "ok"), () => kbToast("Clipboard blocked", "err")); } },
  { id: "inbox", label: "Inbox — mentions and what was shared with you", run: openInbox },
  { id: "trash", label: "Trash — restore something you deleted", run: openTrash },
  { id: "reload", label: "Reload the file tree", run: () => loadTree(true).then(() => kbToast("Tree reloaded", "ok")) },
  { id: "retryspeech", label: "Retry the last dictation", when: dictationReady,
    run: retryDictation },
  { id: "dicthistory", label: "Dictation history — recent transcripts and saved recordings",
    when: dictationReady, run: openDictHistory },
  // The mic is held open between utterances so the next one starts instantly and
  // the browser doesn't re-prompt. This hands it back without waiting out the
  // five-minute idle timer, for anyone who wants the recording indicator gone.
  { id: "releasemic", label: "Release the microphone", when: dictationReady,
    run: () => { releaseMicNow(); kbToast("Microphone released", "ok"); } },
  { id: "speechlang", label: "Dictation language…", when: dictationReady,
    run: dictationLangPrompt },
  { id: "newchat", label: "New agent chat", run: () => openChat() },
  // — the layout: what dragging does, for the keyboard —
  { id: "movetableft", label: "Move tab to the column on the left", when: hasFocusedTab, run: () => moveFocusedTab("left") },
  { id: "movetabright", label: "Move tab to the column on the right", when: hasFocusedTab, run: () => moveFocusedTab("right") },
  { id: "movetabup", label: "Move tab to the group above", when: hasFocusedTab, run: () => moveFocusedTab("up") },
  { id: "movetabdown", label: "Move tab to the group below", when: hasFocusedTab, run: () => moveFocusedTab("down") },
  // (a phone has no panel: its terminal is a tab like any other)
  { id: "movetabdock", label: "Move tab into the terminal panel", when: () => !isMobile() && hasFocusedTab() && !isDock(focusedPane()),
    run: () => moveFocusedTab("dock") },
  { id: "focusnext", label: "Focus the next group", run: () => focusNextPane(1) },
  { id: "focusprev", label: "Focus the previous group", run: () => focusNextPane(-1) },
  // (a phone's dock is always the sheet at the bottom: no side to pick)
  { id: "dockbottom", label: "Terminal panel: bottom", when: () => !isMobile() && dock.side !== "bottom", run: () => setDockSide("bottom") },
  { id: "dockright", label: "Terminal panel: right", when: () => !isMobile() && dock.side !== "right", run: () => setDockSide("right") },
  { id: "dockleft", label: "Terminal panel: left", when: () => !isMobile() && dock.side !== "left", run: () => setDockSide("left") },
  { id: "maximize2", label: "Maximize / restore the focused group", when: hasFocusedTab, run: () => toggleMaximize() },
  { id: "fold", label: "Fold / unfold the focused group", when: hasFocusedTab, run: () => togglePaneFold() },
  { id: "signout", label: "Sign out", run: () => { location.href = "/logout"; } },
];

// Auto-detect handles mixed Czech/English in one session, which is the common
// case here; pinning a language measurably improves accuracy when you only ever
// speak one. Stored per browser, sent to the server as ?lang=.
async function dictationLangPrompt() {
  const cur = localStorage.getItem("kbDictateLang") || "";
  const v = await kbPrompt(
    "Two-letter language code — cs, en, de … Leave empty to auto-detect.", cur,
    { title: "Dictation language", ok: "Save", placeholder: "auto-detect" });
  if (v === null) return;
  const code = v.trim().toLowerCase();
  if (code && !/^[a-z]{2,3}$/.test(code)) { kbToast("That isn't a language code", "err"); return; }
  try {
    if (code) localStorage.setItem("kbDictateLang", code);
    else localStorage.removeItem("kbDictateLang");
  } catch (e) { /* private mode */ }
  kbToast(code ? "Dictation set to " + code : "Dictation set to auto-detect", "ok");
}

function allCommands() {
  const cmds = [];
  for (const b of BINDINGS) {
    if (b.group === null || !b.run) continue;
    if (b.when && !b.when()) continue;
    cmds.push({ label: b.label, keys: b.labelKeys ? b.labelKeys[0] : b.keys[0], run: b.run });
  }
  for (const c of EXTRA_COMMANDS.concat(registeredCommands())) {
    if (c.when && !c.when()) continue;
    cmds.push({ label: c.label, keys: c.keys ? c.keys[0] : null, run: c.run });
  }
  return cmds;
}

function tabIndexFromEvent(e) {
  const m = /^(?:Digit|Numpad)([1-9])$/.exec(e.code || "");
  if (!m) return -1;
  const plainAlt = e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey;
  const ctrlShift = e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey;
  return plainAlt || ctrlShift ? Number(m[1]) - 1 : -1;
}

// Which binding (if any) does this event fire? Used by the global dispatcher
// and by xterm, which asks first so shell keys are never stolen.
function bindingFor(e, inTerm) {
  // Alt+1…9 (Chrome, Safari) or Ctrl+Shift+1…9 (works in Firefox too, which
  // reserves Alt+digit for its own tab switching) — one binding, nine targets
  const i = tabIndexFromEvent(e);
  if (i >= 0) return { id: "gototab", term: true, run: () => gotoTab(i) };
  for (const b of BINDINGS.concat(registeredCommands().filter((c) => c.keys))) {
    if (inTerm && !b.term) continue;
    if (!b.keys.some((k) => comboMatches(e, k))) continue;
    if (b.when && !b.when()) return null;   // the key is ours, but inert right now
    return b;
  }
  return null;
}

function isTyping(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
         !!el.closest(".cm-editor");
}

function wireShortcuts() {
  // Bubble phase, and never when something already handled the key: CodeMirror,
  // xterm, the dialog layer and the table-cell inputs all get first refusal.
  // (Capture phase would be wrong: CodeMirror skips its whole keymap when the
  // event is already defaultPrevented, so capturing would disable the editor.)
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.isComposing || e.keyCode === 229) return;
    // Surfaces that own the whole keyboard while they are up. `.modal-overlay`
    // is the superset — every modal in the app carries it, kbDialog adds
    // `.dlg-overlay` on top — and the @mention popup outranks everything.
    if (document.querySelector(".modal-overlay, .ctx-menu, .cm-tooltip-autocomplete")) {
      if (e.key === "Escape") closeTopModal();
      return;
    }
    const inTerm = !!(e.target && e.target.closest && e.target.closest(".tab-content.term"));
    const b = bindingFor(e, inTerm);
    if (!b) return;
    // A bare printable key (`?`) must never be stolen mid-sentence; anything
    // with Ctrl/⌘/Alt is unambiguous and works wherever you are.
    if (!e.ctrlKey && !e.metaKey && !e.altKey && (e.key || "").length === 1 &&
        !inTerm && isTyping(e.target)) return;
    e.preventDefault();
    b.run();
  });
}

// Escape closes whatever modal is on top. The dialog layer and the palette
// handle their own Escape (capture phase); this covers the older modals —
// permissions, admin, cron, history, launchers — which had no key handling at
// all and could only be dismissed with the mouse.
function closeTopModal() {
  const all = document.querySelectorAll(".modal-overlay");
  const ov = all[all.length - 1];
  if (!ov || ov.classList.contains("dlg-overlay")) return;   // owns its own Escape
  if (ov.classList.contains("palette-overlay")) { closePalette(); return; }
  const x = ov.querySelector(".modal-x");
  if (x) x.click(); else ov.remove();
}

// Tab commands act on the FOCUSED group: in the dock they cycle terminals.
function cycleTab(d) {
  const p = keyPane();
  const list = paneTabs(p);
  if (list.length < 2) return;
  const i = list.indexOf(p.active);
  activateTab(list[(((i < 0 ? 0 : i) + d) % list.length + list.length) % list.length]);
}
function gotoTab(i) { const list = paneTabs(keyPane()); if (list[i]) activateTab(list[i]); }
function toggleSidebar() {
  if (isMobile()) {
    if (document.body.classList.toggle("nav-open")) revealActiveInTree(true);
    return;
  }
  const hide = !document.body.classList.contains("nav-hidden");
  setNavHidden(hide);
  if (!hide) revealActiveInTree(true);
}
function saveNow() {
  // Edits stream into the CRDT and land on disk within a second — there is no
  // save button to press. Say so, rather than letting the browser offer to
  // save the page as a file.
  kbToast(active ? "Saved — every keystroke is written continuously" : "Nothing open", "ok");
}
function findInDoc() {
  const v = active && active.view;
  if (!v) return;
  v.focus();
  openSearchPanel(v);
}
// Make the active tab's row visible: expand every folder above it (a row
// inside a collapsed folder is display:none, so its highlight showed nothing
// and scrollIntoView was a no-op), then scroll the tree to it.
// "center" when the tree just came into view, "nearest" when it was already
// on screen — recentring a visible tree on every tab switch is jumpy.
function revealActiveInTree(center) {
  if (!active) return;
  const parts = active.path.split("/");
  let acc = "", changed = false;
  for (let i = 0; i < parts.length - 1; i++) {
    acc = acc ? acc + "/" + parts[i] : parts[i];
    if (collapsed.delete(acc)) changed = true;
  }
  if (changed) rerenderTree();
  const el = $("#tree").querySelector('.tree-item[data-path="' + cssEsc(active.path) + '"]');
  if (el) el.scrollIntoView({ block: center ? "center" : "nearest" });
}

function revealActive() {
  if (!active) return;
  if (isMobile()) document.body.classList.add("nav-open");
  setNavHidden(false);
  revealActiveInTree(true);
  treeCursor = active.path;
  paintTreeCursor();
}

// ---- the file tree, from the keyboard --------------------------------------
// A roving cursor rather than per-row tabindex: the tree re-renders itself
// every few seconds from the server, so the cursor lives in a variable (a path)
// and is re-painted after each render instead of living in the DOM.
let treeCursor = null;
let _typeAhead = { s: "", at: 0 };

function visibleRows() {
  // [data-path] skips the in-flight upload ghosts, which have no real path yet
  return Array.from($("#tree").querySelectorAll(".tree-item[data-path]"))
    .filter((el) => el.offsetParent !== null);
}
function paintTreeCursor() {
  const host = $("#tree");
  host.querySelectorAll(".tree-item.cursor").forEach((el) => el.classList.remove("cursor"));
  if (!treeCursor) return;
  const el = host.querySelector('.tree-item[data-path="' + cssEsc(treeCursor) + '"]');
  if (el) el.classList.add("cursor");
}
function moveTreeCursor(d) {
  const rows = visibleRows();
  if (!rows.length) return;
  let i = rows.findIndex((el) => el.dataset.path === treeCursor);
  i = i < 0 ? (d > 0 ? 0 : rows.length - 1) : Math.min(Math.max(i + d, 0), rows.length - 1);
  treeCursor = rows[i].dataset.path;
  paintTreeCursor();
  rows[i].scrollIntoView({ block: "nearest" });
}
function nodeAt(path) {
  return flatEntries().find((n) => n.path === path) || null;
}
// Renaming and deleting need write on the PARENT — the same rule renderNodes
// uses to decide whether to draw those buttons. Top-level areas have no parent
// node in the tree and are never removable.
function parentWritable(path) {
  if (!path.includes("/")) return false;
  const parent = nodeAt(path.slice(0, path.lastIndexOf("/")));
  return !!(parent && parent.access && parent.access.write);
}
function jumpTo(i) {
  const rows = visibleRows();
  if (!rows.length) return;
  const el = rows[i < 0 ? rows.length - 1 : i];
  treeCursor = el.dataset.path;
  paintTreeCursor();
  el.scrollIntoView({ block: "nearest" });
}
function focusTree() {
  setNavHidden(false);
  if (isMobile()) document.body.classList.add("nav-open");
  const host = $("#tree");
  host.focus();
  if (!treeCursor || !host.querySelector('.tree-item[data-path="' + cssEsc(treeCursor) + '"]')) {
    const rows = visibleRows();
    treeCursor = rows.length ? rows[0].dataset.path : null;
  }
  paintTreeCursor();
}
function setFolderOpen(path, open) {
  if (open) collapsed.delete(path); else collapsed.add(path);
  rerenderTree();
  paintTreeCursor();
}
// A narrow tree ellipsises long names, and an ellipsis you cannot expand is
// information you do not have. Hovering a truncated row shows the whole name as
// a plain native tooltip — set only when it IS cut off, since a title on every
// row would fire on every hover and say nothing new. Measured on hover, not at
// render time: the row's action buttons appear on hover and shrink the label,
// so the truncation you see is the one measured here.
function wireTreeTooltips() {
  $("#tree").addEventListener("mouseover", (e) => {
    const row = e.target.closest(".tree-item");
    if (!row) return;
    const label = row.querySelector(".tlabel");
    if (!label) return;
    if (label.scrollWidth > label.clientWidth + 1) row.title = label.textContent;
    else row.removeAttribute("title");
  });
}

function wireTreeKeys() {
  const host = $("#tree");
  host.tabIndex = 0;
  host.addEventListener("focus", () => { if (!treeCursor) focusTree(); });
  host.addEventListener("click", (e) => {
    const row = e.target.closest(".tree-item");
    if (row) { treeCursor = row.dataset.path; paintTreeCursor(); }
  });
  host.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;   // leave the global layer alone
    const n = treeCursor ? nodeAt(treeCursor) : null;
    const open = (p) => !collapsed.has(p);
    switch (e.key) {
      case "ArrowDown": e.preventDefault(); moveTreeCursor(1); return;
      case "ArrowUp": e.preventDefault(); moveTreeCursor(-1); return;
      case "Home": e.preventDefault(); jumpTo(0); return;
      case "End": e.preventDefault(); jumpTo(-1); return;
      case "ArrowRight":
        e.preventDefault();
        if (n && n.dir && !open(n.path)) setFolderOpen(n.path, true);
        else moveTreeCursor(1);
        return;
      case "ArrowLeft":
        e.preventDefault();
        if (n && n.dir && open(n.path)) setFolderOpen(n.path, false);
        else if (treeCursor && treeCursor.includes("/")) {
          treeCursor = treeCursor.slice(0, treeCursor.lastIndexOf("/"));
          paintTreeCursor();
          const el = $("#tree").querySelector('.tree-item[data-path="' + cssEsc(treeCursor) + '"]');
          if (el) el.scrollIntoView({ block: "nearest" });
        }
        return;
      case "Enter":
      case " ":
        e.preventDefault();
        if (!n) return;
        if (n.dir) setFolderOpen(n.path, !open(n.path));
        else openEntry(n);
        return;
      // The kernel would refuse anyway, but the keyboard must not offer a verb
      // the mouse UI hides: those buttons appear only where the PARENT is
      // writable, and top-level areas are never deletable.
      case "F2": e.preventDefault(); if (n && parentWritable(n.path)) renameEntry(n); return;
      case "Delete": e.preventDefault(); if (n && parentWritable(n.path)) deleteEntry(n); return;
      case "Escape": treeCursor = null; paintTreeCursor(); host.blur(); return;
      default: break;
    }
    // type-ahead: jump to the next row whose name starts with what you type
    if (e.key.length === 1 && !e.repeat) {
      const now = Date.now();
      _typeAhead.s = now - _typeAhead.at > 900 ? e.key : _typeAhead.s + e.key;
      _typeAhead.at = now;
      const rows = visibleRows();
      const from = Math.max(rows.findIndex((el) => el.dataset.path === treeCursor), 0);
      const q = foldText(_typeAhead.s);
      for (let k = 1; k <= rows.length; k++) {
        const el = rows[(from + (_typeAhead.s.length > 1 ? k - 1 : k)) % rows.length];
        const nm = (el.dataset.path || "").slice((el.dataset.path || "").lastIndexOf("/") + 1);
        if (foldText(nm).startsWith(q)) {
          treeCursor = el.dataset.path; paintTreeCursor();
          el.scrollIntoView({ block: "nearest" });
          break;
        }
      }
    }
  });
}

// ---- command palette / go-to-file ------------------------------------------
// One surface for three questions: which file, which command, and where is that
// word. Files and commands are matched locally against data already in memory,
// so results appear as fast as you type; document contents come from the
// backend and stream in underneath.
let _palette = null;

function closePalette() {
  if (!_palette) return;
  clearTimeout(_palette.timer);   // a content search in flight must not paint into dead DOM
  _palette.ov.remove();
  _palette = null;
}

function openPalette(mode, seed) {
  closePalette();
  const ov = document.createElement("div");
  ov.className = "modal-overlay palette-overlay";
  ov.setAttribute("data-testid", "palette");
  const card = document.createElement("div");
  card.className = "modal-card palette-card";
  const head = document.createElement("div");
  head.className = "palette-head";
  const kind = document.createElement("span");
  kind.className = "palette-kind";
  kind.setAttribute("data-testid", "palette-kind");
  const x = document.createElement("button");
  x.className = "modal-x palette-x";
  x.textContent = "×";
  x.title = "Close";
  x.setAttribute("aria-label", "Close");
  x.addEventListener("click", closePalette);
  const input = document.createElement("input");
  input.className = "palette-input";
  input.setAttribute("data-testid", "palette-input");
  input.spellcheck = false;
  input.autocomplete = "off";
  head.append(kind, input, x);   // on a phone the card fills the screen, so the
                                 // click-the-backdrop escape hatch isn't reachable
  const list = document.createElement("div");
  list.className = "palette-list";
  list.setAttribute("data-testid", "palette-list");
  const foot = document.createElement("div");
  foot.className = "palette-foot";
  foot.innerHTML = '<span>↑↓ move · ⏎ open · esc close</span>';
  const help = document.createElement("button");
  help.className = "linkish";
  help.textContent = "Keyboard shortcuts";
  help.addEventListener("click", () => { closePalette(); openShortcuts(); });
  foot.appendChild(help);
  card.append(head, list, foot);
  ov.appendChild(card);
  document.body.appendChild(ov);
  ov.addEventListener("click", (e) => { if (e.target === ov) closePalette(); });

  let items = [], sel = 0, seq = 0;
  // The two searched sections are held apart and composed on every change:
  // document matches render ABOVE file matches even though they arrive later,
  // and each section shows only its head until its "Show more" row is taken.
  // `fixed` (commands, or the Open/Recent list before anything is typed)
  // bypasses the sectioning entirely.
  let fixed = null, docFull = [], fileFull = [], docsOpen = false, filesOpen = false;
  const DOC_CUT = 10, FILE_CUT = 5;
  // Is a content search in flight? Filename matches are computed locally and
  // appear instantly; document contents come from the server. Without this the
  // gap between the two showed "No matches" — stating as fact something we did
  // not know yet.
  let searching = false;
  const willSearch = (q) => !!q && !q.startsWith(">") && q.length >= 2;

  const spinnerRow = (label) => {
    const row = document.createElement("div");
    row.className = "palette-searching";
    row.setAttribute("role", "status");
    row.setAttribute("data-testid", "palette-searching");
    const s = document.createElement("span");
    s.className = "upspin";
    const txt = document.createElement("span");
    txt.textContent = label;
    row.append(s, txt);
    return row;
  };

  const render = () => {
    list.innerHTML = "";
    kind.classList.toggle("busy", searching);
    if (!items.length && !searching) {
      const empty = document.createElement("div");
      empty.className = "palette-empty muted";
      empty.textContent = "No matches";
      list.appendChild(empty);
      return;
    }
    let group = null;
    items.forEach((it, i) => {
      if (it.group !== group) {
        group = it.group;
        const g = document.createElement("div");
        g.className = "palette-group";
        g.textContent = group;
        list.appendChild(g);
      }
      const row = document.createElement("div");
      row.className = "palette-item" + (i === sel ? " sel" : "") + (it.more ? " pi-more" : "");
      row.setAttribute("data-testid", it.more ? "palette-more" : "palette-item");
      row.dataset.index = String(i);
      if (it.path) row.dataset.path = it.path;
      const ic = document.createElement("span");
      ic.className = "pi-icon";
      ic.innerHTML = it.icon || "";
      const body = document.createElement("span");
      body.className = "pi-body";
      // Build the label HERE, every render. An item may not cache DOM: a
      // DocumentFragment is EMPTIED by appendChild (its children are moved),
      // so a cached one paints once and then renders a blank row on every
      // later pass — and render() runs again on each arrow key and when the
      // content results land.
      const main = document.createElement("span");
      main.className = "pi-main";
      main.appendChild(markHits(it.main.text, it.main.hits));
      body.appendChild(main);
      if (it.sub) {
        const sub = document.createElement("span");
        sub.className = "pi-sub";
        sub.appendChild(markHits(it.sub.text, it.sub.hits));
        body.appendChild(sub);
      }
      row.append(ic, body);
      if (it.keys) {
        const k = document.createElement("kbd");
        k.className = "pi-keys";
        k.textContent = comboLabel(it.keys);
        row.appendChild(k);
      }
      row.addEventListener("mousemove", () => {
        if (sel === i) return;
        sel = i;
        list.querySelectorAll(".palette-item.sel").forEach((e2) => e2.classList.remove("sel"));
        row.classList.add("sel");
      });
      row.addEventListener("click", () => choose(i));
      list.appendChild(row);
    });
    // The spinner sits exactly where the "In documents" section will appear —
    // at the BOTTOM, under the file matches that are already on screen. The
    // results replace it in place, so nothing above it ever moves.
    if (searching) list.appendChild(spinnerRow("Searching documents…"));
  };

  const choose = (i) => {
    const it = items[i];
    if (!it) return;
    if (it.more) { it.more(); return; }   // expand the section, stay open
    closePalette();
    it.run();
  };

  const moreItem = (group, n, fn) => ({
    group, icon: I.chevronDown, more: fn,
    main: { text: "Show " + n + " more", hits: [] },
  });

  // Rebuild `items` from the current sections and expansion state. Called on
  // every keystroke AND when the content results land or a section expands.
  //
  // Files first, documents appended BELOW them. Filename matches are computed
  // locally and are on screen within the keystroke; document matches come back
  // from the server a few hundred ms later. Whichever section arrives last has
  // to be the bottom one — documents used to insert above, which shoved every
  // file row the pointer was already travelling towards further down the list.
  const compose = () => {
    if (fixed) { items = fixed; return; }
    items = filesOpen ? [...fileFull] : fileFull.slice(0, FILE_CUT);
    if (!filesOpen && fileFull.length > FILE_CUT)
      items.push(moreItem("Files", fileFull.length - FILE_CUT,
                          () => { filesOpen = true; compose(); render(); }));
    items.push(...(docsOpen ? docFull : docFull.slice(0, DOC_CUT)));
    if (!docsOpen && docFull.length > DOC_CUT)
      items.push(moreItem("In documents", docFull.length - DOC_CUT,
                          () => { docsOpen = true; compose(); render(); }));
  };

  const fileIcon = (n) => (n.dir ? I.folder : isSecretPath(n.path) ? I.lock
    : n.kind === "artifact" ? I.artifact : n.kind === "file" ? I.file : I.doc);

  const fileItem = (n, hit) => ({
    group: "Files", icon: fileIcon(n), path: n.path,
    main: { text: n.path.slice(n.path.lastIndexOf("/") + 1),
            hits: hit && hit.on === "name" ? hit.hits : [] },
    sub: { text: n.path, hits: hit && hit.on === "path" ? hit.hits : [] },
    run: () => (n.dir ? revealFolder(n.path) : openEntry(n)),
  });

  const build = (q) => {
    fixed = null; docFull = []; fileFull = [];
    docsOpen = filesOpen = false;
    const out = [];
    if (q.startsWith(">")) {
      kind.textContent = "Run";
      const cq = q.slice(1).trim();
      for (const c of allCommands()) {
        const hit = cq ? labelScore(cq, c.label) : { score: 1, hits: [] };
        if (!hit) continue;
        out.push({ group: "Commands", icon: I.command, score: hit.score, keys: c.keys,
                   main: { text: c.label, hits: hit.hits }, run: c.run });
      }
      out.sort((a, b) => b.score - a.score);
      fixed = out;
      return;
    }
    kind.textContent = "Find";
    const entries = flatEntries();
    if (!q) {
      // nothing typed yet: what you had open, then what you opened recently
      for (const t of tabs) {
        const n = entries.find((x) => x.path === t.path);
        if (n) out.push({ ...fileItem(n, null), group: "Open" });
      }
      for (const p of recents) {
        if (out.some((o) => o.path === p)) continue;
        const n = entries.find((x) => x.path === p);
        if (n) out.push({ ...fileItem(n, null), group: "Recent" });
        if (out.length > 14) break;
      }
      fixed = out;
      return;
    }
    const scored = [];
    for (const n of entries) {
      if (!showHidden && n.path.split("/").some((s) => s.startsWith("."))) continue;
      // secrets are findable in the tree, never through search — the platform
      // promises they stay out of git history, the index and this list
      if (isSecretPath(n.path)) continue;
      const hit = pathScore(q, n.path);
      if (!hit) continue;
      let score = hit.score;
      const r = recents.indexOf(n.path);
      if (r >= 0) score += 60 - r;               // things you actually use, first
      if (n.dir) score -= 40;                    // a folder is rarely the target
      scored.push({ n, hit, score });
    }
    scored.sort((a, b) => b.score - a.score || a.n.path.length - b.n.path.length);
    fileFull = scored.slice(0, 20).map((s) => fileItem(s.n, s.hit));
  };

  // contents come from the backend; they arrive after the local list is drawn
  let contentTimer = null;
  const searchContents = (q) => {
    clearTimeout(contentTimer);
    // bump BEFORE the early return: shrinking the query below two characters,
    // or switching to commands, must also invalidate a request already in flight
    const mine = ++seq;
    if (!q || q.startsWith(">") || q.length < 2) return;
    const self = _palette;
    contentTimer = _palette.timer = setTimeout(async () => {
      let j;
      try { j = await (await fetch("/api/search?q=" + encodeURIComponent(q))).json(); }
      catch (e) {
        // Offline or the backend refused: stop claiming a search is running.
        if (mine === seq && _palette === self) { searching = false; render(); }
        return;
      }
      // Superseded or closed: a NEWER search owns `searching` now — leave it.
      if (mine !== seq || _palette !== self) return;
      searching = false;
      docFull = (j.results || []).map((m) => ({
        group: "In documents", icon: I.doc, path: m.path + ":" + m.line,
        main: { text: m.text, hits: contentHits(q, m.text) },
        sub: { text: m.path + " · line " + m.line, hits: [] },
        run: () => openAtLine(m.path, m.line),
      }));
      // Documents append BELOW the files, so every index already on screen —
      // the selection included — keeps its meaning and there is nothing to
      // re-anchor. Enter cannot change meaning depending on whether the server
      // has answered yet. With no file rows at all, sel is 0 and the first
      // document hit takes the selection, which is still what you want.
      compose();
      render();
    }, 180);
  };

  const refresh = () => {
    const q = input.value.trim();
    build(q);
    compose();
    sel = 0;
    // Set BEFORE render so the very first paint after a keystroke already
    // shows the pending state — not one frame late.
    searching = willSearch(q);
    render();
    searchContents(q);
  };

  input.addEventListener("input", refresh);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || (e.ctrlKey && e.key === "n")) {
      e.preventDefault(); sel = Math.max(0, Math.min(sel + 1, items.length - 1)); render();
      const el = list.querySelector(".palette-item.sel"); if (el) el.scrollIntoView({ block: "nearest" });
    } else if (e.key === "ArrowUp" || (e.ctrlKey && e.key === "p")) {
      e.preventDefault(); sel = Math.max(0, Math.min(sel - 1, items.length - 1)); render();
      const el = list.querySelector(".palette-item.sel"); if (el) el.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault(); choose(sel);
    } else if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation(); closePalette();
    }
  });

  _palette = { ov, input, timer: null };
  input.value = mode === "commands" ? ">" : (seed || "");
  refresh();
  input.placeholder = mode === "commands"
    ? "Type a command…"
    : "Type a file name, or > for commands";
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

// Which characters of a content match to emphasise: every occurrence of each
// query word, which is close enough to what Postgres actually matched.
function contentHits(q, text) {
  const ft = foldText(text), hits = [];
  for (const w of foldText(q).split(/\s+/)) {
    if (w.length < 2) continue;
    let at = ft.indexOf(w);
    while (at >= 0) {
      for (let k = 0; k < w.length; k++) hits.push(at + k);
      at = ft.indexOf(w, at + w.length);
    }
  }
  return hits;
}

function revealFolder(path) {
  collapsed.delete(path);
  let acc = "";
  for (const seg of path.split("/")) { acc = acc ? acc + "/" + seg : seg; collapsed.delete(acc); }
  rerenderTree();
  setNavHidden(false);   // Alt+B may have hidden it
  if (isMobile()) document.body.classList.add("nav-open");
  treeCursor = path;
  paintTreeCursor();
  const el = $("#tree").querySelector('.tree-item[data-path="' + cssEsc(path) + '"]');
  if (el) el.scrollIntoView({ block: "center" });
  else if (_lastTreePaths && _lastTreePaths.size && !_lastTreePaths.has(path))
    kbToast(path + " is not there any more", "err");
}

async function openAtLine(path, line) {
  await openPath(path, kindForPath(path));
  const t = tabs.find((x) => x.path === path);
  if (!t || !t.view || !line) return;
  // A freshly opened document is EMPTY until the CRDT seed arrives over the
  // websocket, so jumping straight to the line would always land on line 1.
  // Wait for the text (bounded), and give up quietly if the tab goes away.
  for (let i = 0; i < 60 && t.view && t.view.state.doc.lines < line; i++) {
    await new Promise((r) => setTimeout(r, 50));
  }
  if (!t.view || !tabs.includes(t) || active !== t) return;
  const doc = t.view.state.doc;
  const l = doc.line(Math.min(Math.max(line, 1), doc.lines));
  t.view.dispatch({ selection: { anchor: l.from },
                    effects: EditorView.scrollIntoView(l.from, { y: "center" }) });
  t.view.focus();
}

// ---- the shortcut sheet ----------------------------------------------------
// Rendered from BINDINGS, so it cannot drift from what the keys actually do.
function openShortcuts() {
  if (document.querySelector('[data-testid="shortcuts"]')) return;
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.setAttribute("data-testid", "shortcuts");
  const card = document.createElement("div");
  card.className = "modal-card keys-card";
  const head = document.createElement("div");
  head.className = "modal-head";
  head.innerHTML = "<b>Keyboard shortcuts</b>";
  const x = document.createElement("button");
  x.className = "modal-x"; x.textContent = "×"; x.title = "Close";
  head.appendChild(x);
  card.appendChild(head);

  const groups = [];
  for (const b of BINDINGS) {
    if (!b.group) continue;
    let g = groups.find((z) => z.name === b.group);
    if (!g) { g = { name: b.group, rows: [] }; groups.push(g); }
    g.rows.push({ keys: b.labelKeys || b.keys, label: b.label, hint: b.hint });
  }
  groups.push({ name: "In the file tree", rows: [
    { keys: ["ArrowUp", "ArrowDown"], label: "Move between files" },
    { keys: ["ArrowRight", "ArrowLeft"], label: "Open / close a folder" },
    { keys: ["Enter"], label: "Open the file" },
    { keys: ["F2"], label: "Rename" },
    { keys: ["Delete"], label: "Delete" },
    { keys: ["a…z"], label: "Jump to a name", hint: "type the first letters" },
  ] });
  groups.push({ name: "Good to know", rows: [
    { keys: [], label: "Right-click any file for rename, move, copy, download and permissions" },
    { keys: [], label: "Right-click a folder to download the whole thing as a ZIP" },
    { keys: [], label: "Drag a file onto a folder to move it; drop files onto a folder to upload" },
    { keys: [], label: "Drag a file from the tree INTO an open document to link it",
      hint: "images and video embed; everything else becomes a link you can click" },
    { keys: [], label: "Drop a whole folder from your computer onto a folder — subfolders and all",
      hint: "or right-click the folder → Upload folder" },
    { keys: [], label: "In a terminal, Ctrl shortcuts go to the shell — use " +
        comboLabel("Alt+BracketRight") + ", " + comboLabel("Alt+W") + " or " +
        comboLabel("Mod+Backquote") },
  ] });

  for (const g of groups) {
    const h = document.createElement("div");
    h.className = "keys-group";
    h.textContent = g.name;
    card.appendChild(h);
    for (const r of g.rows) {
      const row = document.createElement("div");
      row.className = "keys-row";
      const kk = document.createElement("span");
      kk.className = "keys-combo";
      r.keys.forEach((c, i) => {
        if (i) { const or = document.createElement("span"); or.className = "keys-or"; or.textContent = "or"; kk.appendChild(or); }
        const b = document.createElement("kbd");
        b.textContent = /^[A-Za-z]…[a-z]$/.test(c) ? c : comboLabel(c);
        kk.appendChild(b);
      });
      const lab = document.createElement("span");
      lab.className = "keys-label";
      lab.textContent = r.label;
      if (r.hint) {
        const hint = document.createElement("span");
        hint.className = "keys-hint";
        hint.textContent = r.hint;
        lab.appendChild(hint);
      }
      row.append(kk, lab);
      card.appendChild(row);
    }
  }
  ov.appendChild(card);
  document.body.appendChild(ov);
  const close = () => { ov.remove(); document.removeEventListener("keydown", onKey, true); };
  const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); } };
  document.addEventListener("keydown", onKey, true);
  x.addEventListener("click", close);
  ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
}

// ---- search box ------------------------------------------------------------
// The topbar field is the visible door to the palette — it never grew its own
// result list, because two ranked lists that disagree is worse than one.
function wireSearch() {
  // A button, not a text field: it opens the palette rather than accepting
  // text. Opening on `focus` would make it untraversable — Tab would land on
  // it and immediately fling a modal at you — so it opens on activation only,
  // which is what a button does anyway (click covers Enter and Space).
  $("#search").addEventListener("click", () => {
    if (_palette) return;
    openPalette("find");
  });
}
function escapeHtml(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

// ---- new doc + upload -----------------------------------------------------
// Alt+N, or "New document" in the palette. (The top bar had a button for this
// once; the tree's "New file here" and the palette cover it without the chrome.)
async function newDocument() {
  const path = await kbPrompt("Path for the new document:", "company/untitled",
                              { title: "New document", ok: "Create", placeholder: "company/notes" });
  if (path) createAndOpen(path.trim());
}
// Prevent the browser from navigating away if a file is dropped OUTSIDE an
// editor (inside one, mediaExtension already handles it). Otherwise the drop
// replaces the whole app with the raw file.
function wireUpload() {
  const ed = $("#panes");
  ed.addEventListener("dragover", (e) => { e.preventDefault(); });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    if (!e.target.closest(".cm-editor")) e.preventDefault();
  });
}

// ---- terminals (VS-Code style: tabbed, dockable bottom panel) -------------
// Each terminal is a live xterm + PTY websocket in its own container. The panel
// is part of the page flow (not an overlay): hiding it gives the space back to
// the editor. Killing the last terminal hides the panel entirely.
let termSeq = 0;
const terms = [];        // {id, name, el, term, fit, ws, sid, rx, retries, …}
let activeTerm = null;
// The backend's protocol version (from /api/cron). Gates the pty features an
// OLD still-running backend doesn't speak: keepalive pings (it would TYPE them
// into the shell) and offset replay (it always resends its whole buffer).
let backendV = 0;
let whoamiUser = "";

function wireTerminal() {
  $("#toggleterm").addEventListener("click", toggleTerminalPanel);
  wireTouchTabDrag();
  for (const b of document.querySelectorAll(".chat-btn, #chats-new")) b.addEventListener("click", () => { closeNav(); openChat(); });

  defineSlot("topbar", $("#topbar-actions") || document.querySelector(".topbar"));
  // (the ＋ / ⤢ / ▾ of the dock, like every group's, are drawn by renderTabBar)
  // Ctrl+` lives in BINDINGS with every other shortcut — see wireShortcuts().
  // Refit whenever the terminal area actually changes size (panel resize,
  // window resize) — a single fit-on-open is not enough.
  // (each group's host is watched by _fitObserver — see finishPane)
  // If a terminal opened before the webfont finished loading, xterm measured
  // fallback glyphs — poke the font option to re-measure, then refit.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      for (const t of terms) t.term.options.fontFamily = termFont();
      refitDisplayedTerminals();
    });
  }
  window.addEventListener("resize", () => { applyDockSide(); refitDisplayedTerminals(); });
  wireTermKeys();
  wireViewport();
  // Keepalive: a quiet terminal sends no bytes for hours, and idle-timeouting
  // proxies (Cloudflare, the hub) drop the socket under it. A small ping every
  // 25s keeps traffic on the wire; the backend swallows it before the shell.
  setInterval(() => {
    if (backendV < 11) return;   // an old backend would type the ping into the shell
    for (const t of terms) {
      if (t.ws && t.ws.readyState === 1) {
        try { t.ws.send('{"ping":1}'); } catch (e) { /* racing close */ }
      }
    }
  }, 25000);
  window.addEventListener("online", retryTermsNow);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) retryTermsNow();
  });
  window.__kbterms = terms;   // test hook
  window.__kbopenview = openView;   // test hook: open any registered view kind
  window.__kbDictTarget = dictationTarget;   // test hook: routing is testable
}

// The on-screen keyboard: some mobile browsers cover the page instead of
// resizing it, then scroll the app off-screen to reveal the focused input —
// the terminal's bottom rows vanish under the keyboard. Pin the app to the
// VISIBLE area ourselves: size <body> to the visualViewport and scroll back to
// origin, so the topbar stays put and the panel ends where the keyboard begins.
function wireViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  let pinned = false;
  const unpin = () => {
    document.body.style.height = "";
    document.querySelectorAll(".pane.maximized").forEach((e) => { e.style.height = ""; });
    const p = $("#terminal-panel");
    if (p) p.style.height = "";
    pinned = false;
  };
  const apply = () => {
    const covered = window.innerHeight - vv.height;   // ~0 when the browser resizes the layout itself
    const panel = $("#terminal-panel");
    // A PINCH shrinks the visible viewport the same way a keyboard does.
    // Pinning the app to it then squeezes the whole page into the zoomed
    // rectangle — the top is cut off, the document bar looms — and the
    // scroll handler below would fight every pan. While the page is
    // zoomed, leave the layout alone and let the browser do its job.
    if (vv.scale > 1.01) {
      if (pinned) unpin();
      return;
    }
    if (covered > 80) {
      document.body.style.height = vv.height + "px";
      // The full-screen terminal is position:fixed, so the body pinning above
      // does nothing for it — size the panel to the VISIBLE viewport directly,
      // and it ends exactly where the keyboard begins (iOS overlays the
      // keyboard instead of resizing the layout; interactive-widget in the
      // meta tag only helps Chrome).
      const maxed = document.querySelector(".pane.maximized");
      if (maxed && isMobile()) {
        // the visible viewport, less the keybar's strip when it is up
        const kb = document.body.classList.contains("kb-term") ? ($("#term-keys").offsetHeight || 0) : 0;
        maxed.style.height = Math.max(120, vv.height - kb) + "px";
      }
      window.scrollTo(0, 0);
      pinned = true;
    } else if (pinned) {
      unpin();
    }
    refitDisplayedTerminals();
  };
  vv.addEventListener("resize", apply);
  vv.addEventListener("scroll", () => { if (pinned && vv.scale <= 1.01) window.scrollTo(0, 0); });
}

// ---- touch keybar: the keys a phone keyboard doesn't have -----------------
// esc / tab / shift+tab / arrows send their escape sequences straight to the
// pty; "ctrl" arms a one-shot modifier applied to the next typed character
// (so ctrl+r, ctrl+d, … work). Buttons preventDefault on pointerdown so the
// soft keyboard never closes while you tap them.
let ctrlArmed = false;
function setCtrlArmed(on) {
  ctrlArmed = on;
  $("#tk-ctrl").classList.toggle("active", on);
}
let _offlineNagAt = 0;
function rawSend(t, s) {
  if (t && t.stopFling) t.stopFling();   // typing lands at the bottom — stop any glide
  if (t && t.ws && t.ws.readyState === 1) {
    t.ws.send(new TextEncoder().encode(s));
    return;
  }
  // typing into a disconnected terminal: say so instead of eating the keys
  if (t && terms.includes(t) && Date.now() - _offlineNagAt > 4000) {
    _offlineNagAt = Date.now();
    kbToast("Not connected — reconnecting; your shell is still running", "err");
    retryTermsNow();
  }
}
function sendData(t, d) {
  if (ctrlArmed) {
    setCtrlArmed(false);
    if (d.length === 1) {
      const c = d.toUpperCase().charCodeAt(0);
      if (c >= 63 && c <= 95) d = String.fromCharCode(c & 0x1f);
    }
  }
  rawSend(t, d);
}
// Touch on the terminal. OUR gestures are pan (scroll), flick (momentum) and
// pinch (text size) — and nothing else. Everything below a small slop must
// reach the browser UNTOUCHED: the tap that focuses, the double-tap that
// selects a word (the browser synthesizes the mouse events xterm's selection
// service listens for), the long-press that Firefox on Android turns into a
// selection. Two regressions taught us the cost of over-claiming:
//
//   • Capturing the pointer on pointerdown retargets those synthesized mouse
//     events to the captured element, so they sailed PAST xterm's listeners
//     inside it — and calling preventDefault on every touchmove told the
//     browser's gesture recognizers to stand down. Tap-to-select died.
//   • But NOT capturing at all loses the pan on a repainting screen: xterm's
//     renderer replaces the row under the finger, and events locked to a
//     detached target go silent — the "swiping does nothing in claude code"
//     bug. (TouchEvents are locked for the whole gesture; pointer events only
//     until the implicit capture clears, which is why these are pointer
//     events.)
//
// So the pointer is captured EXACTLY when a finger crosses the slop — the
// moment the gesture is provably a pan and none of the browser's own gestures
// can still want it. From then on it is ours alone: xterm's own touch handler
// is kept out (it would pan the viewport a second time) and native handling
// is suppressed. A swipe scrolls the scrollback in the normal buffer, and
// becomes wheel reports / arrow keys in the alternate screen, where the app
// scrolls itself — the convention mobile terminals like Termux use.
function wireTouchScroll(t) {
  const SLOP = 10;                        // px of travel before a touch is a pan
  const pts = new Map();                  // live fingers: pointerId -> {x, y}
  let panId = null, panning = false, panPrimed = false;
  let downX = 0, downY = 0, lastY = 0, lastX = 0, acc = 0;
  let vel = 0, velAt = 0;                 // finger velocity (px/ms), for the fling
  let pinchD = null, pinchBase = 0;
  const spread = () => {
    const [a, b] = Array.from(pts.values());
    return Math.hypot(a.x - b.x, a.y - b.y);
  };
  const rowH = () => Math.max(8, t.el.clientHeight / t.term.rows);

  // Momentum: the buffer keeps gliding after the finger leaves, decaying like a
  // native scroll view. Without it a long scrollback is a hundred swipes deep
  // and simply never gets read to the top.
  const stopFling = () => { if (t.fling) { cancelAnimationFrame(t.fling); t.fling = 0; } };
  t.stopFling = stopFling;
  const startFling = (v) => {
    // Scrollback only: a fling in an alternate-screen app would machine-gun
    // wheel reports (or arrow keys) at it long after the finger was lifted.
    if (Math.abs(v) < 0.3 || t.term.buffer.active.type === "alternate") return;
    v = Math.max(-6, Math.min(6, v));     // a bogus timestamp must not launch it into orbit
    let carry = 0, prev = 0;
    const step = (ts) => {
      const dt = prev ? Math.min(50, ts - prev) : 16;
      prev = ts;
      carry += v * dt;
      const h = rowH();
      const lines = Math.trunc(carry / h);
      if (lines) {
        carry -= lines * h;
        const was = t.term.buffer.active.viewportY;
        t.term.scrollLines(lines);
        if (t.term.buffer.active.viewportY === was) { t.fling = 0; return; }  // hit an end
      }
      v *= Math.pow(0.996, dt);           // frame-rate independent decay
      if (Math.abs(v) < 0.02) { t.fling = 0; return; }
      t.fling = requestAnimationFrame(step);
    };
    t.fling = requestAnimationFrame(step);
  };

  // Alternate screen = the app scrolls itself, one report per row — and every
  // report makes claude code repaint its whole transcript. Sent one-per-
  // touchmove those repaints interleave into visible tearing on a phone, so
  // reports are COALESCED: accumulated here and flushed at most every 45 ms,
  // from the spot where the pan started (a report whose coordinates wander
  // with the finger reads as pointer motion to the app). The queue is capped
  // at two screenfuls — an unbounded one would keep replaying a fast swipe
  // long after the finger stopped.
  let altPend = 0, altTimer = 0, altAt = null;
  const flushAlt = () => {
    // A direct call (queueAlt's leading edge, lift's trailing flush) may land
    // while the 45ms timer is still pending — clear it, or the orphan fires
    // later and queueAlt re-arms beside it, doubling the flush cadence.
    if (altTimer) { clearTimeout(altTimer); altTimer = 0; }
    if (!altPend || !terms.includes(t)) return;
    if (t.term.buffer.active.type !== "alternate") { altPend = 0; return; }
    const lines = altPend;
    altPend = 0;
    const n = Math.min(Math.abs(lines), 2 * t.term.rows);
    const modes = t.term.modes || {};
    if (modes.mouseTrackingMode && modes.mouseTrackingMode !== "none") {
      const at = altAt || { col: Math.ceil(t.term.cols / 2), row: Math.ceil(t.term.rows / 2) };
      rawSend(t, `\x1b[<${lines > 0 ? 65 : 64};${at.col};${at.row}M`.repeat(n));
    } else {
      // No mouse support (plain less/vim): cursor keys — the Termux convention.
      const app = modes.applicationCursorKeysMode;
      rawSend(t, ((app ? "\x1bO" : "\x1b[") + (lines > 0 ? "B" : "A")).repeat(n));
    }
  };
  const queueAlt = (lines) => {
    altPend = Math.max(-2 * t.term.rows, Math.min(2 * t.term.rows, altPend + lines));
    if (!altTimer) { flushAlt(); altTimer = setTimeout(flushAlt, 45); }
  };

  // ── Long-press = select ────────────────────────────────────────────────────
  // The phone's missing selection gesture, done OURSELVES through xterm's
  // select() API: hold a finger still, the word under it is selected; drag to
  // extend; lift, and it is copied. Programmatic selection sidesteps the mouse
  // pipeline entirely — which is the whole point: inside a mouse-tracking app
  // (claude code) taps become click reports for the app and xterm's own
  // selection can never fire, so on a phone there was NO way to select there
  // at all. (Native long-press never worked either: the rows are
  // user-select:none.)
  const LP_MS = 420;   // under Chrome Android's ~500 ms contextmenu long-press
  let lpTimer = 0, selecting = false, selA = null, selEndAt = 0;
  const cellAt = (x, y) => {
    const rect = t.el.getBoundingClientRect();
    return {
      col: Math.max(0, Math.min(t.term.cols - 1,
        Math.floor((x - rect.left) / (rect.width / t.term.cols)))),
      row: t.term.buffer.active.viewportY + Math.max(0, Math.min(t.term.rows - 1,
        Math.floor((y - rect.top) / (rect.height / t.term.rows)))),
    };
  };
  const wordAt = (cell) => {
    const line = t.term.buffer.active.getLine(cell.row);
    const s = line ? line.translateToString(true) : "";
    let c = Math.min(cell.col, Math.max(0, s.length - 1));
    if (!s || /\s/.test(s[c] || " ")) return { row: cell.row, c1: cell.col, c2: cell.col };
    let a = c, b = c;
    while (a > 0 && !/\s/.test(s[a - 1])) a--;
    while (b < s.length - 1 && !/\s/.test(s[b + 1])) b++;
    return { row: cell.row, c1: a, c2: b };
  };
  const applySel = (lo, hi) => {          // absolute cell indices, inclusive
    const cols = t.term.cols;
    t.term.select(lo % cols, Math.floor(lo / cols), hi - lo + 1);
  };
  const extendSel = (x, y) => {
    const cols = t.term.cols, head = cellAt(x, y);
    const hp = head.row * cols + head.col;
    const a1 = selA.row * cols + selA.c1, a2 = selA.row * cols + selA.c2;
    // the anchor WORD stays whole; the selection grows from whichever end
    if (hp > a2) applySel(a1, hp);
    else if (hp < a1) applySel(hp, a2);
    else applySel(a1, a2);
  };
  const armLongPress = (e) => {
    clearTimeout(lpTimer);
    const id = e.pointerId;
    lpTimer = setTimeout(() => {
      if (panning || pinchD || selecting || pts.size !== 1 || !pts.has(id)) return;
      const p = pts.get(id);
      selecting = true;
      selA = wordAt(cellAt(p.x, p.y));
      try { t.el.setPointerCapture(id); } catch (err) { /* gone */ }
      applySel(selA.row * t.term.cols + selA.c1, selA.row * t.term.cols + selA.c2);
    }, LP_MS);
  };

  const beginPan = (e) => {
    panning = true;
    clearTimeout(lpTimer);
    // NOW the browser's own gestures are out of the running — claim the
    // pointer so an xterm repaint under the finger cannot end the pan.
    try { t.el.setPointerCapture(e.pointerId); } catch (err) { /* finger already gone */ }
    if (t.term.buffer.active.type === "alternate") {
      const rect = t.el.getBoundingClientRect();
      altAt = {
        col: Math.max(1, Math.min(t.term.cols,
          Math.ceil((e.clientX - rect.left) / (rect.width / t.term.cols)))),
        row: Math.max(1, Math.min(t.term.rows,
          Math.ceil((e.clientY - rect.top) / (rect.height / t.term.rows)))),
      };
    }
  };

  t.el.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "touch") return;   // a mouse drag belongs to xterm's selection
    stopFling();                             // a finger down stops the glide, as everywhere
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 2) {                    // a pinch is never also a pan
      panId = null; panning = false;
      clearTimeout(lpTimer); selecting = false;
      pinchD = spread(); pinchBase = termFontSize();
      // multi-touch synthesizes no mouse events — capturing costs nothing here
      for (const id of pts.keys()) {
        try { t.el.setPointerCapture(id); } catch (err) { /* gone */ }
      }
      return;
    }
    pinchD = null;
    if (pts.size > 2) { panId = null; panning = false; return; }
    panId = e.pointerId; panning = false; panPrimed = false;
    downX = e.clientX; downY = e.clientY;
    lastX = e.clientX; lastY = e.clientY;
    acc = 0; vel = 0; velAt = e.timeStamp;
    selecting = false;
    armLongPress(e);
  });

  t.el.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "touch" || !pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinchD && pts.size === 2) {
      const n = Math.round(pinchBase * spread() / pinchD);
      if (n !== termFontSize()) setTermFontSize(n);
      return;
    }
    if (e.pointerId !== panId) return;
    if (selecting) { extendSel(e.clientX, e.clientY); return; }
    const dy = lastY - e.clientY;           // finger up = positive = later output
    lastX = e.clientX;
    if (!panning) {
      // Below the slop nothing is decided; past it, it is a pan — however long
      // the finger sat first. (No long-press carve-out on purpose: the rows
      // are user-select:none, so there is no native selection-drag to yield
      // to, and a touch-hesitate-then-drag that scrolls nothing is exactly
      // the "swiping sometimes does nothing" feel this file exists to kill.)
      // Slop is DISPLACEMENT from the touch origin, not path length — an hour
      // of hold-jitter must never add up to a pan.
      if (!panPrimed &&
          Math.hypot(e.clientX - downX, e.clientY - downY) < SLOP) {
        lastY = e.clientY; velAt = e.timeStamp;
        return;
      }
      beginPan(e);
    }
    acc += dy;
    lastY = e.clientY;
    // Smoothed, so one stuttery last frame does not decide the whole fling
    vel = vel * 0.4 + (dy / Math.max(1, e.timeStamp - velAt)) * 0.6;
    velAt = e.timeStamp;
    const h = rowH();
    const lines = Math.trunc(acc / h);
    if (!lines) return;
    acc -= lines * h;
    if (t.term.buffer.active.type !== "alternate") t.term.scrollLines(lines);
    else queueAlt(lines);
  });

  const lift = (e) => {
    if (e.pointerType !== "touch" || !pts.has(e.pointerId)) return;
    const wasPinch = !!pinchD;
    pts.delete(e.pointerId);
    if (pts.size < 2) pinchD = null;
    clearTimeout(lpTimer);
    if (selecting && e.pointerId === panId) {
      // Lifting off a long-press selection COPIES it — "when I highlight
      // something, I want it copied". The selection stays painted; the
      // touchend suppression below keeps the browser's synthesized mousedown
      // from immediately clearing it.
      selecting = false; selEndAt = Date.now();
      if (t.term.hasSelection()) copyTermSelection(t);
      panId = null; panning = false;
      return;
    }
    if (e.pointerId === panId) {
      if (panning) {
        flushAlt();                         // don't sit on a queued remainder
        // Let go mid-swipe → glide on. A finger that had already stopped
        // moving (no move for a moment) is a hold, not a flick.
        if (e.timeStamp - velAt < 120) startFling(vel);
      }
      // A sub-slop lift is a TAP (or the end of a long-press): entirely the
      // browser's business. Its synthesized mouse events focus xterm and
      // drive double-tap selection exactly as they did before touch scrolling
      // existed — doing anything here would only break that again.
      panId = null; panning = false;
    }
    // One finger off a pinch: the survivor pans, effective immediately (it was
    // already mid-gesture — making it wait out the slop again reads as dead).
    if (wasPinch && pts.size === 1) {
      const id = Array.from(pts.keys())[0], p = pts.get(id);
      panId = id; panning = false; panPrimed = true;
      lastX = p.x; lastY = p.y;
      acc = 0; vel = 0; velAt = e.timeStamp;
    }
  };
  // On the WINDOW, not t.el: an uncaptured (sub-slop) pointer that slides off
  // the panel delivers its pointerup wherever it ends, and a lift t.el never
  // hears leaves a phantom entry in pts — after which every one-finger swipe
  // counts as half a pinch: scrolling dead, font resizing at random.
  window.addEventListener("pointerup", lift, true);
  window.addEventListener("pointercancel", lift, true);
  t.unwireTouchScroll = () => {           // killTerminal calls this — window
    window.removeEventListener("pointerup", lift, true);      // listeners must
    window.removeEventListener("pointercancel", lift, true);  // not outlive t
    stopFling();
    clearTimeout(lpTimer);
    if (altTimer) { clearTimeout(altTimer); altTimer = 0; altPend = 0; }
  };
  // A long-press ends in browser follow-ups that would undo the selection the
  // instant it was made: SYNTHESIZED mouse events (xterm clears its selection
  // on mousedown — observed eating the selection within a frame of the lift)
  // and the contextmenu. preventDefault on touchend cannot stop them here,
  // because setPointerCapture makes Chrome CANCEL the touch stream (the lift
  // arrives as pointerup + compat mouse events, with no touchend at all) — so
  // the mouse events themselves are swallowed, exactly while a selection
  // gesture is live or just finished. Never for plain taps: those must keep
  // focusing xterm and driving double-tap selection.
  const squelch = (e) => {
    if (selecting || Date.now() - selEndAt < 500) { e.preventDefault(); e.stopPropagation(); }
  };
  t.el.addEventListener("touchend", squelch, { passive: false, capture: true });
  // mousemove is on the list for the mouse-tracking case: a synthesized move
  // becomes a motion report to the app, and xterm treats its own outgoing
  // data as typing — which clears the selection it just made.
  for (const ty of ["mousedown", "mouseup", "mousemove", "click"])
    t.el.addEventListener(ty, squelch, true);
  t.el.addEventListener("contextmenu", (e) => {
    if (selecting || Date.now() - selEndAt < 500) e.preventDefault();
  });

  // xterm's own touch scrolling (bound to .xterm inside us) must never also
  // run — but the browser's NATIVE handling stands down only while a pan or a
  // pinch is actually in progress. preventDefault on every touchmove was what
  // killed long-press selection: gesture recognizers treat a consumed move as
  // "the page owns this".
  t.el.addEventListener("touchmove", (e) => {
    e.stopPropagation();
    if (panning || pinchD || selecting) e.preventDefault();
  }, { passive: false, capture: true });
}

function wireTermKeys() {
  const bar = $("#term-keys");
  bar.addEventListener("pointerdown", (e) => e.preventDefault());
  // The bar is fixed over the terminal, so the terminal must end where the
  // bar begins. Its height is not a constant — the safe-area inset, the
  // font size and the row's own padding all move it — so it is measured and
  // published as --kb-h, and the terminal refits to the room that leaves.
  // (A hardcoded 46px hid the bottom rows of a full-screen app: exactly the
  // lines Claude Code draws its prompt on.)
  const publish = () => {
    const h = bar.offsetHeight || 0;
    const now = h ? h + "px" : "0px";
    if (document.documentElement.style.getPropertyValue("--kb-h") === now) return;
    document.documentElement.style.setProperty("--kb-h", now);
    requestAnimationFrame(refitDisplayedTerminals);
  };
  publish();
  new ResizeObserver(publish).observe(bar);
  window.addEventListener("resize", publish);
  bar.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-k]");
    if (!b || !activeTerm) return;
    const t = activeTerm;
    if (b.dataset.k === "ctrl") { setCtrlArmed(!ctrlArmed); t.term.focus(); return; }
    if (b.dataset.k === "mic") {
      // Focus the terminal FIRST: the whole point of a mic in the keybar is
      // that the route to it never leaves the terminal, unlike the ⋯ menu.
      t.term.focus();
      if (dictationReady()) toggleDictation();
      else kbToast("Dictation isn't available here", "err");
      return;
    }
    if (b.dataset.k === "fminus") { setTermFontSize(termFontSize() - 1); return; }
    if (b.dataset.k === "fplus")  { setTermFontSize(termFontSize() + 1); return; }
    // Ends of the scrollback in one tap. Swiping there is fine for a screenful
    // or two; the top of a long agent run is thousands of rows away.
    if (b.dataset.k === "top" || b.dataset.k === "live") {
      if (t.stopFling) t.stopFling();
      if (t.term.buffer.active.type === "alternate") {
        kbToast("This app draws its own screen — there is no scrollback to jump in", "err");
        return;
      }
      if (b.dataset.k === "top") t.term.scrollToTop(); else t.term.scrollToBottom();
      t.term.focus();
      return;
    }
    if (b.dataset.k === "copy") {
      const sel = t.term.getSelection();
      if (!sel) { kbToast("Nothing selected — long-press a word (drag to extend)", "err"); return; }
      navigator.clipboard.writeText(sel)
        .then(() => kbToast("Copied"))
        .catch(() => kbToast("Clipboard blocked by the browser", "err"));
      t.term.focus();
      return;
    }
    if (b.dataset.k === "paste") {
      navigator.clipboard.readText()
        .then((txt) => { if (txt) t.term.paste(txt); t.term.focus(); })
        .catch(() => kbToast("Paste blocked — long-press the terminal and use the system menu", "err"));
      return;
    }
    // honor application-cursor mode (vim, less, htop want ESC O; shells ESC [)
    const app = t.term.modes && t.term.modes.applicationCursorKeysMode;
    const A = (s) => (app ? "\x1bO" : "\x1b[") + s;
    const seq = { esc: "\x1b", tab: "\t", stab: "\x1b[Z", cc: "\x03",
                  up: A("A"), down: A("B"), right: A("C"), left: A("D") }[b.dataset.k];
    if (seq) rawSend(t, seq);
    t.term.focus();
  });
}

// Mobile terminal modes: FULL (fixed overlay, owns the screen — the default,
// because the phone use case is full attention) or HALF (in-flow, share with
// the doc). The choice is remembered; ⤢ in the header flips it. On desktop
// the class is never set and the panel behaves exactly as before.
// Terminal text size: user-adjustable, remembered, one size for all terminals.
// every open terminal takes the theme's colours, live
function retintTerminals() {
  const th = termTheme(), font = termFont();
  for (const t of terms) if (t.term) { t.term.options.theme = th; t.term.options.fontFamily = font; }
  refitDisplayedTerminals();
}

// Every terminal that is on screen, refit — there can be one per group now.
function refitDisplayedTerminals() {
  for (const t of terms) if (isDisplayed(t)) fitTerm(t);
}

// The terminal the keybar, dictation and __kbterm act on: the one most
// recently focused or activated, wherever its group is.
function noteActiveTerm(t) {
  activeTerm = t;
  window.__kbterm = t && t.term ? t.term : null;   // test hook: the xterm instance
  paintTermLive(t);
}

function termFontSize() {
  const n = parseInt((() => { try { return localStorage.getItem("kbTermFont"); }
                             catch (e) { return null; } })(), 10);
  return Number.isFinite(n) && n >= 9 && n <= 24 ? n : 13;
}
function setTermFontSize(n) {
  n = Math.min(24, Math.max(9, n));
  try { localStorage.setItem("kbTermFont", String(n)); } catch (e) { /* private mode */ }
  for (const t of terms) t.term.options.fontSize = n;
  refitDisplayedTerminals();
  // No toast: the text visibly changing size IS the feedback, and during a
  // pinch this fires many times a second.
}

// The phone's "full screen" terminal is the dock, maximized — the one
// mechanism every group has. Remembered as the phone's preference.
function setTermMax(on, remember) {
  if (remember === undefined) remember = true;
  if (!dockPane) return;
  // The keyboard-pinning in wireViewport() sets an inline height on the fixed
  // group; carrying that into the other mode would freeze it at a stale size.
  dockPane.el.style.height = "";
  if (remember) { try { localStorage.setItem("kbTermMode", on ? "max" : "half"); } catch (e) { /* private mode */ } }
  if (on && !dockPane.el.hidden) { if (maximizedPaneId !== dockPane.id) setMaximized(dockPane.id); }
  else if (!on && maximizedPaneId === dockPane.id) setMaximized(null);
  requestAnimationFrame(refitDisplayedTerminals);
}
function preferredTermMax() {
  // A landscape phone has no room for a document above a half-height
  // terminal: the sheet goes full, and the remembered mode is left alone.
  if (window.matchMedia("(pointer: coarse) and (max-height: 500px)").matches) return true;
  try { return (localStorage.getItem("kbTermMode") || "max") === "max"; }
  catch (e) { return true; }
}

// Ctrl+` and the Terminal button. The dock has tabs: show it (and put the
// keyboard in its terminal) or hide it. The dock is empty but a terminal
// lives in some other group: go to that terminal — never spawn a second one
// under a user who dragged their only shell to the right and now wants it
// back. No terminal anywhere: a fresh one in the dock, as always.
function toggleTerminalPanel() {
  if (!canShell) return;
  if (isPhone()) { phoneTerminal(); return; }
  if (maximizedPaneId && maximizedPaneId !== dockPane.id) {
    // a group is maximized: Ctrl+` means "show me the terminal" — the
    // layout comes back, and the open dock is simply there again
    setMaximized(null);
    if (!dockHidden()) { activateTab(dockPane.active || paneTabs(dockPane)[0]); return; }
  }
  if (dockHidden()) {
    const inDock = paneTabs(dockPane);
    if (!inDock.length) {
      const elsewhere = terms.filter((t) => !isDock(paneOf(t)));
      if (elsewhere.length) {
        const t = elsewhere.includes(activeTerm) ? activeTerm : elsewhere[elsewhere.length - 1];
        if (paneOf(t).collapsed) expandPane(paneOf(t));
        activateTab(t);
        return;
      }
      newTerminal();
      return;
    }
    showDock();
    activateTab(dockPane.active || inDock[0]);
    saveSession();
  } else {
    hideTerminalPanel();
  }
}

// The phone's terminal: a tab beside your documents. Asking for one goes to
// the terminal you already have (bringing it up out of the panel if a
// desktop session left it there), or opens a new one in the group you are
// looking at.
function phoneTerminal() {
  const here = terms.filter((t) => !isDock(paneOf(t)));
  if (here.length) {
    const t = here.includes(activeTerm) ? activeTerm : here[here.length - 1];
    if (paneOf(t).collapsed) expandPane(paneOf(t));
    activateTab(t);
    return;
  }
  const inPanel = paneTabs(dockPane);
  if (inPanel.length) { liftPanelOntoPhone(); return; }
  newTerminal();
}

// A layout made on a desktop, opened on a phone: whatever sits in the panel
// joins the workspace, and the panel goes away.
function liftPanelOntoPhone() {
  const inPanel = paneTabs(dockPane);
  if (!inPanel.length) return;
  const target = panes.find((p) => !p.collapsed) || panes[0];
  for (const t of inPanel) moveTabToPane(t, target, paneTabs(target).length);
  hideTerminalPanel(true);
  activateTab(inPanel[inPanel.length - 1]);
}

// Un-collapse the dock — every door into it honours the mobile mode and gets
// the drawer out of the way (a full-screen terminal covers the ☰).
function showDock() {
  if (!dockHidden()) return;
  expandPane(dockPane);
}

// Collapse the dock. `quiet` skips the handover of `active`, for callers that
// are in the middle of moving things themselves.
function hideTerminalPanel(quiet) {
  // the dock folds like any group; empty, it is simply out of the way
  if (!paneTabs(dockPane).length) { dockPane.collapsed = true; dockPane.el.hidden = true; if (maximizedPaneId === dockPane.id) { maximizedPaneId = null; applyMaximize(); } normalizeSplits(); renderTabBar(); if (!quiet) saveSession(); return; }
  collapsePane(dockPane, quiet);
}

function openTermWith(cmd) {
  if (!canShell) { kbToast("This account has no terminal access", "err"); return; }
  closeNav();
  newTerminal(cmd);
}

function renderTermTabs() { renderTabBar(); }   // the dock strip is a tab strip like the others

// The modifier that forces a LOCAL text selection when a mouse-tracking app
// (claude code, htop, vim) owns the mouse: xterm hard-codes Shift on Linux/
// Windows and Option (Alt) on Mac — and only if macOptionClickForcesSelection
// is on, which we enable below.
const SELECT_MODIFIER = IS_APPLE ? "⌥ Option" : "Shift";

function copyTermSelection(t, quiet) {
  const s = t.term.getSelection();
  if (!s) return false;
  if (!(navigator.clipboard && navigator.clipboard.writeText)) {
    kbToast("Clipboard needs an https:// connection", "err");
    return false;
  }
  navigator.clipboard.writeText(s).then(
    () => { if (!quiet) kbToast("Copied", "ok"); },
    () => kbToast("The browser blocked clipboard access", "err"));
  return true;
}

// Copy from the terminal the way people expect it to work:
//   • Select text and it is copied immediately (the "select = copy" convention).
//     A normal drag selects in a shell; in a mouse-tracking app (claude code,
//     htop) hold the SELECT_MODIFIER while dragging — Option on Mac, Shift
//     elsewhere — since the app otherwise owns the mouse.
//   • Ctrl+C copies when something is selected, and interrupts otherwise (as a
//     terminal always has); Ctrl+Shift+C always copies; Ctrl+V and Ctrl+Shift+V
//     both paste (⌘V on a Mac, where Ctrl+V stays the shell's).
function wireTermClipboard(t) {
  // OSC 52: the escape sequence a program inside the terminal uses to put text
  // on the system clipboard (claude code's "copy", vim/tmux yank, etc.). xterm
  // ignores it by default, so claude code's copy silently went nowhere — here we
  // honor the WRITE half (a program setting your clipboard) and deliberately
  // DROP the read half (`?`), so nothing running in the shell can exfiltrate
  // what's already on your clipboard.
  t.term.parser.registerOscHandler(52, (data) => {
    const semi = data.indexOf(";");
    const payload = semi < 0 ? data : data.slice(semi + 1);
    if (!payload || payload === "?") return true;   // read query — refused on purpose
    if (!(navigator.clipboard && navigator.clipboard.writeText)) {
      kbToast("Clipboard needs an https:// connection", "err");
      return true;
    }
    try {
      const bin = atob(payload);
      const text = new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
      navigator.clipboard.writeText(text).then(
        () => kbToast("Copied", "ok"),
        () => kbToast("The browser blocked clipboard access — hold "
                      + SELECT_MODIFIER + " and drag to select instead", "err"));
    } catch (e) { /* malformed base64 — ignore */ }
    return true;
  });
  // copy as soon as a drag-selection finishes — no extra keypress needed
  t.el.addEventListener("mouseup", () => {
    if (t.term.hasSelection()) copyTermSelection(t);
  });
  t.term.attachCustomKeyEventHandler((ev) => {
    if (ev.type !== "keydown") return true;
    // App navigation the terminal is allowed to give up (Alt+…, Ctrl+`).
    // Everything else — Ctrl+K, Ctrl+P, Ctrl+R — belongs to the shell, so
    // returning true here keeps readline and full-screen apps intact.
    if (bindingFor(ev, true)) return false;
    if (!ev.ctrlKey || ev.altKey) return true;
    // `return false` makes xterm bail out early — WITHOUT its usual
    // preventDefault — so each branch decides for itself whether the browser
    // still gets to act on the key.
    if (ev.code === "KeyC" && ev.shiftKey) { ev.preventDefault(); copyTermSelection(t); return false; }
    if (ev.code === "KeyC" && t.term.hasSelection()) {
      ev.preventDefault(); copyTermSelection(t); t.term.clearSelection(); return false;
    }
    // Ctrl+V / Ctrl+Shift+V: hand the paste straight back to the browser — bail
    // out of xterm (so it doesn't send ^V) but leave the default action alone.
    // The browser's own paste event carries the clipboard text with it, and
    // xterm already listens for that event and turns it into a bracketed paste.
    //
    // Plain Ctrl+V pastes as well, the way Windows Terminal and VS Code's
    // terminal do it. The cost is that ^V itself can no longer be typed —
    // readline's quoted-insert, page-down in nano/emacs — which is the trade
    // those terminals make too. On a Mac Ctrl+V is not a paste shortcut at all,
    // so there it still goes to the shell and ⌘V pastes natively.
    //
    // Reading the clipboard ourselves (navigator.clipboard.readText) was the
    // bug, and it broke both ways: for text copied in ANOTHER app Chrome
    // requires permission and pops its little "Paste" confirmation chip, so
    // nothing ever arrived; and for text copied inside the page — where no
    // permission is needed — the text landed TWICE, because Chrome ran its own
    // paste-as-plain-text regardless of the preventDefault we had here. One
    // paste path is the only way to be sure there is exactly one paste.
    if (ev.code === "KeyV" && (ev.shiftKey || !IS_APPLE)) return false;
    return true;
  });
}

// Open a shell — or REATTACH to one: each terminal has a session id, and the
// backend keeps the shell (plus recent output) alive across disconnects, so a
// page refresh comes back to the same running shell. A dropped connection is
// NOT a dead shell: the socket reconnects on its own (backoff, plus instant
// retries when the network returns or the tab becomes visible again) and
// replays only the bytes this client missed. The tab retires only when the
// server says the shell actually ended. With `cmd`, type that command into a
// fresh shell once the prompt has painted (launcher buttons).
// The terminal's code is a separate chunk (src/term.js): a quarter of the
// bundle that a viewer account never needs and nobody needs to read a
// document. Fetched once, the first time a terminal is wanted — or at t=0 by
// a restore that already knows it will want one — and cached immutable.
let _termMod = null;
function warmTerminal() {
  if (!_termMod) _termMod = import("./term.js").catch((e) => { _termMod = null; throw e; });
  return _termMod;
}

// A new terminal: a tab of kind "term", in the dock unless a group is named.
// Returns the tab (which is also the terminal record), or null.
async function newTerminal(cmd, sid, savedName, paneId) {
  return openView("term", { cmd, sid, name: savedName }, paneId);
}

function randomSid() {
  return Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");
}

// A restored terminal at t=0: its tab and "⟳ name" row are on screen at once;
// the shell behind it is attached in restoreRest, once whoami has answered.
function pendingTerminal(p, spec) {
  const el = document.createElement("div");
  el.className = "tab-content term term-content connecting";
  p.hostEl.appendChild(el);
  const t = { id: ++tabSeq, kind: "term", path: null, name: spec.name || "bash", el, paneId: p.id,
              sid: spec.sid, pending: true, connected: false, term: null, ws: null, exited: false };
  tabs.push(t);
  if (!p.active) p.active = t;
  return t;
}

// Put a live xterm into a terminal tab's element and connect it — a new
// terminal, or a restored one that sat as a placeholder until now. Resolves
// true when the terminal is running, false when the chunk could not load.
async function attachTerminal(t, cmd) {
  let xterm;
  try { xterm = await warmTerminal(); }
  catch (e) {
    // The chunk is a hashed file beside the bundle and a deploy replaces it.
    // A page from before the deploy asking for its first terminal finds
    // nothing at the old name — the honest answer is a reload, not a blank
    // panel that looks like a broken terminal.
    kbToast("A new version was deployed — reload the page to open a terminal", "err");
    return false;
  }
  if (!tabs.includes(t) || t.term) return !!t.term;
  const { Terminal, FitAddon } = xterm;
  // Render size is the user's choice (A−/A+ in the keybar, persisted). The
  // iOS no-focus-zoom constraint (16px minimum on the FOCUSED element) is
  // satisfied in CSS instead, by pinning xterm's hidden textarea to 16px —
  // the glyphs on screen are free to be any size.
  const term = new Terminal({
    fontSize: termFontSize(),
    fontFamily: termFont(), theme: termTheme(),
                              // deep enough to still hold the start of a long
                              // agent run: a phone terminal is ~48 columns, so
                              // output wraps to several rows per printed line
                              scrollback: 20000,
                              // let Mac users Option+drag to select inside a
                              // mouse-tracking app (claude code) — otherwise no
                              // modifier can force a selection there on macOS
                              macOptionClickForcesSelection: true });
  const fit = new FitAddon();
  term.loadAddon(fit);
  term.open(t.el);
  Object.assign(t, { term, fit, ws: null,
                     rx: 0,             // bytes received, in the session's coordinates
                     retries: 0, reTimer: null, exited: false, connected: true, bn: null,
                     cmd, cmdSent: !cmd, pending: false });
  t.el.classList.remove("connecting");
  wireTouchScroll(t);
  wireTermClipboard(t);
  connectTerm(t);
  term.onData((d) => sendData(t, d));
  term.onScroll(() => { if (t === activeTerm) paintTermLive(t); });
  terms.push(t);
  return true;
}

registerView("term", {
  label: "Terminal",
  icon: () => I.term,
  title: (t) => (t.connected === false ? "⟳ " : "") + t.name,
  tooltip: (t) => (t.exited ? "exited — " : "") + t.name + (t.pending ? " (connecting)" : ""),
  key: () => null,          // every terminal is its own thing
  contentClass: "term term-content",
  placement: { target: "dock" },
  init: (t, spec) => {
    t.path = null;
    t.sid = spec.sid || randomSid();
    t.name = spec.name ||
      (spec.cmd ? spec.cmd.trim().split(/\s+/)[0].slice(0, 14) : "bash " + (++termSeq));
    t.pending = true; t.connected = false; t.term = null; t.ws = null; t.exited = false;
  },
  restore: (spec) => (typeof spec.sid === "string" && spec.sid
    ? { kind: "term", sid: spec.sid, name: typeof spec.name === "string" ? spec.name : undefined } : null),
  serialize: (t) => ({ kind: "term", sid: t.sid, name: t.name }),
  open: async (t, spec) => {
    if (isDock(paneOf(t))) showDock();
    const ok = await attachTerminal(t, spec.cmd);
    if (!ok) { const e = new Error("no terminal chunk"); e.quiet = true; throw e; }
    activateTerm(t, true);
    saveSession();
  },
  close: () => { /* a live terminal goes through killTerminal; a placeholder has nothing to end */ },
  focus: (t) => { if (t.term) t.term.focus(); },
  resize: (t) => fitTerm(t),
});

function connectTerm(t) {
  if (t.ws && t.ws.readyState <= 1) return;   // already connecting/connected
  if (t.reTimer) { clearTimeout(t.reTimer); t.reTimer = null; }
  // an old backend (v<11) replays its whole buffer on reattach — start from a
  // clean screen so nothing shows up twice
  if (backendV < 11 && t.rx) { t.term.reset(); t.rx = 0; }
  // The FIRST connect may create the shell (that's how a reload after a backend
  // restart gets your layout back). Every RECONNECT is attach-only: if the
  // session is gone the shell really ended, and silently handing over a fresh
  // one under the same tab name would hide that.
  const ws = new WebSocket(wsBase() + "/pty?session=" + t.sid + "&have=" + t.rx +
                           (t.everConnected ? "&create=0" : ""));
  ws.binaryType = "arraybuffer";
  t.ws = ws;
  // every handler checks it still speaks for t.ws — a replaced socket's late
  // events (a CLOSING one finally closing) must not touch the terminal
  ws.onopen = () => {
    if (t.ws !== ws) return;
    t.retries = 0; t.everConnected = true; setTermConn(t, true); sendResize(t);
  };
  ws.onmessage = (ev) => {
    if (t.ws !== ws) return;
    if (typeof ev.data === "string") { termCtl(t, ev.data); return; }
    t.rx += ev.data.byteLength;
    t.term.write(new Uint8Array(ev.data));
    if (!t.cmdSent) {   // first output = the prompt is up; keys sent earlier can be lost
      t.cmdSent = true;
      setTimeout(() => rawSend(t, t.cmd + "\n"), 150);
    }
  };
  // The server says explicitly when the shell ENDED ({"exit"} → t.exited). Any
  // other close is a dropped connection — the shell is still running server-
  // side, so keep the tab and quietly work on getting the session back.
  ws.onclose = async () => {
    if (t.ws !== ws || !terms.includes(t)) return;
    if (t.exited) { killTerminal(t); return; }
    setTermConn(t, false);
    // An OLD backend (v10) closes the socket without ever saying why — a shell
    // that exited and a dropped network look identical there, and reattaching
    // would FORK A NEW SHELL every time. So against v10 keep the old rule: a
    // close retires the tab. Re-probe each time, since a backend can be
    // upgraded (or first reached) long after this page booted.
    const v = await refreshBackendV();
    if (v && v < 11) { killTerminal(t); return; }
    if (t.ws !== ws || !terms.includes(t) || t.exited) return;   // changed while probing
    const delay = Math.min(15000, 1000 * Math.pow(2, Math.min(t.retries++, 4)));
    t.reTimer = setTimeout(() => { if (terms.includes(t)) connectTerm(t); }, delay);
  };
}

// The backend's pty protocol version, re-checked (briefly cached) whenever a
// decision depends on it. A failed probe keeps the last known value — a backend
// that is merely restarting must not look like an old one.
let _bvAt = 0;
async function refreshBackendV() {
  if (backendV && Date.now() - _bvAt < 5000) return backendV;
  try {
    const j = await (await fetch("/api/cron")).json();
    if (j && typeof j.v === "number") { backendV = j.v; _bvAt = Date.now(); }
  } catch (e) { /* backend down mid-restart — keep what we knew */ }
  return backendV;
}

// Control frames from the backend (TEXT frames; pty output is BINARY).
function termCtl(t, raw) {
  let m;
  try { m = JSON.parse(raw); } catch (e) { return; }
  if (m.reset) {            // replay restarts from the oldest byte the server holds
    t.term.reset();
    t.rx = m.base || 0;
  } else if (m.exit) {      // the shell really ended — the close that follows retires the tab
    t.exited = true;
  } else if (m.gone) {      // reattach found nothing: it ended while we were away
    t.exited = true;
    kbToast('Terminal "' + t.name + '" ended while it was disconnected');
  } else if (m.detached) {  // another window took this session over — let it go
    t.exited = true;
    kbToast('Terminal "' + t.name + '" is now attached in another window');
  } else if (m.error) {
    t.exited = true;
    kbToast(m.error, "err");
  }
}

function setTermConn(t, on) {
  if (t.connected === on) return;
  t.connected = on;
  t.el.classList.toggle("term-offline", !on);
  if (!on && !t.bn) {
    t.bn = document.createElement("div");
    t.bn.className = "term-reconnect";
    t.bn.textContent = "reconnecting… (click to retry)";
    t.bn.title = "The shell is still running on the server; this tab is "
      + "getting the connection back. Click to retry now.";
    t.bn.addEventListener("click", retryTermsNow);
    t.el.appendChild(t.bn);
  } else if (on && t.bn) { t.bn.remove(); t.bn = null; }
  renderTermTabs();
}

// The instant the world comes back (network up, tab visible again), skip the
// backoff and reattach every disconnected terminal right away.
function retryTermsNow() {
  for (const t of terms) {
    if (t.exited || (t.ws && t.ws.readyState <= 1)) continue;
    if (t.reTimer) { clearTimeout(t.reTimer); t.reTimer = null; }
    t.retries = 0;
    connectTerm(t);
  }
}

// The ⤓ key lights up whenever the view sits above the live output: on a phone
// there is no scrollbar to say so, and a terminal that is merely scrolled up
// otherwise reads as a terminal that has stopped producing anything.
function paintTermLive(t) {
  const b = $("#tk-live");
  if (!b) return;
  const buf = t && t.term && t.term.buffer.active;
  b.classList.toggle("away", !!buf && buf.type === "normal" && buf.viewportY < buf.baseY);
}

function activateTerm(t, focus) {
  const p = paneOf(t);
  p.active = t;
  focusedPaneId = p.id;
  noteActiveTerm(t);
  showEachPanesTab();
  renderTabBar();
  saveSession();   // which terminal is showing is part of "where you left off"
  // When the activation wasn't user-initiated (a background shell exited and a
  // neighbour got promoted), only take focus if it was already in the group —
  // never yank the user out of the editor.
  if (focus === undefined) {
    focus = p.el.contains(document.activeElement) ||
      document.activeElement === document.body;
  }
  // Fit only AFTER layout settles, or FitAddon measures a pre-constraint height.
  requestAnimationFrame(() => { fitTerm(t); if (focus && t.term) t.term.focus(); });
}

function killTerminal(t, fromPointer) {
  const i = terms.indexOf(t);
  if (i < 0) { dropTab(t); return; }
  const p = paneOf(t);
  if (fromPointer) lockTabStrip(p.barEl); else unlockTabStrip(p.barEl);
  const inPane = paneTabs(p);
  const j = inPane.indexOf(t);
  terms.splice(i, 1);
  tabs.splice(tabs.indexOf(t), 1);
  if (t.unwireTouchScroll) t.unwireTouchScroll();   // window listeners + fling + timers
  t.ws.onclose = null;
  if (t.reTimer) { clearTimeout(t.reTimer); t.reTimer = null; }
  // explicit kill: end the SHELL, not just the connection (a plain close is a
  // detach — the session would keep running for reattachment)
  if (t.ws.readyState === 1) {
    try { t.ws.send(JSON.stringify({ kill: true })); } catch (e) { /* racing close */ }
  } else if (!t.exited) {
    // Killed while disconnected: the shell may still be running server-side —
    // reach the session once more just to end it. create=0 is essential: with
    // it, a session that is already gone stays gone (without it this would
    // FORK a fresh shell purely to kill it — and at the cap, evict someone
    // else's detached session to make room for it).
    try {
      const w = new WebSocket(wsBase() + "/pty?session=" + t.sid + "&have=0&create=0");
      w.onopen = () => { try { w.send(JSON.stringify({ kill: true })); } catch (e) { /* ok */ } };
      setTimeout(() => { try { w.close(); } catch (e) { /* ok */ } }, 8000);
    } catch (e) { /* offline — nothing to reach */ }
  }
  try { t.ws.close(); } catch (e) { /* already closed */ }
  const hadFocus = p.el.contains(document.activeElement) || document.activeElement === document.body;
  t.term.dispose();
  t.el.remove();
  // The tab you get next is this group's neighbour, not some other column's.
  const rest = paneTabs(p);
  if (p.active === t) p.active = rest[j] || rest[j - 1] || null;
  if (activeTerm === t) {
    activeTerm = null; window.__kbterm = null;
    const next = (p.active && p.active.kind === "term") ? p.active : terms.find(isDisplayed) || terms[terms.length - 1] || null;
    if (next) noteActiveTerm(next);
  }
  if (!rest.length) paneEmptied(p);
  if (p.active && p.active.kind === "term" && paneById(p.id)) activateTerm(p.active, hadFocus);
  else if (p.active && paneById(p.id) && hadFocus && viewKind(p.active.kind).isActiveDocument) activateTab(p.active);
  else { showEachPanesTab(); renderTabBar(); }
  refocus();
  saveSession();
}

function fitTerm(t) {
  if (!t || !t.term || !isDisplayed(t)) return;
  try { t.fit.fit(); } catch (e) { /* container not laid out yet */ }
  sendResize(t);
}

function sendResize(t) {
  if (!t || !t.ws || t.ws.readyState !== 1) return;
  t.ws.send(JSON.stringify({ resize: { rows: t.term.rows, cols: t.term.cols } }));
}

// Drag the panel's top edge to resize it, like the VS-Code panel divider.
// Pointer events (with capture) cover mouse, touch and pen with one handler.
// (the dock is resized by the split handle applyDockSide() makes)

// ---- session expiry -------------------------------------------------------

// The session cookie lives 12h (common.SESSION_TTL). Close the laptop over a
// weekend and it is gone — but the tab still shows a fully painted app, and
// every call quietly 401s: the tree poll swallows the error, saves do nothing,
// search returns nothing. It LOOKS alive and is not, and the only way out was
// to know to hit reload.
//
// One wrapper around fetch is enough, because every call the app makes goes
// through it. The 4s tree poll means an expiry is noticed within seconds even
// if you touch nothing, and the visibilitychange nudge below makes it
// immediate when you come back to the tab.
let _sessionGone = false;

function installSessionGuard() {
  const raw = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const r = await raw(...args);
    if (r.status === 401) sessionExpired();
    return r;
  };
}

function sessionExpired() {
  if (_sessionGone) return;   // many in-flight calls fail at once; bounce once
  _sessionGone = true;
  // replace(), not href: the dead app must not sit in the back-stack waiting
  // to be returned to. Documents are CRDT-synced continuously, so there is no
  // unsaved editor state to lose here.
  // Carry the document you were on through the login, exactly as the hub does
  // for an unauthenticated deep link (hub.deep_link) — expiring mid-session
  // used to drop you back on "/" and lose the file you had open.
  const here = location.pathname;
  const back = pathFromUrl() ? "?next=" + encodeURIComponent(here) : "";
  location.replace("/login" + back);
}

// ---- boot -----------------------------------------------------------------
async function boot() {
  // Before anything else, so even the boot requests below are covered.
  installSessionGuard();
  insertPane(0);   // there is always at least one editor pane
  adoptDock();     // and the terminal panel is a group like it
  applyDockSide();
  seatDocChrome(panes[0]);   // the document bar and the formatting dock start in it
  // Safety net: a drag cancelled with Escape, or dropped on the desktop, still
  // ends — and the pane drop overlays (which sit on top of the editor) must
  // come back down even when no drop handler of ours ever ran.
  document.addEventListener("dragend", endTabDrag);
  // Coming back to a tab that has been asleep: check immediately instead of
  // waiting up to 4s for the next poll (background tabs are throttled to about
  // once a minute, so the poll alone can feel slow at exactly the moment you
  // are looking at it).
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !_sessionGone) { settings.fetch(); loadChats(); }   // the tree: events.js wake()
  });
  // Everything the first paint needs leaves NOW, together. The tree is the
  // slow one — a permission-checked walk of the whole repo — and it used to
  // sit, awaited, in front of restoring your tabs and terminals, which need
  // nothing but localStorage and never read it. That wait is what put the tab
  // bar and the terminal panel on screen two seconds late, shoving the editor
  // around under a page that already looked ready. Whoami IS awaited: the
  // restore needs canShell, and a viewer account must never try to open a pty.
  const treeP = fetchTree().catch(() => null);
  const deepLinksP = fetch("/company", { method: "HEAD" }).then((r) => r.ok, () => false);
  const whoamiP = loadWhoami();
  const settingsP = settings.fetch();      // same burst; awaited with whoami below
  loadChats();                             // the sidebar's recent chats, whenever they land
  // Read the pasted URL BEFORE restoring: restoring activates every tab it
  // reopens, and activateTab → syncUrl() replaceState()s the address bar onto
  // that tab — so reading location afterwards yields the RESTORED path, not the
  // link someone sent you. That is what made a shared link land on whatever
  // document happened to be open last time.
  const deep = pathFromUrl();
  // Tabs come back NOW, before we even know who you are (see restoreTabs).
  const restoring = restoreTabs();
  await whoamiP; await settingsP;
  wireSearch(); wireUpload(); wireTerminal(); wireMdBar(); wireNav();
  wireTabStrip();
  wireShortcuts(); wireTreeKeys(); wireTreeTooltips(); wireSidebarResize();
  // null-guarded: a browser holding a cached older app.html must not lose the
  // whole boot sequence over one missing button
  // icon + label markup lives in app.html now
  const keysBtn = $("#keys-btn");
  if (keysBtn) keysBtn.addEventListener("click", openShortcuts);
  const settingsBtn = $("#settings-btn");
  if (settingsBtn) settingsBtn.addEventListener("click", () => openSettings());
  const pinsBtn = $("#pins-btn");
  if (pinsBtn) pinsBtn.addEventListener("click", showLaunchersModal);
  const trashBtn = $("#trash-btn");
  if (trashBtn) trashBtn.addEventListener("click", openTrash);
  const inboxBtn = $("#inbox-btn");
  if (inboxBtn) inboxBtn.addEventListener("click", openInbox);
  loadTrash();
  loadInbox();
  // the inbox is a file somebody else writes: look again when the window comes
  // back, and on the heartbeat the event stream already sends
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadInbox(); });
  setInterval(loadInbox, 120000);
  // Dictation. Hidden outright where it cannot work (no MediaRecorder, or an
  // insecure context — getUserMedia needs https or localhost), which also makes
  // `when: dictationReady` false and hands F9 back to the shell.
  // The mic lives in the formatting dock (and the terminal keybar has its own);
  // the dock's is dictation's button: press-and-hold, and the recording state.
  const micBtn = document.querySelector('#mdbar button[data-md="mic"]');
  const dhBtn = $("#dict-hist-btn");   // null-guarded: cached older app.html
  if (micBtn && dictationReady()) {
    initDictation({ resolveTarget: dictationTarget, insert: insertDictation,
                    toast: kbToast, button: micBtn });
    if (dhBtn) dhBtn.addEventListener("click", openDictHistory);
  } else {
    if (micBtn) micBtn.hidden = true;
    if (dhBtn) dhBtn.hidden = true;    // no mic here → the history would stay empty
  }
  const ht = $("#hidden-toggle");
  ht.classList.toggle("on", showHidden);
  ht.addEventListener("click", () => {
    showHidden = !showHidden;
    try { localStorage.setItem("kbShowHidden", showHidden ? "1" : "0"); } catch (e) { /* ok */ }
    ht.classList.toggle("on", showHidden);
    rerenderTree();
  });
  $("#tree-fold").addEventListener("click", toggleFoldAll);
  $("#doc-history").innerHTML = I.history;
  $("#doc-history").addEventListener("click", () => { if (active) openHistory(active.path); });
  // a physical keyboard is worth advertising the shortcut to; a phone is not
  if (!window.matchMedia("(hover: none)").matches) {
    const kbd = $("#search-kbd");
    if (kbd) { kbd.textContent = comboLabel("Mod+K"); kbd.hidden = false; }
  }
  $("#cron-btn").addEventListener("click", openCron);
  // Show the Admin entry (in the user menu) to platform admins (sudo group) —
  // and, network-section only, to users delegated write access on
  // .os/egress.json. Whether it appears is not something the first paint waits
  // two round trips for.
  (async () => {
    try {
      const me = await (await fetch("/admin/me")).json();
      let show = false;
      if (me.admin) { isAdmin = true; show = true; }
      else {
        try { show = !!(await (await fetch("/admin/egress")).json()).can_edit; }
        catch (e) { /* not delegated */ }
      }
      if (show) {
        const b = $("#admin-btn");
        b.hidden = false;
        if (!isAdmin) b.querySelector(".btn-label").textContent = "Network";
        b.addEventListener("click", openAdmin);
      }
    } catch (e) { /* not admin */ }
    // The Pinned rows render with isAdmin in hand — after the answer, so they
    // paint once and right, rather than as a non-admin's until the 30 s poll.
    loadLaunchers();
  })();
  // A backend from before whoami carried `v` still answers it on /api/cron;
  // fire-and-forget, terminals opened before the answer use the safe fallback.
  if (canShell && !backendV) {
    fetch("/api/cron").then((r) => r.json())
      .then((j) => { backendV = j.v || 0; }).catch(() => { /* old backend */ });
  }
  // Terminals now that whoami has answered and the terminal UI is wired; then
  // the tabs' contents. The tree paints whenever it lands — restoreTreeState()
  // ran inside loadWhoami, so the folders are open and closed the way you left
  // them on this first paint, not on a second.
  const restoreP = restoreRest(restoring);
  applyTree(await treeP, false);
  await restoreP;
  // activateTab reveals the active file in the tree — a no-op while the tree
  // was still on its way, so do it once now that both are here.
  if (active && !isMobile() && !document.body.classList.contains("nav-hidden"))
    revealActiveInTree(false);
  _deepLinksOk = await deepLinksP;   // long resolved; awaited only where it matters
  // deep link: a shared /company/….md URL wins over the restored active tab
  if (deep) await openDeepLink(deep);
  // On a phone the file list is the home screen — but only when there is no
  // home to come back to. Restoring a document or a terminal means the drawer
  // would open ON TOP of it, which is decided here, after the restore, rather
  // than opening it early and hoping something closes it again.
  if (isMobile() && !tabs.length && $("#terminal-panel").hidden)
    document.body.classList.add("nav-open");
  syncUrl(true);   // boot never adds a history entry, it just settles the URL
  _settling = false;
  window.addEventListener("popstate", () => {
    const p = pathFromUrl();
    if (p) openDeepLink(p);
  });
  // the context menu follows the pointer, and dies with it
  document.addEventListener("click", closeCtxMenu);
  // A right-click anywhere closes the menu — except on something that opens
  // one of its own (it has just opened it).
  document.addEventListener("contextmenu", (e) => {
    if (!e.target.closest(".tree-item, .chat-row, .lchip, .pin-row")) closeCtxMenu();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCtxMenu(); }, true);
  // Scrolling the list under an open menu closes it — but NOT the scroll the
  // opening click itself caused: right-clicking a row moves the tree cursor,
  // which scrolls the row into view, which used to shut the menu before it
  // was ever seen (three context-menu tests, 2026-09-21).
  (document.querySelector(".sb-scroll") || $("#tree")).addEventListener("scroll", () => {
    if (performance.now() - _ctxOpenedAt > 350) closeCtxMenu();
  }, true);
  window.addEventListener("blur", closeCtxMenu);
  syncTestHooks();
  window.__kbrerender = rerenderTree;   // test hook: force a tree repaint
  // Live filesystem, presence and config arrive over one event stream (no
  // polling): a new or newly shared file appears within ~2 s, a colleague's
  // cursor within ~3 s, an admin's launcher button as soon as it is saved.
  connectEvents({
    hello: (d) => {
      // the catch-up: the stream's first event says where the server is now
      if (_treeEtag && d.etag && d.etag !== _treeEtag) refreshTreeWhenIdle();
      if (d.presence) {
        _treePresence = d.presence; updateTreePresence();
        if (_presencePoll) { clearInterval(_presencePoll); _presencePoll = null; }
      } else if (!_presencePoll) {
        // an older syncd cannot feed presence into the stream: poll it, slowly
        _presencePoll = setInterval(() => { if (!document.hidden) loadPresence(); }, 10000);
      }
    },
    tree: (d) => { if (d.full) refreshTreeWhenIdle(); else patchTreeMtimes(d.changed || [], d.etag); },
    presence: (d) => {
      _treePresence = d.presence || {}; updateTreePresence();
      if (_presencePoll) { clearInterval(_presencePoll); _presencePoll = null; }
    },
    config: () => { loadLaunchers(); settings.fetch(); },
    catchUp: () => { refreshTreeWhenIdle(); loadPresence(); },
    poll: () => { refreshTreeWhenIdle(); loadPresence(); },
    probe: () => fetch("/api/whoami").catch(() => {}),
  });
  loadPresence();
}
boot();
