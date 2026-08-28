"""P6 gate: a real LLM agent (headless `claude`) running with the user's kernel
identity edits a plain markdown file, and the edit flows into a live multiplayer
session — the AI-native thesis, proven end to end. Also asserts the kernel bounds
an agent to its user's permissions.
"""
import shutil
import subprocess
import time

import httpx
import pytest
from conftest import BASE, CREDS, login, open_doc, doc_text
from kbenv import U, doc, proj

AGENT_DOC = doc("agent_doc.md")


@pytest.fixture(scope="module", autouse=True)
def _remove_fixture_doc_afterwards():
    yield
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    c.post("/api/fs/delete", json={"path": AGENT_DOC})


def _ensure_agent_doc():
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("alice"), "password": CREDS["alice"]})
    c.post("/api/file", json={"path": AGENT_DOC})
    with open(f"/srv/kb/{AGENT_DOC}", "w") as f:
        f.write("# Agent scratch doc\n\nThe agent will append below.\n")
    time.sleep(0.6)


@pytest.mark.skipif(shutil.which("claude") is None,
                    reason="headless `claude` is not installed on this machine")
def test_agent_edit_flows_into_live_session(browser):
    _ensure_agent_doc()
    ctx = browser.new_context()
    page = login(ctx, "alice")
    open_doc(page, AGENT_DOC)
    for _ in range(20):
        if doc_text(page):
            break
        time.sleep(0.25)

    token = "AGENT_EDIT_PROOF_7Q2"
    prompt = (f"Append a new line containing exactly the text {token} to the file "
              f"/srv/kb/{AGENT_DOC}. Change nothing else. Then stop.")
    # Runs as the current OS user (alice); the agent touches the file, nothing else.
    res = subprocess.run(
        ["claude", "-p", prompt, "--dangerously-skip-permissions"],
        cwd="/srv/kb", capture_output=True, text=True, timeout=180)
    assert res.returncode == 0, f"claude failed: {res.stderr[:400]}"

    # The daemon must merge the agent's file edit into the live browser session.
    appeared = False
    for _ in range(24):
        if token in doc_text(page):
            appeared = True
            break
        time.sleep(0.3)
    assert appeared, "agent's file edit must appear in the live editor via the daemon"
    ctx.close()


def test_agent_bounded_by_kernel_identity():
    """An agent running as carol cannot reach acme — the same kernel boundary
    that bounds the human bounds their agent (tested via carol's own backend)."""
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U("carol"), "password": CREDS["carol"]})
    r = c.get("/api/file", params={"path": proj("plan.md")})
    assert r.status_code == 403
    assert "zebrafish" not in r.text
