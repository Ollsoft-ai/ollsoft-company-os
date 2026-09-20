// Live events — one server-sent event stream per tab, instead of polling.
//
// The connection is the easy part; the rest of this file is what happens when
// it is not there. Every situation ends in the same place: a fresh stream
// whose first event (`hello`) is the catch-up, so nothing that happened while
// we were away is ever needed from the stream itself.
//
//   dropped / network switch      the browser retries; we own the backoff
//   half-open socket              no event for 60 s -> reopen (the server
//                                 sends `ping` every 20 s)
//   tab frozen or throttled       on visibilitychange/pageshow/online: reopen
//                                 if the link is dead or quiet, else a cheap
//                                 catch-up — nothing relies on background timers
//   hub restarted (deploy)        errors, backoff 3 s -> 60 s, backend respawns
//   session expired               EventSource cannot see a 401, so an error is
//                                 followed by one ordinary fetch, which the
//                                 session guard turns into the login redirect
//   older backend / SSE blocked   five failures in a row -> slow polling while
//                                 visible, and the stream is retried every minute
export function connectEvents(h) {
  const S = { mode: "connecting", opens: 0, failures: 0, lastMsg: 0, polls: 0, events: 0,
              es: null, reopen: null };
  let reopenTimer = null, pollTimer = null, lastProbe = 0;
  const now = () => Date.now();

  function close() {
    if (S.es) { try { S.es.close(); } catch (e) { /* already closed */ } S.es = null; }
  }
  function startPolling() {
    if (pollTimer) return;
    S.mode = "poll";
    pollTimer = setInterval(() => { if (!document.hidden) { S.polls++; h.poll(); } }, 15000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  // An expired cookie looks like any other error from here. One guarded fetch
  // settles it (401 -> login), at most every 30 s.
  function probe() {
    if (now() - lastProbe < 30000) return;
    lastProbe = now();
    try { h.probe(); } catch (e) { /* the guard handles it */ }
  }
  function fail() {
    S.failures++;
    close();
    if (S.failures >= 5) startPolling();
    const delay = Math.min(60000, 3000 * 2 ** Math.min(S.failures - 1, 4));
    clearTimeout(reopenTimer);
    reopenTimer = setTimeout(open, delay);
    probe();
  }
  function on(es, name, fn) {
    es.addEventListener(name, (e) => {
      S.lastMsg = now(); S.events++;
      let data = {};
      try { data = e.data ? JSON.parse(e.data) : {}; } catch (x) { return; }
      try { fn(data); } catch (x) { console.error("events:", name, x); }
    });
  }
  function open() {
    clearTimeout(reopenTimer); reopenTimer = null;
    close();
    let es;
    try { es = new EventSource("/api/events"); } catch (e) { fail(); return; }
    S.es = es; S.mode = S.mode === "poll" ? "poll" : "connecting";
    es.addEventListener("open", () => {
      S.opens++; S.failures = 0; S.lastMsg = now(); S.mode = "sse"; stopPolling();
    });
    on(es, "hello", h.hello);
    on(es, "tree", h.tree);
    on(es, "presence", h.presence);
    on(es, "config", h.config);
    on(es, "ping", () => {});
    // The browser would retry on its own (CONNECTING) — or not at all (CLOSED,
    // e.g. a 404 from an older backend). Both go through our backoff instead,
    // so behaviour is the same everywhere and never a 3 s hammer forever.
    es.onerror = () => { if (S.es === es) fail(); };
  }
  S.reopen = open;

  // Dead-link watchdog: a phone that changed networks keeps a socket that
  // will never speak again. Quiet for 60 s (three missed pings) means reopen.
  setInterval(() => {
    if (S.es && S.es.readyState === 1 && now() - S.lastMsg > 60000) { S.failures++; open(); }
  }, 15000);
  // Coming back: a frozen tab's stream is usually dead without a single error
  // having fired. Reopen when it is not open or has gone quiet; otherwise the
  // catch-up alone is enough (an unchanged tree is a 304).
  const wake = () => {
    if (document.hidden) return;
    if (!S.es || S.es.readyState !== 1 || now() - S.lastMsg > 45000) open();
    else h.catchUp();
  };
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("pageshow", wake);
  window.addEventListener("online", wake);
  window.addEventListener("pagehide", close);   // free the server's slot at once

  open();
  window.__kbevents = S;     // test hook
  return S;
}
