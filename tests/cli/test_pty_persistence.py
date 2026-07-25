"""Persistent pty sessions, at the protocol level: the shell OUTLIVES the
websocket. A reconnect replays only the bytes the client missed (or resets and
replays the buffer when it can't bridge the gap), a takeover tells the old
client it was superseded, keepalive pings never reach the shell, and a real
shell exit is announced explicitly — a bare close means "connection lost",
never "shell died"."""
import asyncio
import json
import os
import time

import aiohttp
import httpx

BASE = "http://127.0.0.1:8300"
CREDS = json.load(open("/tmp/kb-test-creds.json"))


def cl(user):
    c = httpx.Client(base_url=BASE, timeout=25)
    r = c.post("/login", data={"username": user, "password": CREDS[user]})
    assert r.status_code == 200, r.text
    return c


async def collect(ws, want=None, timeout=10.0, quiet=None):
    """Receive frames until `want` appears in the binary stream (or, with
    quiet=<s>, until the stream stays silent that long). Returns (texts, blob);
    ends early on close."""
    texts, blob = [], b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if want is not None and want in blob:
            break
        try:
            per_recv = quiet if quiet is not None else deadline - time.monotonic()
            msg = await asyncio.wait_for(ws.receive(), max(0.05, per_recv))
        except asyncio.TimeoutError:
            if quiet is not None:
                break
            continue
        if msg.type == aiohttp.WSMsgType.TEXT:
            texts.append(json.loads(msg.data))
        elif msg.type == aiohttp.WSMsgType.BINARY:
            blob += msg.data
        else:
            break
    return texts, blob


def pty_url(sid, have=0):
    return f"{BASE}/pty?session={sid}&have={have}"


def run(coro):
    return asyncio.run(coro)


def test_reconnect_replays_only_missed_bytes():
    """Detach, reattach with have=<received>: same shell, no reset, and the
    already-seen output is NOT resent."""
    sid = os.urandom(8).hex()
    cookies = dict(cl("bob").cookies)

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            async with s.ws_connect(pty_url(sid)) as ws:
                texts, blob = await collect(ws, want=b"$")
                assert texts and texts[0].get("reset") is True, texts
                await ws.send_bytes(b"PM=alive_77; echo born_$PM\n")
                _, more = await collect(ws, want=b"born_alive_77")
                blob += more
                _, tail = await collect(ws, quiet=0.6)   # drain the next prompt
                blob += tail
                have = len(blob)
            # plain close above = detach; the shell keeps running
            async with s.ws_connect(pty_url(sid, have)) as ws:
                await ws.send_bytes(b"echo again_$PM\n")
                texts, blob2 = await collect(ws, want=b"again_alive_77")
                assert b"again_alive_77" in blob2, blob2[-200:]
                assert not any(t.get("reset") for t in texts), \
                    f"a covered gap must not reset: {texts}"
                assert b"born_alive_77" not in blob2, "old output was replayed again"
                await ws.send_str(json.dumps({"kill": True}))

    run(go())


def test_fresh_client_gets_reset_and_full_replay():
    """A client with nothing (have=0, e.g. after a page reload) is told to
    start clean and receives the buffered scrollback — without retyping."""
    sid = os.urandom(8).hex()
    cookies = dict(cl("bob").cookies)

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            async with s.ws_connect(pty_url(sid)) as ws:
                await collect(ws, want=b"$")
                await ws.send_bytes(b"echo replay_me_99\n")
                await collect(ws, want=b"replay_me_99")
            async with s.ws_connect(pty_url(sid)) as ws:
                texts, blob = await collect(ws, want=b"replay_me_99")
                assert texts and texts[0].get("reset") is True, texts
                assert b"replay_me_99" in blob, "scrollback was not replayed"
                await ws.send_str(json.dumps({"kill": True}))

    run(go())


def test_takeover_notifies_the_old_client():
    """Attaching elsewhere must TELL the first client it was superseded — so
    it retires instead of fighting to reconnect (two windows, one shell)."""
    sid = os.urandom(8).hex()
    cookies = dict(cl("bob").cookies)

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            ws1 = await s.ws_connect(pty_url(sid))
            await collect(ws1, want=b"$")
            ws2 = await s.ws_connect(pty_url(sid))
            texts1, _ = await collect(ws1, quiet=1.5)
            assert any(t.get("detached") for t in texts1), \
                f"old client was not told about the takeover: {texts1}"
            texts2, blob2 = await collect(ws2, want=b"$")
            assert texts2 and texts2[0].get("reset") is True, texts2
            await ws2.send_bytes(b"echo owner_two\n")
            _, out = await collect(ws2, want=b"owner_two")
            assert b"owner_two" in out
            await ws2.send_str(json.dumps({"kill": True}))
            await ws1.close()
            await ws2.close()

    run(go())


def test_keepalive_ping_never_reaches_the_shell():
    sid = os.urandom(8).hex()
    cookies = dict(cl("bob").cookies)

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            async with s.ws_connect(pty_url(sid)) as ws:
                _, blob = await collect(ws, want=b"$")
                await ws.send_str('{"ping":1}')
                await ws.send_bytes(b"echo after_ping_ok\n")
                _, out = await collect(ws, want=b"after_ping_ok")
                assert b"after_ping_ok" in out
                assert b'{"ping"' not in out, "the keepalive was typed into the shell"
                await ws.send_str(json.dumps({"kill": True}))

    run(go())


def test_reattach_to_a_dead_session_reports_gone_not_a_new_shell():
    """create=0 is the reconnect contract: if the session ended while we were
    away, say so. Silently forking a fresh shell under the same tab would hide
    a dead shell behind a live-looking terminal."""
    sid = os.urandom(8).hex()
    cookies = dict(cl("bob").cookies)

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            async with s.ws_connect(pty_url(sid)) as ws:
                await collect(ws, want=b"$")
                await ws.send_str(json.dumps({"kill": True}))
                await collect(ws, timeout=5.0)
            async with s.ws_connect(pty_url(sid) + "&create=0") as ws:
                texts, blob = await collect(ws, timeout=6.0)
                assert any(t.get("gone") for t in texts), texts
                assert not blob, "an attach-only probe must not start a shell"

    run(go())


def test_kill_while_disconnected_never_forks_a_shell():
    """Killing a tab whose socket is down reaches the session with create=0 —
    it must not conjure a shell just to kill it (which at the cap would evict
    someone else's detached session)."""
    sid = os.urandom(8).hex()
    cookies = dict(cl("bob").cookies)

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            # the client's kill-while-disconnected path, against a stale sid
            async with s.ws_connect(pty_url(sid) + "&create=0") as ws:
                await ws.send_str(json.dumps({"kill": True}))
                texts, blob = await collect(ws, timeout=6.0)
                assert any(t.get("gone") for t in texts), texts
                assert not blob
            # and the session still does not exist afterwards
            async with s.ws_connect(pty_url(sid) + "&create=0") as ws:
                texts, _ = await collect(ws, timeout=6.0)
                assert any(t.get("gone") for t in texts), texts

    run(go())


def test_reset_replay_when_the_gap_outgrew_the_ring():
    """A valid offset whose gap exceeds the 256KB ring can't be bridged — the
    client must be told to reset rather than handed a torn stream."""
    sid = os.urandom(8).hex()
    cookies = dict(cl("bob").cookies)

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            async with s.ws_connect(pty_url(sid)) as ws:
                _, blob = await collect(ws, want=b"$")
                have = len(blob)
                # print far more than PTY_BUF_MAX (~590KB) so the ring wraps.
                # The sentinel is split in the SOURCE so the shell's echo of the
                # command line can't satisfy the wait before the flood has run.
                await ws.send_bytes(b"seq 1 100000; echo FLOOD''DONE\n")
                await collect(ws, want=b"FLOODDONE", timeout=30.0)
            # reattach claiming an offset from BEFORE the flood
            async with s.ws_connect(pty_url(sid, have)) as ws:
                texts, _ = await collect(ws, want=b"FLOODDONE", timeout=15.0)
                assert texts and texts[0].get("reset") is True, \
                    f"an unbridgeable gap must reset: {texts[:2]}"
                assert texts[0].get("base", 0) > 0, texts[0]
                await ws.send_str(json.dumps({"kill": True}))

    run(go())


def test_cap_refuses_a_new_session_without_evicting_attached_ones():
    """At the cap, attached terminals are never sacrificed: the 13th attach is
    refused with an error frame instead."""
    cookies = dict(cl("carol").cookies)      # own backend, own session budget

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            held = []
            try:
                for _ in range(12):
                    ws = await s.ws_connect(pty_url(os.urandom(8).hex()))
                    await collect(ws, want=b"$", timeout=10.0)
                    held.append(ws)
                extra = await s.ws_connect(pty_url(os.urandom(8).hex()))
                texts, _ = await collect(extra, timeout=6.0)
                assert any("too many" in str(t.get("error", "")) for t in texts), texts
                await extra.close()
                # every attached shell is still alive and responsive
                await held[0].send_bytes(b"echo still_here_1\n")
                _, out = await collect(held[0], want=b"still_here_1", timeout=8.0)
                assert b"still_here_1" in out, "an attached session was evicted"
            finally:
                for ws in held:
                    try:
                        await ws.send_str(json.dumps({"kill": True}))
                        await ws.close()
                    except Exception:
                        pass

    run(go())


def test_shell_exit_is_announced_explicitly():
    """`exit` must produce an {"exit": true} frame before the close — that is
    what lets the client distinguish a dead shell from a dropped connection."""
    sid = os.urandom(8).hex()
    cookies = dict(cl("bob").cookies)

    async def go():
        async with aiohttp.ClientSession(cookies=cookies) as s:
            async with s.ws_connect(pty_url(sid)) as ws:
                await collect(ws, want=b"$")
                await ws.send_bytes(b"exit\n")
                texts, _ = await collect(ws, timeout=12.0)
                assert any(t.get("exit") for t in texts), \
                    f"shell exit was not announced: {texts}"

    run(go())
