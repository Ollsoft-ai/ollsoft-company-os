// Accounts signed in on this browser — the switcher in the user menu.
//
// The hub holds the sessions (kb_session is the active account, kb_accounts
// the others, both httpOnly); this module only shows them and asks the hub to
// switch. What it does own is the BROWSER's state: open tabs, recent files,
// tree state, dictation transcripts and the rest live in localStorage under
// fixed "kb…" keys, which belonged to whoever used the browser last. So the
// keys are the active account's, and the moment the page sees a different
// account it stashes the previous owner's keys and restores this one's. One
// account is active per browser at a time (the cookie is per browser), which
// is what makes a swap at boot enough.

const OWNER = "kbAccountOwner";
const STASH = "kbAccountStash:";
// Per browser, not per account: the list the menu offers (incl. accounts
// whose session ended, so signing back in is one click to a filled-in form).
const KNOWN = "kbAccountsKnown";
const KEEP = new Set([OWNER, KNOWN]);

export const bootUser = (() => {
  const m = document.querySelector('meta[name="kb-user"]');
  return m ? m.getAttribute("content") || "" : "";
})();

function own(key) {
  return key && key.startsWith("kb") && !KEEP.has(key) && !key.startsWith(STASH);
}

// Runs at import, before any other module reads localStorage.
(function swapBrowserState() {
  if (!bootUser) return;
  try {
    const owner = localStorage.getItem(OWNER);
    if (owner && owner !== bootUser) {
      const mine = {};
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (own(k)) mine[k] = localStorage.getItem(k);
      }
      for (const k of Object.keys(mine)) localStorage.removeItem(k);
      localStorage.setItem(STASH + owner, JSON.stringify(mine));
      const back = JSON.parse(localStorage.getItem(STASH + bootUser) || "{}");
      for (const [k, v] of Object.entries(back)) if (own(k)) localStorage.setItem(k, v);
      localStorage.removeItem(STASH + bootUser);
    }
    // No owner yet: an existing browser's state is adopted by the first
    // account that opens it, which is whoever it belonged to before.
    localStorage.setItem(OWNER, bootUser);
    remember(bootUser);
  } catch (e) { /* private mode or quota: nothing to separate */ }
})();

// Once another tab has made someone else the owner, this page is about to
// reload — and must not write its account's tabs into theirs on the way out
// (the open-tabs record is saved on pagehide). Writes to owned keys are
// dropped from then on.
(function guardWrites() {
  if (!bootUser || typeof Storage === "undefined") return;
  const proto = Storage.prototype;
  const set = proto.setItem, del = proto.removeItem, get = proto.getItem;
  const stale = (store, k) => store === window.localStorage && own(k)
    && get.call(store, OWNER) !== bootUser;
  proto.setItem = function (k, v) { if (!stale(this, k)) set.call(this, k, v); };
  proto.removeItem = function (k) { if (!stale(this, k)) del.call(this, k); };
})();

function knownList() {
  try { return JSON.parse(localStorage.getItem(KNOWN) || "[]").filter((u) => typeof u === "string"); }
  catch (e) { return []; }
}

function remember(user) {
  const list = [user, ...knownList().filter((u) => u !== user)].slice(0, 8);
  try { localStorage.setItem(KNOWN, JSON.stringify(list)); } catch (e) { /* ok */ }
}

export function forget(user) {
  try {
    localStorage.setItem(KNOWN, JSON.stringify(knownList().filter((u) => u !== user)));
    localStorage.removeItem(STASH + user);
  } catch (e) { /* ok */ }
}

// The menu's rows: every account signed in now, then the remembered ones whose
// session has ended (they go to the sign-in page, username filled in).
export async function listAccounts() {
  const r = await fetch("/accounts", { headers: { Accept: "application/json" } });
  if (!r.ok) return [];
  const j = await r.json();
  const live = (j.accounts || []).map((a) => ({ ...a, live: true }));
  for (const a of live) remember(a.user);
  const liveSet = new Set(live.map((a) => a.user));
  const ended = knownList().filter((u) => !liveSet.has(u)).map((u) => ({ user: u, name: "", live: false }));
  return [...live, ...ended];
}

const channel = (() => {
  try { return new BroadcastChannel("kb-account"); } catch (e) { return null; }
})();
if (channel) channel.onmessage = (e) => { if (e.data && e.data.user !== bootUser) location.reload(); };

export async function switchTo(user) {
  const r = await fetch("/accounts/switch", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user }),
  });
  if (!r.ok) {
    // the session ended since the menu was drawn: sign in again, prefilled
    location.href = "/login?add=1&user=" + encodeURIComponent(user);
    return;
  }
  if (channel) channel.postMessage({ user });
  // "/" and not reload(): the open file may be one this account cannot read
  location.href = "/";
}

export function addAccount() {
  location.href = "/login?add=1";
}

// Every response names the active account (hub.roll_session). A tab still
// showing someone else — switched in another tab, or another tab signed in —
// reloads instead of acting as the wrong person.
export function checkAccount(resp) {
  const who = resp.headers.get("X-KB-Account");
  if (bootUser && who && who !== bootUser) location.reload();
}
