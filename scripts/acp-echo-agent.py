#!/usr/bin/env python3
"""A tiny ACP agent for tests and demos: it echoes, streams a thought, runs
one fake tool call (asking permission when the prompt says "permission"),
publishes a plan, and knows two slash commands. Newline-delimited JSON-RPC on
stdio, protocol version 1 — the same wire as the real agents, none of the
cost. Hidden from the picker unless the page asks for it."""
import json
import os
import sys
import time

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

# Sessions outlive the process, as a real agent's do: a restarted agent
# answers session/load from this file (the chat relies on that to bring a
# conversation back once the process that held it is gone).
STORE = os.path.join(os.path.expanduser("~"), ".cache", "acp-echo", "sessions.json")
def _load():
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
def _save():
    try:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        tmp = STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(SESSIONS, f)
        os.replace(tmp, STORE)
    except OSError:
        pass
SESSIONS = _load()
CWD: dict[str, str] = {}      # sessionId -> the folder session/new asked for
next_req = 1000

def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": upd}})

def main():
    global next_req
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue
        mid, method, params = m.get("id"), m.get("method"), m.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True, "promptCapabilities": {"image": True}},
                "authMethods": [], "agentInfo": {"name": "echo", "title": "Echo", "version": "1"}}})
            send({"jsonrpc": "2.0", "method": "_auth/status_update",
                  "params": {"authStatus": {"kind": "external", "label": "no account needed"}}})
        elif method == "session/new":
            sid = "echo-" + str(int(time.time() * 1000))
            SESSIONS[sid] = []
            CWD[sid] = params.get("cwd") or ""      # a real agent would chdir; we report it
            _save()
            send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid, "modes": {
                "currentModeId": "ask", "availableModes": [
                    {"id": "ask", "name": "Ask first", "description": "Asks before the fake tool runs"},
                    {"id": "auto", "name": "Just do it", "description": "Never asks"}]}}})
            update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": [
                {"name": "help", "description": "What this agent can do"},
                {"name": "plan", "description": "Show a sample plan", "input": {"hint": "topic"}}]})
        elif method == "session/load":
            sid = params.get("sessionId")
            if sid not in SESSIONS:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown session " + str(sid)}})
                continue
            for entry in SESSIONS.get(sid, []):
                update(sid, entry)
            send({"jsonrpc": "2.0", "id": mid, "result": {"modes": {
                "currentModeId": "ask", "availableModes": [
                    {"id": "ask", "name": "Ask first", "description": "Asks before the fake tool runs"},
                    {"id": "auto", "name": "Just do it", "description": "Never asks"}]}}})
        elif method == "session/set_mode":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "session/prompt":
            sid = params.get("sessionId")
            text = " ".join(p.get("text", "") for p in params.get("prompt", []) if p.get("type") == "text")
            # the context the client sent with the prompt, and where we stand
            links = [p.get("title") or p.get("name") or p.get("uri")
                     for p in params.get("prompt", []) if p.get("type") == "resource_link"]
            if links:
                text += " [context: " + ", ".join(str(x) for x in links) + "]"
            if "where" in text:
                text += " [cwd: " + str(CWD.get(sid, "")) + "]"
            hist = SESSIONS.setdefault(sid, [])
            def emit(upd):
                hist.append(upd); update(sid, upd)
            if "glacial" in text:
                time.sleep(7)            # longer than the client's 5 s stop fallback
            elif "slow" in text:
                time.sleep(2.5)          # a turn long enough to type into (queueing)
            emit({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}})
            emit({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "The person said: " + text[:60]}})
            if text.startswith("/plan"):
                emit({"sessionUpdate": "plan", "entries": [
                    {"content": "Read the request", "priority": "high", "status": "completed"},
                    {"content": "Do the thing", "priority": "medium", "status": "in_progress"},
                    {"content": "Tidy up", "priority": "low", "status": "pending"}]})
            if "permission" in text or "tool" in text:
                emit({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "echo " + text[:20], "kind": "execute", "status": "pending",
                      "rawInput": {"command": "echo " + text}})
                allowed = True
                if "permission" in text:
                    next_req += 1
                    send({"jsonrpc": "2.0", "id": next_req, "method": "session/request_permission", "params": {
                        "sessionId": sid, "toolCall": {"toolCallId": "t1"},
                        "options": [{"optionId": "yes", "name": "Allow once", "kind": "allow_once"},
                                    {"optionId": "no", "name": "Reject", "kind": "reject_once"}]}})
                    ans = json.loads(sys.stdin.readline() or "{}")
                    out = (ans.get("result") or {}).get("outcome") or {}
                    allowed = out.get("optionId") == "yes"
                emit({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed" if allowed else "failed",
                      "content": [{"type": "content", "content": {"type": "text", "text": text if allowed else "not allowed"}}]})
            if "edit" in text:
                emit({"sessionUpdate": "tool_call", "toolCallId": "t2", "title": "Edit notes.md", "kind": "edit", "status": "completed",
                      "content": [{"type": "diff", "path": "/srv/kb/company/notes.md", "oldText": "a\nb\nc\n", "newText": "a\nB\nc\nd\n"}]})
            reply = "You said: **" + text + "**\n\n```python\nprint(" + json.dumps(text) + ")\n```\n" if not text.startswith("/help") else "I echo. Try `permission`, `tool`, `edit`, `/plan`."
            for i in range(0, len(reply), 24):
                emit({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply[i:i + 24]}})
                time.sleep(0.01)
            _save()
            send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            pass
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})

if __name__ == "__main__":
    main()
