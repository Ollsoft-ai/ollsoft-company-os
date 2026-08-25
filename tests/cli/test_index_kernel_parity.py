"""kb.visible_files against the KERNEL, not against kb.can_read.

The index materialises "who may see what" so search can be a plain query under
RLS. Its correctness has only ever been checked against kb.can_read — a second
implementation of the same idea, by the same hand. Two models that agree with
each other prove nothing: they drift together and the parity test stays green.

The kernel is the authorization engine for everything else in this product, so
it is the oracle here too. Both directions matter:

  over-grant  — a row exists but the kernel refuses      => search leaks
  under-grant — the kernel allows but no row exists      => a document quietly
                                                            vanishes from search

Sampled rather than exhaustive: this shells out per pair, and a full cross
product on a real box is ~700 files x ~14 accounts.
"""
import os
import random
import subprocess

import pytest

DB = os.environ.get("KB_PG_DB", "kb")
SAMPLE = int(os.environ.get("KB_PARITY_SAMPLE", "40"))


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _sudo(cmd):
    return cmd if os.geteuid() == 0 else ["sudo", "-n"] + cmd


pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 and subprocess.run(["sudo", "-n", "true"],
                                         capture_output=True).returncode != 0,
    reason="needs root or passwordless sudo to ask the kernel as another user",
)


def _psql(sql):
    out = subprocess.run(_sudo(["runuser", "-u", "postgres", "--",
                                "psql", "-d", DB, "-tAF\x1f", "-c", sql]),
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        pytest.skip(f"cannot query the index: {out.stderr.strip()[:200]}")
    return [ln.split("\x1f") for ln in out.stdout.splitlines() if ln.strip()]


def _kernel_reads(user, rel):
    return subprocess.run(
        _sudo(["runuser", "-u", user, "--", "/usr/bin/test", "-r", f"/srv/kb/{rel}"]),
        capture_output=True).returncode == 0


def test_no_row_in_visible_files_over_grants():
    """Every (user, path) the index publishes must survive a kernel check."""
    rows = _psql(f"SELECT usr, path FROM kb.visible_files "
                 f"TABLESAMPLE SYSTEM (5) LIMIT {SAMPLE}")
    if not rows:
        rows = _psql(f"SELECT usr, path FROM kb.visible_files LIMIT {SAMPLE}")
    if not rows:
        pytest.skip("kb.visible_files is empty")
    bad = [(u, p) for u, p in rows if not _kernel_reads(u, p)]
    assert not bad, (
        f"the index grants {len(bad)} of {len(rows)} sampled rows the kernel refuses "
        f"— these are searchable by someone who cannot open them: {bad[:5]}"
    )


def test_visible_files_does_not_under_grant():
    """And the reverse: a file the kernel lets you read must be indexed for you.

    Under-granting is the quieter failure — the document simply never appears
    in your search results and nobody files a bug, they just conclude search is
    bad.
    """
    users = [u for (u,) in _psql(
        "SELECT DISTINCT usr FROM kb.visible_files ORDER BY usr")]
    files = [p for (p,) in _psql(f"SELECT path FROM kb.files LIMIT 400")]
    if not users or not files:
        pytest.skip("nothing indexed yet")
    rnd = random.Random(1234)
    missing = []
    for _ in range(SAMPLE):
        u, p = rnd.choice(users), rnd.choice(files)
        has_row = bool(_psql("SELECT 1 FROM kb.visible_files WHERE usr=%s AND path=%s"
                             .replace("%s", "{}").format(_lit(u), _lit(p))))
        if not has_row and _kernel_reads(u, p):
            missing.append((u, p))
    assert not missing, (
        f"{len(missing)} sampled files are readable by a user the index does not "
        f"list them for — they are silently missing from that person's search: "
        f"{missing[:5]}"
    )

