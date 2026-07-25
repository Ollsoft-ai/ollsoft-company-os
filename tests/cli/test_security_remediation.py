"""Regression tests for the security-sweep remediations."""
import json
import os
import subprocess
import tempfile
import time

import httpx

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": user, "password": CREDS[user]})
    return c


# --- CRITICAL: per-user backend socket dir is not squattable ---------------
def test_socket_dir_not_world_writable():
    st = os.stat("/run/kb/users")
    assert st.st_uid == 0
    assert not (st.st_mode & 0o022), "socket dir must not be group/other-writable"
    # a live per-user socket dir is 0700 owned by the user
    d = "/run/kb/users/alice"
    if os.path.isdir(d):
        ds = os.stat(d)
        assert ds.st_mode & 0o777 == 0o700 and ds.st_uid != 0


# --- CRITICAL: fs_upload cannot be redirected by a planted symlink ---------
def test_fs_upload_refuses_symlink():
    target = "/tmp/kb_sec_target_%d" % int(time.time())
    link = "/srv/kb/users/alice/evil_link"
    if os.path.exists(target):
        os.remove(target)
    subprocess.run(["ln", "-sf", target, link], check=True)   # alice owns his dir
    try:
        kry = cl("alice")
        r = kry.post("/fs/upload", params={"dir": "users/alice"},
                     files={"file": ("evil_link", b"PWNED", "text/plain")})
        assert r.status_code != 200, "upload through a symlink must fail"
        assert not os.path.exists(target), "root must not have written through the symlink"
    finally:
        subprocess.run(["rm", "-f", link], check=False)


# --- MEDIUM: the indexer never indexes symlinks ----------------------------
def test_indexer_skips_symlinks():
    link = "evillink_%d.md" % int(time.time())
    subprocess.run(["sg", "kb-users", "-c",
                    f"ln -sf /srv/kb/company/overview.md /srv/kb/company/{link}"], check=False)
    try:
        time.sleep(3)
        out = subprocess.run(["sg", "kb-users", "-c",
                              f"psql -d kb -tAc \"SELECT count(*) FROM kb.files WHERE path='company/{link}'\""],
                             capture_output=True, text=True)
        assert out.stdout.strip() == "0", "a symlink must not be indexed"
        # and the real target's metadata is untouched (owner not clobbered)
        owner = subprocess.run(["sg", "kb-users", "-c",
                               "psql -d kb -tAc \"SELECT owner_name FROM kb.files WHERE path='company/overview.md'\""],
                              capture_output=True, text=True).stdout.strip()
        assert owner == "alice", "symlink must not clobber the target's indexed owner"
    finally:
        subprocess.run(["sg", "kb-users", "-c", f"rm -f /srv/kb/company/{link}"], check=False)


# --- HIGH: RLS no longer over-shares via the ACL mask ----------------------
def test_acl_info_demasks_group_bits():
    from kb_platform.indexer import acl_info
    from pathlib import Path
    d = tempfile.mkdtemp()
    f = Path(d) / "x.md"
    f.write_text("x")
    os.chmod(f, 0o600)                                   # owner-only
    subprocess.run(["setfacl", "-m", "u:bob:r", "--", str(f)], check=True)  # share with one user
    mode, users, groups, x_users, x_groups = acl_info(f)
    subprocess.run(["rm", "-rf", d], check=False)
    assert (mode >> 3) & 7 == 0, "group bits must reflect real group:: (---), not the ACL mask"
    assert "bob" in users, "the named-user share must still be recorded"


# --- HIGH: the two-arg can_read oracle stays gone --------------------------
def test_can_read_single_arg_only():
    r = cl("carol").post("/api/artifact/query",
                          json={"sql": "SELECT kb.can_read('company/overview.md','alice')"})
    assert "error" in r.json(), "arbitrary-user can_read oracle must not exist"
