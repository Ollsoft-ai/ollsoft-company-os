"""Regression tests for the security-sweep remediations."""
import json
import os
import subprocess
import tempfile
import time

import httpx
from kbenv import AREA, BASE, CREDS, U, doc



def cl(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
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
    """The link is planted in the run's shared area — group-writable to every
    kb-users member — so the test works no matter which OS user runs the suite
    (an earlier version used users/alice/, writable only when the runner IS
    alice, which is true on CI and false on a dev box). It has to be the SAME
    directory the upload targets, or the upload simply lands somewhere with no
    symlink in it and passes while proving nothing."""
    t = int(time.time())
    target = f"/tmp/kb_sec_target_{t}"
    name = f"evil_link_{t}"
    link = f"/srv/kb/{AREA}/{name}"
    if os.path.exists(target):
        os.remove(target)
    subprocess.run(["sg", "kb-users", "-c", f"ln -sf {target} {link}"], check=True)
    try:
        r = cl("alice").post("/fs/upload", params={"dir": AREA},
                             files={"file": (name, b"PWNED", "text/plain")})
        assert r.status_code != 200, "upload through a symlink must fail"
        assert not os.path.exists(target), "root must not have written through the symlink"
    finally:
        subprocess.run(["sg", "kb-users", "-c", f"rm -f {link}"], check=False)


# --- MEDIUM: the indexer never indexes symlinks ----------------------------
def test_indexer_skips_symlinks():
    """Self-contained: the symlink targets a file the test creates itself. The
    owner invariant is 'the indexed owner is the FILESYSTEM owner of the
    target, before and after a symlink appears' — not any particular username:
    the hub's newfile deliberately creates group-shared files owned by root,
    and whoever owns company/overview.md on a given box is a coin toss."""
    import pwd as _pwd
    t = int(time.time())
    target_doc = doc(f"kbtest_symtarget_{t}.md")
    link = f"evillink_{t}.md"
    a = cl("alice")
    assert a.post("/fs/newfile", json={"path": target_doc}).status_code == 200
    assert a.post("/api/artifact/write",
                  json={"path": target_doc, "content": "# target\n\nsymlink bait\n"}).status_code == 200
    fs_owner = _pwd.getpwuid(os.stat(f"/srv/kb/{target_doc}").st_uid).pw_name

    def indexed_owner():
        return subprocess.run(["sg", "kb-users", "-c",
                              f"psql -d kb -tAc \"SELECT owner_name FROM kb.files WHERE path='{target_doc}'\""],
                              capture_output=True, text=True).stdout.strip()

    try:
        deadline = time.time() + 15
        while time.time() < deadline and indexed_owner() != fs_owner:
            time.sleep(0.5)
        assert indexed_owner() == fs_owner, "baseline: the target must index with its fs owner"

        subprocess.run(["sg", "kb-users", "-c",
                        f"ln -sf /srv/kb/{target_doc} /srv/kb/{AREA}/{link}"], check=False)
        time.sleep(3)
        out = subprocess.run(["sg", "kb-users", "-c",
                              f"psql -d kb -tAc \"SELECT count(*) FROM kb.files WHERE path='{AREA}/{link}'\""],
                             capture_output=True, text=True)
        assert out.stdout.strip() == "0", "a symlink must not be indexed"
        # and the real target's metadata is untouched (owner not clobbered)
        assert indexed_owner() == fs_owner, "symlink must not clobber the target's indexed owner"
    finally:
        subprocess.run(["sg", "kb-users", "-c", f"rm -f /srv/kb/{AREA}/{link}"], check=False)
        a.post("/api/fs/delete", json={"path": target_doc})


# --- HIGH: RLS no longer over-shares via the ACL mask ----------------------
def test_acl_info_demasks_group_bits():
    from kb_platform.indexer import acl_info
    from pathlib import Path
    d = tempfile.mkdtemp()
    f = Path(d) / "x.md"
    f.write_text("x")
    os.chmod(f, 0o600)                                   # owner-only
    subprocess.run(["setfacl", "-m", f"u:{U('bob')}:r", "--", str(f)], check=True)  # share with one user
    mode, users, groups, x_users, x_groups = acl_info(f)
    subprocess.run(["rm", "-rf", d], check=False)
    assert (mode >> 3) & 7 == 0, "group bits must reflect real group:: (---), not the ACL mask"
    assert "bob" in users, "the named-user share must still be recorded"


# --- HIGH: the two-arg can_read oracle stays gone --------------------------
def test_can_read_single_arg_only():
    r = cl("carol").post("/api/artifact/query",
                          json={"sql": "SELECT kb.can_read('company/overview.md','alice')"})
    assert "error" in r.json(), "arbitrary-user can_read oracle must not exist"
