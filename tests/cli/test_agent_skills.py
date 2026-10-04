"""The agent context every agent CLI reads, as scripts/deploy.sh lays it out:
AGENTS.md (CLAUDE.md a link to it) and ONE skills folder, .agents/skills
(.claude/skills a link to it). Members add skills there and edit each other's;
the platform's own are root's, and the folder's sticky bit stops anyone from
renaming or deleting a skill folder they do not own. All of it is the kernel's
doing, so it is exercised through the same per-user API the editor uses."""
import os
import stat

import httpx
from kbenv import BASE, CREDS, NS, REPO, U

SKILLS = ".agents/skills"
BASE_SKILL = f"{SKILLS}/kb-orientation"


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    r = c.post("/login", data={"username": U(user), "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


def write(c, path, content):
    return c.post("/api/artifact/write", json={"path": path, "content": content})


def test_one_layout_for_every_agent():
    skills = os.lstat(REPO / SKILLS)
    assert stat.S_ISDIR(skills.st_mode) and skills.st_uid == 0
    assert skills.st_mode & stat.S_IWGRP, "members must be able to add skills"
    assert skills.st_mode & stat.S_ISVTX, "without the sticky bit a member could swap a platform skill out"
    assert os.readlink(REPO / ".claude" / "skills") == "../.agents/skills"

    agents = os.lstat(REPO / "AGENTS.md")
    assert stat.S_ISREG(agents.st_mode) and agents.st_uid == 0
    assert os.readlink(REPO / "CLAUDE.md") == "AGENTS.md"
    assert os.lstat(REPO / "CLAUDE.md").st_uid == 0, "a member could otherwise own the name Claude Code reads"

    base = os.lstat(REPO / BASE_SKILL)
    assert stat.S_ISDIR(base.st_mode) and base.st_uid == 0
    assert not base.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def test_members_share_skills_but_not_the_platforms():
    alice, bob = cl("alice"), cl("bob")
    skill = f"{SKILLS}/kbtest-{NS or 'x'}-skill"
    trash = REPO / SKILLS / ".trash"
    trash_existed = trash.exists()
    try:
        assert alice.post("/api/fs/mkdir", json={"path": skill}).status_code == 200
        r = write(alice, f"{skill}/SKILL.md", "---\nname: x\ndescription: by alice\n---\n")
        assert r.status_code == 200, r.text

        # anyone edits a skill a member wrote ...
        r = write(bob, f"{skill}/SKILL.md", "---\nname: x\ndescription: edited by bob\n---\n")
        assert r.status_code == 200, r.text
        back = alice.post("/api/artifact/read", json={"path": f"{skill}/SKILL.md"})
        assert "edited by bob" in back.json()["content"]

        # ... but only its creator renames or deletes the folder itself
        r = bob.post("/api/fs/rename", json={"src": skill, "dst": f"{skill}-mine"})
        assert r.status_code == 403, r.text
        assert bob.post("/api/fs/delete", json={"path": skill}).status_code == 403
        assert (REPO / skill / "SKILL.md").exists()

        # the platform's skills are root's: nobody edits, moves or deletes them
        assert write(bob, f"{BASE_SKILL}/SKILL.md", "hacked").status_code == 403
        assert bob.post("/api/fs/delete", json={"path": BASE_SKILL}).status_code == 403
        r = bob.post("/api/fs/rename", json={"src": BASE_SKILL, "dst": f"{BASE_SKILL}-old"})
        assert r.status_code == 403, r.text
        assert (REPO / BASE_SKILL / "SKILL.md").exists()
    finally:
        alice.post("/api/fs/delete", json={"path": skill, "permanent": True})
        # bob's refused deletes made the folder's .trash/ on the way; it is empty
        if not trash_existed and trash.exists():
            bob.post("/api/fs/delete", json={"path": f"{SKILLS}/.trash", "permanent": True})
    assert not (REPO / skill).exists()
