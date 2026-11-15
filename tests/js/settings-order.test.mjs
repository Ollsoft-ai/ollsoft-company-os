// node --test tests/js — the settings store (frontend/src/settings.js) adopts
// answers in the order it asked: a slow, older answer arriving last must not
// undo a newer one (the boot fetch against a catch-up after a change).
import { test } from "node:test";
import assert from "node:assert/strict";

const pending = [];
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.fetch = (url, opts) => new Promise((resolve) => pending.push({ url, opts, resolve }));
const answer = (bg) => ({ ok: true, json: async () => ({ schema: [], effective: { "ui.theme.custom": { bg } }, source: {} }) });

const { settings } = await import("../../frontend/src/settings.js");

test("an older answer that arrives last is not adopted", async () => {
  const seen = [];
  settings.subscribe("ui.theme.custom", (v) => seen.push(v.bg));
  const first = settings.fetch();           // asked before the change
  const second = settings.fetch();          // asked after it
  const [a, b] = pending.splice(0);
  b.resolve(answer("#123456"));
  await second;
  a.resolve(answer("#000000"));
  await first;
  assert.deepEqual(settings.get("ui.theme.custom"), { bg: "#123456" });
  assert.deepEqual(seen, ["#123456"], "the stale value never reached a subscriber");
});

test("answers in order are all adopted", async () => {
  const p1 = settings.fetch();
  pending.shift().resolve(answer("#111111"));
  await p1;
  assert.equal(settings.get("ui.theme.custom").bg, "#111111");
  const p2 = settings.fetch();
  pending.shift().resolve(answer("#222222"));
  await p2;
  assert.equal(settings.get("ui.theme.custom").bg, "#222222");
});
