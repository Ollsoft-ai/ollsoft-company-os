"""P4 (attachments) + P5b (checkbox aggregation round-trip) gates."""
import json
import subprocess
import time

import httpx

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))


def client(user):
    c = httpx.Client(base_url=BASE, timeout=20)
    r = c.post("/login", data={"username": user, "password": CREDS[user]})
    assert r.status_code == 200
    return c


# --- P4: attachments inherit the folder's permissions ----------------------
def test_attachment_upload_and_permissioned_serving():
    kry = client("alice")
    # Upload into the confidential acme folder.
    files = {"file": ("secret.txt", b"acme attachment payload", "text/plain")}
    r = kry.post("/api/upload", params={"dir": "projects/acme"}, files=files)
    assert r.status_code == 200, r.text
    link = r.json()["path"]  # projects/acme/_files/secret.txt
    assert link == "projects/acme/_files/secret.txt"

    # Owner + teammate can fetch it.
    assert kry.get("/api/attachment", params={"path": link}).status_code == 200
    bob = client("bob")
    assert bob.get("/api/attachment", params={"path": link}).status_code == 200

    # Carol (not on the team) is denied by the kernel.
    carol = client("carol")
    r = carol.get("/api/attachment", params={"path": link})
    assert r.status_code == 403
    assert b"payload" not in r.content


# --- P5b: toggle a checkbox -> source file flips -> index + git follow ------
def test_checkbox_roundtrip():
    kry = client("alice")
    tasks = kry.get("/api/tasks").json()["tasks"]
    target = next(t for t in tasks
                  if t["path"] == "company/overview.md" and "Ship the knowledgebase" in t["text"])
    initial = target["checked"]
    expect = not initial

    before_count = git_count()
    r = kry.post("/api/tasks/toggle", json={"path": target["path"], "line": target["line"]})
    assert r.status_code == 200 and r.json()["checked"] is expect

    # Source file on disk flipped to the new state.
    disk = open(f"/srv/kb/{target['path']}").read().splitlines()
    marker = "- [x]" if expect else "- [ ]"
    assert disk[target["line"] - 1].strip().startswith(marker)

    # Index reflects the new state within ~3s (indexer reparsed the file).
    ok = False
    for _ in range(24):    # generous: the indexer lags under full-suite load
        t2 = kry.get("/api/tasks").json()["tasks"]
        m = next((t for t in t2 if t["path"] == target["path"] and t["line"] == target["line"]), None)
        if m and m["checked"] is expect:
            ok = True
            break
        time.sleep(0.5)
    assert ok, "index must reflect the toggled checkbox"

    # A git snapshot eventually captures the change.
    advanced = False
    for _ in range(12):
        if int(git_count() or "0") > int(before_count or "0"):
            advanced = True
            break
        time.sleep(0.5)
    assert advanced, "git must snapshot the checkbox change"

    # Toggle back to leave the seed as we found it.
    kry.post("/api/tasks/toggle", json={"path": target["path"], "line": target["line"]})


def git_count():
    # .git is root-only (its objects would bypass file permissions); syncd
    # publishes repo stats to a world-readable tmpfs file instead.
    try:
        return str(json.load(open("/run/kb/git-state.json"))["commits"])
    except (OSError, ValueError, KeyError):
        return "0"
