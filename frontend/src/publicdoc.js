// The page a stranger gets when a public link points at a document.
//
// It is the SAME writing surface the app uses — `richview.js`, the rendered
// markdown, the tables you type into, the checkboxes — mounted without any of
// the app behind it: no session, no CRDT, no tree, no agents. What it has
// instead is one file, reached through the share's own URL, and a save that
// checks the file has not moved under it.
//
// Everything it talks to is the container: `__raw` to read, `__stat` to
// notice somebody else's change, `__save` to write. It never learns where the
// document really lives.
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, drawSelection, dropCursor, lineNumbers,
         highlightActiveLine } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { search as cmSearch, searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { markdown } from "@codemirror/lang-markdown";
import { Strikethrough, TaskList, Table } from "@lezer/markdown";
import { syntaxHighlighting } from "@codemirror/language";
import { init as initRichView, mdHighlight, calibrateListMetrics, tableField,
         livePreview, spacedLinks, listIndent, todoInputRule, drawWhole } from "./richview.js";

const conf = JSON.parse(document.getElementById("kb-conf").textContent);
const BASE = conf.base;                    // /s/<id>/<token>
const DIR = conf.path.includes("/") ? conf.path.slice(0, conf.path.lastIndexOf("/")) : "";
const share = (rel) => BASE + "/" + rel.split("/").map(encodeURIComponent).join("/");

// ---- the one bit of chrome this page owns ----------------------------------
const statusEl = document.getElementById("status");
let statusTimer = 0;
function status(text, kind) {
  if (!statusEl) return;
  statusEl.textContent = text;
  statusEl.title = text;                   // the phone header truncates it
  statusEl.className = "status" + (kind ? " " + kind : "");
  clearTimeout(statusTimer);
  if (kind === "ok") statusTimer = setTimeout(() => { statusEl.textContent = ""; }, 2500);
}
function toast(msg, kind) { status(msg, kind === "err" ? "err" : "ok"); }

initRichView({
  toast,
  // A link or a picture written relative to the document resolves INSIDE the
  // share: the container serves everything under the same token.
  mediaUrl: (p) => share(p),
  openPath: (rel) => { window.location.href = share(rel); },
  principals: async () => [],              // nobody to @mention from out here
});

// ---- the document -----------------------------------------------------------
const editable = conf.mode === "edit";
let known = conf.mtime;                    // the mtime (ns) our text came from
let knownSize = null;                      // …and its size, for the same reason
let dirty = false, saving = false, conflicted = false;
// Our own swap-in of somebody else's text is a doc change like any other, and
// without this flag it marks the page dirty — which on a read-only share
// produced "somebody else is editing this too" for a visitor who cannot type.
let applying = false;
let saveTimer = 0;

const view = new EditorView({
  parent: document.getElementById("doc"),
  state: EditorState.create({
    doc: conf.text || "",
    extensions: [
      history(),
      keymap.of([
        { key: "Tab", run: (v) => listIndent(v, 1), shift: (v) => listIndent(v, -1) },
        ...searchKeymap, ...defaultKeymap, ...historyKeymap,
      ]),
      cmSearch({ top: true }),
      highlightSelectionMatches(),
      markdown({ extensions: [TaskList, Strikethrough, Table, spacedLinks] }),
      syntaxHighlighting(mdHighlight),
      EditorView.lineWrapping,
      drawSelection(),
      dropCursor(),
      // A markdown document gets the rendered view; a .txt or .csv is plain
      // text and says so, with line numbers, exactly as Source mode does.
      ...(conf.rich
        ? [livePreview(DIR), tableField(DIR), todoInputRule,
           EditorView.editorAttributes.of({ class: "cm-rich" })]
        : [lineNumbers(), highlightActiveLine()]),
      EditorView.updateListener.of((u) => {
        if (!u.docChanged || applying) return;
        dirty = true;
        if (editable) { clearTimeout(saveTimer); saveTimer = setTimeout(save, 1200); }
      }),
      ...(editable ? [] : [EditorState.readOnly.of(true), EditorView.editable.of(false)]),
    ],
  }),
});
calibrateListMetrics(view.contentDOM);
if (!editable) document.body.classList.add("readonly");
// A reader on a touch screen gets the whole document drawn, as in the app
// (see drawWhole): nothing to type here, so no keyboard to make way for.
if (!editable && window.matchMedia("(pointer: coarse)").matches &&
    view.state.doc.length <= 64 * 1024) drawWhole(view, true);

async function save() {
  if (!editable || saving) return;
  saving = true;
  const text = view.state.doc.toString();
  status("Saving…");
  try {
    const r = await fetch(BASE + "/__save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: conf.path, mtime: known, text }),
    });
    if (r.status === 409) {
      conflicted = true;
      status("Somebody else saved this while you were writing — your text is still here; reload to see theirs", "err");
      return;
    }
    if (!r.ok) { status("Could not save (" + r.status + ")", "err"); return; }
    const j = await r.json();
    known = j.mtime || known;
    if (j.size != null) knownSize = j.size;
    dirty = false;
    status("Saved", "ok");
  } catch (e) {
    status("Could not save — check your connection", "err");
  } finally {
    saving = false;
  }
}

// Somebody else's change, without a websocket: ask the container what the
// file's timestamp is every few seconds and pull the text when it moves. A
// reader sees an edit land within seconds; a writer is told rather than
// overwritten. (Real multiplayer would mean this container talking to the
// platform's CRDT daemon, which is exactly the door the design keeps shut.)
async function poll() {
  if (document.hidden) return;
  try {
    const r = await fetch(BASE + "/__stat?path=" + encodeURIComponent(conf.path),
                          { cache: "no-store" });
    if (!r.ok) return;
    const j = await r.json();
    if (!j.mtime || (j.mtime === known && (knownSize == null || j.size === knownSize))) return;
    if (dirty || saving) {                 // theirs and ours both exist: say so
      if (!conflicted) {
        conflicted = true;
        status("Somebody else is editing this too — reload before you save", "err");
      }
      return;
    }
    const raw = await fetch(BASE + "/__raw?path=" + encodeURIComponent(conf.path),
                            { cache: "no-store" });
    if (!raw.ok) return;
    const text = await raw.text();
    known = Number(raw.headers.get("X-Kb-Mtime")) || j.mtime;
    knownSize = j.size;
    if (text === view.state.doc.toString()) return;
    const sel = view.state.selection.main.head;
    applying = true;
    try {
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: text },
        selection: { anchor: Math.min(sel, text.length) },
      });
    } finally { applying = false; }
    status("Updated — somebody else edited this", "ok");
  } catch (e) { /* offline; try again on the next tick */ }
}
setInterval(poll, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

// Leaving with something unsaved in the box is the one loss this page can
// still cause, so it asks.
window.addEventListener("beforeunload", (e) => {
  if (editable && dirty) { e.preventDefault(); e.returnValue = ""; }
});
// Ctrl+S saves now rather than in a second — muscle memory, and reassuring.
window.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (editable) { clearTimeout(saveTimer); save(); }
  }
});
