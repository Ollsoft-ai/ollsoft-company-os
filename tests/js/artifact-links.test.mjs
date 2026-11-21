// node --test tests/js — the click handler the platform adds to every
// artifact (kb_platform/artifact_links.js), run against fake clicks: which
// links become a kb-open message for the app, and which are left alone.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC = readFileSync(new URL("../../kb_platform/artifact_links.js", import.meta.url), "utf8");

// The page at `dir`: returns click(href, opts) → the message sent to the app,
// or null, and whether the click was taken over.
function load(dir) {
  const on = {};
  const sent = [];
  const attrs = {}, props = {};
  const window = { addEventListener: (type, fn) => { on[type] = fn; } };
  const parent = { postMessage: (m) => sent.push(JSON.parse(JSON.stringify(m))) };
  const document = { documentElement: {
    setAttribute: (k, v) => { attrs[k] = v; },
    style: { setProperty: (k, v) => { props[k] = v; } },
  } };
  vm.runInNewContext(SRC.replace("__KB_ARTIFACT_DIR__", JSON.stringify(dir)), { window, parent, document });
  return { on, sent, attrs, props, parent };
}

function page(dir) {
  const { on, sent } = load(dir);
  const onclick = on.click;
  return (href, opts = {}) => {
    sent.length = 0;
    // the browser's resolved href: protocol-relative takes the app's scheme
    const resolved = typeof href === "string" && href.startsWith("//") ? "https:" + href : href;
    const a = { getAttribute: (n) => (n === "href" ? href : null), href: resolved };
    const ev = {
      button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, defaultPrevented: false,
      ...opts,
      target: { closest: () => (href === undefined ? null : a) },
      preventDefault() { this.taken = true; },
    };
    onclick(ev);
    // made in another realm: compare as plain data
    return { msg: sent[0] ? JSON.parse(JSON.stringify(sent[0])) : null, taken: !!ev.taken };
  };
}

test("a root-relative link opens that file, a #L suffix carries the line", () => {
  const click = page("company");
  assert.deepEqual(click("/company/notes/x.md").msg, { type: "kb-open", path: "company/notes/x.md", line: 0 });
  assert.deepEqual(click("/projects/acme/plan.md#L12").msg, { type: "kb-open", path: "projects/acme/plan.md", line: 12 });
  assert.equal(click("/company/notes/x.md").taken, true, "the frame does not navigate itself");
});

test("a relative link is resolved against the artifact's own folder", () => {
  const click = page("projects/acme/dash");
  assert.equal(click("plan.md").msg.path, "projects/acme/dash/plan.md");
  assert.equal(click("../brief.md").msg.path, "projects/acme/brief.md");
  assert.equal(click("./a%20b.md").msg.path, "projects/acme/dash/a b.md", "escaped names are decoded");
  assert.equal(click("../../../../x.md").msg, null, "nothing above the knowledgebase");
});

test("a link to another artifact carries its #fragment; other anchors on a file do not", () => {
  const click = page("company/servers");
  assert.deepEqual(click("console.html#HEAD-3").msg,
                   { type: "kb-open", path: "company/servers/console.html", line: 0, frag: "#HEAD-3" });
  assert.deepEqual(click("/company/notes/x.md#intro").msg, { type: "kb-open", path: "company/notes/x.md", line: 0 });
  assert.equal(click("board.html#a b").msg.frag, undefined, "only plain characters pass");
  assert.equal(click("board.html#" + "x".repeat(129)).msg.frag, undefined, "and not too many");
});

test("an outside address is copied, never opened", () => {
  const click = page("company");
  for (const [href, want] of [["https://example.com/a?b=1#c", "https://example.com/a?b=1#c"],
                              ["http://example.com/", "http://example.com/"],
                              ["//example.com/x.md", "https://example.com/x.md"]]) {
    const r = click(href);
    assert.deepEqual(r.msg, { type: "kb-clipboard", text: want, link: true }, href);
    assert.equal(r.taken, true, "the frame does not navigate to " + href);
  }
  assert.equal(click("https://example.com/", { ctrlKey: true }).msg, null, "modified clicks are the page's");
  assert.equal(click("https://example.com/", { defaultPrevented: true }).msg, null, "the page handled it itself");
});

test("the page asks for the theme at once and wears what the app sends", () => {
  const { on, sent, attrs, props, parent } = load("company");
  assert.deepEqual(sent[0], { type: "kb-theme-get" });
  on.message({ source: parent, data: { type: "kb-theme", theme: "solarized-light", mode: "light",
                                       tokens: { bg: "#fdf6e3", ink: "#586e75", "Bad Key": "x", n: 3 } } });
  assert.equal(attrs["data-kb-theme"], "solarized-light");
  assert.equal(attrs["data-kb-mode"], "light");
  assert.deepEqual(props, { "--kb-bg": "#fdf6e3", "--kb-ink": "#586e75" });
  on.message({ source: {}, data: { type: "kb-theme", theme: "evil", tokens: { bg: "red" } } });
  assert.equal(attrs["data-kb-theme"], "solarized-light", "only the app itself is listened to");
});

test("anchors, other schemes and modified clicks are left alone", () => {
  const click = page("company");
  for (const href of ["#top", "mailto:a@b.c", "ftp://example.com/x",
                      "javascript:alert(1)", "data:text/html,x", "", undefined]) {
    const r = click(href);
    assert.equal(r.msg, null, String(href));
    assert.equal(r.taken, false, String(href));
  }
  assert.equal(click("/company/x.md", { ctrlKey: true }).msg, null);
  assert.equal(click("/company/x.md", { button: 1 }).msg, null);
  assert.equal(click("/company/x.md", { defaultPrevented: true }).msg, null, "the page handled it itself");
});
