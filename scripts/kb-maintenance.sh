#!/usr/bin/env bash
# Unattended maintenance triage: gather evidence, let an agent judge it, notify
# only if something is actually wrong.
#
#   kb-maintenance.sh [--dry-run] [--stdout]
#
# Run from kb-maintenance.timer. The shape is deliberate:
#
#   root gathers  ->  agent judges  ->  wrapper decides whether to interrupt
#
# 1. THIS SCRIPT (as root) collects a bounded diagnostic bundle. Root does the
#    collecting so the agent never needs privileges to *read* anything, which
#    is most of what a diagnosis is.
# 2. The agent runs as the operator's user with a **harness-enforced tool
#    allowlist** (see TOOLS below) — not merely a prompt asking it to behave.
#    It gets no Write and no Edit tool at all: its report comes back on stdout
#    and this script writes it. It cannot deploy, edit config, touch /srv/kb,
#    or restart anything except the two services whose state is disposable.
# 3. Its verdict decides notification. `## VERDICT: OK` means nobody is told.
#
# Why an agent instead of more `if` statements: the hard part of this box's
# monitoring was never detection, it was *classification*. `status: unsupported`
# on a .doc is correct behaviour; the identical-looking line about a .docx is a
# bug. A crash loop and a one-off corrupt upload produce the same log shape.
# Encoding that as thresholds produced either silence or noise; the policy in
# kb-maintenance-policy.md says what noise looks like and lets something with
# judgement apply it.
set -uo pipefail

DRY_RUN=0; TO_STDOUT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;                # gather + report, never act or notify
    --stdout)  TO_STDOUT=1 ;;              # also print the report (for humans)
    *) echo "usage: $0 [--dry-run] [--stdout]" >&2; exit 64 ;;
  esac
  shift
done

SELF_DIR=$(dirname "$(readlink -f "$0")")
POLICY="$SELF_DIR/kb-maintenance-policy.md"
ALERT="$SELF_DIR/kb-alert.sh"
LOG_DIR=/var/log/kb
LOG="$LOG_DIR/maintenance.log"
STATE_DIR="${STATE_DIRECTORY:-/var/lib/kb-monitor}"
LAST_REPORT="$STATE_DIR/maintenance-last.md"
LAST_RUN_STAMP="$STATE_DIR/maintenance-last-run"
RESTARTS_SNAPSHOT="$STATE_DIR/maintenance-restarts"

cfg() {
  if [ -f /etc/kb/kb.env ]; then
    (. /etc/kb/kb.env 2>/dev/null; eval "printf '%s' \"\${$1:-$2}\"")
  else printf '%s' "$2"; fi
}
REPO=$(cfg KB_REPO /srv/kb)
PGDB=$(cfg KB_PG_DB kb)
HUB_PORT=$(cfg KB_HUB_PORT 8300)
# Whose signed-in claude runs the triage: install.sh records the admin who set
# the box up. Boxes installed before that fall back to the founding admin, whom
# install.sh always lists first in KB_PROTECTED_USERS. No name is baked in.
OPERATOR=$(cfg KB_MAINT_USER "")
[ -n "$OPERATOR" ] || { OPERATOR=$(cfg KB_PROTECTED_USERS ""); OPERATOR=${OPERATOR%%,*}; }
MODEL=$(cfg KB_MAINT_MODEL sonnet)
BUDGET=$(cfg KB_MAINT_TIMEOUT 900)             # seconds; a triage that hangs is a no-op

mkdir -p "$LOG_DIR" "$STATE_DIR" 2>/dev/null || true
UNITS="kb-hub kb-syncd kb-indexer kb-embedd kb-convert postgresql"
# The tunnel is optional (docs/remote-access.md): only watch it where it exists.
systemctl cat cloudflared.service >/dev/null 2>&1 && UNITS="$UNITS cloudflared"

# Everything since the previous run — bounded, so one loud day cannot blow up
# the context window (or the bill).
SINCE=$(cat "$LAST_RUN_STAMP" 2>/dev/null || echo "")
[ -n "$SINCE" ] || SINCE=$(date -d '24 hours ago' +%s 2>/dev/null || echo 0)
SINCE_ISO=$(date -d "@$SINCE" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "24 hours ago")

WORK=$(mktemp -d /tmp/kb-maint-XXXXXX)
# The agent runs as $OPERATOR and must traverse this dir, but the bundle holds
# journal lines, Postgres output and document text from across the whole box —
# 755 published all of it to every local account for the length of the run.
chown "root:$OPERATOR" "$WORK" 2>/dev/null || true
chmod 750 "$WORK"
BUNDLE="$WORK/bundle.md"
trap 'rm -rf "$WORK"' EXIT

# --------------------------------------------------------------------------
# 1. the evidence bundle
# --------------------------------------------------------------------------
{
  echo "# Diagnostic bundle"
  echo
  echo "Generated: $(date -Is)   Host: $(hostname)"
  echo "Window under review: since $SINCE_ISO"
  echo
  echo "## Services"
  echo '```'
  for u in $UNITS; do
    active=$(systemctl is-active "$u" 2>/dev/null || echo unknown)
    nr=$(systemctl show "$u" -p NRestarts --value 2>/dev/null || echo ?)
    prev=$(grep -E "^$u " "$RESTARTS_SNAPSHOT" 2>/dev/null | awk '{print $2}')
    delta=""
    if [ -n "${prev:-}" ] && [ "$nr" != "?" ] && [ "$nr" != "${prev}" ]; then
      delta="  (NRestarts +$(( nr - prev )) since last run)"
    fi
    printf '%-14s %-10s NRestarts=%s%s\n' "$u" "$active" "$nr" "$delta"
  done
  echo '```'
  echo
  echo "### Failed units"
  echo '```'
  systemctl --failed --no-legend --no-pager 2>/dev/null || true
  echo '```'
  echo
  echo "## Resources"
  echo '```'
  df -h / /srv 2>/dev/null | sed 's/^/df  /'
  free -h 2>/dev/null | sed 's/^/mem /'
  echo "load: $(cat /proc/loadavg 2>/dev/null)"
  echo "uptime: $(uptime -p 2>/dev/null)"
  echo '```'
  echo
  echo "## Heartbeat's own view (company/.infrastructure/health.md)"
  echo '```'
  head -30 "$REPO/company/.infrastructure/health.md" 2>/dev/null || echo "(not written yet)"
  echo '```'
  echo
  echo "## Alert log since last run — grouped by title (the repeat count matters)"
  echo '```'
  if [ -f "$LOG_DIR/alerts.log" ]; then
    python3 - "$LOG_DIR/alerts.log" "$SINCE" <<'PY' 2>/dev/null || echo "(could not parse)"
import json, sys, collections, time
path, since = sys.argv[1], int(sys.argv[2])
counts, first, last = collections.Counter(), {}, {}
for line in open(path, errors="replace"):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if int(e.get("ts", 0)) < since:
        continue
    t = e.get("title", "?")
    counts[t] += 1
    first.setdefault(t, e.get("iso", "?"))
    last[t] = e.get("iso", "?")
if not counts:
    print("(no alerts in the window)")
for t, n in counts.most_common(25):
    print(f"{n:4}x  {t}")
    if n > 1:
        print(f"      first {first[t]}  last {last[t]}")
PY
  else
    echo "(no alerts.log yet)"
  fi
  echo '```'
  echo
  # Two views of the same errors, because one view cannot answer both questions
  # and mixing them answers neither. Keeping the timestamp made every line
  # unique, so `uniq -c` collapsed nothing and `sort -rn` shuffled the whole
  # thing out of chronological order — a triage then dated errors by whatever
  # order they happened to land in, and reported times that were simply wrong.
  echo "## Journal errors since last run — WHAT, and how often (timestamps stripped to group)"
  echo '```'
  journalctl --since "$SINCE_ISO" -p err --no-pager -o short 2>/dev/null \
    | sed -E 's/^[A-Za-z]{3} [0-9 ]{2} [0-9:]{8} [^ ]+ //; s/\[[0-9]+\]:/:/' \
    | sort | uniq -c | sort -rn | head -40 || echo "(none)"
  echo '```'
  echo
  echo "## The same errors, WHEN — strict chronological tail, nothing reordered"
  echo '```'
  journalctl --since "$SINCE_ISO" -p err --no-pager -o short 2>/dev/null \
    | sed -E 's/^([A-Za-z]{3} [0-9 ]{2} [0-9:]{8}) [^ ]+ /\1 /' \
    | tail -40 || echo "(none)"
  echo '```'
  echo
  echo "## OOM kills since last run — WITH the cgroup that was killed"
  # Attribution, chronological, not collapsed by uniq. The kernel names the
  # responsible cgroup in the oom-kill line; without it a triage sees "something
  # used 1.9 GB" and guesses. It guessed wrong once, blaming the health check
  # for kb-convert's memory budget, so the evidence now names the unit.
  echo '```'
  journalctl -k --since "$SINCE_ISO" --no-pager -o short 2>/dev/null \
    | grep -E "oom-kill:|Killed process|oom_reaper" \
    | sed -E 's/^([A-Za-z]{3} [0-9 ]{2} [0-9:]{8}).*cpuset=([^,]*).*task=([^ ,]*).*/\1  OOM in unit=\2  (process \3)/; t
              s/^([A-Za-z]{3} [0-9 ]{2} [0-9:]{8}).*Killed process ([0-9]+) \(([^)]*)\).*anon-rss:([0-9]+)kB.*/\1  killed pid \2 (\3) using \4 kB/; t
              d' \
    | tail -25 || true
  [ -z "$(journalctl -k --since "$SINCE_ISO" --no-pager 2>/dev/null | grep -E 'oom-kill:' || true)" ] \
    && echo "(no OOM kills in the window)"
  echo '```'
  echo
  echo "## Platform tracebacks since last run (any is a bug, even if survived)"
  echo '```'
  journalctl --since "$SINCE_ISO" -u kb-hub -u kb-syncd -u kb-indexer -u kb-embedd -u kb-convert \
      --no-pager -o cat 2>/dev/null | grep -E "Traceback|^[A-Za-z_.]+Error|Exception" \
    | sort | uniq -c | sort -rn | head -25 || echo "(none)"
  echo '```'
  echo
  echo "## kb-convert: sidecar outcomes across the whole tree"
  echo '```'
  find "$REPO" -name '.*.md' -not -path '*/.git/*' -print0 2>/dev/null \
    | xargs -0 -r grep -h '^status:' 2>/dev/null | sort | uniq -c | sort -rn
  echo "--- convertible sources without a sidecar (should be 0 once a sweep finished) ---"
  python3 - "$REPO" <<'PY' 2>/dev/null || echo "(could not scan)"
import os, sys
from pathlib import Path
root = Path(sys.argv[1])
CONV = {".docx", ".pptx", ".xlsx", ".pdf", ".doc", ".ppt", ".xls"}
missing = []
for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
    dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_secrets"]
    for fn in filenames:
        p = Path(dirpath) / fn
        if not fn.startswith(".") and p.suffix.lower() in CONV:
            if not (p.parent / f".{p.name}.md").exists():
                missing.append(str(p.relative_to(root)))
print(f"{len(missing)} missing")
for m in missing[:20]:
    print("  " + m)
PY
  echo '```'
  echo
  echo "## Recent kb-convert failures in detail (file + reason)"
  echo '```'
  find "$REPO" -name '.*.md' -not -path '*/.git/*' -print0 2>/dev/null \
    | xargs -0 -r grep -l '^status: failed' 2>/dev/null | head -15 \
    | while read -r f; do
        echo "--- ${f#"$REPO"/}"
        grep -A2 -m1 '^Conversion failed\|^Extraction' "$f" 2>/dev/null | head -3
      done
  echo '```'
  echo
  echo "## Search index freshness"
  echo '```'
  newest=$(find "$REPO/company" "$REPO/projects" -name '*.md' -not -path '*/.git/*' \
           -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1)
  if [ -n "$newest" ]; then
    disk_t=${newest%% *}; disk_t=${disk_t%.*}; rel=${newest#* }; rel=${rel#"$REPO"/}
    echo "newest markdown on disk: $rel ($(( ( $(date +%s) - disk_t ) / 60 )) min ago)"
    idx=$(runuser -u postgres -- psql -d "$PGDB" -tAc \
      "SELECT COALESCE(floor(mtime),0)::bigint FROM kb.files WHERE path='${rel//\'/\'\'}'" 2>/dev/null || echo 0)
    echo "index mtime for that file: ${idx:-0} (disk $disk_t) -> lag $(( disk_t - ${idx:-0} ))s"
  else
    echo "(no markdown found)"
  fi
  echo '```'
  echo
  echo "## Sidecar audience drift"
  # A sidecar carries the FULL extracted text of its source and is what the
  # index serves and what agents are told to read instead of the binary. Its
  # ACL is cloned from the source at conversion time, and the staleness gate is
  # a content hash — permissions are not content. convert.py now re-mirrors the
  # audience on every sweep; this is the check that says so, and the one that
  # notices if that ever regresses. Daily is the right cadence: permissions
  # move on human timescales, not five-minute ones.
  echo '```'
  drift=0
  while IFS= read -r side; do
    d=$(dirname "$side"); b=$(basename "$side"); src="$d/${b#.}"; src="${src%.md}"
    [ -f "$src" ] || continue
    so=$(getfacl -cE -- "$src"  2>/dev/null | awk -F: '/^other::/{print $3}')
    sd=$(getfacl -cE -- "$side" 2>/dev/null | awk -F: '/^other::/{print $3}')
    if [ "${so:---}" = "---" ] && [ -n "$sd" ] && [ "$sd" != "---" ]; then
      drift=$((drift+1))
      echo "WIDER than its source: ${side#"$REPO"/}"
    fi
  done < <(find "$REPO" -type f -name '.*.md' -not -path '*/.git/*' 2>/dev/null)
  if [ "$drift" -eq 0 ]; then
    echo "no sidecar is readable by anyone its source is not"
  else
    echo "$drift sidecar(s) expose text their source does not — kb-convert should"
    echo "have re-mirrored these; if they persist, _refresh_sidecar_acl regressed."
  fi
  echo '```'
  echo
  echo "## Re-check of every file a staleness alert named — is it STILL stale?"
  # A bulk resweep (101 sidecars rewritten in one go) puts the indexer minutes
  # behind and trips the heartbeat's 180s threshold. That alert is true when
  # written and meaningless an hour later. The distinction that matters is
  # whether the lag is still there NOW, so root answers it here rather than
  # leaving the agent to guess from a timestamp — or needing database access.
  echo '```'
  if [ -f "$LOG_DIR/alerts.log" ]; then
    stale_paths=$(python3 - "$LOG_DIR/alerts.log" "$SINCE" <<'PY' 2>/dev/null
import json, re, sys
path, since = sys.argv[1], int(sys.argv[2])
seen = set()
for line in open(path, errors="replace"):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    if int(e.get("ts", 0)) < since:
        continue
    for m in re.finditer(r"index is STALE: (.+?) changed", e.get("message", "")):
        seen.add(m.group(1))
for p in sorted(seen):
    print(p)
PY
)
    if [ -z "$stale_paths" ]; then
      echo "(no staleness alerts in the window)"
    else
      while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        if [ ! -e "$REPO/$rel" ]; then echo "GONE      $rel"; continue; fi
        d=$(stat -c %Y "$REPO/$rel" 2>/dev/null || echo 0)
        i=$(runuser -u postgres -- psql -d "$PGDB" -tAc \
            "SELECT COALESCE(floor(mtime),0)::bigint FROM kb.files WHERE path='${rel//\'/\'\'}'" 2>/dev/null || echo 0)
        i=${i:-0}; lag=$(( d - i ))
        if [ "$lag" -gt 180 ]; then echo "STILL STALE  lag=${lag}s  $rel"
        else echo "caught up    lag=${lag}s  $rel"; fi
      done <<< "$stale_paths"
    fi
  else
    echo "(no alerts.log)"
  fi
  echo '```'
  echo
  echo "## Previous run's report (for spotting what persists vs what is new)"
  echo '```'
  head -60 "$LAST_REPORT" 2>/dev/null || echo "(no previous report)"
  echo '```'
} > "$BUNDLE" 2>&1

# --------------------------------------------------------------------------
# 2. the agent
# --------------------------------------------------------------------------
# Harness-enforced allowlist. Read-only diagnostics plus exactly two restarts,
# both of services whose entire state is derived and rebuilt on start. No Write,
# no Edit, no WebFetch: the report travels on stdout, so the agent never needs
# to create a file, and a tool it does not have is a tool it cannot misuse.
# kb-hub and kb-syncd are deliberately absent — restarting the hub kills every
# open web terminal, and syncd can lose unflushed edits.
TOOLS=(
  "Read" "Grep" "Glob"
  "Bash(systemctl status:*)" "Bash(systemctl show:*)" "Bash(systemctl is-active:*)"
  "Bash(systemctl list-timers:*)" "Bash(systemctl --failed:*)"
  "Bash(sudo journalctl:*)" "Bash(journalctl:*)"
  "Bash(df:*)" "Bash(free:*)" "Bash(ps:*)" "Bash(ss -ltn)" "Bash(uptime:*)"
  "Bash(ls:*)" "Bash(head:*)" "Bash(tail:*)"
  # find/grep removed 2026-08-24: `find -exec` (and grep's pager/-f tricks) are
  # general command-execution primitives, and this agent is fed attacker-writable
  # text (health.md, filenames, journal lines). Read/Grep/Glob cover the same need
  # without a shell. Do not re-add them.
  "Bash(wc:*)" "Bash(stat:*)" "Bash(cat /var/log/kb/*)" "Bash(sudo cat /var/log/kb/*)"
  "Bash(git log:*)" "Bash(git status:*)" "Bash(git diff:*)"
  "Bash(curl -s -o /dev/null -w * http://127.0.0.1:$HUB_PORT/)"
  "Bash(sudo systemctl restart kb-convert)"
  "Bash(sudo systemctl restart kb-indexer)"
  "Bash(sudo logrotate --force /etc/logrotate.d/kb)"
)
BANNED=(
  "Write" "Edit" "NotebookEdit" "WebFetch" "WebSearch" "Task" "Agent"
  "Bash(sudo systemctl restart kb-hub)" "Bash(sudo systemctl restart kb-syncd)"
  "Bash(sudo systemctl restart postgresql)" "Bash(sudo bash:*)" "Bash(sudo sh:*)"
  "Bash(sudo rm:*)" "Bash(rm:*)" "Bash(sudo tee:*)" "Bash(sudo apt:*)"
  "Bash(pip:*)" "Bash(npm:*)" "Bash(git commit:*)" "Bash(git push:*)"
  "Bash(git checkout:*)" "Bash(git reset:*)"
)

PROMPT="You are running unattended. Read the policy at $POLICY and follow it exactly.

Then read the diagnostic bundle at $BUNDLE and triage it.

Print your report to stdout in the exact format the policy's Output contract
specifies, starting with the VERDICT line. Print nothing else — no preamble,
no explanation of your process. Your stdout IS the report file.

$( [ "$DRY_RUN" = 1 ] && echo "DRY RUN: do not CHANGE anything — no restarts, no deletions, no rotation, not even ones the policy allows. Read-only investigation is still expected of you: run the diagnostic commands you need (systemctl show, journalctl, df, grep …) to reach a confident verdict. 'I could not check' is not an acceptable finding when the command was available." )"

if [ "$DRY_RUN" = 1 ]; then
  ALLOWED=("Read" "Grep" "Glob" "Bash(systemctl status:*)" "Bash(systemctl show:*)"
           "Bash(systemctl is-active:*)" "Bash(journalctl:*)" "Bash(sudo journalctl:*)"
           "Bash(df:*)" "Bash(free:*)" "Bash(ls:*)"
           "Bash(head:*)" "Bash(tail:*)" "Bash(cat /var/log/kb/*)" "Bash(sudo cat /var/log/kb/*)")
else
  ALLOWED=("${TOOLS[@]}")
fi

REPORT="$WORK/report.md"
CLAUDE_BIN=""
[ -n "$OPERATOR" ] && CLAUDE_BIN=$(runuser -u "$OPERATOR" -- bash -lc 'command -v claude' 2>/dev/null)
if [ -z "$CLAUDE_BIN" ]; then
  printf '## VERDICT: PROBLEMS\n\n### Real problems\n- **Maintenance agent cannot run** — %s, so nothing was triaged this run. Install Claude Code as that admin and sign in once (`claude` in a web terminal), or set KB_MAINT_USER in /etc/kb/kb.env to an admin who has.\n' \
    "$([ -n "$OPERATOR" ] && echo "the \`claude\` CLI was not found for user $OPERATOR" || echo "no maintenance user is set (KB_MAINT_USER)")" > "$REPORT"
  rc=127
else
  runuser -u "$OPERATOR" -- env \
      HOME="$(getent passwd "$OPERATOR" | cut -d: -f6)" \
      KB_MAINT=1 \
      timeout "$BUDGET" "$CLAUDE_BIN" -p "$PROMPT" \
        --model "$MODEL" \
        --permission-mode default \
        --allowedTools "${ALLOWED[@]}" \
        --disallowedTools "${BANNED[@]}" \
        --add-dir "$WORK" --add-dir "$SELF_DIR" \
      > "$REPORT" 2> "$WORK/agent.err"
  rc=$?
fi

if [ "$rc" -ne 0 ] || [ ! -s "$REPORT" ]; then
  # A triage that could not run is itself worth knowing about — but it must say
  # so in the contract's own vocabulary, or the grep below reads it as "OK".
  {
    echo "## VERDICT: PROBLEMS"
    echo
    echo "### Real problems"
    if [ "$rc" -eq 124 ]; then
      echo "- **Maintenance triage timed out** after ${BUDGET}s and was killed. Nothing was triaged this run; the bundle is discarded. Check whether the box is under load."
    else
      echo "- **Maintenance triage failed** (exit $rc). Nothing was triaged this run."
      echo '```'
      tail -15 "$WORK/agent.err" 2>/dev/null | sed 's/^/  /'
      echo '```'
    fi
  } > "$REPORT"
fi

# --------------------------------------------------------------------------
# 3. record, then decide whether a human hears about it
# --------------------------------------------------------------------------
{
  echo "===== $(date -Is)  kb-maintenance (model=$MODEL, dry_run=$DRY_RUN, exit=$rc) ====="
  cat "$REPORT"
  echo
} >> "$LOG" 2>/dev/null || true
chmod 640 "$LOG" 2>/dev/null || true

[ "$TO_STDOUT" = 1 ] && cat "$REPORT"

if [ "$DRY_RUN" = 0 ]; then
  cp "$REPORT" "$LAST_REPORT" 2>/dev/null || true
  # Agent free-text: if anything ever induced it to quote a secret, this file
  # must not be the place every account reads it from.
  chmod 600 "$LAST_REPORT" 2>/dev/null || true
  date +%s > "$LAST_RUN_STAMP" 2>/dev/null || true
  : > "$RESTARTS_SNAPSHOT" 2>/dev/null || true
  for u in $UNITS; do
    echo "$u $(systemctl show "$u" -p NRestarts --value 2>/dev/null || echo 0)"
  done >> "$RESTARTS_SNAPSHOT" 2>/dev/null || true
fi

if head -1 "$REPORT" | grep -q "VERDICT: OK"; then
  echo "kb-maintenance: OK — nothing to report"
  exit 0
fi

# Something real. This is the one thing on this box still allowed to interrupt
# the operator, so it goes out even though per-occurrence pushes are off — and
# kb-alert's title dedup keeps a problem that persists for days to one push per
# KB_ALERT_DEDUP window rather than one per run.
COUNT=$(grep -c '^- \*\*' "$REPORT" 2>/dev/null || echo "?")
SUMMARY=$(sed -n 's/^- \*\*\([^*]*\)\*\*.*/• \1/p' "$REPORT" | head -6)
echo "kb-maintenance: PROBLEMS ($COUNT) — see $LOG"
if [ "$DRY_RUN" = 0 ]; then
  KB_ALERT_PUSH=1 "$ALERT" \
    "Company OS maintenance: $COUNT problem(s)" \
    "$SUMMARY

Full report: sudo tail -60 $LOG" \
    default wrench || true
fi
exit 0
