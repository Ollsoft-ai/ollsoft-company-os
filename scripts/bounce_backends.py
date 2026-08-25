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
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8300"
# Whatever seeding is currently in place. A namespaced test run writes
# /tmp/kb-test-creds-<ns>.json; the human demo writes the un-suffixed name. Both
# shapes are read below, and neither is required to exist — this tool drives the
# product as real people, so it needs a password for each account it bounces.
CREDS_FILE = os.environ.get("KB_TEST_CREDS") or next(
    (str(f) for f in sorted(Path("/tmp").glob("kb-test-creds*.json"))), "")
# Imported, never restated: this was MIN_V = 20 against a backend serving v: 23,
# so every backend reported itself current and the bounce silently skipped them.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from kb_platform.user_server import BACKEND_V as MIN_V
except Exception:                     # deployed copy without the source tree
    MIN_V = 24


def load_creds():
    """logical-or-real name -> password, tolerating both credential shapes."""
    if not CREDS_FILE or not Path(CREDS_FILE).exists():
        raise SystemExit(
            "no credentials file found (looked for /tmp/kb-test-creds*.json).\n"
            "This tool logs in AS each user to have them drop their own backend,\n"
            "rather than having root reach into their processes, so it needs their\n"
            "passwords. Seed a fixture first:\n"
            "    sudo bash scripts/seed-demo.sh --namespace tmp\n"
            "or pass KB_TEST_CREDS=/path/to/creds.json")
    d = json.load(open(CREDS_FILE))
    if "users" in d:                      # namespaced shape
        out = {k: v["password"] for k, v in d["users"].items()}
        out.update({v["name"]: v["password"] for v in d["users"].values()})
        return out
    return d                              # legacy flat shape


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
    # Wait for the app to have BOOTED, not merely navigated: #toggleterm is
    # wired after the first render, so clicking as soon as the URL changes
    # races that and lands on a dead button. The click is swallowed and this
    # then times out waiting for a terminal that was never opened — reporting
    # a Playwright error while leaving the backend un-bounced.
    page.wait_for_selector('[data-testid="tree"] .tree-item')
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
    # Default to every user in the creds file rather than a hardcoded list —
    # the accounts differ per box (demo users in CI, real people in production).
    # Resolved AFTER parsing, not as an argparse default: reading the file
    # eagerly made even --help fail when no fixture was seeded.
    ap.add_argument("users", nargs="*")
    ap.add_argument("--yes", action="store_true", help="don't ask before ending shell sessions")
    args = ap.parse_args()
    creds_all = load_creds()
    users = args.users or sorted(creds_all)
    creds = creds_all

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
