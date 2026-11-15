// Which files open in the text viewer (textview.js), with which highlighting,
// and the two small parsers it needs. Pure — no DOM — so app.js can route by
// it without loading the viewer, and tests/js/textfiles.test.mjs runs it.

// extension → highlight.js language ("" = no highlighting, plain text)
const LANG = {
  txt: "", log: "", csv: "", tsv: "",
  json: "json", jsonl: "json", ndjson: "json", geojson: "json",
  yaml: "yaml", yml: "yaml",
  toml: "ini", ini: "ini", cfg: "ini", conf: "ini", env: "ini", properties: "ini",
  xml: "xml",
  sh: "bash", bash: "bash", zsh: "bash",
  py: "python", js: "javascript", mjs: "javascript", cjs: "javascript", ts: "typescript",
  css: "css", sql: "sql", go: "go", rs: "rust", java: "java",
  diff: "diff", patch: "diff",
};

export function extOf(path) {
  const name = String(path || "").split("/").pop();
  const i = name.lastIndexOf(".");
  return i > 0 || (i === 0 && name.length > 1) ? name.slice(i + 1).toLowerCase() : "";
}

// Opens in the text viewer: a known text extension, outside `_secrets/`.
export function isTextPath(path) {
  if (String(path || "").split("/").includes("_secrets")) return false;
  return Object.prototype.hasOwnProperty.call(LANG, extOf(path));
}

export const languageFor = (path) => LANG[extOf(path)] || "";
export const isTablePath = (path) => ["csv", "tsv"].includes(extOf(path));

// RFC 4180: quoted fields may hold the separator, quotes ("" for one) and line
// breaks. A trailing newline does not make an empty last row.
export function parseCsv(text, sep = ",") {
  const rows = [];
  let row = [], field = "", quoted = false, i = 0;
  const s = String(text || "");
  while (i < s.length) {
    const c = s[i];
    if (quoted) {
      if (c === '"') {
        if (s[i + 1] === '"') { field += '"'; i += 2; continue; }
        quoted = false; i++; continue;
      }
      field += c; i++; continue;
    }
    if (c === '"' && field === "") { quoted = true; i++; continue; }
    if (c === sep) { row.push(field); field = ""; i++; continue; }
    if (c === "\r" && s[i + 1] === "\n") { i++; continue; }
    if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; i++; continue; }
    field += c; i++;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  return rows;
}

// Where a JSON text stops being JSON: the offset of the first character that
// cannot continue it, or -1. Run only after JSON.parse has already failed, to
// name the line — engines word (and position) their errors differently.
const NUM = /-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/y;
export function jsonErrorAt(s) {
  let i = 0;
  const fail = () => { throw i; };
  const ws = () => { while (i < s.length && (s[i] === " " || s[i] === "\t" || s[i] === "\n" || s[i] === "\r")) i++; };
  const str = () => {
    i++;
    while (i < s.length) {
      const c = s[i];
      if (c === '"') { i++; return; }
      if (c === "\\") { i += 2; continue; }
      if (c < " ") fail();
      i++;
    }
    fail();
  };
  const value = () => {
    ws();
    const c = s[i];
    if (c === "{" || c === "[") {
      const close = c === "{" ? "}" : "]";
      i++; ws();
      if (s[i] === close) { i++; return; }
      for (;;) {
        if (c === "{") { ws(); if (s[i] !== '"') fail(); str(); ws(); if (s[i] !== ":") fail(); i++; }
        value(); ws();
        if (s[i] === ",") { i++; continue; }
        if (s[i] === close) { i++; return; }
        fail();
      }
    }
    if (c === '"') return str();
    for (const w of ["true", "false", "null"]) if (s.startsWith(w, i)) { i += w.length; return; }
    NUM.lastIndex = i;
    const m = NUM.exec(s);
    if (!m) fail();
    i += m[0].length;
  };
  try { value(); ws(); if (i < s.length) fail(); return -1; }
  catch (e) { if (typeof e === "number") return Math.min(e, s.length); return -1; }   // RangeError: nested too deep
}

// null when it parses; otherwise a short message naming the line.
// JSON Lines: every non-empty line on its own.
export function jsonProblem(text, lines = false) {
  const s = String(text || "");
  if (lines) {
    const all = s.split("\n");
    for (let n = 0; n < all.length; n++) {
      if (!all[n].trim()) continue;
      try { JSON.parse(all[n]); } catch (e) { return "line " + (n + 1) + ": not valid JSON"; }
    }
    return null;
  }
  try { JSON.parse(s); return null; } catch (e) {
    const at = jsonErrorAt(s);
    if (at < 0) return String(e.message).split("\n")[0];
    const before = s.slice(0, at).split("\n");
    return "not valid JSON at line " + before.length + ", column " + (before[before.length - 1].length + 1);
  }
}
