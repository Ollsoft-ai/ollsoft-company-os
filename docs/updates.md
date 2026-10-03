# Updates

**The server updates itself from the public repository, waits for your
colleagues to stop typing, and puts the old version back if the new one does
not come up.**

## How it works

- The box keeps its own clone of the public repo at **`KB_SRC`** (default
  `/opt/kb-src`), made by `scripts/install.sh`.
- `kb-update.timer` runs `scripts/kb-update.sh` weekly — **Sunday 02:00** as
  installed.
- The script fetches, picks a target for the channel, and **refuses anything
  that is not a fast-forward**: a rewritten upstream history is a surprise, and
  an unattended script should not resolve surprises on its own.
- Then `scripts/deploy.sh` — the same script a developer runs by hand, so an
  update and a manual redeploy cannot drift apart.

## Channels

- **`stable`** — released tags only. The default, and what a company should run.
- **`edge`** — every commit on `main`. For a box you are happy to fix.
- **`off`** — never updates by itself.

```sh
sudo sed -i 's/^KB_UPDATE_CHANNEL=.*/KB_UPDATE_CHANNEL=stable/' /etc/kb/kb.env
```

## It waits for live terminals

Restarting `kb-hub` kills **every open web-terminal shell, including other
people's unsaved work** — the per-user backends live in the hub's cgroup. So a
scheduled hour is not enough on its own, because somebody is always the
exception.

Before restarting anything, the updater counts live shells in the hub's cgroup.
If anyone is working, it **waits an hour and looks again**, up to
`KB_UPDATE_DEFERRALS` times (3). After that it gives up for this week, says so
through the alert log, and leaves the running version alone.

## It can go back

The commit that was live is recorded before anything moves. After the deploy the
hub must answer HTTP on its local port; if it does not, the previous commit is
checked out, deployed again, and the operator is told it was rolled back. An
installation that updates itself has to be able to undo that.

## Change the schedule

The schedule is a **systemd drop-in**, not a key in `kb.env`, so an update can
never overwrite the time you chose:

```sh
sudo install -d /etc/systemd/system/kb-update.timer.d
sudo tee /etc/systemd/system/kb-update.timer.d/schedule.conf <<'CONF'
[Timer]
OnCalendar=
OnCalendar=Sun *-*-* 02:00:00
CONF
sudo systemctl daemon-reload && sudo systemctl restart kb-update.timer
```

The empty `OnCalendar=` is required — it clears the shipped value instead of
adding a second schedule. Check the result with
`systemctl list-timers kb-update.timer`.

## Update by hand

```sh
sudo bash /opt/kb-platform/scripts/kb-update.sh --dry-run   # what would happen
sudo bash /opt/kb-platform/scripts/kb-update.sh --now       # don't wait for terminals
```

## Versions

- `VERSION` in the repo is the release number; the git tag is the same number
  with a `v`.
- `scripts/deploy.sh` writes `KB_VERSION` into `/etc/kb/kb.env` from
  `git describe`, so a working copy reports its distance from the last tag and a
  release reports exactly `1.0.0`.
- **Each release carries its own Change Date under the licence** (see `LICENSE`),
  which is why releases are cut by `scripts/release.sh` rather than by hand.
