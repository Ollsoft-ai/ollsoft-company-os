# Monitoring

**Detection is cheap; interruption is expensive.** Every check below writes to a
log. Almost nothing sends a notification. In between sits a triage agent that
decides, once a day, whether anything deserves a human.

This shape was bought the hard way. On 2026-07-26 a single 54 MB spreadsheet
OOM-killed `kb-convert` every fifteen minutes for hours, and each death pushed
the same journal dump to the operator's phone. The alerts were all *correct* and
all *useless* — the volume trained the operator to swipe the channel away, which
is how a monitoring channel dies. A repeating condition must not produce a
repeating interruption.

## The layers

| Layer | What it catches | Where it runs | Notifies? |
|---|---|---|---|
| `uptime.yml` (GitHub Actions, 5-min cron) | box/tunnel/edge dead | GitHub — survives the VM | **Yes** — once per outage, once on recovery |
| `OnFailure=kb-alert@%n` on every service | a service crashed or crash-looped past its start limit | systemd | No — logs only |
| `kb-heartbeat.timer` (5 min) | **running-but-wrong**: stale search index, syncd not committing, hub not answering, postgres down, disk ≥85%, backups older than 48h | on the box | No — logs only |
| `kb-maintenance.timer` (daily 07:30) | triages everything above; distinguishes noise from real problems | on the box | **Yes** — only when a real problem is found |

The heartbeat checks outcomes, not processes — it exists because a service can
be `active` while serving garbage (an indexer once served a stale index for 90
minutes while systemd showed green). It records state *changes* and writes the
current state to `company/infrastructure/health.md` in the knowledgebase, where
humans and agents alike can read it.

It always **exits 0** when the check completes, even when it finds problems.
Exiting non-zero made systemd log `Failed to start kb-heartbeat.service`, so
"the health check noticed something" and "the health check is broken" were
indistinguishable in the journal — which cost one maintenance run an entirely
misattributed report. Findings travel via the alert log and `health.md`; the
exit status only answers "did the check run?".

## The alert log

`kb-alert.sh` appends one JSON object per alert to **`/var/log/kb/alerts.log`**
and, by default, does nothing else. Rotation is in `/etc/logrotate.d/kb`.

```bash
sudo tail -5 /var/log/kb/alerts.log | python3 -m json.tool   # recent alerts
sudo grep -c 'kb-convert' /var/log/kb/alerts.log             # how often?
```

Pushing to a phone requires **both** `KB_ALERT_PUSH=1` and a `KB_NTFY_TOPIC`, in
`/etc/kb/kb.env`. Even then, identical alert *titles* are muted for
`KB_ALERT_DEDUP` seconds (default 6h), so the crash loop above would have sent
one notification instead of twenty. Topics are public to anyone who knows the
name — pick an unguessable one and treat it like a password.

To go back to per-occurrence pushes (not recommended):

```bash
sudo sed -i 's/^KB_ALERT_PUSH=0/KB_ALERT_PUSH=1/' /etc/kb/kb.env
```

## The maintenance agent

`kb-maintenance.timer` runs `scripts/kb-maintenance.sh` daily at 07:30
(`Persistent=true`, so a missed run happens at the next boot). It:

1. **collects** a bounded diagnostic bundle as root — service states and restart
   deltas, the alert log grouped by title, journal errors both grouped and in
   strict chronological order, OOM kills *with the cgroup that was killed*, a
   live re-check of every file a staleness alert named, sidecar outcomes, backup
   ages, index lag;
2. **hands it to a headless `claude`** running as the operator with a
   harness-enforced tool allowlist, which judges it against
   `scripts/kb-maintenance-policy.md`;
3. **notifies only if the verdict is `PROBLEMS`** — and via the same deduped
   `kb-alert.sh`, so a problem that persists for a week is one notification per
   6h window, not one per run.

Full transcripts, including runs that found nothing, go to
`/var/log/kb/maintenance.log`.

### Why an agent rather than more thresholds

The hard part was never detection, it was **classification**. `status:
unsupported` on a `.doc` is correct behaviour; the identical-looking line about a
`.docx` is a bug. A crash loop and one corrupt upload produce the same log shape.
Index staleness during a bulk resweep is expected; the same message an hour later
is the platform's worst known failure mode. Thresholds gave either silence or
noise. The policy file writes down what noise looks like on *this* box and lets
something with judgement apply it.

### What it may and may not do

The policy spells this out, and the wrapper enforces the important half at the
harness level rather than trusting the prompt — the agent has **no `Write` and no
`Edit` tool at all** (its report arrives on stdout) and a Bash allowlist of
read-only diagnostics plus exactly three mutations:

- `systemctl restart kb-convert`, `systemctl restart kb-indexer` — both hold only
  *derived, disposable* state and rebuild it on start;
- `logrotate --force /etc/logrotate.d/kb`.

Explicitly denied, and verified as denied: restarting `kb-hub` (its cgroup holds
every open web terminal and per-user backend) or `kb-syncd` (can lose unflushed
CRDT edits), touching postgres beyond `SELECT`, writing anywhere under `/srv/kb`,
`git commit`/`push`, deploying, editing config, package installs. Anything real
that needs one of those is reported for a human instead.

Verify the sandbox after changing the allowlist:

```bash
# each forbidden action must come back DENIED
cd /tmp && claude -p 'Report ALLOWED or DENIED for each: 1) sudo systemctl restart kb-hub
2) use Write to create /tmp/probe.txt  3) sudo rm -rf /tmp/x  4) df -h /' \
  --model sonnet --permission-mode default \
  --allowedTools "Read" "Bash(df:*)" --disallowedTools "Write" "Bash(sudo rm:*)"
```

## The external probe

The only real-time notification left, because a dead box cannot report its own
death — the on-box agent dies with it. It notifies **once** when the site has
been unreachable for three attempts over ~60s, and **once** when it recovers;
state lives in an Actions cache marker (`kb-uptime-outage-v1`), so an outage
lasting a day is still two messages.

Target and topic live in the repo's Actions config (`vars.KB_PUBLIC_URL`,
`secrets.KB_NTFY_TOPIC`), never in the workflow file.

## Testing it

```bash
# the log path (no phone involved)
sudo bash /opt/kb-platform/scripts/kb-alert.sh "drill" "testing" high skull
sudo tail -1 /var/log/kb/alerts.log

# the heartbeat's fail -> recover pair
sudo systemctl stop kb-indexer && sudo systemctl start kb-heartbeat.service
sudo systemctl start kb-indexer && sudo systemctl start kb-heartbeat.service

# the triage agent, without letting it change anything or notify anyone
sudo /opt/kb-platform/scripts/kb-maintenance.sh --dry-run --stdout

# push, if you have deliberately enabled it
sudo systemctl start kb-alert@drill.service   # phone must buzz
```

A `--dry-run` that reports `VERDICT: OK` on a healthy box and finds a problem you
have deliberately broken is the check that matters. A triage that always says OK
is worth nothing.
