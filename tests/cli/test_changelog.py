"""Version and changelog: one source, shown everywhere.

release.sh writes each release's section of CHANGELOG.md from the commit
titles since the previous tag (scripts/changelog.sh); deploy.sh lays the
version and the changelog into the deployed tree; the hub serves them to
Settings → About. The release half runs in a throwaway repository, so it
never tags or commits anything real."""
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
from kbenv import BASE, CREDS, U

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GIT_ID = {"GIT_AUTHOR_NAME": "Release Bot", "GIT_AUTHOR_EMAIL": "bot@example.invalid",
          "GIT_COMMITTER_NAME": "Release Bot", "GIT_COMMITTER_EMAIL": "bot@example.invalid"}


def sh(repo, *cmd):
    return subprocess.run(cmd, cwd=repo, env={**os.environ, **GIT_ID}, check=True,
                          capture_output=True, text=True).stdout


def commit(repo, title, **files):
    for name, text in files.items():
        p = repo / name.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", title)


def sections(repo):
    """{version: [lines]} in file order, plus that order."""
    out, order, cur = {}, [], None
    for line in (repo / "CHANGELOG.md").read_text().splitlines():
        m = re.match(r"^## (\S+) — (\d{4}-\d{2}-\d{2})$", line)
        if m:
            cur = m.group(1)
            order.append(cur)
            out[cur] = []
        elif line.startswith("- ") and cur:
            out[cur].append(line[2:])
    return out, order


def test_release_writes_the_changelog_from_commit_titles(tmp_path):
    repo = tmp_path / "os"
    (repo / "scripts").mkdir(parents=True)
    for s in ("release.sh", "changelog.sh"):
        shutil.copy(SCRIPTS / s, repo / "scripts" / s)
    sh(repo, "git", "init", "-q", "-b", "main")
    commit(repo, "Start the platform", VERSION="0.0.0\n", LICENSE="Change Date: 2030-01-01\n",
           NOTICE="On 2030-01-01, or four years after\n", app__main_py="print(1)\n")
    sh(repo, "bash", "scripts/release.sh", "1.0.0")
    assert sh(repo, "git", "tag").split() == ["v1.0.0"]

    commit(repo, "Show who wrote each line", app__main_py="print(2)\n")
    commit(repo, "Reword the README", **{"README.md": "hi\n"})
    commit(repo, "Cover blame with a test", tests__test_blame_py="assert 1\n")
    commit(repo, "Tell agents what `kb-search` covers", app__search_py="x = 1\n")
    out = sh(repo, "bash", "scripts/release.sh", "1.1.0")
    assert "2 line(s) since v1.0.0" in out

    secs, order = sections(repo)
    assert order == ["1.1.0", "1.0.0"], "newest release first"
    assert secs["1.1.0"] == ["Show who wrote each line", "Tell agents what `kb-search` covers"], \
        "README-, test- and release-only commits are not changes anyone notices"
    assert secs["1.0.0"] == ["Start the platform"]
    assert (repo / "CHANGELOG.md").read_text().startswith("# Changelog\n")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert f"## 1.1.0 — {today}" in (repo / "CHANGELOG.md").read_text()
    # the changelog rides in the release commit, beside VERSION and the licence
    assert "CHANGELOG.md" in sh(repo, "git", "show", "--name-only", "--format=", "v1.1.0")
    assert (repo / "VERSION").read_text().strip() == "1.1.0"

    # the same rule a deploy uses for "not released yet"
    commit(repo, "Fold all keeps the top level open", app__tree_py="y = 2\n")
    assert sh(repo, "bash", "scripts/changelog.sh", "v1.1.0..HEAD") == "- Fold all keeps the top level open\n"

    sh(repo, "bash", "scripts/release.sh", "1.1.1")

    # a release with nothing user-facing still says so, rather than an empty heading
    commit(repo, "Document the tree", docs__tree_md="tree\n")
    sh(repo, "bash", "scripts/release.sh", "1.1.2")
    secs, order = sections(repo)
    assert order == ["1.1.2", "1.1.1", "1.1.0", "1.0.0"]
    assert secs["1.1.2"] == ["No user-facing changes."]
    assert secs["1.1.1"] == ["Fold all keeps the top level open"]


def test_about_needs_a_sign_in_and_serves_version_and_changelog():
    anon = httpx.Client(base_url=BASE, timeout=15)
    assert anon.get("/about").status_code == 401, "a version string is not for strangers"
    c = httpx.Client(base_url=BASE, timeout=15)
    assert c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]}).status_code == 200
    r = c.get("/about")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    j = r.json()
    assert re.match(r"^\d+\.\d+\.\d+", j["version"]), j["version"]
    assert j["changelog"].startswith("# Changelog") and "\n## " in j["changelog"]
    assert isinstance(j["unreleased"], str)
