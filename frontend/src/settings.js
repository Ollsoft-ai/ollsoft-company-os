// Settings — the store for everything the registry in kb_platform/settings.py
// declares. Layers, lowest to highest: shipped default → company → yours; one
// value from exactly one layer, and source() says which. The browser holds no
// layer of its own: what is cached here is only the last resolved state, so
// the page can paint (the theme, say) before the first fetch answers.
//
// Not settings, and deliberately not here: the open tabs, the tree state, the
// recent files, the dictation history, and the older per-browser preferences
// (edit mode, terminal font, sidebar width …). Those keep their own
// localStorage keys until each is promoted into the registry.
const LS_KEY = "kbSettings";

let state = null;            // the last /api/settings payload (GET or POST reply)
let cache = {};              // the previous session's effective values, for the first paint
const subs = new Map();      // key | "*" -> Set<fn>

try {
  const raw = localStorage.getItem(LS_KEY);
  if (raw) cache = JSON.parse(raw).effective || {};
} catch (e) { cache = {}; }

function save() {
  try { localStorage.setItem(LS_KEY, JSON.stringify({ effective: state ? state.effective : cache })); }
  catch (e) { /* private mode: in-memory only */ }
}

function notify(key, value, source) {
  for (const fn of subs.get(key) || []) { try { fn(value, key, source); } catch (e) { console.error(e); } }
  for (const fn of subs.get("*") || []) { try { fn(key, value, source); } catch (e) { console.error(e); } }
}

// Adopt a payload as the truth; tell subscribers about every key whose
// effective value changed — so a change from the server applies exactly like
// one made here, through the same subscriber.
function adopt(payload) {
  const before = state ? state.effective : cache;
  state = payload;
  save();
  const keys = new Set([...Object.keys(before || {}), ...Object.keys(payload.effective || {})]);
  for (const k of keys) {
    if (JSON.stringify(before[k]) !== JSON.stringify(payload.effective[k]))
      notify(k, payload.effective[k], payload.source[k]);
  }
  // the dialog re-renders on any adoption (source pills, company values)
  for (const fn of subs.get("*") || []) { try { fn(null); } catch (e) { console.error(e); } }
}

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "content-type": "application/json" },
                               body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) return { ok: false, error: j.error || "could not save the setting" };
  return { ok: true, body: j };
}

async function change(scope, body) {
  if (scope === "company") {
    const r = await post("/admin/settings", body);
    if (!r.ok) return r;
    await settings.fetch();              // re-resolve as me: my layer may still win
    return { ok: true };
  }
  const r = await post("/api/settings", body);
  if (!r.ok) return r;
  adopt(r.body);
  return { ok: true };
}

export const settings = {
  // the effective value: the server's answer once it has arrived, the cache before
  get(key) {
    if (state && key in state.effective) return state.effective[key];
    return cache[key];
  },
  source(key) { return state ? state.source[key] : undefined; },   // "default" | "company" | "user"
  entry(key) { return state ? state.schema.find((e) => e.key === key) : undefined; },
  schema() { return state ? state.schema : []; },
  state() { return state; },
  subscribe(key, fn) {
    if (!subs.has(key)) subs.set(key, new Set());
    subs.get(key).add(fn);
    return () => subs.get(key).delete(fn);
  },
  // never throws: an older backend (404) or a network blip keeps the cache
  async fetch() {
    try {
      const r = await fetch("/api/settings");
      if (!r.ok) return null;
      const j = await r.json();
      if (!j || !j.schema) return null;
      adopt(j);
      return j;
    } catch (e) { return null; }
  },
  set(key, value, scope = "user") { return change(scope, { set: { [key]: value } }); },
  unset(key, scope = "user") { return change(scope, { unset: [key] }); },
};
window.__kbsettings = settings;      // test hook, like window.__kbterms
