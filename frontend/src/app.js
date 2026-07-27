import * as Y from "yjs";
import { WebsocketProvider } from "y-websocket";
import { EditorState, Compartment, StateField } from "@codemirror/state";
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
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { initDictation, toggleDictation, dictationReady, retryDictation,
         releaseMicNow } from "./dictation.js";

// Markdown on the Ollsoft palette: content stays ink; the machinery (marks,
// urls, code) recedes into blues so the words lead.
const mdHighlight = HighlightStyle.define([
  { tag: t.heading, color: "#BAD7FF", fontWeight: "600" },
  { tag: t.strong, color: "#E9EFFA", fontWeight: "600" },
  { tag: t.emphasis, color: "#E9EFFA", fontStyle: "italic" },
  { tag: t.strikethrough, color: "#8CA1C1", textDecoration: "line-through" },
  { tag: t.link, color: "#4D9DFF" },
  { tag: t.url, color: "#3E7FD1" },
  { tag: t.monospace, color: "#7FD8C4" },
  { tag: t.quote, color: "#8CA1C1", fontStyle: "italic" },
  { tag: t.meta, color: "#5E7499" },
  { tag: t.processingInstruction, color: "#5E7499" },
  { tag: t.contentSeparator, color: "#4D9DFF" },
]);

// One shared terminal look, matched to the app chassis.
const TERM_THEME = {
  background: "#071019", foreground: "#DDE7F5", cursor: "#4D9DFF",
  cursorAccent: "#071019", selectionBackground: "#4D9DFF4D",
  black: "#1A2942", red: "#F2809C", green: "#2FCE98", yellow: "#E5AE58",
  blue: "#4D9DFF", magenta: "#B48CF2", cyan: "#5AC8DE", white: "#DDE7F5",
  brightBlack: "#5E7499", brightRed: "#FF9DB4", brightGreen: "#5FE3B8",
  brightYellow: "#F5C97E", brightBlue: "#7FB8FF", brightMagenta: "#CBA9FF",
  brightCyan: "#8ADEEE", brightWhite: "#FFFFFF",
};
const TERM_FONT = '"IBM Plex Mono", ui-monospace, monospace';

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
            if (!touches(n.from, n.to)) {
              const markName = name === "InlineCode" ? "CodeMark"
                : name === "Strikethrough" ? "StrikethroughMark" : "EmphasisMark";
              for (const m of n.node.getChildren(markName)) hide(m.from, m.to);
            }
          } else if (name === "Link") {
            const node = n.node;
            const marks = node.getChildren("LinkMark");
            const urlN = node.getChild("URL");
            if (marks.length >= 2 && urlN && !touches(n.from, n.to)) {
              const url = state.sliceDoc(urlN.from, urlN.to);
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
            // never swap it for raw markdown and re-flow the page
            const m = state.sliceDoc(n.from, n.to)
              .match(/^!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?[^)]*\)$/);
            if (m) {
              replace(n.from, n.to,
                      new MediaWidget(resolveMediaUrl(dir, m[2]), m[1], mediaKind(m[2])));
            }
          } else if (name === "Table") {
            return false;   // handled by tableField (a block decoration, below)
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
        const url = view.state.sliceDoc(u.from, u.to);
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
async function mentionUsers() {
  if (!_mentionCache || Date.now() - _mentionAt > 60000) {
    try {
      _mentionCache = (await (await fetch("/api/principals")).json()).users || [];
      _mentionAt = Date.now();
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

// ---- media drops & screenshot pastes (both modes) --------------------------
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
    const fd = new FormData();
    fd.append("file", f, name);
    const r = await fetch("/api/upload?dir=" + encodeURIComponent(dirName(tab.path) || "company"),
      { method: "POST", body: fd });
    if (r.status === 413) { kbToast(name + " is larger than the server's upload limit", "err"); continue; }
    const j = await r.json().catch(() => ({}));
    if (!j.ok) { kbToast(j.error || "upload failed", "err"); continue; }
    let snippet = (isImg ? "!" : "") + "[" + alt + "](" + j.link + ")";
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

function mediaExtension(tab) {
  return EditorView.domEventHandlers({
    drop(e, view) {
      const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
      if (!files.length) return false;
      e.preventDefault();
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
      uploadAndInsert(view, tab, files, pos == null ? view.state.selection.main.head : pos);
      return true;
    },
    paste(e, view) {
      const items = [...((e.clipboardData && e.clipboardData.items) || [])]
        .filter((i) => i.kind === "file");
      const files = items.map((i) => i.getAsFile()).filter(Boolean);
      if (!files.length) return false;   // plain text paste -> CodeMirror handles it
      e.preventDefault();
      uploadAndInsert(view, tab, files, view.state.selection.main.head);
      return true;
    },
  });
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
let lastPane = null;   // "term" | "doc" | {kind:"field", el}
document.addEventListener("focusin", (e) => {
  const el = e.target;
  if (!el || !el.closest) return;
  if (el.closest("#terminal-panel")) { lastPane = "term"; return; }
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
  if (el && el.closest && el.closest("#terminal-panel") && activeTerm)
    target = { kind: "term", t: activeTerm };
  else if (el && (el.tagName === "TEXTAREA" ||
             (el.tagName === "INPUT" && /^(text|search|url|tel|email)$/.test(el.type))))
    target = { kind: "field", el };

  // Focus is on chrome (a menu button, the body): fall back to the last pane
  // the user actually worked in, not to "whatever document happens to be open".
  if (!target && lastPane === "term" && activeTerm && !$("#terminal-panel").hidden)
    target = { kind: "term", t: activeTerm };
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
  if (!target && activeTerm && !$("#terminal-panel").hidden)
    target = { kind: "term", t: activeTerm };

  // Say where the words will land while they can still be stopped: the pill
  // shows "→ terminal" for the whole recording, so a misroute is visible
  // before the text lands instead of after.
  const ind = $("#ptt-target");
  if (ind) ind.textContent =
    target ? { term: "→ terminal", doc: "→ document", field: "→ field" }[target.kind] : "";
  return target;
}

function insertDictation(target, text) {
  if (!text) { kbToast("Nothing was said", "err"); return; }
  // Keep the transcript BEFORE routing it anywhere. Even a transcript that has
  // nowhere to land — or lands somewhere and gets deleted by a stray swipe — is
  // recoverable from the history for a day.
  dictHistAdd(text);
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
// dictation.js) and the text went somewhere unexpected. Text only, never
// audio: blobs would blow the quota, and the transcript is what you copy.
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

function dictHistAdd(text) {
  const arr = dictHistLoad();               // load() already expired the old ones
  arr.unshift({ t: Date.now(), text });
  if (arr.length > DICT_HIST_MAX) arr.length = DICT_HIST_MAX;
  try { localStorage.setItem(DICT_HIST_KEY, JSON.stringify(arr)); }
  catch (e) { /* quota or private mode — dictation itself still works */ }
}

function dictHistAgo(t) {
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  return Math.round(s / 3600) + " h ago";
}

function openDictHistory() {
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
  const card = document.createElement("div");
  card.className = "modal-card";
  card.innerHTML = `
    <div class="modal-head"><b>Dictation history</b>
      <span class="muted">last 24 h, this browser only</span>
      <button class="modal-x" title="Close">×</button></div>
    <div class="dh-list" data-testid="dh-list"></div>
    <div class="modal-foot"><button class="modal-close">Close</button></div>`;
  ov.appendChild(card);
  document.body.appendChild(ov);
  card.querySelector(".modal-x").addEventListener("click", () => ov.remove());
  card.querySelector(".modal-close").addEventListener("click", () => ov.remove());

  const list = card.querySelector(".dh-list");
  const entries = dictHistLoad();
  if (!entries.length) {
    list.innerHTML = '<div class="muted">Nothing yet — every transcript you dictate is kept here for 24 hours, in case it lands in the wrong place or gets deleted.</div>';
    return;
  }
  for (const e of entries) {
    const row = document.createElement("div");
    row.className = "dh-item";
    row.innerHTML = `<div class="dh-text"></div>
      <span class="dh-when">${dictHistAgo(e.t)}</span>
      <button class="mini dh-copy" title="Copy this transcript">copy</button>`;
    row.querySelector(".dh-text").textContent = e.text;
    row.querySelector(".dh-copy").addEventListener("click", () => {
      navigator.clipboard.writeText(e.text)
        .then(() => kbToast("Copied", "ok"), () => kbToast("Clipboard blocked", "err"));
    });
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
  const imgInput = $("#up-img"), fileInput = $("#up-file");
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
  $("#mdbar").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-md]");
    if (!b) return;
    switch (b.dataset.md) {
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
      case "image": pickInto(imgInput); break;
      case "video": pickInto($("#up-video")); break;
      case "file": pickInto(fileInput); break;
      case "mic": toggleDictation(); break;
    }
  });
}

const $ = (s) => document.querySelector(s);
const wsBase = () => (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host;
const isMobile = () => window.matchMedia("(max-width: 880px)").matches;

// ═══ Icons ══════════════════════════════════════════════════════════════════
// One consistent stroke family (outline, 24-grid) instead of the mixed
// glyph/emoji set — same visual weight everywhere, color only where it carries
// meaning (artifact = accent, secret = amber).
const svgIcon = (paths) =>
  '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + "</svg>";
const I = {
  chevron: svgIcon('<polyline points="9 18 15 12 9 6"/>'),
  doc: svgIcon('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'),
  file: svgIcon('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>'),
  artifact: svgIcon('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'),
  lock: svgIcon('<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>'),
  plus: svgIcon('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'),
  folderPlus: svgIcon('<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="10" x2="12" y2="16"/><line x1="9" y1="13" x2="15" y2="13"/>'),
  upload: svgIcon('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>'),
  download: svgIcon('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>'),
  share: svgIcon('<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/>'),
  trash: svgIcon('<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'),
  more: svgIcon('<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>'),
  pencil: svgIcon('<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>'),
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

function saveSession() {
  if (_restoring) return;
  try {
    localStorage.setItem("kbOpen", JSON.stringify({
      tabs: tabs.map((t) => ({ path: t.path, kind: t.kind })),
      active: active ? active.path : null,
      terms: terms.map((t) => ({ sid: t.sid, name: t.name })),
      activeTerm: activeTerm ? activeTerm.sid : null,
      termOpen: !$("#terminal-panel").hidden,
    }));
  } catch (e) { /* private mode */ }
}

async function restoreSession() {
  let s;
  try { s = JSON.parse(localStorage.getItem("kbOpen") || "null"); } catch (e) { return; }
  if (!s) return;
  _restoring = true;
  try {
    // Open every saved tab CONCURRENTLY. openPath registers the tab (and its DOM
    // order) synchronously before its first await, so mapping over the list
    // preserves order while firing all the props/epoch/websocket round-trips at
    // once — N tabs restore in one batch instead of one-after-another.
    await Promise.allSettled((s.tabs || []).map((t) => openPath(t.path, t.kind)));
    if (s.active) {
      const t = tabs.find((x) => x.path === s.active);
      if (t) activateTab(t);
    }
    if (canShell && (s.terms || []).length) {
      let act = null;
      for (const o of s.terms) {   // older saves stored bare sid strings
        const sid = typeof o === "string" ? o : o.sid;
        const t = newTerminal(undefined, sid, typeof o === "string" ? undefined : o.name);
        if (sid === s.activeTerm) act = t;
      }
      if (act) activateTerm(act, false);
      if (!s.termOpen) hideTerminalPanel();
    }
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
const kbConfirm = (message, o) =>
  kbDialog({ title: (o && o.title) || "Confirm", message, ok: o && o.ok,
             danger: !!(o && o.danger) }).then((v) => v !== null);
const kbPrompt = (message, value, o) =>
  kbDialog({ title: o && o.title, message, ok: o && o.ok,
             input: { value, placeholder: o && o.placeholder } });

function kbToast(msg, kind) {
  let host = document.getElementById("toasts");
  if (!host) {
    host = document.createElement("div");
    host.id = "toasts";
    document.body.appendChild(host);
  }
  const t = document.createElement("div");
  t.className = "toast" + (kind ? " " + kind : "");
  t.setAttribute("data-testid", "toast");
  t.textContent = msg;
  host.appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 350); }, 4200);
}

// ---- mobile chrome: file-tree drawer + topbar ⋯ menu ----------------------
// Same DOM on every screen size — CSS turns the sidebar into a drawer and the
// action buttons into a dropdown below 880px, so nothing here forks by device.
function closeNav() { document.body.classList.remove("nav-open"); }
function wireNav() {
  $("#nav-btn").addEventListener("click", () =>
    document.body.classList.toggle("nav-open"));
  $("#scrim").addEventListener("click", closeNav);
  const menu = $("#topbar-actions"), more = $("#more-btn");
  more.addEventListener("click", (e) => {
    e.stopPropagation();
    menu.classList.toggle("open");
  });
  document.addEventListener("click", (e) => {
    if (menu.classList.contains("open") && !menu.contains(e.target)) menu.classList.remove("open");
  });
  // choosing any action closes the menu (the action opens its own surface)
  menu.addEventListener("click", (e) => {
    if (e.target.closest("button, a")) menu.classList.remove("open");
  });
}

// ---- identity -------------------------------------------------------------
let canShell = true;   // false for viewer accounts (no terminal, no cron)

async function loadWhoami() {
  const r = await fetch("/api/whoami");
  const j = await r.json();
  canShell = j.shell !== false;
  $("#whoami").textContent = j.user + " · uid " + j.uid;
  $("#whoami-m").textContent = j.user + " · uid " + j.uid;
  $("#term-user").textContent = j.user;
  window.__kbuser = j.user;
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
    if (t.path.includes("/") && _lastTreePaths.has(t.path) && !newPaths.has(t.path) &&
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

async function loadTree(force) {
  let j;
  try { j = await (await fetch("/api/tree")).json(); } catch (e) { return; }
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
    const b = document.createElement("button");
    b.className = "ctx-item" + (it.danger ? " danger" : "");
    b.innerHTML = (it.icon || "") + "<span></span>";
    b.querySelector("span").textContent = it.label;
    b.addEventListener("click", (e) => { e.stopPropagation(); closeCtxMenu(); it.fn(); });
    m.appendChild(b);
  }
  document.body.appendChild(m);
  const r = m.getBoundingClientRect();
  m.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
  m.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
  m.querySelector(".ctx-item").focus();   // Tab/arrows walk the menu; Escape closes it
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
      if (fileClipboard) {
        items.push({ icon: I.paste, label: "Paste " + baseName(fileClipboard.path),
                     fn: () => pasteInto(n.path) });
      }
      items.push("-");
    }
  } else {
    items.push({ icon: I.open, label: "Open", fn: () => openEntry(n) });
    items.push({ icon: I.download, label: "Download",
                 fn: () => { location.href = "/api/attachment?dl=1&path=" + encodeURIComponent(n.path); } });
    items.push("-");
  }
  items.push({ icon: I.copy, label: "Copy", fn: () => copyEntry(n) });
  if (parentWritable) {
    items.push({ icon: I.pencil, label: "Rename…", fn: () => renameEntry(n) });
    items.push({ icon: I.move, label: "Move to…", fn: () => moveToEntry(n) });
  }
  items.push("-");
  items.push({ icon: I.share, label: "Permissions & sharing", fn: () => openPerms(n.path) });
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
  const reopen = affected.map((t) => ({ old: t.path }));
  affected.forEach((t) => _movingPaths.add(t.path));
  let j, r;
  try {
    r = await fetch("/api/fs/rename", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ src: srcPath, dst }),
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
    try { await openPath(np, kindForPath(np)); } catch (e) { /* gone */ }
  }
  affected.forEach((t) => _movingPaths.delete(t.path));
  // restore whatever tab was active before (re-homed if it was one that moved)
  if (keepPath) {
    const want = affected.some((t) => t.path === keepPath) ? remap(keepPath) : keepPath;
    const t = tabs.find((x) => x.path === want);
    if (t) activateTab(t);
  }
  loadTree(true);
  return true;
}

async function renameEntry(n) {
  const cur = baseName(n.path);
  const name = await kbPrompt("New name:", cur, { title: "Rename " + cur, ok: "Rename" });
  if (!name || !name.trim() || name.trim() === cur) return;
  if (name.includes("/")) { kbToast("a name cannot contain / — use Move to… instead", "err"); return; }
  const dir = dirName(n.path);
  await moveEntry(n.path, (dir ? dir + "/" : "") + name.trim());
}

async function moveToEntry(n) {
  const dest = await kbPrompt("Destination folder:", dirName(n.path),
                              { title: "Move " + baseName(n.path), ok: "Move",
                                placeholder: "company/subfolder" });
  if (!dest || !dest.trim()) return;
  const d = dest.trim().replace(/^\/+|\/+$/g, "");
  if (d === dirName(n.path)) return;
  await moveEntry(n.path, d + "/" + baseName(n.path));
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

// fetch() can't report request-body progress — XHR is still the only way
function uploadWithProgress(folder, file, onPct) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/fs/upload?dir=" + encodeURIComponent(folder));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onPct(Math.round((100 * e.loaded) / e.total));
    };
    const done = () => resolve({
      status: xhr.status,
      error: (() => {
        try { return JSON.parse(xhr.responseText).error; } catch (e) { return null; }
      })(),
    });
    xhr.onload = done;
    xhr.onerror = () => resolve({ status: 0, error: "network error" });
    const fd = new FormData();
    fd.append("file", file, file.name);
    xhr.send(fd);
  });
}

// Shared by drag-drop and the ⇪ picker: register every ghost up front (later
// ones say "waiting…"), then upload one at a time.
async function uploadMany(folder, files) {
  if (!files.length) return 0;
  const m = _pendingUploads.get(folder) || new Map();
  _pendingUploads.set(folder, m);
  const ids = files.map((f) => {
    const id = ++_upSeq;
    m.set(id, { name: f.name, pct: null });
    return id;
  });
  collapsed.delete(folder);   // the user should SEE the ghosts appear
  rerenderTree();
  let ok = 0;
  try {
    for (let i = 0; i < files.length; i++) {
      const f = files[i], id = ids[i];
      updateGhostRow(id, 0);
      const r = await uploadWithProgress(folder, f, (pct) => updateGhostRow(id, pct));
      if (r.status >= 200 && r.status < 300) ok++;
      else if (r.status === 413) kbToast(f.name + " is larger than the server's upload limit", "err");
      else kbToast(r.error || "could not upload " + f.name, "err");
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

// hidden (dot-prefixed) entries are filtered client-side; the sidebar's `.*`
// toggle reveals them (default off — machinery like .claude and artifacts'
// .ll/ working folders stay out of sight)
let showHidden = false;
try { showHidden = localStorage.getItem("kbShowHidden") === "1"; } catch (e) { /* private mode */ }

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
    actions.appendChild(mkBtn(I.share, "Permissions", (e) => { e.stopPropagation(); openPerms(n.path); }));
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
    // dragging a row moves the file/folder; top-level areas stay fixed
    if (n.path.includes("/")) {
      row.draggable = true;
      row.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("application/x-kb-path", n.path);
        e.dataTransfer.effectAllowed = "move";
      });
    }

    if (n.dir) {
      const caret = document.createElement("span");
      caret.className = "caret" + (collapsed.has(n.path) ? "" : " open");
      caret.innerHTML = I.chevron;
      row.append(caret, icon, label, pres, actions, more);
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
      row.append(icon, label, pres, actions, more);
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

function renderTabBar() {
  const bar = $("#tabbar");
  bar.innerHTML = "";
  bar.hidden = tabs.length === 0;
  const dup = {};
  tabs.forEach((t) => { dup[t.name] = (dup[t.name] || 0) + 1; });
  for (const t of tabs) {
    const el = document.createElement("div");
    el.className = "tab" + (t === active ? " active" : "");
    el.title = t.path;
    el.dataset.path = t.path;
    const icon = document.createElement("span");
    icon.className = "tab-icon";
    icon.innerHTML = t.kind === "artifact" ? I.artifact : t.kind === "secret" ? I.lock : I.doc;
    const name = document.createElement("span");
    name.className = "tab-name";
    name.textContent = t.name;
    el.append(icon, name);
    if (dup[t.name] > 1 && dirName(t.path)) {   // disambiguate same-named files
      const d = document.createElement("span");
      d.className = "tab-dir";
      d.textContent = dirName(t.path);
      el.appendChild(d);
    }
    const x = document.createElement("button");
    x.className = "tab-x"; x.title = "Close"; x.textContent = "×";
    x.addEventListener("click", (e) => { e.stopPropagation(); closeTab(t); });
    el.appendChild(x);
    el.addEventListener("click", () => activateTab(t));
    el.addEventListener("auxclick", (e) => { if (e.button === 1) { e.preventDefault(); closeTab(t); } });
    bar.appendChild(el);
  }
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
    if (_restoring || replace) window.history.replaceState(st, "", want);
    else window.history.pushState(st, "", want);
  } catch (e) { /* about:blank in tests */ }
}

async function openDeepLink(p) {
  const existing = tabs.find((t) => t.path === p);
  if (existing) { activateTab(existing); return; }   // already open (e.g. restored) — don't navigate away
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

function activateTab(t) {
  active = t;
  for (const o of tabs) o.el.style.display = o === t ? "" : "none";
  renderDocTitle(t);
  setAccessBadge(t ? t.access : null);
  document.querySelectorAll(".tree-item").forEach((e) =>
    e.classList.toggle("active", !!t && e.dataset.path === t.path));
  syncTestHooks();
  updateModeUI();
  renderPresence();
  renderSyncBadge();
  renderTabBar();
  saveSession();
  syncUrl();
}

function closeTab(t) {
  const i = tabs.indexOf(t);
  if (i < 0) return;
  tabs.splice(i, 1);
  // Full teardown: the provider does NOT destroy its awareness (which keeps a
  // heartbeat interval) or the Y.Doc — without these, every closed doc tab
  // leaks an interval + document forever.
  if (t.view) t.view.destroy();
  if (t.provider) { t.provider.awareness.destroy(); t.provider.destroy(); }
  if (t.ydoc) t.ydoc.destroy();
  t.view = t.provider = t.ydoc = t.frame = null;
  t.el.remove();
  if (active === t) activateTab(tabs[i] || tabs[i - 1] || null);
  else renderTabBar();
}

async function openPath(path, kind) {
  closeNav();   // on mobile the drawer covers the editor — opening a file is leaving it
  const existing = tabs.find((t) => t.path === path);
  if (existing) { activateTab(existing); return; }
  const el = document.createElement("div");
  el.className = "tab-content";
  $("#editor").appendChild(el);
  const t = { id: ++tabSeq, path, kind, name: baseName(path), el,
              view: null, provider: null, ydoc: null, frame: null,
              access: null, synced: false,
              mode: localStorage.getItem("kbEditMode") || "rich", modeComp: null };
  tabs.push(t);
  noteRecent(path);
  activateTab(t);
  if (kind === "artifact") mountArtifact(t);
  else if (kind === "secret") await mountSecret(t);
  else await mountDoc(t);
}

// ---- secret viewer: masked, reveal/copy/edit — never the collab editor ----
async function mountSecret(t) {
  const r = await fetch("/api/file?path=" + encodeURIComponent(t.path));
  const j = await r.json().catch(() => ({}));
  if (!tabs.includes(t)) return;
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

async function mountDoc(t) {
  // Decide editable-ness up front so the editor opens in the right mode. A
  // read-only file still opens in the live session (you see it + updates), just
  // not editable — the daemon also refuses to persist edits from a read-only join.
  let access = { read: true, write: true };
  try {
    const rr = await fetch("/fs/props?path=" + encodeURIComponent(t.path));
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

  // the doc's lineage id: presenting it is what admits us to the live session
  // (a tab holding an older lineage would re-merge stale history as duplicated
  // text — the relay refuses those instead)
  let epoch = "";
  try { epoch = (await (await fetch("/api/doc-epoch?path=" + encodeURIComponent(t.path))).json()).epoch || ""; }
  catch (e) { /* offline; the connect will just be refused */ }
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
    markdown({ extensions: [TaskList, Strikethrough, Table] }),
    syntaxHighlighting(mdHighlight),
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
  const me = window.__kbuser || "user";
  const setMe = () => provider.awareness.setLocalStateField("user", { name: me, ...userColors(me) });
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
    if (active === t) { window.__kbsynced = isSynced; renderSyncBadge(); }
  });
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

function renderSyncBadge() {
  const b = $("#sync-badge");
  const t = active;
  if (!t || t.kind !== "doc" || !t.provider) { b.hidden = true; return; }
  b.hidden = false;
  const live = t.conn === "connected" && t.synced;
  b.textContent = live ? "live" : "not syncing";
  b.className = "sync-badge " + (live ? "live" : "off");
  b.title = live ? "Connected — everyone sees your edits in real time"
                 : "NOT connected to the live session — your edits are not reaching others. Reopen the tab if this persists.";
}

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
    const cur = seen.get(u.name) || { name: u.name, color: u.color || "#4D9DFF", self: false };
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
  const frame = document.createElement("iframe");
  frame.className = "artifact-frame";
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
  return b.target.endsWith(".html") ? I.artifact : b.target.endsWith(".md") ? I.doc : I.file;
}

function runLauncher(b) {
  if (b.kind === "term") { openTermWith(b.target); return; }
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

function renderLaunchbar() {
  // Two hosts, one source of truth: the bar (desktop) and the ⋯ menu section
  // (mobile, where a permanent bar would cost a whole row of screen).
  for (const [sel, testid] of [["#launchbar", "launcher-manage"],
                               ["#menu-launchers", "launcher-manage-m"]]) {
    const host = $(sel);
    host.textContent = "";
    for (const [list, cls] of [[launchers.company || [], "company"],
                               [launchers.mine || [], "mine"]]) {
      for (const b of list) {
        if (b.kind === "term" && !canShell) continue;   // viewers have no shell
        const chip = launcherChip(b, cls);
        chip.addEventListener("click", () => runLauncher(b));
        host.appendChild(chip);
      }
    }
    const add = document.createElement("button");
    add.className = "lchip manage";
    add.title = "Add or edit launcher buttons";
    add.setAttribute("data-testid", testid);
    add.setAttribute("aria-label", "Add or edit launcher buttons");
    add.textContent = "＋";
    add.addEventListener("click", showLaunchersModal);
    host.appendChild(add);
  }
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
    if (!r.ok) { kbToast(j.error || "could not save buttons", "err"); return false; }
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
      none.className = "muted"; none.textContent = "no buttons yet";
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
        <input data-testid="lnch-label-${scope}" placeholder="label" maxlength="24">
        <select data-testid="lnch-kind-${scope}">
          <option value="file">opens a file / artifact</option>
          ${canShell ? '<option value="term">runs a command in a terminal</option>' : ""}
        </select>
        <input data-testid="lnch-target-${scope}" class="lnch-target" placeholder="path (e.g. company/todos.html)">
        <button data-testid="lnch-add-${scope}" class="primary">Add button</button>`;
      const kind = form.querySelector("select");
      const target = form.querySelector(".lnch-target");
      kind.addEventListener("change", () => {
        target.placeholder = kind.value === "term"
          ? "command (e.g. claude)" : "path (e.g. company/todos.html)";
      });
      form.querySelector("button").addEventListener("click", async () => {
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
      <div class="modal-head"><b>Launcher buttons</b>
        <span class="muted">one tap opens a file — or a shell running a command</span>
        <button class="modal-x" title="Close">×</button></div>`;
    card.appendChild(section("Company — everyone sees these", "company",
      launchers.company || [], isAdmin,
      isAdmin ? "" : "Set by admins. Ask one to add a button for the whole company."));
    card.appendChild(section("Yours — only you see these", "mine",
      launchers.mine || [], true,
      "A terminal button types the command into a fresh shell running as you."));
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

// ---- admin panel (user & group management; admins only) -------------------
async function openAdmin() {
  // Admins get the full panel; users with write access to .claude/egress.json
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
      <input id="nu-pw" type="password" placeholder="password" size="10">
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
      <span class="mono">.claude/egress.json</span> (grant it via ⚙ on that file).</div>
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

// ---- permissions modal ----------------------------------------------------
async function openPerms(path) {
  const [r, pr] = await Promise.all([
    fetch("/fs/props?path=" + encodeURIComponent(path)),
    fetch("/api/principals").catch(() => null),
  ]);
  const p = await r.json();
  if (!r.ok) { kbToast(p.error || "cannot read properties", "err"); return; }
  let principals = { users: [], groups: [] };
  try { if (pr && pr.ok) principals = await pr.json(); } catch (e) { /* fall back to text */ }
  showPermsModal(p, principals);
}

function showPermsModal(p, principals) {
  principals = principals || { users: [], groups: [] };
  // options for a picker: the current value stays choosable even when it is
  // not a pickable principal (root-owned files, system groups)
  const opts = (arr, cur, placeholder) => {
    const list = [...new Set([...(cur ? [cur] : []), ...arr])];
    return (placeholder ? '<option value="">' + placeholder + "</option>" : "") +
      list.map((n) => `<option${n === cur ? " selected" : ""}>${escapeHtml(n)}</option>`).join("");
  };
  const removals = [];   // {type,name}
  const additions = [];  // {type,name,perms}
  const ov = document.createElement("div");
  ov.className = "modal-overlay";
  ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });

  const card = document.createElement("div");
  card.className = "modal-card";
  const ro = !p.can_edit;
  card.innerHTML = `
    <div class="modal-head">
      <b>${escapeHtml(p.path)}</b> <span class="muted">${p.is_dir ? "folder" : "file"} · mode ${p.mode}</span>
      <button class="modal-x" title="Close">×</button>
    </div>
    ${ro ? '<div class="note">Read-only — only the owner or an admin can change this.</div>' : ""}
    <label class="frow"><span>Owner</span>${ro
      ? `<input id="pm-owner" value="${escapeHtml(p.owner)}" disabled>`
      : `<select id="pm-owner">${opts(principals.users, p.owner)}</select>`}</label>
    <label class="frow"><span>Group</span>${ro
      ? `<input id="pm-group" value="${escapeHtml(p.group)}" disabled>`
      : `<select id="pm-group">${opts(principals.groups, p.group)}</select>`}</label>
    ${ro ? "" : (() => {
      const m = parseInt(p.mode, 8) || 0;
      const cur = (m & 0o004) ? "company" : (m & 0o040) ? "team" : "private";
      const oct = p.is_dir ? { company: "2775", team: "2770", private: "0700" }
                           : { company: "664", team: "660", private: "600" };
      return `<label class="frow"><span>Access</span>
        <select id="pm-vis" data-testid="pm-vis" title="Plain chmod presets — owner/group/others permission bits">
          <option value="company"${cur === "company" ? " selected" : ""}>company · everyone can read · chmod ${oct.company}</option>
          <option value="team"${cur === "team" ? " selected" : ""}>team · only the group above · chmod ${oct.team}</option>
          <option value="private"${cur === "private" ? " selected" : ""}>private · owner + people below · chmod ${oct.private}</option>
        </select></label>
      <input type="hidden" id="pm-vis0" value="${cur}">`;
    })()}
    <div class="acl-title">Shared with (ACLs)</div>
    <div id="pm-acls" class="acl-list"></div>
    ${ro ? "" : `<div class="acl-add">
      <select id="pm-type"><option value="user">user</option><option value="group">group</option></select>
      <select id="pm-name">${opts(principals.users, null, "who…")}</select>
      <select id="pm-perms"><option value="r">read</option><option value="rw">read+write</option><option value="rwx">rwx</option></select>
      <button id="pm-addacl">add</button></div>`}
    <div class="modal-foot">
      ${ro ? "" : '<button id="pm-save" class="primary">Save</button>'}
      <button id="pm-close">Close</button>
    </div>`;
  ov.appendChild(card);
  document.body.appendChild(ov);

  const aclHost = card.querySelector("#pm-acls");
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
        const x = mkBtn("×", "Remove", () => {
          const ai = additions.indexOf(a);
          if (ai >= 0) additions.splice(ai, 1);
          else removals.push({ type: a.type, name: a.name });
          renderAcls();
        });
        row.appendChild(x);
      }
      aclHost.appendChild(row);
    }
  }
  renderAcls();

  const close = () => ov.remove();
  card.querySelector(".modal-x").addEventListener("click", close);
  card.querySelector("#pm-close").addEventListener("click", close);
  if (!ro) {
    // the "who" picker follows the user/group toggle
    card.querySelector("#pm-type").addEventListener("change", () => {
      const type = card.querySelector("#pm-type").value;
      card.querySelector("#pm-name").innerHTML =
        opts(type === "group" ? principals.groups : principals.users, null, "who…");
    });
    card.querySelector("#pm-addacl").addEventListener("click", () => {
      const name = card.querySelector("#pm-name").value.trim();
      if (!name) return;
      additions.push({ type: card.querySelector("#pm-type").value, name,
                       perms: card.querySelector("#pm-perms").value });
      card.querySelector("#pm-name").value = "";
      renderAcls();
    });
    card.querySelector("#pm-save").addEventListener("click", async () => {
      const body = { path: p.path, acl_add: additions, acl_remove: removals };
      const owner = card.querySelector("#pm-owner").value.trim();
      const group = card.querySelector("#pm-group").value.trim();
      if (owner && owner !== p.owner) body.owner = owner;
      if (group && group !== p.group) body.group = group;
      const vis = card.querySelector("#pm-vis");
      if (vis && vis.value !== card.querySelector("#pm-vis0").value) body.visibility = vis.value;
      const r = await fetch("/fs/props", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
      });
      const j = await r.json();
      if (r.ok) {
        if (j.granted_traverse && j.granted_traverse.length) {
          kbAlert("Also granted traverse (folder pass-through, no listing) on: " +
                  j.granted_traverse.join(", ") + " — so they can reach this file.", "Shared");
        } else kbToast("Permissions saved", "ok");
        close(); loadTree();
      } else kbToast(j.error || "could not save", "err");
    });
  }
}

function setAccessBadge(access) {
  const b = $("#access-badge");
  if (!access) { b.hidden = true; return; }
  const label = access.write ? "read · write" : access.read ? "read-only" : "no access";
  b.textContent = label;
  b.className = "access-badge " + (access.write ? "rw" : "ro");
  b.hidden = false;
}

async function createAndOpen(path) {
  const r = await fetch("/fs/newfile", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ path }),
  });
  const j = await r.json();
  if (r.ok) { await loadTree(); openPath(path, path.endsWith(".html") ? "artifact" : "doc"); }
  else kbToast(j.error || "could not create file", "err");
}
async function newFileIn(folder) {
  const name = await kbPrompt("Name — .md for a document, .html for an artifact:", "note.md",
                              { title: "New file in " + folder, ok: "Create" });
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
  if (!await kbConfirm(`Delete ${what}?`, { title: "Delete", ok: "Delete", danger: true })) return;
  const r = await fetch("/api/fs/delete", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ path: n.path }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { kbToast(j.error || "could not delete", "err"); return; }
  // Retire any tabs that were showing the deleted path (or anything under it).
  for (const t of tabs.filter((t) => t.path === n.path || t.path.startsWith(n.path + "/"))) {
    closeTab(t);
  }
  loadTree(true);
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
    const files = [...(e.dataTransfer ? e.dataTransfer.files : [])];
    if (!files.length) return;
    await uploadMany(folder, files);   // ghost rows + progress, then the real tree
  });
}

// ---- fuzzy matching --------------------------------------------------------
// One scorer, mirroring the server's _name_score (user_server.py), so a file
// ranks the same whether it came from the tree we already hold or from the API.
// Diacritics are folded, so `lekarska` finds `lékařská zpráva.png`.
function foldText(s) {
  return s.toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "");
}

// Are the query's characters present, in order? Score by how tightly packed the
// match is — consecutive characters and matches right after a separator count
// for more, which is what makes `apl` rank projects/acme/plan.md first.
function subseqMatch(q, hay) {
  let i = 0, score = 0, run = 0, last = 0;
  const hits = [];
  for (let pos = 0; pos < hay.length && i < q.length; pos++) {
    if (hay[pos] !== q[i]) { run = 0; continue; }
    run++;
    score += 2 + run;
    if (pos === 0 || " -_./".includes(hay[pos - 1])) score += 4;
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
                   Escape: "Esc", Enter: "⏎", ArrowUp: "↑", ArrowDown: "↓",
                   ArrowLeft: "←", ArrowRight: "→", Delete: "Del" };
  return combo.split("+").map((p) => map[p] || pretty[p] || p).join(IS_APPLE ? "" : "+");
}

const hasDoc = () => !!(active && active.kind === "doc");
const hasTab = () => !!active;

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
    label: "Close tab", when: hasTab, run: () => { if (active) closeTab(active); } },
  { id: "focustree", keys: ["Mod+Shift+E", "Alt+E"], group: "Navigate",
    label: "Focus the file tree", run: focusTree },
  { id: "sidebar", keys: ["Alt+B"], group: "Navigate",
    label: "Show / hide the file tree", run: toggleSidebar },
  { id: "reveal", keys: ["Alt+R"], group: "Navigate", when: hasTab,
    label: "Reveal the open file in the tree", run: revealActive },

  // — Documents —
  { id: "newdoc", keys: ["Alt+N"], group: "Documents", label: "New document",
    run: () => $("#newdoc").click() },
  { id: "mode", keys: ["Alt+M"], group: "Documents", when: hasDoc,
    label: "Switch Rich ⇄ Source", run: () => setMode(active.mode === "rich" ? "source" : "rich") },
  { id: "find", keys: ["Mod+F"], group: "Documents", when: hasDoc,
    label: "Find in this document", run: findInDoc },
  { id: "history", keys: ["Alt+H"], group: "Documents", when: hasTab,
    label: "Version history", run: () => { if (active) openHistory(active.path); } },
  { id: "perms", keys: ["Alt+S"], group: "Documents", when: hasTab,
    label: "Sharing and permissions", run: () => { if (active) openPerms(active.path); } },
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
  { id: "hidden", label: "Show / hide dot-files", run: () => $("#hidden-toggle").click() },
  { id: "foldall", label: "Collapse / expand all folders", run: toggleFoldAll },
  { id: "cron", label: "Scheduled jobs (cron)", when: () => canShell, run: openCron },
  { id: "admin", label: "Admin — users, groups, network", when: () => !$("#admin-btn").hidden,
    run: openAdmin },
  { id: "copypath", label: "Copy the open file's path", when: hasTab,
    run: () => { if (!active) return;
      navigator.clipboard.writeText(active.path)
        .then(() => kbToast("Path copied", "ok"), () => kbToast("Clipboard blocked", "err")); } },
  { id: "reload", label: "Reload the file tree", run: () => loadTree(true).then(() => kbToast("Tree reloaded", "ok")) },
  { id: "retryspeech", label: "Retry the last dictation", when: dictationReady,
    run: retryDictation },
  { id: "dicthistory", label: "Dictation history — transcripts from the last 24 hours",
    when: dictationReady, run: openDictHistory },
  // The mic is held open between utterances so the next one starts instantly and
  // the browser doesn't re-prompt. This hands it back without waiting out the
  // five-minute idle timer, for anyone who wants the recording indicator gone.
  { id: "releasemic", label: "Release the microphone", when: dictationReady,
    run: () => { releaseMicNow(); kbToast("Microphone released", "ok"); } },
  { id: "speechlang", label: "Dictation language…", when: dictationReady,
    run: dictationLangPrompt },
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
  for (const c of EXTRA_COMMANDS) {
    if (c.when && !c.when()) continue;
    cmds.push({ label: c.label, keys: null, run: c.run });
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
  for (const b of BINDINGS) {
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
    const inTerm = !!(e.target && e.target.closest && e.target.closest("#terminal-panel"));
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

function cycleTab(d) {
  if (tabs.length < 2) return;
  const i = tabs.indexOf(active);
  activateTab(tabs[(((i < 0 ? 0 : i) + d) % tabs.length + tabs.length) % tabs.length]);
}
function gotoTab(i) { if (tabs[i]) activateTab(tabs[i]); }
function toggleSidebar() {
  if (isMobile()) { document.body.classList.toggle("nav-open"); return; }
  document.body.classList.toggle("nav-hidden");
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
function revealActive() {
  if (!active) return;
  // open every folder on the way down, then scroll the row into view
  const parts = active.path.split("/");
  let acc = "";
  for (let i = 0; i < parts.length - 1; i++) {
    acc = acc ? acc + "/" + parts[i] : parts[i];
    collapsed.delete(acc);
  }
  if (isMobile()) document.body.classList.add("nav-open");
  document.body.classList.remove("nav-hidden");
  rerenderTree();
  treeCursor = active.path;
  const el = $("#tree").querySelector('.tree-item[data-path="' + cssEsc(active.path) + '"]');
  if (el) { el.scrollIntoView({ block: "center" }); paintTreeCursor(); }
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
  document.body.classList.remove("nav-hidden");
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

  const render = () => {
    list.innerHTML = "";
    if (!items.length) {
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
      row.className = "palette-item" + (i === sel ? " sel" : "");
      row.setAttribute("data-testid", "palette-item");
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
  };

  const choose = (i) => {
    const it = items[i];
    if (!it) return;
    closePalette();
    it.run();
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
      return out;
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
      return out;
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
    for (const s of scored.slice(0, 20)) out.push(fileItem(s.n, s.hit));
    return out;
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
      catch (e) { return; }
      if (mine !== seq || _palette !== self) return;   // superseded or closed
      const hits = (j.results || []).slice(0, 12);
      if (!hits.length) return;
      for (const m of hits) {
        items.push({
          group: "In documents", icon: I.doc, path: m.path + ":" + m.line,
          main: { text: m.text, hits: contentHits(q, m.text) },
          sub: { text: m.path + " · line " + m.line, hits: [] },
          run: () => openAtLine(m.path, m.line),
        });
      }
      render();
    }, 180);
  };

  const refresh = () => {
    const q = input.value.trim();
    items = build(q);
    sel = 0;
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
  document.body.classList.remove("nav-hidden");   // Alt+B may have hidden it
  if (isMobile()) document.body.classList.add("nav-open");
  treeCursor = path;
  paintTreeCursor();
  const el = $("#tree").querySelector('.tree-item[data-path="' + cssEsc(path) + '"]');
  if (el) el.scrollIntoView({ block: "center" });
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
    { keys: [], label: "Drag a file onto a folder to move it; drop files onto a folder to upload" },
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
function wireNewDoc() {
  $("#newdoc").addEventListener("click", async () => {
    const path = await kbPrompt("Path for the new document:", "company/untitled.md",
                                { title: "New document", ok: "Create", placeholder: "company/notes.md" });
    if (path) createAndOpen(path.trim());
  });
}
// Prevent the browser from navigating away if a file is dropped OUTSIDE an
// editor (inside one, mediaExtension already handles it). Otherwise the drop
// replaces the whole app with the raw file.
function wireUpload() {
  const ed = $("#editor");
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

function wireTerminal() {
  $("#toggleterm").addEventListener("click", toggleTerminalPanel);
  $("#term-new").addEventListener("click", () => newTerminal());
  $("#term-hide").addEventListener("click", hideTerminalPanel);
  const maxBtn = $("#term-max");   // null-guarded: cached older app.html
  if (maxBtn) maxBtn.addEventListener("click", () =>
    setTermMax(!document.body.classList.contains("term-max")));
  // how to select+copy where a full-screen app (claude code) owns the mouse
  const hint = $("#term-hint");
  if (hint) {
    hint.textContent = SELECT_MODIFIER + "+drag to select · copies on select";
    hint.title = "In a full-screen terminal app (e.g. claude code) the app owns "
      + "the mouse, so hold " + SELECT_MODIFIER + " while dragging to select text. "
      + "Selecting copies it; "
      + (IS_MAC ? "⌘V" : "Ctrl+V or Ctrl+Shift+V") + " pastes.";
  }
  // Ctrl+` lives in BINDINGS with every other shortcut — see wireShortcuts().
  // Refit whenever the terminal area actually changes size (panel resize,
  // window resize) — a single fit-on-open is not enough.
  new ResizeObserver(() => fitTerm(activeTerm)).observe($("#terminal"));
  // If a terminal opened before the webfont finished loading, xterm measured
  // fallback glyphs — poke the font option to re-measure, then refit.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      for (const t of terms) t.term.options.fontFamily = TERM_FONT;
      fitTerm(activeTerm);
    });
  }
  window.addEventListener("resize", () => {
    if (!$("#terminal-panel").hidden) fitTerm(activeTerm);
  });
  wireTermResizer();
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
  const apply = () => {
    const covered = window.innerHeight - vv.height;   // ~0 when the browser resizes the layout itself
    const panel = $("#terminal-panel");
    if (covered > 80) {
      document.body.style.height = vv.height + "px";
      // The full-screen terminal is position:fixed, so the body pinning above
      // does nothing for it — size the panel to the VISIBLE viewport directly,
      // and it ends exactly where the keyboard begins (iOS overlays the
      // keyboard instead of resizing the layout; interactive-widget in the
      // meta tag only helps Chrome).
      if (document.body.classList.contains("term-max"))
        panel.style.height = vv.height + "px";
      window.scrollTo(0, 0);
      pinned = true;
    } else if (pinned) {
      document.body.style.height = "";
      panel.style.height = "";
      pinned = false;
    }
    if (!panel.hidden) fitTerm(activeTerm);
  };
  vv.addEventListener("resize", apply);
  vv.addEventListener("scroll", () => { if (pinned) window.scrollTo(0, 0); });
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
// Touch scrolling, driven by POINTER events with setPointerCapture. Two things
// make that necessary rather than fancy:
//
//   1. The finger's target keeps being destroyed under it. xterm's DOM renderer
//      replaces the spans of every repainted row, and a touch event whose target
//      has left the document never reaches an ancestor listener — so on a screen
//      that repaints (claude code redraws on every wheel report it answers) a
//      swipe delivered ONE touchmove and then went silent. That is the "swiping
//      sometimes does nothing" bug. A captured pointer is delivered to this
//      element no matter what the renderer does to its children.
//   2. xterm ships its own touch handler, bound to the .xterm element inside
//      ours and active whenever the program is not tracking the mouse. Left
//      alone it pans its viewport behind our back and the buffer moves twice as
//      far as the finger, in jumps — so touchmove is swallowed here.
//
// A swipe scrolls the scrollback in the normal buffer, and becomes arrow keys /
// wheel reports in the alternate screen, where there is no scrollback and the
// app scrolls itself — the convention mobile terminals like Termux use.
function wireTouchScroll(t) {
  const pts = new Map();                  // live fingers: pointerId -> {x, y}
  let panId = null, lastY = 0, lastX = 0, acc = 0, travel = 0;
  let vel = 0, velAt = 0;                 // finger velocity (px/ms), for the fling
  // Pinch = text size, the gesture every phone user will try first. The
  // browser's own pinch-zoom never fires here (#terminal has touch-action:
  // none), so the gesture is ours to implement: scale the font by the ratio
  // of the current finger distance to where the pinch started.
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
    // arrow keys (or wheel reports) at it long after the finger was lifted.
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

  // One swipe step, in whole rows: down the scrollback, or into the app.
  const scrollRows = (lines, x, y) => {
    if (t.term.buffer.active.type !== "alternate") { t.term.scrollLines(lines); return; }
    // Alternate screen = no scrollback; the app owns scrolling.
    const n = Math.min(Math.abs(lines), 40);
    const modes = t.term.modes || {};
    if (modes.mouseTrackingMode && modes.mouseTrackingMode !== "none") {
      // The app listens for the mouse (htop, some TUIs): forward the swipe as
      // SGR wheel events — the same reports desktop wheel scrolling sends — and
      // the app scrolls its own view.
      const rect = t.el.getBoundingClientRect();
      const col = Math.max(1, Math.min(t.term.cols,
        Math.ceil((x - rect.left) / (rect.width / t.term.cols))));
      const row = Math.max(1, Math.min(t.term.rows,
        Math.ceil((y - rect.top) / (rect.height / t.term.rows))));
      rawSend(t, `\x1b[<${lines > 0 ? 65 : 64};${col};${row}M`.repeat(n));
    } else {
      // No mouse support (plain less/vim): cursor keys — the Termux convention.
      const app = modes.applicationCursorKeysMode;
      rawSend(t, ((app ? "\x1bO" : "\x1b[") + (lines > 0 ? "B" : "A")).repeat(n));
    }
  };

  const startPan = (id, x, y, at) => {
    panId = id; lastX = x; lastY = y;
    acc = 0; travel = 0; vel = 0; velAt = at;
  };

  t.el.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "touch") return;   // a mouse drag belongs to xterm's selection
    stopFling();                             // a finger down stops the glide, as everywhere
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    // Capture immediately: the row this finger landed on may be replaced by the
    // next repaint, and an uncaptured pointer would go with it.
    try { t.el.setPointerCapture(e.pointerId); } catch (err) { /* already gone */ }
    if (pts.size === 2) {                    // a pinch is never also a swipe
      panId = null; pinchD = spread(); pinchBase = termFontSize();
      return;
    }
    pinchD = null;
    if (pts.size > 2) { panId = null; return; }
    startPan(e.pointerId, e.clientX, e.clientY, e.timeStamp);
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
    const y = e.clientY;
    lastX = e.clientX;
    const dy = lastY - y;                 // finger up = positive = later output
    acc += dy; travel += Math.abs(dy);
    lastY = y;
    // Smoothed, so one stuttery last frame does not decide the whole fling
    vel = vel * 0.4 + (dy / Math.max(1, e.timeStamp - velAt)) * 0.6;
    velAt = e.timeStamp;
    const h = rowH();
    const lines = Math.trunc(acc / h);
    if (!lines) return;
    acc -= lines * h;
    scrollRows(lines, lastX, y);
  });

  const lift = (e) => {
    if (e.pointerType !== "touch") return;
    pts.delete(e.pointerId);
    if (pts.size < 2) pinchD = null;
    if (e.pointerId === panId) {
      panId = null;
      // A tap is not a swipe: hand it the keyboard. (We capture the pointer, so
      // the tap-to-focus the browser would have done itself is ours to do.)
      if (travel < 8) t.term.focus();
      // Let go mid-swipe → glide on. A finger that had already stopped moving
      // (no move for a moment) is a hold, not a flick.
      else if (e.timeStamp - velAt < 120) startFling(vel);
    }
    // one finger lifted off a pinch: the other one goes back to panning
    if (pts.size === 1 && panId === null) {
      const id = Array.from(pts.keys())[0], p = pts.get(id);
      startPan(id, p.x, p.y, e.timeStamp);
      travel = 99;                        // it was a pinch — releasing it is no tap
    }
  };
  t.el.addEventListener("pointerup", lift);
  t.el.addEventListener("pointercancel", lift);

  // The gesture is handled above; this only stops xterm's own touch scrolling
  // (bound to .xterm inside us) and any browser panning from also running.
  t.el.addEventListener("touchmove", (e) => {
    e.stopPropagation();
    e.preventDefault();
  }, { passive: false, capture: true });
}

function wireTermKeys() {
  const bar = $("#term-keys");
  bar.addEventListener("pointerdown", (e) => e.preventDefault());
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
      if (!sel) { kbToast("Nothing selected — long-press to select first", "err"); return; }
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
function termFontSize() {
  const n = parseInt((() => { try { return localStorage.getItem("kbTermFont"); }
                             catch (e) { return null; } })(), 10);
  return Number.isFinite(n) && n >= 9 && n <= 24 ? n : 13;
}
function setTermFontSize(n) {
  n = Math.min(24, Math.max(9, n));
  try { localStorage.setItem("kbTermFont", String(n)); } catch (e) { /* private mode */ }
  for (const t of terms) t.term.options.fontSize = n;
  fitTerm(activeTerm);
  // No toast: the text visibly changing size IS the feedback, and during a
  // pinch this fires many times a second.
}

function setTermMax(on) {
  document.body.classList.toggle("term-max", on);
  // The keyboard-pinning in wireViewport() sets an inline height on the fixed
  // panel; carrying that into the other mode would freeze the panel at a stale
  // size. Clear it — the next visualViewport resize re-applies if needed.
  $("#terminal-panel").style.height = "";
  try { localStorage.setItem("kbTermMode", on ? "max" : "half"); } catch (e) { /* private mode */ }
  const b = $("#term-max");
  if (b) { b.textContent = on ? "⤡" : "⤢"; b.title = on ? "Shrink to half screen" : "Full screen"; }
  requestAnimationFrame(() => fitTerm(activeTerm));
}
function preferredTermMax() {
  try { return (localStorage.getItem("kbTermMode") || "max") === "max"; }
  catch (e) { return true; }
}

function toggleTerminalPanel() {
  if (!canShell) return;
  const panel = $("#terminal-panel");
  if (panel.hidden) {
    closeNav();   // the drawer would cover the panel on mobile
    panel.hidden = false;
    if (isMobile()) setTermMax(preferredTermMax());
    if (!terms.length) newTerminal();
    else if (activeTerm) activateTerm(activeTerm, true);
    saveSession();
  } else {
    hideTerminalPanel();
  }
}
function hideTerminalPanel() {
  $("#terminal-panel").hidden = true;
  document.body.classList.remove("term-max");
  saveSession();
}

function openTermWith(cmd) {
  if (!canShell) { kbToast("This account has no terminal access", "err"); return; }
  closeNav();
  newTerminal(cmd);
}

function renderTermTabs() {
  const host = $("#term-tabs");
  host.innerHTML = "";
  for (const t of terms) {
    const el = document.createElement("div");
    el.className = "term-tab" + (t === activeTerm ? " active" : "");
    const name = document.createElement("span");
    name.textContent = (t.connected === false ? "⟳ " : "") + t.name;
    el.appendChild(name);
    const x = document.createElement("button");
    x.className = "term-x"; x.title = "Kill terminal"; x.textContent = "×";
    x.addEventListener("click", (e) => { e.stopPropagation(); killTerminal(t); });
    el.appendChild(x);
    el.addEventListener("click", () => {
      activateTerm(t, true);
      if (t.connected === false) retryTermsNow();   // clicking a stalled tab retries now
    });
    host.appendChild(el);
  }
}

const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
// The modifier that forces a LOCAL text selection when a mouse-tracking app
// (claude code, htop, vim) owns the mouse: xterm hard-codes Shift on Linux/
// Windows and Option (Alt) on Mac — and only if macOptionClickForcesSelection
// is on, which we enable below.
const SELECT_MODIFIER = IS_MAC ? "⌥ Option" : "Shift";

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
    if (ev.code === "KeyV" && (ev.shiftKey || !IS_MAC)) return false;
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
function newTerminal(cmd, sid, savedName) {
  const wasHidden = $("#terminal-panel").hidden;
  $("#terminal-panel").hidden = false;
  // Every door into the terminal honours the mobile mode, not just the toggle —
  // launcher buttons and session restore land here too, and each of them must
  // also get the drawer out of the way (a full-screen terminal covers the ☰).
  if (wasHidden && isMobile()) { closeNav(); setTermMax(preferredTermMax()); }
  const el = document.createElement("div");
  el.className = "term-content";
  $("#terminal").appendChild(el);
  // Render size is the user's choice (A−/A+ in the keybar, persisted). The
  // iOS no-focus-zoom constraint (16px minimum on the FOCUSED element) is
  // satisfied in CSS instead, by pinning xterm's hidden textarea to 16px —
  // the glyphs on screen are free to be any size.
  const term = new Terminal({
    fontSize: termFontSize(),
    fontFamily: TERM_FONT, theme: TERM_THEME,
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
  term.open(el);
  const name = savedName ||
    (cmd ? cmd.trim().split(/\s+/)[0].slice(0, 14) : "bash " + (termSeq + 1));
  const t = { id: ++termSeq, name, el, term, fit, ws: null,
              rx: 0,             // bytes received, in the session's coordinates
              retries: 0, reTimer: null, exited: false, connected: true, bn: null,
              cmd, cmdSent: !cmd,
              sid: sid || Array.from(crypto.getRandomValues(new Uint8Array(8)),
                                     (b) => b.toString(16).padStart(2, "0")).join("") };
  wireTouchScroll(t);
  wireTermClipboard(t);
  connectTerm(t);
  term.onData((d) => sendData(t, d));
  term.onScroll(() => { if (t === activeTerm) paintTermLive(t); });
  terms.push(t);
  activateTerm(t, true);
  saveSession();
  return t;
}

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
  const buf = t && t.term.buffer.active;
  b.classList.toggle("away", !!buf && buf.type === "normal" && buf.viewportY < buf.baseY);
}

function activateTerm(t, focus) {
  activeTerm = t;
  window.__kbterm = t ? t.term : null;   // test hook
  for (const o of terms) o.el.style.display = o === t ? "" : "none";
  renderTermTabs();
  paintTermLive(t);
  saveSession();   // which terminal is active is part of "where you left off"
  // When the activation wasn't user-initiated (a background shell exited and a
  // neighbour got promoted), only take focus if it was already in the panel —
  // never yank the user out of the editor.
  if (focus === undefined) {
    const panel = $("#terminal-panel");
    focus = panel.contains(document.activeElement) ||
      document.activeElement === document.body;
  }
  // Fit only AFTER layout settles, or FitAddon measures a pre-constraint height.
  requestAnimationFrame(() => { fitTerm(t); if (focus) t.term.focus(); });
}

function killTerminal(t) {
  const i = terms.indexOf(t);
  if (i < 0) return;
  terms.splice(i, 1);
  if (t.stopFling) t.stopFling();   // a glide outliving term.dispose() would throw
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
  saveSession();
  t.term.dispose();
  t.el.remove();
  if (activeTerm === t) { activeTerm = null; window.__kbterm = null; }
  if (!terms.length) { hideTerminalPanel(); renderTermTabs(); return; }
  if (!activeTerm) activateTerm(terms[Math.min(i, terms.length - 1)]);
  else renderTermTabs();
}

function fitTerm(t) {
  if (!t || $("#terminal-panel").hidden) return;
  try { t.fit.fit(); } catch (e) { /* container not laid out yet */ }
  sendResize(t);
}

function sendResize(t) {
  if (!t || !t.ws || t.ws.readyState !== 1) return;
  t.ws.send(JSON.stringify({ resize: { rows: t.term.rows, cols: t.term.cols } }));
}

// Drag the panel's top edge to resize it, like the VS-Code panel divider.
// Pointer events (with capture) cover mouse, touch and pen with one handler.
function wireTermResizer() {
  // Free-drag resize is a desktop affordance. On touch the handle is a 6px
  // target that fights scrolling and the keyboard — the ⤢ snap states replace
  // it there (the CSS hides the handle; this spares the dead listeners).
  if (window.matchMedia("(pointer: coarse)").matches) return;
  const handle = $("#term-resizer");
  const panel = $("#terminal-panel");
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    const startY = e.clientY;
    const startH = panel.getBoundingClientRect().height;
    const move = (ev) => {
      const h = Math.min(Math.max(startH + (startY - ev.clientY), 110),
                         Math.round(window.innerHeight * 0.8));
      panel.style.height = h + "px";
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
}

// ---- boot -----------------------------------------------------------------
async function boot() {
  try { _deepLinksOk = (await fetch("/company", { method: "HEAD" })).ok; }
  catch (e) { /* old hub — URLs stay at / until it restarts */ }
  await loadWhoami();
  await loadTree();
  wireSearch(); wireNewDoc(); wireUpload(); wireTerminal(); wireMdBar(); wireNav();
  wireShortcuts(); wireTreeKeys();
  // null-guarded: a browser holding a cached older app.html must not lose the
  // whole boot sequence over one missing button
  // icon + label markup lives in app.html now
  const keysBtn = $("#keys-btn");
  if (keysBtn) keysBtn.addEventListener("click", openShortcuts);
  // Dictation. Hidden outright where it cannot work (no MediaRecorder, or an
  // insecure context — getUserMedia needs https or localhost), which also makes
  // `when: dictationReady` false and hands F9 back to the shell.
  const micBtn = $("#mic-btn");
  const dhBtn = $("#dict-hist-btn");   // null-guarded: cached older app.html
  if (micBtn && dictationReady()) {
    initDictation({ resolveTarget: dictationTarget, insert: insertDictation,
                    toast: kbToast, button: micBtn });
    if (dhBtn) dhBtn.addEventListener("click", openDictHistory);
  } else if (micBtn) {
    micBtn.hidden = true;
    if (dhBtn) dhBtn.hidden = true;    // no mic here → the history would stay empty
    const mdMic = document.querySelector('#mdbar button[data-md="mic"]');
    if (mdMic) mdMic.hidden = true;
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
    const lbl = $("#search-label");
    if (lbl) lbl.textContent = "Search files and contents…  " + comboLabel("Mod+K");
  }
  $("#cron-btn").addEventListener("click", openCron);
  // Show the Admin panel to platform admins (sudo group) — and, network-section
  // only, to users delegated write access on .claude/egress.json.
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
      if (!isAdmin) b.textContent = "Network";
      b.addEventListener("click", openAdmin);
    }
  } catch (e) { /* not admin */ }
  loadLaunchers();
  // learn the backend's pty protocol version (gates pings + offset replay);
  // fire-and-forget — terminals opened before the answer use the safe fallback
  if (canShell) {
    fetch("/api/cron").then((r) => r.json())
      .then((j) => { backendV = j.v || 0; }).catch(() => { /* old backend */ });
  }
  await restoreSession();   // reopen tabs + reattach terminals from last time
  // deep link: a shared /company/….md URL wins over the restored active tab
  const deep = pathFromUrl();
  if (deep) await openDeepLink(deep);
  // On a phone the file list is the home screen — but only when there is no
  // home to come back to. Restoring a document or a terminal means the drawer
  // would open ON TOP of it, which is decided here, after the restore, rather
  // than opening it early and hoping something closes it again.
  if (isMobile() && !tabs.length && $("#terminal-panel").hidden)
    document.body.classList.add("nav-open");
  syncUrl(true);   // boot never adds a history entry, it just settles the URL
  window.addEventListener("popstate", () => {
    const p = pathFromUrl();
    if (p) openDeepLink(p);
  });
  // the context menu follows the pointer, and dies with it
  document.addEventListener("click", closeCtxMenu);
  document.addEventListener("contextmenu", (e) => {
    if (!e.target.closest(".tree-item")) closeCtxMenu();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCtxMenu(); }, true);
  $("#tree").addEventListener("scroll", closeCtxMenu, true);
  window.addEventListener("blur", closeCtxMenu);
  syncTestHooks();
  window.__kbrerender = rerenderTree;   // test hook: force a tree repaint
  // Live filesystem: newly created or newly shared files appear on their own.
  setInterval(() => loadTree(false), 4000);
  // Presence: who has which doc open, painted onto the tree rows.
  loadPresence();
  setInterval(loadPresence, 4000);
  // Company launcher buttons added by an admin show up without a reload.
  setInterval(loadLaunchers, 30000);
}
boot();
