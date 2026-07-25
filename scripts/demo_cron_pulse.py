"""Cron demo: bob schedules an every-minute job that feeds a live artifact.

What it builds (idempotent, rerunnable), all through the product as bob:
  company/cron-demo/pulse.sh   executable heartbeat script (runs from bob's crontab)
  company/cron-demo/data.log   file channel — cron appends a line every minute
  u_bob.pulse                 database channel — cron INSERTs a row every minute
                               (SELECT granted to alice + carol; RLS-style GRANT demo)
  company/cron-demo/pulse.html artifact that shows both channels live, as the viewer

The cron entry itself is added through the Cron UI (the point of the demo).
Every action runs with bob's own authority: his terminal, his API, his crontab.

Usage:  .venv/bin/python scripts/demo_cron_pulse.py [--wait-beat] [--shots DIR]
"""
import argparse
import json
import re
import time

import httpx
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))
DEMO_DIR = "company/cron-demo"
CRON_CMD = "/srv/kb/company/cron-demo/pulse.sh >> /home/bob/.pulse.log 2>&1"

PULSE_SH = """#!/bin/bash
# company/cron-demo/pulse.sh — bob's every-minute heartbeat, run by bob's
# crontab (so it runs AS bob, with bob's files + database identity).
# It writes to BOTH channels an artifact can read:
#   file channel:     data.log next to the artifact (read via the kb-read bridge)
#   database channel: u_bob.pulse (read via the SQL bridge; viewers need a GRANT)
set -eu
d="/srv/kb/company/cron-demo"
ts="$(date -Is)"
echo "$ts beat from $(whoami)" >> "$d/data.log"
# trim to the last 40 lines ($$ in the temp name: concurrent runs can't collide;
# the .kbtmp suffix keeps it out of the tree/sync/index)
tmp="$d/data.log.$$.kbtmp"
tail -n 40 "$d/data.log" > "$tmp" && mv "$tmp" "$d/data.log"
psql -q -d kb -c "INSERT INTO u_bob.pulse(note) VALUES ('beat from cron');"
# keep the table bounded (the artifact polls it every 10s per viewer)
psql -q -d kb -c "DELETE FROM u_bob.pulse WHERE id < (SELECT max(id) FROM u_bob.pulse) - 1000;"
# cap the cron log too
log="$HOME/.pulse.log"
if [ -f "$log" ] && [ "$(stat -c%s "$log")" -gt 65536 ]; then : > "$log"; fi
"""

PULSE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #0D1626; color: #E9EFFA; padding: 1.2rem; }
  h1 { font-size: 1.15rem; margin: 0 0 .2rem; }
  h2 { font-size: .85rem; color: #8CA1C1; text-transform: uppercase; letter-spacing: .06em; margin: 1.2rem 0 .4rem; }
  .sub { color: #8CA1C1; font-size: .82rem; margin-bottom: .6rem; }
  .sub b, code { color: #4D9DFF; font-family: ui-monospace, monospace; }
  .alive { color: #2FCE98; } .stale { color: #E5AE58; }
  .dot { display: inline-block; width: .5rem; height: .5rem; border-radius: 50%; background: #2FCE98;
    margin-right: .4rem; animation: pulse 2s infinite; }
  .dot.stale { background: #E5AE58; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
  pre { background: #081020; border: 1px solid #223350; border-radius: 8px; padding: .6rem .8rem;
    font-size: .78rem; overflow-x: auto; max-width: 640px; }
  table { border-collapse: collapse; font-size: .85rem; max-width: 640px; width: 100%; }
  th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #223350; }
  th { color: #8CA1C1; font-weight: 600; font-size: .72rem; text-transform: uppercase; }
  td.mono { font-family: ui-monospace, monospace; }
  .err { background: #331A26; color: #F2809C; padding: .6rem .8rem; border-radius: 8px;
    font-size: .8rem; font-family: ui-monospace, monospace; max-width: 640px; }
</style>
</head>
<body>
  <h1><span class="dot" id="dot"></span>Cron pulse — bob's every-minute job</h1>
  <div class="sub">A <code>* * * * *</code> entry in <b>bob</b>'s crontab runs
    <code>pulse.sh</code> as bob. This artifact reads the results as
    <b id="who">…</b> (you), refreshing every 10s.</div>
  <div class="sub" id="status">loading…</div>

  <h2>File channel — data.log (kb-read bridge)</h2>
  <pre id="filelog">…</pre>

  <h2>Database channel — u_bob.pulse (SQL bridge, needs bob's GRANT)</h2>
  <div id="db">…</div>

<script>
  // bridge SDK: everything goes through the host via postMessage; the host runs
  // it AS THE VIEWER (kernel for files, Postgres GRANTs for the table).
  let _id = 0; const _pending = {};
  window.addEventListener("message", (ev) => {
    const m = ev.data || {};
    if (m.type !== "kb-result" || !_pending[m.id]) return;
    const p = _pending[m.id]; delete _pending[m.id]; p(m);
  });
  const ask = (msg) => new Promise((res) => { const id = ++_id; _pending[id] = res;
    parent.postMessage(Object.assign({ id }, msg), "*"); });
  const kbQuery = (sql, params = []) => ask({ type: "kb-query", sql, params });
  const kbRead = (path) => ask({ type: "kb-read", path });
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  kbQuery("SELECT current_user").then((r) => {
    if (r.rows && r.rows[0]) document.getElementById("who").textContent = r.rows[0][0];
  });

  async function refresh() {
    const f = await kbRead("company/cron-demo/data.log");
    const dot = document.getElementById("dot");
    if (f.error) {
      document.getElementById("filelog").textContent = "read failed: " + f.error;
      dot.className = "dot stale";
      document.getElementById("status").innerHTML = '<span class="stale">file channel unreadable</span>';
    } else {
      const lines = (f.content || "").trim().split("\\n").filter(Boolean);
      document.getElementById("filelog").textContent =
        lines.slice(-8).join("\\n") || "(no beats yet — cron runs on the minute)";
      const last = lines[lines.length - 1];
      const ts = last ? Date.parse(last.split(" ")[0]) : NaN;
      const age = isNaN(ts) ? null : Math.round((Date.now() - ts) / 1000);
      const ok = age !== null && age < 130;
      dot.className = "dot" + (ok ? "" : " stale");
      document.getElementById("status").innerHTML = age === null
        ? "waiting for the first beat…"
        : (ok ? '<span class="alive">alive</span>' : '<span class="stale">stale</span>') +
          " — last beat " + esc(age) + "s ago";
    }
    const q = await kbQuery(
      "SELECT count(*), max(at) FROM u_bob.pulse");
    const db = document.getElementById("db");
    if (q.error) {
      db.innerHTML = '<div class="err">query denied: ' + esc(q.error) +
        "<br>— bob has not granted your role SELECT on u_bob.pulse</div>";
    } else {
      const rows = await kbQuery(
        "SELECT to_char(at, 'HH24:MI:SS') AS at, note FROM u_bob.pulse ORDER BY at DESC LIMIT 6");
      db.innerHTML = "<div class='sub'>" + esc(q.rows[0][0]) + " beats total · latest " +
        esc(q.rows[0][1]) + "</div>" +
        "<table><thead><tr><th>at</th><th>note</th></tr></thead><tbody>" +
        (rows.rows || []).map((r) =>
          "<tr><td class='mono'>" + esc(r[0]) + "</td><td>" + esc(r[1]) + "</td></tr>").join("") +
        "</tbody></table>";
    }
  }
  refresh();
  setInterval(refresh, 10000);
</script>
</body>
</html>
"""


def http(user):
    c = httpx.Client(base_url=BASE, timeout=20)
    r = c.post("/login", data={"username": user, "password": CREDS[user]})
    assert r.status_code == 200, f"login {user}: {r.text}"
    return c


def login_page(ctx, user):
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', CREDS[user])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.wait_for_selector('[data-testid="tree"] .tree-item')
    return page


def term_run(page, cmd, sentinel):
    """Type a command into the user's own web terminal and wait for its sentinel."""
    page.click("#terminal")
    page.keyboard.type(f"{cmd} && echo {sentinel}")
    page.keyboard.press("Enter")
    deadline = time.time() + 15
    while time.time() < deadline:
        txt = page.inner_text("#terminal")
        # sentinel must appear on its own line (not just in the echoed command)
        if re.search(rf"^{re.escape(sentinel)}$", txt, re.M):
            return
        page.wait_for_timeout(200)
    raise RuntimeError(f"terminal command did not finish: {cmd}\n--- buffer:\n{txt[-2000:]}")


def ensure_fresh_backend(browser, user):
    """If this user's backend predates the cron API, have the USER restart it
    from their own terminal (their process, their authority); the hub respawns
    it with the deployed code on the next request."""
    c = http(user)
    r = c.get("/api/cron")
    if r.status_code == 200 and r.json().get("v", 0) >= 4:
        print(f"{user}: backend already current")
        return
    ctx = browser.new_context()
    page = login_page(ctx, user)
    page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    page.click("#terminal")
    # -KILL: a SIGTERM'd backend with a live pty websocket hangs in graceful
    # shutdown forever (observed) — kill it outright; the hub respawns cleanly.
    page.keyboard.type("pkill -KILL -u $(whoami) -f kb_platform.user_server")
    page.keyboard.press("Enter")
    # (don't wait on the terminal UI: since persistent sessions, a dead backend
    # leaves the tab in "reconnecting", it no longer hides the panel)
    page.wait_for_timeout(1000)
    ctx.close()
    for _ in range(20):
        try:
            r = c.get("/api/cron")
            if r.status_code == 200 and r.json().get("v", 0) >= 4:
                print(f"{user}: backend restarted with current code")
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{user}: backend did not come back with cron support")


def sql(c, stmt):
    r = c.post("/api/artifact/query", json={"sql": stmt})
    assert r.status_code == 200, f"{stmt[:60]}…: {r.text}"
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-beat", action="store_true",
                    help="wait ~70s to watch a real cron beat arrive")
    ap.add_argument("--shots", default=None, help="directory for screenshots")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

        for u in ("bob", "carol"):
            ensure_fresh_backend(browser, u)

        bob = http("bob")

        # -- files, as bob, via his own terminal + API ---------------------
        ctx = browser.new_context()
        page = login_page(ctx, "bob")
        page.click('[data-testid="toggle-term"]')
        page.wait_for_selector("#terminal .xterm-rows")
        # data.log is minute-churn — gitignore it BEFORE it exists so syncd's
        # auto-snapshot never starts tracking it (a tracked file ignores
        # .gitignore, and untracking again needs root: git -C /srv/kb rm
        # --cached company/cron-demo/data.log).
        term_run(page, f"mkdir -p /srv/kb/{DEMO_DIR}"
                       f" && printf 'data.log\\n' > /srv/kb/{DEMO_DIR}/.gitignore"
                       f" && touch /srv/kb/{DEMO_DIR}/data.log",
                 "MKDIR_OK")
        for name, content in (("pulse.sh", PULSE_SH), ("pulse.html", PULSE_HTML)):
            r = bob.post("/api/artifact/write",
                          json={"path": f"{DEMO_DIR}/{name}", "content": content})
            assert r.status_code == 200, r.text
        term_run(page, f"chmod +x /srv/kb/{DEMO_DIR}/pulse.sh", "CHMOD_OK")
        print("files: pulse.sh (+x), pulse.html, data.log created as bob")

        # -- database, as bob ---------------------------------------------
        sql(bob, "CREATE TABLE IF NOT EXISTS u_bob.pulse ("
                  "id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(), "
                  "note text NOT NULL)")
        sql(bob, "GRANT USAGE ON SCHEMA u_bob TO alice, carol")
        sql(bob, "GRANT SELECT ON u_bob.pulse TO alice, carol")
        print("db: u_bob.pulse ready; SELECT granted to alice + carol")

        # -- the cron entry, through the Cron UI (the feature under demo) ---
        listing = bob.get("/api/cron").json()
        if any(CRON_CMD in j["command"] and not j["paused"] for j in listing["jobs"]):
            print("cron: entry already installed and active")
        else:
            page.click('[data-testid="cron-btn"]')
            page.wait_for_selector('[data-testid="cron-jobs"]')
            page.fill('[data-testid="cron-schedule"]', "* * * * *")
            page.fill('[data-testid="cron-command"]', CRON_CMD)
            page.click('[data-testid="cron-add"]')
            page.wait_for_selector('.cron-cmd[title*="pulse.sh"]')
            if args.shots:
                page.screenshot(path=f"{args.shots}/1-bob-cron-ui.png")
            page.click(".modal-close")
            print("cron: bob added '* * * * * pulse.sh' through the Cron UI")

        # first beat now, so the artifact has data before the minute ticks over
        term_run(page, f"/srv/kb/{DEMO_DIR}/pulse.sh", "BEAT_OK")
        ctx.close()

        # -- view the artifact as alice (grant path + file path) ----------
        ctx = browser.new_context()
        page = login_page(ctx, "alice")
        page.wait_for_selector(f'.tree-item[data-path="{DEMO_DIR}/pulse.html"]', timeout=15000)
        page.click(f'.tree-item[data-path="{DEMO_DIR}/pulse.html"]')
        frame = page.frame_locator("iframe.artifact-frame")
        frame.locator("tbody tr").first.wait_for(timeout=15000)
        assert "beat from cron" in frame.locator("body").inner_text()
        if args.shots:
            page.screenshot(path=f"{args.shots}/2-alice-views-pulse.png")
        print("artifact: alice sees bob's beats (file + db channels)")

        if args.wait_beat:
            k = http("alice")
            n0 = sql(k, "SELECT count(*) FROM u_bob.pulse")["rows"][0][0]
            print(f"waiting for cron to fire (rows now: {n0})…")
            deadline = time.time() + 150
            while time.time() < deadline:
                time.sleep(10)
                n = sql(k, "SELECT count(*) FROM u_bob.pulse")["rows"][0][0]
                if int(n) > int(n0):
                    print(f"cron fired: rows {n0} -> {n}")
                    break
            else:
                raise RuntimeError("no cron beat within 150s — check /home/bob/.pulse.log")
            page.wait_for_timeout(11000)   # let the artifact auto-refresh
            if args.shots:
                page.screenshot(path=f"{args.shots}/3-live-after-cron-beat.png")
        ctx.close()
        browser.close()
    print("demo complete")


if __name__ == "__main__":
    main()
