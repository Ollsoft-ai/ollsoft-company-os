# Monitoring

Three layers, all alerting to the operator's phone via [ntfy](https://ntfy.sh).
The topic is `KB_NTFY_TOPIC` in `/etc/kb/kb.env` — topics are public to anyone
who knows the name, so pick an unguessable one and treat it like a password.
Empty topic = monitoring runs but stays silent (the CI/fresh-install default).

| Layer | What it catches | Where it runs |
|---|---|---|
| `uptime.yml` (GitHub Actions, 5-min cron) | box/tunnel/edge dead | GitHub — survives the VM |
| `OnFailure=kb-alert@%n` on every service | a service crashed or crash-looped past its start limit | systemd |
| `kb-heartbeat.timer` (5 min) | **running-but-wrong**: stale search index, syncd not committing, hub not answering, postgres down, disk ≥85%, backups older than 48h | on the box |

The heartbeat checks outcomes, not processes — it exists because a service can
be `active` while serving garbage (an indexer once served a stale index for 90
minutes while systemd showed green). It alerts only on state *change* (one
message when broken, one when recovered) and writes the current state to
`company/infrastructure/health.md` in the knowledgebase, where humans and
agents alike can read it.

The external probe's target and topic live in the repo's Actions config
(`vars.KB_PUBLIC_URL`, `secrets.KB_NTFY_TOPIC`), never in the workflow file.

Test the pipeline after changes:

```bash
sudo systemctl start kb-alert@drill.service   # phone must buzz
sudo systemctl stop kb-indexer && sudo systemctl start kb-heartbeat.service
sudo systemctl start kb-indexer && sudo systemctl start kb-heartbeat.service
```
