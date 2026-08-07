#!/usr/bin/env python3
"""One-shot sync of kb.visible_files from the current index state.

Run as kbindexer (the table's only writer), against the DEPLOYED code:

    sudo -u kbindexer /opt/kb-venv/bin/python /opt/kb-platform/scripts/refresh_visibility.py

Exists for the materialized-visibility migration — the table must be populated
BEFORE the RLS policies start gating on it, and the running indexer may predate
the maintenance code — and for manual resync after surgery. Idempotent; prints
per-user counts so the result can be eyeballed against expectations.
"""
import os
import pwd
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import psycopg

from kb_platform import common
from kb_platform.indexer import VIS_LOCK, compute_visibility


def main() -> int:
    users = [u.pw_name for u in pwd.getpwall() if 1000 <= u.pw_uid < 65000]
    conn = psycopg.connect(f"dbname={common.PG_DB}")
    with conn.cursor() as cur:
        # Serialise against the running indexer's own refresh_visibility: both
        # are read-modify-write on this table under READ COMMITTED, so without
        # the lock each can compute its diff from a snapshot the other has
        # already moved past and re-INSERT pairs the other just revoked.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (VIS_LOCK,))
        cur.execute("SELECT path, owner_name, group_name, mode, acl_users, "
                    "acl_groups, acl_x_users, acl_x_groups FROM kb.files")
        files_rows = cur.fetchall()
        cur.execute("SELECT usr, grp FROM kb.user_groups")
        group_rows = cur.fetchall()
        desired = compute_visibility(files_rows, group_rows, users)
        cur.execute("SELECT usr, path FROM kb.visible_files")
        current = set(cur.fetchall())
        gone, new = sorted(current - desired), sorted(desired - current)
        if gone:
            cur.executemany("DELETE FROM kb.visible_files WHERE usr=%s AND path=%s", gone)
        if new:
            cur.executemany("INSERT INTO kb.visible_files(usr,path) VALUES(%s,%s) "
                            "ON CONFLICT DO NOTHING", new)
    conn.commit()
    per_user = Counter(u for u, _ in desired)
    print(f"files={len(files_rows)} users={len(users)} pairs={len(desired)} "
          f"(+{len(new)} -{len(gone)})")
    for u in sorted(per_user):
        print(f"  {u}: {per_user[u]}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
