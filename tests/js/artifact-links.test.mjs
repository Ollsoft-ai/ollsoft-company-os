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
function page(dir) {
  let onclick = null;
  const sent = [];
  const window = { addEventListener: (type, fn) => { if (type === "click") onclick = fn; } };
  const parent = { postMessage: (m) => sent.push(m) };
  vm.runInNewContext(SRC.replace("__KB_ARTIFACT_DIR__", JSON.stringify(dir)), { window, parent });
  return (href, opts = {}) => {
    sent.length = 0;
    const a = { getAttribute: (n) => (n === "href" ? href : null) };
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

test("anchors, external addresses and modified clicks are left alone", () => {
  const click = page("company");
  for (const href of ["#top", "https://example.com/x.md", "//example.com/x.md", "mailto:a@b.c",
                      "javascript:alert(1)", "data:text/html,x", "", undefined]) {
    const r = click(href);
    assert.equal(r.msg, null, String(href));
    assert.equal(r.taken, false, String(href));
  }
  assert.equal(click("/company/x.md", { ctrlKey: true }).msg, null);
  assert.equal(click("/company/x.md", { button: 1 }).msg, null);
  assert.equal(click("/company/x.md", { defaultPrevented: true }).msg, null, "the page handled it itself");
});
