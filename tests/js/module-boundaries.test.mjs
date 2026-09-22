// node --test tests/js — the frontend is plain ES modules with no bundler
// checks worth the name: esbuild resolves IMPORTS, and leaves an identifier
// nobody declares as a reference to a global. The page then throws the first
// time that line runs, which may be days later and in somebody else's hands.
//
// That is exactly how the first attempt at pulling the rich editor out of
// app.js failed (2026-09-22): richview.js used `StateEffect` and imported
// only `StateField`, and the app booted to a blank page. This test walks the
// names the frontend modules declare and export and fails when one of them
// is used somewhere that neither declares nor imports it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const FILES = ["app.js", "richview.js", "publicdoc.js", "chat.js", "layout.js",
               "views.js", "settings.js", "events.js", "dictation.js"]
  .map((f) => "frontend/src/" + f);

// What a file OWNS at its top level: the names another file could be
// borrowing without saying so.
function declared(src) {
  const out = new Set();
  let m;
  const decl = /^(?:export\s+)?(?:async\s+)?(?:function\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)/gm;
  while ((m = decl.exec(src))) out.add(m[1]);
  const destructured = /^(?:export\s+)?(?:const|let|var)\s*[{[]([^}\]]+)[}\]]\s*=/gm;
  while ((m = destructured.exec(src))) {
    for (const part of m[1].split(",")) {
      const n = part.split(":").pop().split("=")[0].trim();
      if (/^[A-Za-z_$][\w$]*$/.test(n)) out.add(n);
    }
  }
  return out;
}

// Every name a file binds ANYWHERE — locals, parameters, catch bindings. A
// borrowed name is only a borrowed name when the file has no binding of its
// own for it, and `el`, `status` and `view` are locals in half these files.
function bound(src) {
  const out = new Set();
  let m;
  const decl = /(?:function\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)/g;
  while ((m = decl.exec(src))) out.add(m[1]);
  const destructured = /(?:const|let|var)\s*[{[]([^}\]]{0,200}?)[}\]]\s*=/g;
  while ((m = destructured.exec(src))) {
    for (const part of m[1].split(",")) {
      const n = part.split(":").pop().split("=")[0].replace(/\.\.\./, "").trim();
      if (/^[A-Za-z_$][\w$]*$/.test(n)) out.add(n);
    }
  }
  const params = /(?:function\*?\s*[A-Za-z_$\w]*\s*|catch\s*|=>\s*)?\(([^()]{0,300})\)\s*(?:=>|{)/g;
  while ((m = params.exec(src))) {
    for (const part of m[1].split(",")) {
      const n = part.split("=")[0].replace(/\.\.\./, "").trim();
      if (/^[A-Za-z_$][\w$]*$/.test(n)) out.add(n);
    }
  }
  const one = /(?:^|[^\w$.])([A-Za-z_$][\w$]*)\s*=>/g;
  while ((m = one.exec(src))) out.add(m[1]);
  const loops = /for\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g;
  while ((m = loops.exec(src))) out.add(m[1]);
  const methods = /^\s{2,}(?:async\s+|get\s+|set\s+|\*)?([A-Za-z_$][\w$]*)\s*\([^()]{0,200}\)\s*{/gm;
  while ((m = methods.exec(src))) out.add(m[1]);
  return out;
}

function imported(src) {
  const out = new Set();
  let m;
  const re = /^import\s+([\s\S]*?)\s+from\s+["'][^"']+["'];/gm;
  while ((m = re.exec(src))) {
    const clause = m[1];
    const braces = clause.match(/{([\s\S]*?)}/);
    if (braces) {
      for (const part of braces[1].split(",")) {
        const n = part.trim().split(/\s+as\s+/).pop().trim();
        if (n) out.add(n);
      }
    }
    for (const part of clause.replace(/{[\s\S]*?}/, "").replace(/\*\s+as\s+/, "").split(",")) {
      const n = part.trim();
      if (/^[A-Za-z_$][\w$]*$/.test(n)) out.add(n);
    }
  }
  return out;
}

// Names that are also ordinary words in this codebase — an object property, a
// local, a method — and would otherwise be reported everywhere they appear.
const AMBIGUOUS = new Set(["init", "host", "openPath", "menu", "toast", "prompt",
                           "confirm", "icons", "principals", "mediaUrl", "state",
                           "view", "sanitize", "parse", "serialize", "commands",
                           // also HTML attributes and object keys inside the
                           // template literals this crude strip cannot always
                           // pair up (a `${…}` holding another template)
                           "shell", "target"]);

test("no module uses a name another module owns", () => {
  const info = new Map();
  const universe = new Set();
  for (const f of FILES) {
    const src = readFileSync(f, "utf8");
    const d = declared(src), i = imported(src);
    info.set(f, { src, have: new Set([...bound(src), ...i]) });
    // one- and two-letter helpers ($, el, I) are everybody's locals
    // Both halves matter: a name a module OWNS (borrowed across the
    // boundary) and a name a module IMPORTS from a library (the file that
    // uses it must import it too — `StateEffect` is how this went wrong).
    for (const n of [...d, ...i]) if (n.length > 2 && !AMBIGUOUS.has(n)) universe.add(n);
  }
  const problems = [];
  for (const [f, { src, have }] of info) {
    // Only real code is evidence: comments, strings, template literals and
    // the import statements themselves all mention names innocently.
    const code = src
      .replace(/^import\s+[\s\S]*?\sfrom\s+["'][^"']+["'];/gm, " ")
      .replace(/\/\*[\s\S]*?\*\//g, " ")
      .replace(/(^|[^:"'`\\])\/\/[^\n]*/gm, "$1 ")
      .replace(/`(?:[^`\\]|\\.)*`/gs, "``")
      // regex literals: /\.(png|svg)$/ is not a use of `svg`
      .replace(/(^|[(,=:[!&|?{};\n]\s*)\/(?![/*])(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n])+\/[gimsuy]*/g, "$1/RE/")
      .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')
      .replace(/'(?:[^'\\\n]|\\.)*'/g, "''");
    for (const n of universe) {
      if (have.has(n)) continue;
      // a use, not a property access and not a key in an object literal
      if (new RegExp(`(^|[^\\w$.])${n}\\s*[({[.,;)=<>!+*/&|?\\]}\\s-]`, "m").test(code)) {
        problems.push(`${f}: uses "${n}", which it neither declares nor imports`);
      }
    }
  }
  assert.deepEqual(problems, [], problems.join("\n"));
});
