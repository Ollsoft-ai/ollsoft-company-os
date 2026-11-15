// The text viewer: a plain-text file — JSON, YAML, CSV, a log, a config, a
// script — in an app tab instead of a raw browser tab. Read-only: highlighted,
// with line numbers; a CSV or TSV also as a table; JSON checked as it opens.
// Loaded the first time one is opened (import("./textview.js")), so the
// highlighter costs nothing until then. The file is read through
// /api/attachment, as the viewer — whatever they may read, nothing more.
import hljs from "highlight.js/lib/core";
import json from "highlight.js/lib/languages/json";
import yaml from "highlight.js/lib/languages/yaml";
import ini from "highlight.js/lib/languages/ini";
import xml from "highlight.js/lib/languages/xml";
import bash from "highlight.js/lib/languages/bash";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import css from "highlight.js/lib/languages/css";
import sql from "highlight.js/lib/languages/sql";
import go from "highlight.js/lib/languages/go";
import rust from "highlight.js/lib/languages/rust";
import java from "highlight.js/lib/languages/java";
import diff from "highlight.js/lib/languages/diff";
import { extOf, isTablePath, jsonProblem, languageFor, parseCsv } from "./textfiles.js";

const LANGS = { json, yaml, ini, xml, bash, python, javascript, typescript, css, sql, go, rust, java, diff };
for (const [name, def] of Object.entries(LANGS)) if (!hljs.getLanguage(name)) hljs.registerLanguage(name, def);

const MAX_BYTES = 5 * 1024 * 1024;    // past this, offer the raw file instead
const HILITE_MAX = 512 * 1024;        // past this, shown plain: highlighting would stall the tab
const TABLE_MAX_ROWS = 5000;

const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const human = (n) => (n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB");

// shell: { loading(t, label), loaded(t), alive(t) } — the tab spinner and "is the tab still open"
export async function mount(t, shell) {
  const name = t.path.split("/").pop();
  shell.loading(t, "Opening " + name + "…");
  const url = "/api/attachment?path=" + encodeURIComponent(t.path);
  let r = null;
  try { r = await fetch(url); } catch (e) { r = null; }
  if (!shell.alive(t)) return;
  const host = t.el;
  host.classList.add("textview");
  host.textContent = "";
  const raw = () => { const a = el("a", "textview-raw", "Open raw"); a.href = url; a.target = "_blank"; a.rel = "noopener"; return a; };
  const notice = (msg, withRaw) => {
    shell.loaded(t);
    const box = el("div", "textview-msg", msg);
    if (withRaw) { box.append(" "); box.append(raw()); }
    host.append(box);
  };
  if (!r || !r.ok) {
    notice(r && r.status === 403 ? "You don't have access to this file." : "Could not open this file.", false);
    return;
  }
  const declared = Number(r.headers.get("content-length")) || 0;
  if (declared > MAX_BYTES) {
    try { r.body && r.body.cancel(); } catch (e) { /* already done */ }
    notice("This file is " + human(declared) + " — too large to show here.", true);
    return;
  }
  const bytes = new Uint8Array(await r.arrayBuffer());
  if (!shell.alive(t)) return;
  if (bytes.length > MAX_BYTES) { notice("This file is " + human(bytes.length) + " — too large to show here.", true); return; }
  if (bytes.subarray(0, 8000).includes(0)) { notice("This looks like a binary file.", true); return; }
  const text = new TextDecoder("utf-8").decode(bytes);   // a bad byte shows as �, never fails
  shell.loaded(t);
  render(t, host, text, bytes.length, raw, () => { host.textContent = ""; mount(t, shell); });
}

function render(t, host, text, size, raw, reload) {
  const ext = extOf(t.path);
  const lang = languageFor(t.path);
  const lines = text.length ? text.split("\n").length - (text.endsWith("\n") ? 1 : 0) : 0;
  const bar = el("div", "textview-bar");
  bar.append(el("span", "textview-kind", (ext || "text").toUpperCase()),
             el("span", "", human(size) + " · " + lines + (lines === 1 ? " line" : " lines")));
  if (lang === "json") {
    const p = jsonProblem(text, ext === "jsonl" || ext === "ndjson");
    const chk = el("span", "textview-check " + (p ? "bad" : "ok"), p ? "✗ " + p.replace(/\s+/g, " ") : "✓ valid JSON");
    if (p) chk.title = p;
    bar.append(chk);
  }
  bar.append(el("span", "textview-gap"));
  const body = el("div", "textview-body");
  let mode = isTablePath(t.path) ? "table" : "text";
  const draw = () => {
    body.textContent = "";
    if (mode === "table") drawTable(body, text, ext === "tsv" ? "\t" : ",");
    else drawText(body, text, lang, lines);
  };
  if (isTablePath(t.path)) {
    const seg = el("span", "textview-seg");
    for (const m of ["table", "text"]) {
      const b = el("button", m === mode ? "on" : "", m === "table" ? "Table" : "Text");
      b.type = "button";
      b.addEventListener("click", () => { mode = m; seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); draw(); });
      seg.append(b);
    }
    bar.append(seg);
  }
  const again = el("button", "textview-btn", "Reload"); again.type = "button"; again.title = "Read the file again";
  again.addEventListener("click", reload);
  bar.append(again, raw());
  host.append(bar, body);
  draw();
}

function drawText(body, text, lang, lines) {
  const wrap = el("div", "textview-code");
  const gutter = el("pre", "textview-gutter");
  gutter.setAttribute("aria-hidden", "true");
  let nums = "";
  for (let i = 1; i <= Math.max(lines, 1); i++) nums += i + "\n";
  gutter.textContent = nums;
  const pre = el("pre", "textview-text");
  const code = el("code", "hljs");
  if (lang && text.length <= HILITE_MAX) {
    try { code.innerHTML = hljs.highlight(text, { language: lang, ignoreIllegals: true }).value; }   // escaped by hljs
    catch (e) { code.textContent = text; }
  } else code.textContent = text;
  pre.append(code);
  wrap.append(gutter, pre);
  body.append(wrap);
}

function drawTable(body, text, sep) {
  const rows = parseCsv(text, sep);
  if (!rows.length) { body.append(el("div", "textview-msg", "Empty.")); return; }
  const width = Math.max(...rows.slice(0, TABLE_MAX_ROWS + 1).map((r) => r.length));
  const table = el("table", "textview-table");
  const head = el("thead"), hr = el("tr");
  for (let c = 0; c < width; c++) hr.append(el("th", "", rows[0][c] ?? ""));
  head.append(hr);
  const tb = el("tbody");
  for (const r of rows.slice(1, TABLE_MAX_ROWS + 1)) {
    const tr = el("tr");
    for (let c = 0; c < width; c++) tr.append(el("td", "", r[c] ?? ""));
    tb.append(tr);
  }
  table.append(head, tb);
  body.append(table);
  if (rows.length - 1 > TABLE_MAX_ROWS) {
    body.append(el("div", "textview-msg", "Showing the first " + TABLE_MAX_ROWS + " of " + (rows.length - 1) + " rows — the Text view has them all."));
  }
}
