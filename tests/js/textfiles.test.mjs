// node --test tests/js — which files the text viewer opens, with which
// highlighting, and the CSV and JSON checks it shows (frontend/src/textfiles.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { extOf, isTextPath, isTablePath, jsonErrorAt, jsonProblem, languageFor, parseCsv } from "../../frontend/src/textfiles.js";

test("text files open in the viewer; documents, artifacts, binaries and secrets do not", () => {
  for (const p of ["company/a.json", "company/b.YAML", "x/c.yml", "x/d.csv", "x/e.tsv", "x/f.txt", "x/g.log",
                   "x/h.toml", "x/i.xml", "x/run.sh", "x/m.py", "x/n.ts", "x/.env"]) assert.ok(isTextPath(p), p);
  for (const p of ["company/a.md", "company/a.html", "x/p.pdf", "x/i.png", "x/d.docx", "x/Makefile", "x/folder",
                   "projects/p/_secrets/key.json", "x/_secrets/a.txt"]) assert.ok(!isTextPath(p), p);
});

test("highlighting follows the extension", () => {
  assert.equal(languageFor("a/b.json"), "json");
  assert.equal(languageFor("a/b.jsonl"), "json");
  assert.equal(languageFor("a/b.yml"), "yaml");
  assert.equal(languageFor("a/b.toml"), "ini");
  assert.equal(languageFor("a/b.csv"), "", "tables and plain text are not highlighted");
  assert.equal(extOf("a/.env"), "env");
  assert.equal(extOf("a/noext"), "");
  assert.ok(isTablePath("a/b.tsv") && !isTablePath("a/b.json"));
});

test("CSV: quotes, separators and line breaks inside fields, CRLF, no phantom last row", () => {
  assert.deepEqual(parseCsv('a,b,c\r\n1,"x, y",3\r\n"he said ""hi""","two\nlines",\n'),
                   [["a", "b", "c"], ["1", "x, y", "3"], ['he said "hi"', "two\nlines", ""]]);
  assert.deepEqual(parseCsv("a\tb\n1\t2", "\t"), [["a", "b"], ["1", "2"]]);
  assert.deepEqual(parseCsv(""), []);
});

test("JSON: fine, broken with the line named, and JSON Lines line by line", () => {
  assert.equal(jsonProblem('{"a": [1, 2]}'), null);
  assert.equal(jsonProblem('{\n  "a": 1,\n  "b": \n}'), "not valid JSON at line 4, column 1");
  assert.equal(jsonProblem('[1, 2,]'), "not valid JSON at line 1, column 7");
  assert.equal(jsonProblem('{"a": "x\\"y" "b"}'), "not valid JSON at line 1, column 14");
  assert.equal(jsonProblem('{"a": 1} x'), "not valid JSON at line 1, column 10");
  assert.equal(jsonProblem('"open'), "not valid JSON at line 1, column 6");
  assert.equal(jsonProblem('{"a":1}\n{"b":2}\n', true), null);
  assert.match(jsonProblem('{"a":1}\n{oops}\n', true), /^line 2: /);
});

test("the scanner agrees with JSON.parse on what is valid", () => {
  for (const ok of ['{"a": [1, -2.5e3, true, false, null, {"b": "\\u00e9\\n"}]}', "[]", " 0 ", '"x"'])
    assert.equal(jsonErrorAt(ok), -1, ok);
  for (const bad of ["{'a': 1}", "[01]", "[1 2]", "{\"a\" 1}", "", "tru"]) assert.ok(jsonErrorAt(bad) >= 0, bad);
});
