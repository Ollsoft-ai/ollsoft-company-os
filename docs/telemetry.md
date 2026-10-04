# Telemetry

**Anonymous, on by default, one command away from being read or switched off.**
It answers questions we cannot answer any other way — does the installer
actually finish on your distro, what versions are still out there, how many
people a typical installation carries — and it is deliberately incapable of
identifying your company.

## What is sent

Everything, with nothing omitted from this list:

- `install_id` — a random UUID made once, kept in `/etc/kb/install-id`
- `version` — e.g. `1.0.0`
- `users` — the number of **named users**, the same count the licence uses and
  the same number the UI shows (`common.named_users`)
- `licensed` — whether a commercial licence was recorded on this box
- `os` — distro id and version, from `/etc/os-release`
- `postgres` — server version, `major.minor`
- `event` — `ping`, `install_started`, `install_ok`, `install_failed`,
  `update_started`, `update_ok`, `update_rollback`, `update_deferred`
- `detail` — for a failure, the **name of the install step**; never a message,
  a path or any output

## What is never sent

Hostname, domain, IP address, account names, e-mail addresses, file or folder
names, document counts, anything from inside the knowledgebase.

**The receiving server does not log source IP addresses either.** An IP is
visible to any receiver at the TCP level whether it wants it or not, so it is
dropped at the edge rather than stored. That is what makes "anonymous" a fact
about this system and not a promise about our intentions.

## See it for yourself

```sh
sudo kb-telemetry show
```

Prints the exact JSON that would be posted, the URL, and whether sending is on.
Not a description of the payload — the payload.

## It never runs in CI

`CI=true` is set by GitHub Actions, GitLab, CircleCI and Travis, and this sender
treats it as a hard off. A pipeline builds a fresh machine for every push, so
each run would otherwise make a new install id and register as a brand new
installation, burying the real ones. `kb-telemetry show` says so when that is
why it is quiet.

## Turn it off

```sh
sudo sed -i 's/^KB_TELEMETRY=.*/KB_TELEMETRY=off/' /etc/kb/kb.env
```

Takes effect on the next run; nothing to restart. The guided installer asks
before switching it on, `scripts/install.sh --no-telemetry` never switches it
on, and re-running the installer never switches it back on.

## When it runs

`kb-telemetry.timer`, weekly, with up to six hours of random delay so that
every installation in the world does not ping the same server in the same
second. Install and update outcomes are sent when they happen.

Delivery failure is silent: five-second timeout, no retries, no log line,
no effect on anything. If the receiver is down, nobody notices.

## Why it is not identified

Knowing *who* runs Company OS would be commercially useful and we decided
against it. An IP address is personal data under the GDPR, this is the one
product whose whole promise is that your company's knowledge stays on your own
server, and the source is public — so an identifying ping would cost trust and
collect nothing from anyone who minded. Registration for the update channel is
where we learn who you are, because you chose to tell us.
