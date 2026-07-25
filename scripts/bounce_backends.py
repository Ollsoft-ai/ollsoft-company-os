#!/usr/bin/env python3
"""Restart per-user backends after a deploy, from each user's own authority.

`scripts/deploy.sh` rsyncs code into /opt but restarts nothing. The hub and
syncd are root services you restart with systemctl; the PER-USER backends are
long-lived processes owned by each user, and root must not reach into them.
So this drives the product: it logs in as the user, opens their web terminal
and has THEM run the kill. The hub respawns the backend with the deployed code
on the next request.

Which users need it is decided by the version marker returned by /api/cron
("v"), which is bumped whenever backend behaviour changes — a backend still
reporting an older v is running stale code.

    ./.venv/bin/python scripts/bounce_backends.py            # bob carol
    ./.venv/bin/python scripts/bounce_backends.py alice bob carol

Anyone with a live web-terminal session loses it (their shells are children of
the backend), so the script says whose sessions it is about to end and asks,
unless you pass --yes.
"""
import argparse
import json
import subprocess
import sys
import time

import httpx
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8300"
CREDS_FILE = "/tmp/kb-test-creds.json"
MIN_V = 13          # bump together with _cron_listing's "v" in user_server.py


def http(user, creds):
    c = httpx.Client(base_url=BASE, timeout=20)
    r = c.post("/login", data={"username": user, "password": creds[user]})
    if r.status_code != 200:
        raise SystemExit(f"{user}: login failed ({r.status_code})")
    return c


def backend_v(c):
    try:
        r = c.get("/api/cron")
        return r.json().get("v", 0) if r.status_code == 200 else 0
    except Exception:
        return 0


def live_shells(user):
    """Shells that would die with the backend — a claude session in someone's
    web terminal can be hours of work, so never kill one silently."""
    try:
        pids = subprocess.run(["pgrep", "-u", user, "-f", "[k]b_platform.user_server"],
                              capture_output=True, text=True).stdout.split()
    except OSError:
        return []
    out = []
    for pid in pids:
        kids = subprocess.run(["pgrep", "-P", pid], capture_output=True, text=True).stdout.split()
        for k in kids:
            cmd = subprocess.run(["ps", "-o", "args=", "-p", k],
                                 capture_output=True, text=True).stdout.strip()
            if cmd:
                out.append(cmd)
    return out


def bounce(browser, user, creds):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', creds[user])
    page.click('button[type="submit"]')
    page.wait_for_url(BASE + "/")
    page.click('[data-testid="toggle-term"]')
    page.wait_for_selector("#terminal .xterm-rows")
    page.click("#terminal")
    # -KILL, not TERM: a backend holding a live pty websocket hangs in aiohttp's
    # graceful shutdown indefinitely. The bracket in "[k]b_" keeps the pattern
    # from matching the pkill command line itself.
    page.keyboard.type('pkill -KILL -u $(whoami) -f "[k]b_platform.user_server"')
    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)
    ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("users", nargs="*", default=["bob", "carol"])
    ap.add_argument("--yes", action="store_true", help="don't ask before ending shell sessions")
    args = ap.parse_args()
    users = args.users or ["bob", "carol"]
    creds = json.load(open(CREDS_FILE))

    todo = []
    for u in users:
        if u not in creds:
            print(f"{u}: no password in {CREDS_FILE} — skipping")
            continue
        v = backend_v(http(u, creds))
        if v >= MIN_V:
            print(f"{u}: already on v{v}")
        else:
            todo.append((u, v))
    if not todo:
        print("every backend is current")
        return

    for u, v in todo:
        shells = live_shells(u)
        print(f"{u}: on v{v}, needs v{MIN_V}" +
              (f" — {len(shells)} live shell(s) will be killed: {shells}" if shells else ""))
    if not args.yes and any(live_shells(u) for u, _ in todo):
        if input("proceed? [y/N] ").strip().lower() != "y":
            sys.exit("aborted")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        for u, _ in todo:
            bounce(browser, u, creds)
            c = http(u, creds)
            for _ in range(20):
                if backend_v(c) >= MIN_V:
                    print(f"{u}: now on v{MIN_V}")
                    break
                time.sleep(0.5)
            else:
                print(f"{u}: STILL STALE — check `journalctl -u kb-hub`")
        browser.close()


if __name__ == "__main__":
    main()
