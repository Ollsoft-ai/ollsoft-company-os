"""Moves: what lets a document's history follow it to a new name or folder.

Git stores no renames. It infers one when a single commit deletes a file and
adds a similar one — and kb-syncd commits one file per commit (so a commit
never names a file its reader cannot see), which splits every move made in the
app into a delete and an add that git never pairs. Before this module a moved
document started life with no past: the version list began at the move, blame
credited every line to the mover, and the activity feed lost everything its
authors had done under the old name.

So syncd keeps its own record of moves — a ledger, root-only inside .git
(kb-moves.jsonl) — and every history read follows it. A move is recorded only
on evidence in the commits themselves, never on anyone's say-so: a claim in the
world-writable hint drop-box would let a user graft someone else's history onto
a file of their own. The evidence is one of:

  commit  a rename git detects INSIDE one commit at >= 90% similarity
          (a `mv` the sweep committed whole, maybe with a small edit);
  pair    a delete and an add of the SAME, non-empty content no more than
          PAIR_WINDOW seconds apart, the only such pair in that window (a move
          the commit loop split into two commits).

Copying a document and deleting the original a minute later is therefore not a
move — the way to give a document a fresh start without its past.

Pure functions over a git runner, so tests can point them at a scratch repo.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable

PAIR_WINDOW = 10          # seconds: one commit pass (0.25 s flush + 4 s quiet) with room to spare
RENAME_SIMILARITY = "90%"
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
MAX_CHAIN = 20            # moves followed back from one document

Git = Callable[[list], str]           # args -> stdout (raises nothing; "" on failure)


# ── finding moves in history ──────────────────────────────────────────────────
def _raw_commits(out: str) -> list[dict]:
    """Parse `git log --format=%x01%H%x1f%at --raw -z --no-abbrev`: one dict
    per commit, {sha, ts, entries: [(status, old_blob, new_blob, paths)]}.
    Same stream shape as the activity feed's parser: the format's newline
    lands on the first raw token, R/C carry two paths."""
    commits: list[dict] = []
    toks = out.split("\0")
    i, n, cur = 0, len(toks), None
    while i < n:
        t = toks[i]
        if t.startswith("\x01") and "\x1f" in t:
            sha, _, ts = t[1:].partition("\x1f")
            cur = {"sha": sha, "ts": int(ts) if ts.isdigit() else 0, "entries": []}
            commits.append(cur)
            i += 1
            continue
        t = t.lstrip("\n")
        if not t.startswith(":") or cur is None:
            i += 1
            continue
        parts = t[1:].split(" ")
        if len(parts) < 5:
            i += 1
            continue
        status = parts[4]
        want = 2 if status[:1] in ("R", "C") else 1
        cur["entries"].append((status[:1], parts[2], parts[3], toks[i + 1:i + 1 + want]))
        i += 1 + want
    return commits


def find_moves(git: Git, versioned: Callable[[str], bool], since: str | None = None) -> list[dict]:
    """Every move the evidence supports, oldest first. `since` (an ISO time)
    limits the scan to recent commits — the commit loop's incremental pass;
    None is the whole history (the one-time backfill)."""
    args = ["log", "--reverse", "--format=%x01%H%x1f%at", "--raw", "-z", "--no-abbrev",
            f"-M{RENAME_SIMILARITY}", "--diff-filter=ADR"]
    if since:
        args.append(f"--since={since}")
    links: list[dict] = []
    adds: dict[str, list] = {}
    dels: dict[str, list] = {}
    for c in _raw_commits(git(args)):
        for status, old_blob, new_blob, paths in c["entries"]:
            if status == "R" and len(paths) == 2:
                old, new = paths
                if versioned(old) and versioned(new) and old != new:
                    links.append({"old": old, "new": new, "add": c["sha"], "del": c["sha"],
                                  "ts": c["ts"], "via": "commit"})
            elif status == "A" and paths and versioned(paths[0]) and new_blob != EMPTY_BLOB:
                adds.setdefault(new_blob, []).append((c["ts"], c["sha"], paths[0]))
            elif status == "D" and paths and versioned(paths[0]) and old_blob != EMPTY_BLOB:
                dels.setdefault(old_blob, []).append((c["ts"], c["sha"], paths[0]))
    for blob, ds in dels.items():
        as_ = adds.get(blob)
        if not as_:
            continue
        for d in ds:
            # an add at the very path being deleted (its own creation moments
            # before, or a rewrite) is never where it went
            near = [a for a in as_ if abs(a[0] - d[0]) <= PAIR_WINDOW and a[2] != d[2]]
            if len(near) != 1:
                continue                       # nothing, or ambiguous: link nothing
            a = near[0]
            rivals = [x for x in ds if abs(x[0] - a[0]) <= PAIR_WINDOW and x[2] != a[2]]
            if len(rivals) != 1:
                continue                       # two candidate sources: link nothing
            links.append({"old": d[2], "new": a[2], "add": a[1], "del": d[1],
                          "ts": a[0], "via": "pair"})
    links.sort(key=lambda link: link["ts"])
    return links


# ── the ledger ───────────────────────────────────────────────────────────────
class Ledger:
    """Append-only JSONL of moves, loaded whole (a line per move: small) and
    indexed by new and old path. Thread-safe: the commit loop appends from an
    executor while request handlers read."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._keys: set = set()
        self.by_new: dict[str, list] = {}
        self.by_old: dict[str, list] = {}
        try:
            for line in path.read_text().splitlines():
                try:
                    self._index(json.loads(line))
                except (ValueError, KeyError, TypeError):
                    continue
        except OSError:
            pass

    def _index(self, link: dict) -> bool:
        key = (link["old"], link["new"], link["add"])
        if key in self._keys:
            return False
        self._keys.add(key)
        self.by_new.setdefault(link["new"], []).append(link)
        self.by_old.setdefault(link["old"], []).append(link)
        return True

    def add(self, links: list[dict]) -> int:
        with self._lock:
            fresh = [link for link in links if self._index(link)]
            if fresh:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as f:
                    for link in fresh:
                        f.write(json.dumps(link, ensure_ascii=False) + "\n")
            return len(fresh)

    def __len__(self) -> int:
        return len(self._keys)


# ── following a document back through its moves ──────────────────────────────
def _path_log(git: Git, upto: str, path: str) -> list[dict]:
    """[{sha, author, ts, subject, status}] for `path` up to `upto`, newest
    first — status is this path's own (A/M/D) in that commit. Never `-n`:
    the commit that created the file can be far back, and the walk needs it."""
    args = ["log", "--no-renames", "--name-status", "-z",
            "--format=%x01%H%x1f%an%x1f%at%x1f%s", upto, "--", path]
    out, toks = [], git(args).split("\0")
    for t in toks:
        if t.startswith("\x01"):
            parts = t[1:].split("\x1f", 3)
            if len(parts) == 4 and parts[2].isdigit():
                out.append({"sha": parts[0], "author": parts[1], "ts": int(parts[2]),
                            "subject": parts[3], "status": ""})
        elif out and not out[-1]["status"]:
            s = t.lstrip("\n")[:1]
            if s in ("A", "M", "D", "T"):
                out[-1]["status"] = s
    return out


def lineage(git: Git, ledger: Ledger, rel: str, limit: int | None = None) -> list[dict]:
    """The document now at `rel`, back through every recorded move:
    [{path, entries, link}] — segment 0 is `rel` itself; each later segment is
    an earlier name, `link` the move that ended it.

    A segment ends where its file was CREATED: if that creation is a recorded
    move, the walk continues at the old name, just before it was deleted. An
    earlier, unrelated file that once had the same name is never followed —
    only the current file's own past. (Segment 0 keeps the whole path's log
    when it did not arrive by a move: that has always been its history.)

    `limit` bounds the entries gathered (the version list); None walks it all
    (resolving which name a version had)."""
    segs: list[dict] = []
    path, upto, budget = rel, "HEAD", limit
    for depth in range(MAX_CHAIN):
        entries = _path_log(git, upto, path)
        born = next((i for i, e in enumerate(entries) if e["status"] == "A"), None)
        link = None
        if born is not None:
            link = next((m for m in ledger.by_new.get(path, ()) if m["add"] == entries[born]["sha"]), None)
            if link is not None or depth > 0:
                entries = entries[:born + 1]
        if budget is not None:
            entries = entries[:budget]
            budget -= len(entries)
        segs.append({"path": path, "entries": entries, "link": link})
        if link is None or (budget is not None and budget <= 0):
            break
        path, upto = link["old"], link["del"] + "^"
    return segs


def path_at(segs: list[dict], sha: str) -> str | None:
    """The name the document had in commit `sha`, or None if that commit is
    not part of its history."""
    for s in segs:
        if any(e["sha"] == sha for e in s["entries"]):
            return s["path"]
    return None
