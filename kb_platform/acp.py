"""Agent chat: the Agent Client Protocol (ACP) bridge.

An ACP agent — Claude Code, Codex, Gemini CLI, and the rest of the registry
at agentclientprotocol.com — is a subprocess that speaks JSON-RPC 2.0 over
its stdio, one JSON object per line. The person's own backend (this module
runs inside user_server, as the person) is the ACP *client*: it spawns the
agent as them, in the knowledgebase, so every file the agent reads or writes
is read or written with their permissions and nothing else; and it bridges
the wire to the browser over a WebSocket.

One agent process per (person, agent id), shared by every session of that
agent and every browser tab, and kept alive across page reloads: a running
turn survives a refresh the way a shell in the terminal panel does. The
process is spawned on the first connection, `initialize`d once by this
module, and reaped after an idle half hour.

The WebSocket (/acp?agent=<id>) carries two kinds of text frames:

  browser → backend
    {"kb":"attach","sessionId":S,"have":N}  this tab owns S from now on
                                             (permission requests land here);
                                             replay S's log from seq N
    {"kb":"detach","sessionId":S}
    a JSON-RPC request  {id, method, params} — forwarded to the agent; the
                        id is re-mapped so two tabs cannot collide, and the
                        response comes back carrying the tab's own id
    a JSON-RPC notification {method, params} — forwarded (session/cancel)
    a JSON-RPC response {id, result|error} — the answer to an agent→client
                        request (permission, elicitation), mapped back

  backend → browser
    {"kb":"hello","agent":{…},"init":{…},"sessions":[…]}   the agent is up
    {"kb":"error","message":"…"}                            it is not
    {"kb":"u","sessionId":S,"seq":N,"m":{…}}   a relayed session/update
                        (or any other agent notification) — logged per
                        session so a reconnecting tab can catch up
    {"kb":"turn","sessionId":S,"seq":N,"stopReason":"…"}   a prompt ended
    {"kb":"req","m":{…}}   an agent→client request for the owning tab
    {"kb":"reset","sessionId":S,"base":N}   the log no longer reaches back
                        to what you had — use session/load
    {"kb":"exit","code":N}    the agent process died
    {"kb":"stderr","line":"…"}   its stderr, for the details panel
    a JSON-RPC response {id, result|error}   to one of this tab's requests

Capabilities this client advertises: elicitation (form and url — a web UI
renders both), boolean config options, and auth over a terminal (the agent's
login command runs in a terminal tab; this is what makes signing in work on a
box nobody has a browser on). Not advertised: fs and terminal — the agent
runs as the person and does its own file and shell work, streaming results
as tool-call content, which is exactly what we want to show.

Stdlib + aiohttp only; the hub imports the catalogue too.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import re
import shutil
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import common

log = logging.getLogger("kb.acp")

PROTOCOL_VERSION = 1
AGENTS_PREFIX = Path(os.environ.get("KB_AGENTS_PREFIX", "/opt/kb-agents"))
BIN_DIR = AGENTS_PREFIX / "node_modules" / ".bin"
NODE_BIN = AGENTS_PREFIX / "node" / "bin"     # the private Node 22 (scripts/install-agents.sh)


def agent_path() -> str:
    """PATH for an agent process and its login: our Node first (Ubuntu's is
    18, the Claude adapter wants 22), then the adapters an admin installed
    for everyone, then the person's own ~/.local/bin (Hermes and anything
    else installed per person lives there), then the system's."""
    parts = [str(NODE_BIN)] if NODE_BIN.is_dir() else []
    parts.append(str(BIN_DIR))
    try:
        parts.append(str(_home() / ".local" / "bin"))
    except Exception:   # noqa: BLE001 — a nameless uid; the system path still applies
        pass
    parts.append(os.environ.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return os.pathsep.join(parts)
IDLE_SECONDS = 30 * 60
# A spare session, made before anyone asks for one. session/new costs seconds
# inside the agent and — unlike the process itself — does not get cheaper on a
# warm one, so it is the part worth doing in advance. A spare freezes what the
# agent reads when a session is born (CLAUDE.md, the skills, today's date), so
# it is short-lived on purpose; KB_SPARE_SESSIONS=0 turns the whole thing off.
SPARE_TTL = 4 * 60
SPARE_CWDS = 3                       # how many folders to keep a spare for
SPARE_MAX = 50                       # per process lifetime, so a loop cannot pile them up
SPARES_ON = os.environ.get("KB_SPARE_SESSIONS", "1") not in ("0", "no", "off", "false")
STREAM_LIMIT = 64 * 1024 * 1024      # one line can carry a base64 image or a whole replayed history
LOG_ENTRIES = 4000                   # per session, for replay after a reconnect
LOG_BYTES = 8 * 1024 * 1024
STDERR_LINES = 200
PERMISSION_WAIT = 20 * 60            # a permission nobody is there to answer
CHATS_FILE = "chats.json"
KEYS_FILE = "agent-keys.json"
MAX_CHATS = 300
_SID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# The catalogue: what can be installed, how it starts in ACP mode, how a
# person signs in on a headless box. `login.how`:
#   terminal — run `cmd` in a terminal tab (a URL is printed; sign in on any
#              device and paste the code back where it says);
#   key      — paste an API key in the dialog; it is kept in the person's
#              own .os/agent-keys.json (0600) and handed to the agent's env.
# Both may apply; `keys` lists the env variables a key can be given as.
CATALOGUE: list[dict] = [
    {"id": "claude", "name": "Claude Code", "vendor": "Anthropic",
     "bin": "claude-agent-acp", "args": [], "env": {"NO_BROWSER": "1", "CLAUDE_CODE_REMOTE": "1"},
     "npm": ["@agentclientprotocol/claude-agent-acp@0.81.2"],
     "keys": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
     "cred": ["~/.claude/.credentials.json", "~/.claude/credentials.json"],
     "login": {"how": "terminal", "cmd": ["claude-agent-acp", "--cli", "auth", "login", "--claudeai"],
               "alt": ["claude-agent-acp", "--cli", "auth", "login", "--console"],
               "note": "A sign-in link is printed. Open it on any device; if the browser shows a "
                       "code instead of coming back, paste it where the terminal asks."}},
    {"id": "codex", "name": "Codex", "vendor": "OpenAI",
     "bin": "codex-acp", "args": [], "env": {"NO_BROWSER": "1"},
     "npm": ["@agentclientprotocol/codex-acp@1.12.0"],
     "keys": ["OPENAI_API_KEY", "CODEX_API_KEY"], "cred": ["~/.codex/auth.json"],
     "login": {"how": "terminal", "cmd": ["codex-acp", "cli", "login", "--device-auth"],
               "note": "Device sign-in: open the link on any device and enter the code shown. "
                       "A ChatGPT workspace admin must have enabled device code login."}},
    {"id": "gemini", "name": "Gemini CLI", "vendor": "Google",
     "bin": "gemini", "args": ["--acp"], "env": {"NO_BROWSER": "true"},
     "npm": ["@google/gemini-cli@0.60.0"],
     "keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
     "cred": ["~/.gemini/gemini-credentials.json", "~/.gemini/oauth_creds.json", "~/.gemini/google_accounts.json"],
     "login": {"how": "terminal", "cmd": ["gemini"], "env": {"NO_BROWSER": "true"},
               "note": "Choose “Login with Google”, open the printed link on any device and paste "
                       "the authorization code back within five minutes."}},
    {"id": "copilot", "name": "Copilot CLI", "vendor": "GitHub",
     "bin": "copilot", "args": ["--acp"], "env": {},
     "npm": ["@github/copilot@1.0.86"],
     "keys": ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"],
     "cred": ["~/.copilot/config.json", "~/.copilot/apps.json", "~/.config/github-copilot/apps.json"],
     "login": {"how": "terminal", "cmd": ["copilot", "login"],
               "note": "Device sign-in: open the link on any device and enter the code."}},
    {"id": "grok", "name": "Grok Build", "vendor": "xAI",
     "bin": "grok", "args": ["agent", "stdio"], "env": {},
     "npm": ["@xai-official/grok@1.0.38"],
     "keys": ["XAI_API_KEY"], "cred": ["~/.grok/auth.json", "~/.grok/credentials.json"],
     "login": {"how": "terminal", "cmd": ["grok", "login"],
               "note": "Sign in with the printed link, or paste an xAI API key instead."}},
    {"id": "qwen", "name": "Qwen Code", "vendor": "Alibaba",
     "bin": "qwen", "args": ["--acp", "--experimental-skills"], "env": {},
     "npm": ["@qwen-code/qwen-code@0.24.2"],
     "keys": ["OPENAI_API_KEY", "BAILIAN_CODING_PLAN_API_KEY"],
     "cred": ["~/.qwen/.env", "~/.qwen/oauth_creds.json"],
     "login": {"how": "key", "note": "Qwen Code takes an OpenAI-compatible key: paste it here, and "
                                     "set OPENAI_BASE_URL and OPENAI_MODEL in ~/.qwen/.env."}},
    {"id": "opencode", "name": "OpenCode", "vendor": "SST",
     "bin": "opencode", "args": ["acp"], "env": {},
     "npm": ["opencode-ai@1.18.31"],
     "keys": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
     "cred": ["~/.local/share/opencode/auth.json", "~/.config/opencode/auth.json"],
     "login": {"how": "terminal", "cmd": ["opencode", "auth", "login"],
               "note": "Pick a provider and paste its key when asked."}},
    # a test double: hidden from the picker, used by the browser tests
    {"id": "echo", "name": "Echo (test agent)", "vendor": "Company OS", "hidden": True,
     "bin": "acp-echo-agent.py", "path": str(Path(__file__).resolve().parents[1] / "scripts" / "acp-echo-agent.py"),
     "args": [], "env": {}, "npm": [], "keys": [], "cred": None,
     "login": {"how": "key", "note": "Nothing to sign in to."}},
    # Hermes is a Python agent a person installs in their own home
    # (~/.hermes), not an npm package an admin installs for everyone — so it
    # is found on the person's PATH and installed from a terminal tab.
    {"id": "hermes", "name": "Hermes", "vendor": "Nous Research",
     "bin": "hermes", "args": ["acp"], "env": {"NO_BROWSER": "1"},
     "npm": [],
     "install": {"how": "terminal", "cmd": ["hermes", "acp", "--check"],
                 "note": "Hermes installs into your own home. Install it as its site says, then "
                         "add the ACP adapter: cd ~/.hermes/hermes-agent && uv pip install -e '.[acp]'. "
                         "`hermes acp --check` says whether this app can use it."},
     "keys": ["OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
     "cred": ["~/.hermes/auth.json", "~/.hermes/.env"],
     "login": {"how": "terminal", "cmd": ["hermes", "acp", "--setup"],
               "note": "Pick the provider and model Hermes should use; it keeps them in ~/.hermes, "
                       "the same configuration its own CLI uses."}},
    {"id": "deepseek", "name": "DeepSeek Harness", "vendor": "DeepSeek",
     "bin": "dsh-acp-server", "args": [], "env": {},
     "npm": ["@deepseek-ai/dsh", "dsh-acp-server@0.12.0"],
     "keys": ["DEEPSEEK_API_KEY"], "cred": None,
     "login": {"how": "key", "note": "Paste a DeepSeek API key."}},
]
BY_ID = {a["id"]: a for a in CATALOGUE}
AGENT_IDS = [a["id"] for a in CATALOGUE if not a.get("hidden")]


def bin_path(agent: dict) -> str | None:
    """The agent's executable: its own path, else the shared prefix, else PATH."""
    if agent.get("path"):
        return agent["path"] if os.access(agent["path"], os.X_OK) else None
    p = BIN_DIR / agent["bin"]
    if p.is_file() and os.access(p, os.X_OK):
        return str(p)
    # the same PATH the agent itself would get, so an agent in the person's
    # own home counts as installed for them
    return shutil.which(agent["bin"], path=agent_path())


def _home() -> Path:
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        return Path(os.path.expanduser("~"))


# ---- the person's files: chat index, API keys -------------------------------
def _read_user_json(name: str, default):
    try:
        p = common.user_config(common.ME if hasattr(common, "ME") else pwd.getpwuid(os.geteuid()).pw_name, name)
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _me() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def read_keys() -> dict:
    d = _read_user_json(KEYS_FILE, {})
    return d if isinstance(d, dict) else {}


def write_keys(d: dict) -> None:
    common.write_user_config(_me(), KEYS_FILE, json.dumps(d, indent=1).encode())


def read_chats() -> list[dict]:
    d = _read_user_json(CHATS_FILE, [])
    return [c for c in d if isinstance(c, dict) and isinstance(c.get("id"), str)] if isinstance(d, list) else []


def write_chats(items: list[dict]) -> None:
    # pinned first, then the most recent; a pin is never dropped by the cap
    items = sorted(items, key=lambda c: (bool(c.get("pinned")), c.get("updatedAt", 0)), reverse=True)[:MAX_CHATS]
    common.write_user_config(_me(), CHATS_FILE, json.dumps(items, indent=1).encode())


def remember_chat(sid: str, agent: str, cwd: str, title: str | None = None) -> None:
    chats = read_chats()
    now = int(time.time())
    for c in chats:
        if c["id"] == sid:
            c["updatedAt"] = now
            if title:
                c["title"] = title
            break
    else:
        chats.append({"id": sid, "agent": agent, "cwd": cwd, "title": title or "", "createdAt": now, "updatedAt": now})
    write_chats(chats)


def forget_chat(sid: str) -> None:
    write_chats([c for c in read_chats() if c["id"] != sid])


def set_chat_flag(sid: str, **fields) -> bool:
    """Pin or rename a chat from the list, with no session open: the index
    is the person's own file, so the title and the pin live there."""
    chats = read_chats()
    for c in chats:
        if c["id"] == sid:
            c.update(fields)
            write_chats(chats)
            return True
    return False


def agent_status(agent: dict) -> dict:
    """What the picker shows: installed? a credential file present? a key stored?"""
    keys = read_keys()
    # `cred` is a list because a CLI renames its credential file between
    # versions (Gemini 0.60 writes gemini-credentials.json where 0.4 wrote
    # oauth_creds.json) — any one of them means signed in.
    cred = agent.get("cred") or []
    if isinstance(cred, str):
        cred = [cred]
    has_cred = False
    for c in cred:
        try:
            if ((_home() / c[2:]) if c.startswith("~/") else Path(c)).is_file():
                has_cred = True
                break
        except OSError:
            continue
    return {
        "id": agent["id"], "name": agent["name"], "vendor": agent["vendor"],
        "installed": bin_path(agent) is not None,
        "hasCred": has_cred, "hasKey": bool(keys.get(agent["id"])),
        "keys": agent["keys"], "login": agent["login"], "bin": agent["bin"], "hidden": bool(agent.get("hidden")),
        "npm": bool(agent.get("npm")), "install": agent.get("install"),
    }


def _sh(word: str) -> str:
    return word if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", word) else "'" + word.replace("'", "'\\''") + "'"


def login_command(agent: dict, method: dict | None = None) -> str:
    """The shell line a terminal tab runs to sign in: our prefix on PATH, the
    agent's login env, the login command. With `method` — a terminal-type
    auth method the running agent advertised — the agent's own command and
    args are used, which is what the protocol says to do."""
    env = dict(agent.get("env", {}))
    if method:
        env.update(method.get("env") or {})
        cmd = [agent["bin"]] + list(method.get("args") or [])
    else:
        env.update(agent["login"].get("env", {}))
        cmd = list(agent["login"]["cmd"])
    prefix = (str(NODE_BIN) + ":" if NODE_BIN.is_dir() else "") + str(BIN_DIR)
    parts = ["PATH=" + _sh(prefix) + ":$PATH"] + [f"{k}={_sh(str(v))}" for k, v in env.items()]
    return " ".join(parts + [" ".join(_sh(w) for w in cmd)])


def terminal_auth_method(init: dict | None) -> dict | None:
    """The first terminal-type auth method an agent advertised, if any."""
    for m in (init or {}).get("authMethods") or []:
        if isinstance(m, dict) and m.get("type") == "terminal":
            return m
    return None


# ---- the agent process -------------------------------------------------------
class Conn:
    """One browser tab's socket."""
    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.owned: set[str] = set()      # session ids this tab answers for

    async def send(self, obj: dict) -> bool:
        try:
            await self.ws.send_str(json.dumps(obj, separators=(",", ":")))
            return True
        except (ConnectionResetError, RuntimeError):
            return False


class SessionLog:
    def __init__(self):
        self.seq = 0
        self.base = 0
        self.entries: list[tuple[int, dict]] = []
        self.bytes = 0
        self.turn_running = False

    def add(self, obj: dict) -> int:
        n = self.seq
        self.seq += 1
        s = len(json.dumps(obj))
        self.entries.append((n, obj))
        self.bytes += s
        while self.entries and (len(self.entries) > LOG_ENTRIES or self.bytes > LOG_BYTES):
            _, old = self.entries.pop(0)
            self.bytes -= len(json.dumps(old))
            self.base = self.entries[0][0] if self.entries else self.seq
        return n

    def clear(self) -> None:
        self.entries.clear()
        self.bytes = 0
        self.base = self.seq


class AgentProc:
    """One agent subprocess for one person, multiplexed to any number of tabs."""

    def __init__(self, agent: dict, cwd: str):
        self.agent = agent
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self.init: dict | None = None
        self.init_error: str | None = None
        self.conns: set[Conn] = set()
        self.logs: dict[str, SessionLog] = {}
        self.owner: dict[str, Conn] = {}           # session id → the tab that answers for it
        self.next_id = 1
        self.pending_out: dict[int, tuple[Conn | None, object, str, dict]] = {}   # agent-facing id → (tab, tab's id, method, params)
        self.pending_in: dict[object, tuple[str | None, asyncio.Future]] = {}      # agent's request id → (session, future)
        self.stderr: list[str] = []
        self.meta: dict[str, dict] = {}            # session id → {modes, configOptions}: what a reattaching tab needs
        self.last_used = time.time()
        self.auth: dict | None = None
        self.exit_code: int | None = None
        self._reader: asyncio.Task | None = None
        self._err_reader: asyncio.Task | None = None
        self._started = asyncio.Event()
        self._lock = asyncio.Lock()
        self.spares: dict[str, tuple[float, dict]] = {}   # cwd → (made at, session/new result)
        self._sparing: set[str] = set()                   # cwds with a spare being made right now
        self._spares_made = 0
        self._warming: set[asyncio.Task] = set()          # strong refs: a bare task can be collected

    # -- lifecycle --
    async def start(self) -> None:
        async with self._lock:
            if self.proc is not None or self.init_error:
                return
            exe = bin_path(self.agent)
            if not exe:
                self.init_error = f"{self.agent['name']} is not installed on this server"
                self._started.set()
                return
            env = dict(os.environ)
            env["PATH"] = agent_path()
            env.update(self.agent.get("env", {}))
            keys = read_keys()
            key = keys.get(self.agent["id"])
            if key and self.agent["keys"]:
                env[self.agent["keys"][0]] = key
            env.setdefault("HOME", str(_home()))
            env.pop("KB_BACKEND_UDS", None)
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    exe, *self.agent.get("args", []), cwd=self.cwd, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT, start_new_session=True)
            except OSError as e:
                self.init_error = f"could not start {self.agent['name']}: {e}"
                self._started.set()
                return
            self._reader = asyncio.create_task(self._read_stdout())
            self._err_reader = asyncio.create_task(self._read_stderr())
            try:
                res = await asyncio.wait_for(self.request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "auth": {"terminal": True},
                        "elicitation": {"form": {}, "url": {}},
                        "session": {"configOptions": {"boolean": {}}},
                        "_meta": {"terminal-auth": True},
                    },
                    "clientInfo": {"name": "company-os", "title": "Ollsoft Company OS", "version": "1"},
                }), timeout=120)
                if res.get("protocolVersion") not in (PROTOCOL_VERSION, None):
                    self.init_error = f"{self.agent['name']} speaks ACP version {res.get('protocolVersion')}, not {PROTOCOL_VERSION}"
                else:
                    self.init = res
            except asyncio.TimeoutError:
                self.init_error = f"{self.agent['name']} did not answer initialize"
            except RpcError as e:
                self.init_error = f"{self.agent['name']} refused initialize: {e}"
            except Exception as e:   # noqa: BLE001 — the process died mid-handshake
                self.init_error = f"{self.agent['name']} failed to start: {e}"
            self._started.set()

    # -- spare sessions --
    def warm_later(self, cwd: str | None = None) -> None:
        """Make a spare in the background. Never awaited by anything a tab waits on."""
        if not SPARES_ON:
            return
        t = asyncio.create_task(self.warm(cwd))
        self._warming.add(t)
        t.add_done_callback(self._warming.discard)

    async def warm(self, cwd: str | None = None) -> None:
        cwd = cwd if isinstance(cwd, str) and cwd.startswith("/") else self.cwd
        if not SPARES_ON or not self.alive() or self._spares_made >= SPARE_MAX:
            return
        if cwd in self._sparing or self._spare_fresh(cwd):
            return
        if cwd not in self.spares and len(self.spares) >= SPARE_CWDS:
            return
        self._sparing.add(cwd)
        self._spares_made += 1
        try:
            res = await asyncio.wait_for(self.request("session/new", {"cwd": cwd, "mcpServers": []}), timeout=120)
        except RpcError as e:
            # the agent refuses to open sessions at all (not signed in, most likely):
            # stop trying, so a reconnecting tab does not poke it once a second.
            log.info("acp %s: no spare sessions (%s)", self.agent["id"], e.message)
            self._spares_made = SPARE_MAX
            return
        except Exception:   # noqa: BLE001 — a spare is a nicety; it never becomes a tab's error
            return
        finally:
            self._sparing.discard(cwd)
        if isinstance(res, dict) and isinstance(res.get("sessionId"), str):
            self.spares[cwd] = (time.time(), res)

    def _spare_fresh(self, cwd: str) -> bool:
        ent = self.spares.get(cwd)
        if ent is None:
            return False
        if time.time() - ent[0] > SPARE_TTL:
            del self.spares[cwd]
            return False
        return True

    def _take_spare(self, params: dict) -> dict | None:
        """A spare answers only a session/new that asks for exactly what the spare
        is: same folder, no MCP servers, nothing else in the params."""
        cwd = params.get("cwd")
        if not SPARES_ON or not isinstance(cwd, str):
            return None
        if params.get("mcpServers") or set(params) - {"cwd", "mcpServers"}:
            return None
        if not self._spare_fresh(cwd):
            return None
        return self.spares.pop(cwd)[1]

    async def _hand_spare(self, conn: Conn, tab_id, res: dict, params: dict) -> None:
        """Give a tab a session made earlier, on exactly the terms
        `_on_agent_response` gives it one made on demand."""
        sid = res["sessionId"]
        m = self.meta.setdefault(sid, {})
        for k in ("modes", "configOptions"):
            if res.get(k) is not None:
                m[k] = res[k]
        self.owner[sid] = conn
        conn.owned.add(sid)
        self.last_used = time.time()
        try:
            remember_chat(sid, self.agent["id"], params.get("cwd", self.cwd))
        except OSError:
            pass
        await conn.send({"jsonrpc": "2.0", "id": tab_id, "result": res})
        self.warm_later(params.get("cwd"))

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self.init is not None

    async def stop(self) -> None:
        p = self.proc
        if p is None:
            return
        try:
            if p.stdin and not p.stdin.is_closing():
                p.stdin.close()
        except Exception:   # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(p.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                p.terminate()
                await asyncio.wait_for(p.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    p.kill()
                except ProcessLookupError:
                    pass

    # -- wire --
    async def _write(self, obj: dict) -> None:
        p = self.proc
        if p is None or p.stdin is None or p.stdin.is_closing():
            raise RpcError(-32000, "agent is not running")
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        if "\n" in line:
            line = line.replace("\n", " ")
        p.stdin.write((line + "\n").encode("utf-8"))
        await p.stdin.drain()

    async def request(self, method: str, params: dict, conn: Conn | None = None, tab_id=None) -> dict:
        """A request from us (initialize) — the answer comes back through the
        reader as a future."""
        rid = self.next_id
        self.next_id += 1
        fut = asyncio.get_event_loop().create_future()
        self.pending_out[rid] = (None, fut, method, params)
        await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return await fut

    async def forward_request(self, conn: Conn, msg: dict) -> None:
        """A request from a tab: re-numbered, remembered, forwarded."""
        method, params = msg.get("method"), msg.get("params") or {}
        params = self._fix_params(method, params)
        if method == "session/new":
            spare = self._take_spare(params)
            if spare is not None:                       # already made: no round trip to the agent
                await self._hand_spare(conn, msg.get("id"), spare, params)
                return
        rid = self.next_id
        self.next_id += 1
        self.pending_out[rid] = (conn, msg.get("id"), method, params)
        sid = params.get("sessionId") if isinstance(params, dict) else None
        if method == "session/prompt" and isinstance(sid, str):
            self.log_for(sid).turn_running = True
        if method == "session/load" and isinstance(sid, str):
            self.log_for(sid).clear()      # the replay IS the history
            self.owner[sid] = conn
            conn.owned.add(sid)
        self.last_used = time.time()
        try:
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except RpcError as e:
            self.pending_out.pop(rid, None)
            await conn.send({"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": e.code, "message": e.message}})

    def _fix_params(self, method: str, params):
        if not isinstance(params, dict):
            return {}
        if method in ("session/new", "session/load", "session/resume"):
            params = dict(params)
            cwd = params.get("cwd")
            # the client may pick a working folder for the session (a folder in
            # the knowledgebase). Anything that is not an absolute path to a
            # directory this account can enter falls back to the repo root —
            # the agent runs as the person either way, so this is about a
            # comprehensible failure, not a privilege boundary.
            if not isinstance(cwd, str) or not cwd.startswith("/") or not os.path.isdir(cwd):
                params["cwd"] = self.cwd
            if not isinstance(params.get("mcpServers"), list):
                params["mcpServers"] = []
        return params

    async def forward_notification(self, msg: dict) -> None:
        try:
            await self._write({"jsonrpc": "2.0", "method": msg.get("method"), "params": msg.get("params") or {}})
        except RpcError:
            pass

    async def answer_agent(self, msg: dict) -> None:
        """A tab's answer to an agent→client request."""
        ent = self.pending_in.pop(msg.get("id"), None)
        if ent is None:
            return
        _, fut = ent
        if not fut.done():
            fut.set_result(msg)

    def log_for(self, sid: str) -> SessionLog:
        lg = self.logs.get(sid)
        if lg is None:
            lg = self.logs[sid] = SessionLog()
        return lg

    # -- the reader: everything the agent says --
    async def _read_stdout(self) -> None:
        p = self.proc
        assert p is not None and p.stdout is not None
        try:
            while True:
                try:
                    line = await p.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("acp %s: a line over the limit was dropped", self.agent["id"])
                    continue
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("acp %s: not JSON: %.120s", self.agent["id"], line)
                    continue
                if not isinstance(msg, dict):
                    continue
                try:
                    await self._on_message(msg)
                except Exception:   # noqa: BLE001
                    log.exception("acp %s: handling a message failed", self.agent["id"])
        finally:
            self.exit_code = p.returncode if p.returncode is not None else -1
            try:
                await asyncio.wait_for(p.wait(), timeout=3)
                self.exit_code = p.returncode
            except asyncio.TimeoutError:
                pass
            for rid, (conn, tid, method, _params) in list(self.pending_out.items()):
                self.pending_out.pop(rid, None)
                if isinstance(tid, asyncio.Future):
                    if not tid.done():
                        tid.set_exception(RpcError(-32000, "agent exited"))
                elif conn is not None:
                    await conn.send({"jsonrpc": "2.0", "id": tid, "error": {"code": -32000, "message": "the agent exited"}})
            for _, (sid, fut) in list(self.pending_in.items()):
                if not fut.done():
                    fut.cancel()
            self.pending_in.clear()
            for c in list(self.conns):
                await c.send({"kb": "exit", "code": self.exit_code})
            self.init = None

    async def _read_stderr(self) -> None:
        p = self.proc
        assert p is not None and p.stderr is not None
        while True:
            try:
                line = await p.stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            self.stderr.append(text)
            if len(self.stderr) > STDERR_LINES:
                del self.stderr[: len(self.stderr) - STDERR_LINES]
            for c in list(self.conns):
                await c.send({"kb": "stderr", "line": text[:2000]})

    async def _on_message(self, msg: dict) -> None:
        if "method" in msg and "id" in msg:
            await self._on_agent_request(msg)
        elif "method" in msg:
            await self._on_agent_notification(msg)
        elif "id" in msg:
            await self._on_agent_response(msg)

    async def _on_agent_response(self, msg: dict) -> None:
        ent = self.pending_out.pop(msg.get("id"), None)
        if ent is None:
            return
        conn, tid, method, params = ent
        if isinstance(tid, asyncio.Future):
            if tid.done():
                return
            if "error" in msg:
                e = msg["error"] or {}
                tid.set_exception(RpcError(e.get("code", -32000), e.get("message", "error")))
            else:
                tid.set_result(msg.get("result") or {})
            return
        sid = params.get("sessionId") if isinstance(params, dict) else None
        result = msg.get("result")
        if method == "session/new" and isinstance(result, dict) and isinstance(result.get("sessionId"), str):
            sid = result["sessionId"]
        if method in ("session/new", "session/load", "session/resume") and isinstance(result, dict) and isinstance(sid, str):
            m = self.meta.setdefault(sid, {})
            for k in ("modes", "configOptions"):
                if result.get(k) is not None:
                    m[k] = result[k]
        if method == "session/set_config_option" and isinstance(result, dict) and isinstance(sid, str) and result.get("configOptions") is not None:
            self.meta.setdefault(sid, {})["configOptions"] = result["configOptions"]
        if method == "session/set_mode" and "error" not in msg and isinstance(sid, str) and isinstance(params, dict):
            m = self.meta.setdefault(sid, {})
            if isinstance(m.get("modes"), dict):
                m["modes"] = dict(m["modes"], currentModeId=params.get("modeId"))
        if method == "session/new" and isinstance(result, dict) and isinstance(result.get("sessionId"), str):
            if conn is not None:
                self.owner[sid] = conn
                conn.owned.add(sid)
            try:
                remember_chat(sid, self.agent["id"], params.get("cwd", self.cwd))
            except OSError:
                pass
            # this one was made the slow way — no spare, or one too stale to use.
            # Have the next chat's session ready before anybody asks for it.
            if conn is not None:
                self.warm_later(params.get("cwd"))
        if method == "session/prompt" and isinstance(sid, str):
            lg = self.log_for(sid)
            lg.turn_running = False
            stop = result.get("stopReason") if isinstance(result, dict) else None
            n = lg.add({"kb": "turn", "sessionId": sid, "stopReason": stop or ("error" if "error" in msg else None),
                        "error": msg.get("error")})
            for c in list(self.conns):
                await c.send({"kb": "turn", "sessionId": sid, "seq": n, "stopReason": stop, "error": msg.get("error")})
            try:
                remember_chat(sid, self.agent["id"], self.cwd)
            except OSError:
                pass
        if conn is not None:
            out = {"jsonrpc": "2.0", "id": tid}
            if "error" in msg:
                out["error"] = msg["error"]
            else:
                out["result"] = result if result is not None else {}
            await conn.send(out)

    async def _on_agent_notification(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "_auth/status_update" and isinstance(params, dict):
            self.auth = params.get("authStatus") or params
        sid = params.get("sessionId") if isinstance(params, dict) else None
        if method == "session/update" and isinstance(sid, str):
            upd = params.get("update") or {}
            if isinstance(upd, dict) and upd.get("sessionUpdate") == "session_info_update":
                try:
                    remember_chat(sid, self.agent["id"], self.cwd, upd.get("title") or None)
                except OSError:
                    pass
            if isinstance(upd, dict) and upd.get("sessionUpdate") == "current_mode_update":
                m = self.meta.setdefault(sid, {})
                if isinstance(m.get("modes"), dict):
                    m["modes"] = dict(m["modes"], currentModeId=upd.get("currentModeId"))
            if isinstance(upd, dict) and upd.get("sessionUpdate") == "config_option_update" and upd.get("configOptions") is not None:
                self.meta.setdefault(sid, {})["configOptions"] = upd["configOptions"]
            n = self.log_for(sid).add({"kb": "u", "sessionId": sid, "m": msg})
            for c in list(self.conns):
                await c.send({"kb": "u", "sessionId": sid, "seq": n, "m": msg})
            return
        # anything else the agent says (extensions, cancellations): every tab hears it
        for c in list(self.conns):
            await c.send({"kb": "u", "sessionId": sid, "seq": -1, "m": msg})

    async def _on_agent_request(self, msg: dict) -> None:
        """Permission and elicitation requests: to the tab that owns the
        session; if none is attached, wait for one (a permission asked while
        the laptop lid was closed is answered when it opens), then give up
        with `cancelled` so the agent can finish its turn."""
        method = msg.get("method")
        params = msg.get("params") or {}
        sid = params.get("sessionId") if isinstance(params, dict) else None
        rid = msg.get("id")
        if method in ("fs/read_text_file", "fs/write_text_file") or (method or "").startswith("terminal/"):
            await self._write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
            return
        if method not in ("session/request_permission", "elicitation/create"):
            await self._write({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
            return
        fut = asyncio.get_event_loop().create_future()
        self.pending_in[rid] = (sid, fut)
        n = self.log_for(sid).add({"kb": "req", "m": msg}) if isinstance(sid, str) else -1
        frame = {"kb": "req", "seq": n, "sessionId": sid, "m": msg}
        sent = await self._to_owner(sid, frame)
        deadline = time.time() + PERMISSION_WAIT
        try:
            while True:
                try:
                    ans = await asyncio.wait_for(asyncio.shield(fut), timeout=15)
                    break
                except asyncio.TimeoutError:
                    if time.time() > deadline:
                        raise
                    if not sent:
                        sent = await self._to_owner(sid, frame)
            out = {"jsonrpc": "2.0", "id": rid}
            if "error" in ans:
                out["error"] = ans["error"]
            else:
                out["result"] = ans.get("result") or {}
        except (asyncio.TimeoutError, asyncio.CancelledError):
            out = {"jsonrpc": "2.0", "id": rid, "result": cancel_result(method)}
        finally:
            self.pending_in.pop(rid, None)
        # What was answered travels with the log, so a tab that reloads sees
        # "✓ Allow once", not "answered elsewhere".
        done = {"kb": "req-done", "id": rid}
        picked = ((out.get("result") or {}).get("outcome") or {}) if isinstance(out.get("result"), dict) else {}
        if isinstance(picked, dict) and picked.get("optionId") is not None:
            done["optionId"] = picked["optionId"]
            for o in (params.get("options") or []) if isinstance(params, dict) else []:
                if isinstance(o, dict) and o.get("optionId") == picked["optionId"]:
                    done["label"] = o.get("name")
                    done["kind"] = o.get("kind")
        elif "result" in out and method == "session/request_permission":
            done["label"] = "cancelled"
        if isinstance(sid, str):
            self.log_for(sid).add(dict(done))
        for c in list(self.conns):
            await c.send(dict(done, sessionId=sid))
        try:
            await self._write(out)
        except RpcError:
            pass

    async def _to_owner(self, sid, obj: dict) -> bool:
        conn = self.owner.get(sid) if isinstance(sid, str) else None
        if conn is not None and conn in self.conns and await conn.send(obj):
            return True
        # nobody owns it: the most recent tab gets it
        for c in reversed(list(self.conns)):
            if await c.send(obj):
                if isinstance(sid, str):
                    self.owner[sid] = c
                    c.owned.add(sid)
                return True
        return False

    # -- tabs --
    async def attach(self, conn: Conn, sid: str, have: int) -> None:
        self.owner[sid] = conn
        conn.owned.add(sid)
        lg = self.log_for(sid)
        if have < lg.base:
            await conn.send({"kb": "reset", "sessionId": sid, "base": lg.base, "seq": lg.seq})
            have = lg.base
        for n, obj in lg.entries:
            if n >= have:
                out = dict(obj)
                out["seq"] = n
                if "sessionId" not in out:
                    out["sessionId"] = sid
                await conn.send(out)
        await conn.send({"kb": "attached", "sessionId": sid, "seq": lg.seq, "running": lg.turn_running,
                         "meta": self.meta.get(sid) or {}})

    def detach_conn(self, conn: Conn) -> None:
        self.conns.discard(conn)
        for sid in list(conn.owned):
            if self.owner.get(sid) is conn:
                del self.owner[sid]
        self.last_used = time.time()

    def busy(self) -> bool:
        return bool(self.conns) or any(lg.turn_running for lg in self.logs.values()) or bool(self.pending_in)


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def cancel_result(method: str) -> dict:
    if method == "session/request_permission":
        return {"outcome": {"outcome": "cancelled"}}
    return {"action": "cancel"}


# ---- the registry of running agents, per backend (= per person) ---------------
PROCS: dict[str, AgentProc] = {}
_reaper: asyncio.Task | None = None


async def get_proc(agent_id: str, cwd: str) -> AgentProc:
    global _reaper
    p = PROCS.get(agent_id)
    if p is not None and (p.alive() or (p.proc is not None and p.init is None and not p.init_error and not p._started.is_set())):
        return p
    if p is not None and p.proc is not None and p.proc.returncode is None and p.init_error:
        await p.stop()
    p = PROCS[agent_id] = AgentProc(BY_ID[agent_id], cwd)
    await p.start()
    p.warm_later(cwd)       # the first chat should not wait for session/new either
    if _reaper is None or _reaper.done():
        _reaper = asyncio.create_task(_reap())
    return p


async def _reap() -> None:
    while True:
        await asyncio.sleep(60)
        for aid, p in list(PROCS.items()):
            if p.proc is None:
                continue
            if p.proc.returncode is not None:
                if not p.conns:
                    PROCS.pop(aid, None)
                continue
            if not p.busy() and time.time() - p.last_used > IDLE_SECONDS:
                log.info("acp %s: idle, stopping", aid)
                await p.stop()
                PROCS.pop(aid, None)


async def shutdown() -> None:
    for p in list(PROCS.values()):
        await p.stop()
    PROCS.clear()


# ---- HTTP: the catalogue, keys, the chat index -------------------------------
async def agents(request: web.Request) -> web.Response:
    from . import settings as kbsettings
    try:
        snap = kbsettings.snapshot(_me())
        default = snap.get("effective", {}).get("ai.agent", AGENT_IDS[0])
    except Exception:   # noqa: BLE001
        default = AGENT_IDS[0]
    out = []
    for a in CATALOGUE:
        st = agent_status(a)
        p = PROCS.get(a["id"])
        st["running"] = bool(p and p.alive())
        st["auth"] = p.auth if p else None
        method = terminal_auth_method(p.init) if p else None
        st["authMethods"] = (p.init or {}).get("authMethods") if p else None
        st["loginCommand"] = login_command(a, method) if (method or a["login"].get("cmd")) else None
        out.append(st)
    return web.json_response({"agents": out, "default": default, "prefix": str(AGENTS_PREFIX),
                              "node": str(NODE_BIN / "node") if NODE_BIN.is_dir() else None,
                              "cwd": str(common.REPO_ROOT)})


async def set_key(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad json"}, status=400)
    aid = body.get("agent") if isinstance(body, dict) else None
    key = body.get("key") if isinstance(body, dict) else None
    if aid not in BY_ID or not isinstance(key, str) or len(key) > 4096 or any(ch in key for ch in "\r\n\x00"):
        return web.json_response({"error": "bad request"}, status=400)
    keys = read_keys()
    if key.strip():
        keys[aid] = key.strip()
    else:
        keys.pop(aid, None)
    try:
        write_keys(keys)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    # a running agent keeps its old env — the next start takes the key
    p = PROCS.pop(aid, None)
    if p is not None:
        await p.stop()
    return web.json_response({"ok": True, "hasKey": bool(keys.get(aid))})


async def chats(request: web.Request) -> web.Response:
    return web.json_response({"chats": read_chats()})


async def forget(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad json"}, status=400)
    sid = body.get("id") if isinstance(body, dict) else None
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        return web.json_response({"error": "bad request"}, status=400)
    try:
        forget_chat(sid)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True})


async def pin(request: web.Request) -> web.Response:
    """Pin a chat to the top of the list (or unpin it)."""
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad json"}, status=400)
    sid = body.get("id") if isinstance(body, dict) else None
    if not isinstance(sid, str) or not _SID_RE.match(sid):
        return web.json_response({"error": "bad request"}, status=400)
    try:
        ok = set_chat_flag(sid, pinned=bool(body.get("pinned", True)))
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": ok})


async def rename(request: web.Request) -> web.Response:
    """Name a chat from the list (the open chat does it over the socket)."""
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad json"}, status=400)
    sid = body.get("id") if isinstance(body, dict) else None
    title = body.get("title") if isinstance(body, dict) else None
    if not isinstance(sid, str) or not _SID_RE.match(sid) or not isinstance(title, str):
        return web.json_response({"error": "bad request"}, status=400)
    title = " ".join(title.split())[:120]
    if not title:
        return web.json_response({"error": "a chat needs a name"}, status=400)
    try:
        ok = set_chat_flag(sid, title=title)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": ok, "title": title})


async def restart(request: web.Request) -> web.Response:
    """Stop the agent process (it restarts on the next connection) — after a
    sign-in, or when it is wedged."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    aid = body.get("agent") if isinstance(body, dict) else None
    if aid not in BY_ID:
        return web.json_response({"error": "bad request"}, status=400)
    p = PROCS.pop(aid, None)
    if p is not None:
        await p.stop()
    return web.json_response({"ok": True})


# ---- the WebSocket ------------------------------------------------------------
async def ws_handler(request: web.Request) -> web.StreamResponse:
    aid = request.query.get("agent", "")
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    conn = Conn(ws)
    if aid not in BY_ID:
        await conn.send({"kb": "error", "message": "unknown agent"})
        await ws.close()
        return ws
    proc = await get_proc(aid, str(common.REPO_ROOT))
    await proc._started.wait()
    if proc.init_error or not proc.alive():
        await conn.send({"kb": "error", "message": proc.init_error or "the agent is not running",
                         "stderr": proc.stderr[-40:]})
        await ws.close()
        return ws
    proc.conns.add(conn)
    proc.last_used = time.time()
    await conn.send({"kb": "hello", "agent": agent_status(proc.agent), "init": proc.init, "cwd": proc.cwd,
                     "auth": proc.auth, "chats": [c for c in read_chats() if c.get("agent") == aid],
                     "stderr": proc.stderr[-20:]})
    proc.warm_later(proc.cwd)   # a tab is open: have a session ready for it
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                continue
            try:
                m = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            kb = m.get("kb")
            if kb == "attach":
                sid = m.get("sessionId")
                if isinstance(sid, str) and _SID_RE.match(sid):
                    try:
                        have = max(0, int(m.get("have", 0)))
                    except (TypeError, ValueError):
                        have = 0
                    await proc.attach(conn, sid, have)
                continue
            if kb == "detach":
                sid = m.get("sessionId")
                if isinstance(sid, str):
                    conn.owned.discard(sid)
                    if proc.owner.get(sid) is conn:
                        del proc.owner[sid]
                continue
            if kb == "stderr":
                await conn.send({"kb": "stderr-all", "lines": proc.stderr[-STDERR_LINES:]})
                continue
            if kb == "title":
                sid, title = m.get("sessionId"), m.get("title")
                if isinstance(sid, str) and _SID_RE.match(sid) and isinstance(title, str):
                    try:
                        remember_chat(sid, aid, proc.cwd, title.strip()[:120] or None)
                    except OSError:
                        pass
                continue
            if not proc.alive():
                if "id" in m and "method" in m:
                    await conn.send({"jsonrpc": "2.0", "id": m.get("id"), "error": {"code": -32000, "message": "the agent is not running"}})
                continue
            if "method" in m and "id" in m:
                await proc.forward_request(conn, m)
            elif "method" in m:
                await proc.forward_notification(m)
            elif "id" in m:
                await proc.answer_agent(m)
    finally:
        proc.detach_conn(conn)
    return ws


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/acp/agents", agents)
    app.router.add_post("/api/acp/key", set_key)
    app.router.add_get("/api/acp/chats", chats)
    app.router.add_post("/api/acp/forget", forget)
    app.router.add_post("/api/acp/pin", pin)
    app.router.add_post("/api/acp/rename", rename)
    app.router.add_post("/api/acp/restart", restart)
    app.router.add_get("/acp", ws_handler)

    async def _bye(app):
        await shutdown()
    app.on_shutdown.append(_bye)
