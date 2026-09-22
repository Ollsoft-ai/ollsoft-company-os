#!/usr/bin/env python3
"""kb-share — the only part of Company OS that faces the open internet.

It serves what has been bind-mounted in front of it and nothing else. It has
no database, no session key, no knowledge of `/srv/kb`, no route to the host
and no way to create or widen a share. A share is a directory under /data
named by its id, and a JSON file under /conf that says what may be done with
it. Both are put there by the platform, as root, outside this process.

    /data/<id>/…        the shared file or folder (bind-mounted ro, or rw)
    /conf/<id>.json     {"mode": "view"|"edit", "expires": <unix>,
                         "title": str, "name": str,
                         "pw": {"salt": hex, "hash": hex} | null,
                         "token_hash": hex}

Everything it renders is escaped; Markdown is rendered with raw HTML turned
off, so there is no path from a document's bytes to executable script. A
shared `.html` artifact is shown as source for the same reason.
"""
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(os.environ.get("KB_SHARE_DATA", "/data"))
CONF = Path(os.environ.get("KB_SHARE_CONF", "/conf"))
PORT = int(os.environ.get("KB_SHARE_PORT", "8080"))
BRAND = os.environ.get("KB_SHARE_BRAND", "Company OS")
MAX_BODY = 4 * 1024 * 1024
COOKIE_KEY = secrets.token_bytes(32)          # per boot, in memory only
ID_RE = re.compile(r"^[a-z2-7]{16,32}$")      # base32, as the platform mints them
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")

try:
    from markdown_it import MarkdownIt
    _MD = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
except Exception:                              # noqa: BLE001 — degrade, never fail to start
    _MD = None

# ---- rate limiting: a password is guessed one attempt at a time ------------
_ATTEMPTS: dict[str, list[float]] = {}


def too_many(key: str, limit: int = 10, window: int = 60) -> bool:
    now = time.time()
    hits = [t for t in _ATTEMPTS.get(key, []) if now - t < window]
    _ATTEMPTS[key] = hits
    return len(hits) >= limit


def note_attempt(key: str) -> None:
    _ATTEMPTS.setdefault(key, []).append(time.time())


# ---- the share itself -------------------------------------------------------
def load_share(sid: str) -> dict | None:
    if not ID_RE.match(sid or ""):
        return None
    try:
        conf = json.loads((CONF / (sid + ".json")).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(conf, dict):
        return None
    if int(conf.get("expires") or 0) and time.time() > conf["expires"]:
        return None                            # the mount may still be there; this is the second lock
    if not (DATA / sid).is_dir():
        return None
    return conf


def token_ok(conf: dict, token: str) -> bool:
    want = str(conf.get("token_hash") or "")
    got = hashlib.sha256(token.encode()).hexdigest()
    return bool(want) and hmac.compare_digest(want, got)


def password_ok(conf: dict, given: str) -> bool:
    pw = conf.get("pw")
    if not pw:
        return True
    try:
        h = hashlib.scrypt(given.encode(), salt=bytes.fromhex(pw["salt"]),
                           n=16384, r=8, p=1, dklen=32).hex()
    except (ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(h, str(pw.get("hash", "")))


def cookie_for(sid: str) -> str:
    return hmac.new(COOKIE_KEY, sid.encode(), hashlib.sha256).hexdigest()


def safe_join(root: Path, rel: str) -> Path | None:
    """Inside the share or nowhere. Symlinks are resolved and re-checked, so a
    link planted in a shared folder cannot reach out of it."""
    p = (root / rel.lstrip("/")).resolve()
    root = root.resolve()
    return p if p == root or root in p.parents else None


# ---- rendering --------------------------------------------------------------
CSS = """
:root { color-scheme: dark light; --bg:#0e1524; --panel:#131c2e; --ink:#e7edf7; --muted:#9fb0c9;
        --line:#22304a; --accent:#4d9dff; }
@media (prefers-color-scheme: light) { :root { --bg:#fbfbfa; --panel:#fff; --ink:#1d1c1a;
        --muted:#6b6862; --line:#e6e3dd; --accent:#2b7fff; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.65 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
header { display:flex; align-items:center; gap:.6rem; padding:.7rem 1rem; border-bottom:1px solid var(--line); color:var(--muted); font-size:.85rem; }
header b { color:var(--ink); font-weight:600; }
main { max-width:52rem; margin:0 auto; padding:1.5rem 1.2rem 4rem; }
h1,h2,h3 { line-height:1.25; }
a { color:var(--accent); }
pre { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:.9rem 1rem; overflow:auto; }
code { font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; }
pre code { font-size:13.5px; }
blockquote { margin:0; padding:.2rem 1rem; border-left:3px solid var(--line); color:var(--muted); }
table { border-collapse:collapse; } td,th { border:1px solid var(--line); padding:.35rem .6rem; }
img,video { max-width:100%; border-radius:10px; }
ul.files { list-style:none; padding:0; }
ul.files li { border-bottom:1px solid var(--line); }
ul.files a { display:flex; gap:.6rem; padding:.55rem .2rem; text-decoration:none; color:var(--ink); }
ul.files span { color:var(--muted); font-size:.82rem; margin-left:auto; }
form.pw { max-width:22rem; margin:4rem auto; text-align:center; }
input,button,textarea { font:inherit; }
input[type=password] { width:100%; padding:.6rem .7rem; border-radius:10px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink); }
button { margin-top:.8rem; padding:.6rem 1.1rem; border-radius:999px; border:0; background:var(--accent);
  color:#fff; cursor:pointer; }
textarea { width:100%; min-height:60vh; padding:1rem; border-radius:12px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink); font:14px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; }
.bar { display:flex; gap:.6rem; align-items:center; margin:.8rem 0; }
.muted { color:var(--muted); font-size:.85rem; }
.err { color:#ff6b6b; }
"""


def page(title: str, body: str, sub: str = "") -> bytes:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=referrer content=no-referrer>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>"
            f"<header><b>{html.escape(title)}</b><span>{html.escape(sub)}</span></header>"
            f"<main>{body}</main></body></html>").encode()


def render_markdown(text: str) -> str:
    if _MD is not None:
        return _MD.render(text)
    return "<pre>" + html.escape(text) + "</pre>"


class Handler(BaseHTTPRequestHandler):
    server_version = "kb-share"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 20                # a connection that says nothing loses its thread

    # ---- plumbing ----
    def log_message(self, fmt, *args):          # one line per request, to stdout → journald
        print("kb-share %s %s" % (self.address_string(), fmt % args), flush=True)

    def send(self, code: int, body: bytes, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; img-src 'self' data:; media-src 'self'; "
                         "style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; "
                         "frame-ancestors 'none'")
        for k, v in (extra or {}):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def fail(self, code=404, msg="Nothing here"):
        self.send(code, page("Nothing here", f"<p class=muted>{html.escape(msg)}</p>"))

    def cookies(self) -> dict:
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    # ---- routing ----
    def route(self):
        u = urllib.parse.urlsplit(self.path)
        parts = [urllib.parse.unquote(s) for s in u.path.split("/") if s]
        if not parts or parts[0] != "s" or len(parts) < 3:
            return None
        sid, token, rest = parts[1], parts[2], "/".join(parts[3:])
        if not ID_RE.match(sid) or not TOKEN_RE.match(token):
            return None
        conf = load_share(sid)
        if conf is None or not token_ok(conf, token):
            return None
        return sid, token, rest, conf, urllib.parse.parse_qs(u.query)

    def unlocked(self, sid: str, conf: dict) -> bool:
        return not conf.get("pw") or self.cookies().get("kbs_" + sid) == cookie_for(sid)

    def ask_password(self, sid, token, bad=False):
        body = (f"<form class=pw method=post action='/s/{html.escape(sid)}/{html.escape(token)}/__unlock'>"
                f"<p class=muted>This link is protected by a password.</p>"
                f"<input type=password name=pw autofocus autocomplete='current-password'>"
                f"{'<p class=err>Not that one.</p>' if bad else ''}"
                f"<button>Open</button></form>")
        self.send(200, page(BRAND, body))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        r = self.route()
        if r is None:
            return self.fail()
        sid, token, rest, conf, _ = r
        if not self.unlocked(sid, conf):
            return self.ask_password(sid, token)
        root = DATA / sid
        # A single-file share is mounted as one file inside the share's
        # directory, so its bare link would show a list of exactly one thing.
        # Open the file instead — that is what was shared.
        if not rest and conf.get("kind") == "file" and conf.get("name"):
            rest = conf["name"]
        target = safe_join(root, rest)
        if target is None or not os.path.lexists(target):
            return self.fail()
        title = conf.get("title") or conf.get("name") or BRAND
        if target.is_dir():
            return self.listing(sid, token, root, target, title)
        return self.one_file(sid, token, root, target, conf, title)

    def listing(self, sid, token, root, target, title):
        rows = []
        base = f"/s/{urllib.parse.quote(sid)}/{urllib.parse.quote(token)}"
        rel = target.relative_to(root)
        if str(rel) != ".":
            up = str(rel.parent) if str(rel.parent) != "." else ""
            rows.append(f"<li><a href='{base}/{urllib.parse.quote(up)}'>← up</a></li>")
        for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith("."):
                continue                        # .trash and friends are machinery
            href = f"{base}/{urllib.parse.quote(str(p.relative_to(root)))}"
            size = "" if p.is_dir() else f"{p.stat().st_size:,} bytes"
            rows.append(f"<li><a href='{html.escape(href)}'>{'📁 ' if p.is_dir() else ''}"
                        f"{html.escape(p.name)}<span>{size}</span></a></li>")
        body = f"<ul class=files>{''.join(rows) or '<li class=muted>Empty</li>'}</ul>"
        self.send(200, page(title, body, str(rel) if str(rel) != "." else ""))

    def one_file(self, sid, token, root, target, conf, title):
        name = target.name
        ctype, _ = mimetypes.guess_type(name)
        editable = conf.get("mode") == "edit" and name.lower().endswith((".md", ".txt", ".csv"))
        base = f"/s/{urllib.parse.quote(sid)}/{urllib.parse.quote(token)}"
        if name.lower().endswith((".md", ".txt", ".csv", ".html", ".htm", ".json", ".yml", ".yaml")):
            try:
                text = target.read_text(errors="replace")[:2_000_000]
            except OSError:
                return self.fail(403, "Cannot read that")
            if name.lower().endswith(".md"):
                body = render_markdown(text)
            else:
                # an artifact is shown as SOURCE: running a stranger's script on
                # this origin would be a hole between shares
                body = "<pre><code>" + html.escape(text) + "</code></pre>"
            if editable:
                body += (f"<form method=post action='{base}/__save'>"
                         f"<input type=hidden name=path value='{html.escape(str(target.relative_to(root)))}'>"
                         f"<input type=hidden name=mtime value='{int(target.stat().st_mtime)}'>"
                         f"<div class=bar><span class=muted>You can edit this.</span></div>"
                         f"<textarea name=text>{html.escape(text)}</textarea>"
                         f"<button>Save</button></form>")
            return self.send(200, page(title, body, name))
        try:
            data = target.read_bytes()
        except OSError:
            return self.fail(403, "Cannot read that")
        self.send(200, data, ctype or "application/octet-stream")

    def do_POST(self):
        r = self.route()
        if r is None:
            return self.fail()
        sid, token, rest, conf, _ = r
        try:
            length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
        except ValueError:
            return self.fail(400, "Bad request")
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        if rest == "__unlock":
            key = f"{sid}:{self.address_string()}"
            if too_many(key):
                return self.fail(429, "Too many attempts. Try again in a minute.")
            note_attempt(key)
            if not password_ok(conf, (form.get("pw") or [""])[0]):
                return self.ask_password(sid, token, bad=True)
            self.send(303, b"", extra=[("Location", f"/s/{sid}/{token}/"),
                                       ("Set-Cookie", f"kbs_{sid}={cookie_for(sid)}; Path=/s/{sid}/; "
                                                      "HttpOnly; SameSite=Lax; Secure; Max-Age=86400")])
            return
        if rest == "__save":
            if conf.get("mode") != "edit" or not self.unlocked(sid, conf):
                return self.fail(403, "This link is read-only")
            rel = (form.get("path") or [""])[0]
            target = safe_join(DATA / sid, rel)
            if target is None or not target.is_file() or not rel.lower().endswith((".md", ".txt", ".csv")):
                return self.fail(400, "Cannot write that")
            try:
                was = int((form.get("mtime") or ["0"])[0])
            except ValueError:
                was = 0
            if was and int(target.stat().st_mtime) != was:
                return self.fail(409, "Somebody else saved while you were writing. Reload and try again.")
            try:
                target.write_text((form.get("text") or [""])[0])
            except OSError as e:
                return self.fail(403, f"Could not save: {e}")
            self.send(303, b"", extra=[("Location", f"/s/{sid}/{token}/{urllib.parse.quote(rel)}")])
            return
        self.fail()


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"kb-share listening on {PORT}, data={DATA}, conf={CONF}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
