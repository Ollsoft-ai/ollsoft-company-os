---
name: kb-automation
description: Use when the user asks you to run something on a schedule or in the background — a recurring script, a data feed, a periodic report. Explains how to write a script and schedule it with cron so it runs as the user, with the user's permissions, no root required.
---

# Scheduling work — securely, as yourself

You can create scripts and schedule them to run automatically. They run **as your OS user**, so they inherit exactly your permissions: your files, your database identity, your access — nothing more. No root, no escalation, no separate credentials to manage. This is the safe, correct way to automate.

# Step 1 — write the script in your home directory

Keep scripts and logs in your own space (e.g. `~/scripts/`, `~/logs/`), which only you control:

```python
#!/opt/kb-venv/bin/python
# ~/scripts/collect.py — runs as you; DB connection is authenticated as you.
import psycopg, datetime
conn = psycopg.connect("dbname=kb", autocommit=True)
with conn.cursor() as cur:
    cur.execute("CREATE TABLE IF NOT EXISTS u_%s.log (id bigserial PRIMARY KEY, at timestamptz DEFAULT now(), note text)"
                % __import__("getpass").getuser())
    cur.execute("INSERT INTO u_%s.log(note) VALUES (%%s)" % __import__("getpass").getuser(), ["tick"])
```

Use `/opt/kb-venv/bin/python` — it has the platform's Python packages (psycopg, etc.). Write results to your `u_<you>` schema (see **kb-database**) and/or to a markdown file under `/srv/kb` that you can write.

# Step 2 — schedule it with your crontab

Your personal crontab runs jobs as you. Edit it with `crontab -e`, list it with
`crontab -l` — or use the **Cron** button in the web UI's topbar, which
lists/adds/pauses/deletes entries in the same crontab (it calls
`GET/POST /api/cron*` on your backend, which runs `crontab(1)` as you):

```cron
# minute hour dom mon dow   command
*/5 * * * *  /opt/kb-venv/bin/python /home/<you>/scripts/collect.py >> /home/<you>/logs/collect.log 2>&1
0 8 * * 1    /opt/kb-venv/bin/python /home/<you>/scripts/weekly_report.py >> /home/<you>/logs/weekly.log 2>&1
```

Always redirect output to a log so you can debug failures. To add a line non-interactively:

```bash
( crontab -l 2>/dev/null; echo "*/5 * * * * /opt/kb-venv/bin/python $HOME/scripts/collect.py >> $HOME/logs/collect.log 2>&1" ) | crontab -
```

# Faster than once a minute

Cron's finest granularity is one minute. For "every N seconds", write a loop and run it as a background service. If lingering is enabled for your account you can use a **user** service (no root):

```bash
# ~/.config/systemd/user/myfeed.service  ->  then: systemctl --user enable --now myfeed
```

Otherwise a simple `nohup ~/scripts/loop.py &` works for the session. (A system-level service needs an administrator to install it; as a regular user you use crontab or `systemctl --user`, which don't require root.)

# Security & good-citizen rules

- Your job can **only** do what you can do. It cannot read another user's private files or another team's data — the kernel and Postgres enforce that, so you don't have to.
- Don't hammer the box: keep loops reasonable, add `sleep`, avoid unbounded queries. Shared machine, shared resources.
- Anything important your job produces should also land as **markdown in `/srv/kb`** — that's what's backed up in git. Your database schema is convenient but not the source of truth.
- Never store secrets (API keys, passwords) in the script or in markdown. If the platform has a secrets store, use it; otherwise ask your human.
