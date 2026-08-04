"""kb-history — the knowledgebase's version history, from the terminal.

Talks to kb-syncd over /run/kb/vc.sock; the daemon learns WHO is asking from
SO_PEERCRED (the kernel's word on the calling uid — no login, no token), and
shows history only for files the caller can read right now. Made for humans
and for agents ("what did tomas do yesterday?" is one command away).

Usage:
  kb-history                                  today's changes across your visible files
  kb-history --since "2 days ago" --author tomas_vargosko
  kb-history company/notes.md                 that file's versions
  kb-history company/notes.md --rev a1b2c3    what that version changed (diff)
  kb-history company/notes.md --rev a1b2c3 --show      full content back then
  kb-history company/notes.md --rev a1b2c3 --restore   write that version back (as you)
  kb-history ... --json                       machine-readable output
"""
from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import time
import urllib.parse
from pathlib import Path

VC_SOCK = "/run/kb/vc.sock"
REPO = Path("/srv/kb")


class _UDSConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("kb")
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(self._path)
        self.sock = s


def _get(op: str, **params) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v})
    conn = _UDSConnection(VC_SOCK)
    try:
        conn.request("GET", f"/vc/{op}" + (f"?{qs}" if qs else ""))
        resp = conn.getresponse()
        body = resp.read()
        try:
            data = json.loads(body)
        except ValueError:
            data = {"error": body.decode(errors="replace").strip() or f"HTTP {resp.status}"}
        if resp.status != 200:
            sys.exit(f"kb-history: {data.get('error', 'HTTP %d' % resp.status)}")
        return data
    except (ConnectionRefusedError, FileNotFoundError):
        sys.exit("kb-history: the version service is not running (kb-syncd)")
    finally:
        conn.close()


def _when(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


_STATUS = {"M": "edited", "A": "created", "D": "deleted", "R": "renamed", "C": "copied"}


def main() -> None:
    ap = argparse.ArgumentParser(prog="kb-history", add_help=True,
                                 description="Version history of the knowledgebase — "
                                             "only ever shows files you can read.")
    ap.add_argument("path", nargs="?", help="a document/artifact path for per-file history")
    ap.add_argument("--since", default="1 day ago", help='e.g. "yesterday", "3 days ago" (default: 1 day ago)')
    ap.add_argument("--until", default="", help='e.g. "today 08:00"')
    ap.add_argument("--author", default="", help="only this user's changes")
    # default depends on the mode: one file's versions (50) vs the activity
    # feed (200, the server's own default) — None means "let the mode decide".
    ap.add_argument("--limit", type=int, default=None,
                    help="max rows (default: 50 per file, 200 for the activity feed)")
    ap.add_argument("--rev", default="", help="a version id from the history list")
    ap.add_argument("--show", action="store_true", help="print the file's full content at --rev")
    ap.add_argument("--diff", action="store_true", help="print the patch introduced by --rev (default with --rev)")
    ap.add_argument("--restore", action="store_true", help="write the --rev version back to the file, as you")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    if a.rev and not a.path:
        sys.exit("kb-history: --rev needs a file path")

    if a.path and a.rev:
        if a.restore:
            data = _get("show", path=a.path, rev=a.rev)
            target = REPO / a.path.lstrip("/")
            try:
                target.write_text(data["content"])   # as the caller — kernel decides
            except OSError as e:
                sys.exit(f"kb-history: cannot write {a.path}: {e}")
            print(f"restored {a.path} to version {a.rev} ({len(data['content'])} chars); "
                  f"the live editor picks it up within seconds")
            return
        if a.show:
            data = _get("show", path=a.path, rev=a.rev)
            print(json.dumps(data) if a.json else data["content"], end="")
            return
        data = _get("diff", path=a.path, rev=a.rev)
        print(json.dumps(data) if a.json else data["patch"], end="")
        return

    if a.path:
        data = _get("log", path=a.path, limit=str(a.limit or 50))
        if a.json:
            print(json.dumps(data))
            return
        if not data["entries"]:
            print(f"no history yet for {data['path']}")
            return
        print(f"versions of {data['path']} (newest first):")
        for e in data["entries"]:
            print(f"  {e['rev']}  {_when(e['ts'])}  {e['author']:<18} {e['subject']}")
        print("\nsee one:  kb-history "
              f"{data['path']} --rev <id>   (--show for content, --restore to bring it back)")
        return

    data = _get("activity", since=a.since, until=a.until, author=a.author,
                limit=str(a.limit or 200))
    if a.json:
        print(json.dumps(data))
        return
    commits = data["commits"]
    if not commits:
        who = f" by {a.author}" if a.author else ""
        print(f"no visible changes{who} since {data['since']}")
        return
    print(f"changes since {data['since']}" + (f" by {a.author}" if a.author else "") + ":")
    for c in commits:
        print(f"\n  {_when(c['ts'])}  {c['author']}  ({c['rev']})")
        for f in c["files"]:
            verb = _STATUS.get(f["status"], f["status"])
            print(f"      {verb:<8} " + " -> ".join(f["paths"]))
    # Say it out loud: a capped feed that looks complete is how "no activity"
    # gets reported for someone who was working the whole week.
    if data.get("truncated"):
        print(f"\n! showing the newest {len(commits)} changes only — there are older ones "
              f"in this window.\n  narrow it (--author, --until) or raise --limit.")
    print("\ndetails:  kb-history <path>          versions of one file"
          "\n          kb-history <path> --rev <id>   the change itself")


if __name__ == "__main__":
    main()
