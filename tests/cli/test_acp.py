"""The agent chat bridge, without an agent: a fake ACP agent (a Python script
speaking newline-delimited JSON-RPC) stands in for Claude Code, and the
bridge's rules are checked directly — the catalogue, the person's files, the
process multiplexing, the permission relay, the reconnect replay. Pure unit
tests: no seeding, no hub."""
import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from kb_platform import acp, settings  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("KB_TEST_NO_SEED") not in (None, "1", "0", ""), reason="unit")

FAKE_AGENT = textwrap.dedent(r'''
    import json, sys, time
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
    req_id = 100
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        m = json.loads(line)
        mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1,
                  "agentCapabilities": {"loadSession": True}, "authMethods": [], "agentInfo": {"name": "fake", "version": "0"}}})
            send({"jsonrpc": "2.0", "method": "_auth/status_update", "params": {"authStatus": {"kind": "account", "label": "Fake Plan"}}})
        elif method == "session/new":
            assert params["cwd"].startswith("/"), params
            assert isinstance(params["mcpServers"], list)
            send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "sess-1", "modes": {"currentModeId": "ask", "availableModes": [{"id": "ask", "name": "Ask"}]}}})
        elif method == "session/prompt":
            sid = params["sessionId"]
            text = params["prompt"][0]["text"]
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "echo: " + text}}}})
            if "permission" in text:
                req_id += 1
                send({"jsonrpc": "2.0", "id": req_id, "method": "session/request_permission", "params": {"sessionId": sid,
                      "toolCall": {"toolCallId": "call_1", "title": "rm -rf /", "kind": "execute", "status": "pending"},
                      "options": [{"optionId": "allow-once", "name": "Allow", "kind": "allow_once"}, {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}})
                ans = json.loads(sys.stdin.readline())
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {"sessionUpdate": "tool_call", "toolCallId": "call_1", "title": "rm -rf /", "status": "completed" if ans.get("result", {}).get("outcome", {}).get("optionId") == "allow-once" else "failed"}}})
            if "unknown" in text:
                req_id += 1
                send({"jsonrpc": "2.0", "id": req_id, "method": "fs/read_text_file", "params": {"sessionId": sid, "path": "/etc/passwd"}})
                ans = json.loads(sys.stdin.readline())
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "fs said " + str(ans.get("error", {}).get("code"))}}}})
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            pass
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})
''')


class FakeWS:
    """Enough of an aiohttp WebSocketResponse for Conn.send."""
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_str(self, s):
        if self.closed:
            raise ConnectionResetError()
        self.sent.append(json.loads(s))

    def by(self, kb=None, **match):
        return [m for m in self.sent if (kb is None or m.get("kb") == kb) and all(m.get(k) == v for k, v in match.items())]

    async def wait_for(self, pred, timeout=10):
        t0 = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - t0 < timeout:
            for m in self.sent:
                if pred(m):
                    return m
            await asyncio.sleep(0.02)
        raise AssertionError("nothing matched; got " + json.dumps(self.sent)[-2000:])


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    script = tmp_path / "fake-agent.py"
    script.write_text(FAKE_AGENT)
    monkeypatch.setattr(acp, "BIN_DIR", tmp_path / "bin")
    (tmp_path / "bin").mkdir()
    exe = tmp_path / "bin" / "fake-agent"
    exe.write_text("#!/bin/sh\nexec " + sys.executable + " " + str(script) + "\n")
    exe.chmod(0o755)
    agent = {"id": "fake", "name": "Fake", "vendor": "Tests", "bin": "fake-agent", "args": [], "env": {},
             "npm": [], "keys": ["FAKE_KEY"], "cred": None, "login": {"how": "key", "note": ""}}
    monkeypatch.setitem(acp.BY_ID, "fake", agent)
    # the person's files go to a scratch home
    monkeypatch.setattr(acp, "read_keys", lambda: {})
    remembered = []
    monkeypatch.setattr(acp, "remember_chat", lambda sid, a, cwd, title=None: remembered.append((sid, a, title)))
    agent["_remembered"] = remembered
    return agent


def test_catalogue_is_consistent_with_the_setting():
    entry = settings.BY_KEY["ai.agent"]
    assert entry["options"] == acp.AGENT_IDS, "settings.py lists the catalogue by hand: keep them in step"
    assert set(entry["labels"]) == set(acp.AGENT_IDS)
    for a in acp.CATALOGUE:
        assert a["login"]["how"] in ("terminal", "key")
        if a["login"]["how"] == "terminal":
            assert a["login"]["cmd"], a["id"]
            assert a["bin"] in acp.login_command(a)
            assert "PATH=" in acp.login_command(a)


def test_status_reports_installed_and_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "BIN_DIR", tmp_path / "nowhere")
    monkeypatch.setattr(acp, "read_keys", lambda: {"grok": "xai-1"})
    st = {s["id"]: s for s in (acp.agent_status(a) for a in acp.CATALOGUE)}
    assert st["grok"]["hasKey"] is True and st["claude"]["hasKey"] is False
    assert st["deepseek"]["installed"] is False or acp.bin_path(acp.BY_ID["deepseek"])


def test_a_turn_streams_updates_and_the_log_replays(fake_agent):
    async def run():
        proc = acp.AgentProc(fake_agent, "/tmp")
        await proc.start()
        assert proc.init_error is None, proc.init_error
        assert proc.init["protocolVersion"] == 1
        ws = FakeWS(); conn = acp.Conn(ws)
        proc.conns.add(conn)
        await proc.forward_request(conn, {"jsonrpc": "2.0", "id": "a1", "method": "session/new", "params": {}})
        r = await ws.wait_for(lambda m: m.get("id") == "a1")
        sid = r["result"]["sessionId"]
        assert sid == "sess-1"
        assert proc.owner[sid] is conn, "the tab that opened a session owns it"
        assert fake_agent["_remembered"] and fake_agent["_remembered"][0][0] == sid
        await proc.forward_request(conn, {"jsonrpc": "2.0", "id": "a2", "method": "session/prompt",
                                          "params": {"sessionId": sid, "prompt": [{"type": "text", "text": "hi"}]}})
        turn = await ws.wait_for(lambda m: m.get("kb") == "turn")
        assert turn["stopReason"] == "end_turn"
        ups = ws.by("u", sessionId=sid)
        assert any(u["m"]["params"]["update"]["content"]["text"] == "echo: hi" for u in ups)
        resp = await ws.wait_for(lambda m: m.get("id") == "a2")
        assert resp["result"]["stopReason"] == "end_turn"
        assert proc.auth and proc.auth["label"] == "Fake Plan"
        # a second tab attaches and gets the whole log back, in order
        ws2 = FakeWS(); conn2 = acp.Conn(ws2)
        proc.conns.add(conn2)
        await proc.attach(conn2, sid, 0)
        seqs = [m["seq"] for m in ws2.sent if "seq" in m and m.get("kb") in ("u", "turn")]
        assert seqs == sorted(seqs) and len(seqs) >= 2
        assert ws2.by("attached")[0]["running"] is False
        # and only from where it left off
        ws3 = FakeWS(); conn3 = acp.Conn(ws3)
        await proc.attach(conn3, sid, seqs[-1])
        assert [m["seq"] for m in ws3.sent if "seq" in m and m.get("kb") != "attached"] == [seqs[-1]]
        await proc.stop()
    asyncio.run(run())


def test_permission_requests_reach_the_owner_and_the_answer_goes_back(fake_agent):
    async def run():
        proc = acp.AgentProc(fake_agent, "/tmp")
        await proc.start()
        ws = FakeWS(); conn = acp.Conn(ws)
        proc.conns.add(conn)
        await proc.forward_request(conn, {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
        await ws.wait_for(lambda m: m.get("id") == 1)
        await proc.forward_request(conn, {"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                                          "params": {"sessionId": "sess-1", "prompt": [{"type": "text", "text": "needs permission"}]}})
        req = await ws.wait_for(lambda m: m.get("kb") == "req")
        assert req["m"]["method"] == "session/request_permission"
        assert req["sessionId"] == "sess-1", "the tab routes requests by session"
        assert proc.log_for("sess-1").turn_running is True
        await proc.answer_agent({"jsonrpc": "2.0", "id": req["m"]["id"],
                                 "result": {"outcome": {"outcome": "selected", "optionId": "allow-once"}}})
        await ws.wait_for(lambda m: m.get("kb") == "turn")
        done = ws.by("req-done")
        assert done and done[0]["id"] == req["m"]["id"]
        assert done[0]["optionId"] == "allow-once" and done[0]["label"] == "Allow" and done[0]["kind"] == "allow_once"
        assert proc.meta["sess-1"]["modes"]["currentModeId"] == "ask", "a reattaching tab gets the session's modes"
        tool = [u for u in ws.by("u") if u["m"]["params"]["update"].get("sessionUpdate") == "tool_call"]
        assert tool[-1]["m"]["params"]["update"]["status"] == "completed"
        await proc.stop()
    asyncio.run(run())


def test_fs_requests_are_refused_because_the_agent_does_its_own_io(fake_agent):
    async def run():
        proc = acp.AgentProc(fake_agent, "/tmp")
        await proc.start()
        ws = FakeWS(); conn = acp.Conn(ws)
        proc.conns.add(conn)
        await proc.forward_request(conn, {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
        await ws.wait_for(lambda m: m.get("id") == 1)
        await proc.forward_request(conn, {"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                                          "params": {"sessionId": "sess-1", "prompt": [{"type": "text", "text": "unknown method"}]}})
        await ws.wait_for(lambda m: m.get("kb") == "turn")
        texts = [u["m"]["params"]["update"]["content"]["text"] for u in ws.by("u")
                 if u["m"]["params"]["update"].get("sessionUpdate") == "agent_message_chunk"]
        assert "fs said -32601" in texts
        await proc.stop()
    asyncio.run(run())


def test_a_dead_tab_does_not_lose_the_turn_and_exit_is_reported(fake_agent):
    async def run():
        proc = acp.AgentProc(fake_agent, "/tmp")
        await proc.start()
        ws = FakeWS(); conn = acp.Conn(ws)
        proc.conns.add(conn)
        await proc.forward_request(conn, {"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
        await ws.wait_for(lambda m: m.get("id") == 1)
        ws.closed = True                       # the tab went away mid-flight
        proc.detach_conn(conn)
        await proc.forward_request(acp.Conn(FakeWS()), {"jsonrpc": "2.0", "id": 9, "method": "session/prompt",
                                                        "params": {"sessionId": "sess-1", "prompt": [{"type": "text", "text": "late"}]}})
        for _ in range(200):
            if proc.log_for("sess-1").entries and any(e[1].get("kb") == "turn" for e in proc.log_for("sess-1").entries):
                break
            await asyncio.sleep(0.02)
        assert any(e[1].get("kb") == "turn" for e in proc.log_for("sess-1").entries), "the turn completed into the log"
        ws2 = FakeWS(); conn2 = acp.Conn(ws2)
        proc.conns.add(conn2)
        await proc.stop()
        for _ in range(100):
            if ws2.by("exit"):
                break
            await asyncio.sleep(0.02)
        assert ws2.by("exit"), "every attached tab hears the agent exit"
    asyncio.run(run())


def test_missing_binary_is_a_clean_error(tmp_path, monkeypatch):
    monkeypatch.setattr(acp, "BIN_DIR", tmp_path / "none")
    monkeypatch.setattr(acp.shutil, "which", lambda _b, path=None: None)
    async def run():
        proc = acp.AgentProc(acp.BY_ID["grok"], "/tmp")
        await proc.start()
        assert "not installed" in (proc.init_error or "")
        assert not proc.alive()
    asyncio.run(run())


def test_login_command_prefers_the_agents_own_terminal_method():
    a = acp.BY_ID["claude"]
    static = acp.login_command(a)
    assert "claude-agent-acp --cli auth login --claudeai" in static
    assert static.startswith("PATH=") and "NO_BROWSER=1" in static
    assert str(acp.BIN_DIR) in static
    assert acp.agent_path().split(":")[-1:] != [""], "the system PATH is kept at the end"
    live = acp.login_command(a, {"id": "claude-login", "type": "terminal", "args": ["--cli"], "env": {"X": "a b"}})
    assert live.endswith("claude-agent-acp --cli") and "X='a b'" in live
    assert acp.terminal_auth_method({"authMethods": [{"id": "k"}, {"id": "t", "type": "terminal", "args": []}]})["id"] == "t"
    assert acp.terminal_auth_method(None) is None


def test_cancel_result_shapes():
    assert acp.cancel_result("session/request_permission") == {"outcome": {"outcome": "cancelled"}}
    assert acp.cancel_result("elicitation/create") == {"action": "cancel"}


def test_an_agent_installed_in_your_own_home_counts(tmp_path, monkeypatch):
    """Hermes is a Python agent each person installs in ~/.hermes with its
    launcher in ~/.local/bin — not an npm package an admin installs for
    everyone. It must be found there, and never npm-installed."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    exe = home / ".local" / "bin" / "hermes"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setattr(acp, "BIN_DIR", tmp_path / "none")
    monkeypatch.setattr(acp, "_home", lambda: home)
    monkeypatch.setattr(acp, "read_keys", lambda: {})
    hermes = acp.BY_ID["hermes"]
    assert acp.bin_path(hermes) == str(exe)
    st = acp.agent_status(hermes)
    assert st["installed"] is True and st["npm"] is False and st["install"]["how"] == "terminal"
    assert str(home / ".local" / "bin") in acp.agent_path()
    # its sign-in is a terminal command, like the others
    assert "hermes" in acp.login_command(hermes)


def test_any_of_an_agents_credential_files_counts(tmp_path, monkeypatch):
    """A CLI renames its credential file between versions, so the catalogue
    lists every name it has used and any one of them means signed in."""
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    monkeypatch.setattr(acp, "_home", lambda: home)
    monkeypatch.setattr(acp, "read_keys", lambda: {})
    gemini = acp.BY_ID["gemini"]
    assert isinstance(gemini["cred"], list) and len(gemini["cred"]) > 1
    assert acp.agent_status(gemini)["hasCred"] is False
    # the name the current CLI writes
    (home / ".gemini" / "gemini-credentials.json").write_text("{}")
    assert acp.agent_status(gemini)["hasCred"] is True
    # …and the one older builds wrote
    (home / ".gemini" / "gemini-credentials.json").unlink()
    (home / ".gemini" / "oauth_creds.json").write_text("{}")
    assert acp.agent_status(gemini)["hasCred"] is True
    for a in acp.CATALOGUE:
        assert a["cred"] is None or isinstance(a["cred"], list), a["id"]


def test_a_session_starts_in_a_real_folder_or_the_root(tmp_path):
    """The chat may pick the folder its agent stands in (＋ → Work in a
    folder…). Anything that is not an absolute path to a directory falls back
    to the knowledgebase root, so a stale or mistyped folder is a session in
    the usual place rather than an agent that will not start."""
    proc = acp.AgentProc(acp.BY_ID["echo"], "/srv/kb")
    real = tmp_path / "plans"
    real.mkdir()
    fix = proc._fix_params
    assert fix("session/new", {"cwd": str(real)})["cwd"] == str(real)
    assert fix("session/load", {"cwd": str(real)})["cwd"] == str(real)
    for bad in ({"cwd": "company/plans"}, {"cwd": str(tmp_path / "gone")},
                {"cwd": str(real / "notes.md")}, {"cwd": 7}, {}):
        assert fix("session/new", bad)["cwd"] == "/srv/kb"
    # …and the folder never touches a method that has no business with it
    assert fix("session/prompt", {"cwd": "/nope"}) == {"cwd": "/nope"}
