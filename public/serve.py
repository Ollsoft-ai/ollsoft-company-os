#!/usr/bin/env python3
"""kb-share — the only part of Company OS that faces the open internet.

It serves what has been bind-mounted in front of it and nothing else. It has
no database, no session key, no knowledge of `/srv/kb`, no route to the host
and no way to create or widen a share. A share is a directory under /data
named by its id, and a JSON file under /conf that says what may be done with
it. Both are put there by the platform, as root, outside this process.

    /data/<id>/…        the shared folder — or, for a single-file share, the
                        folder it lives in, where only that one name can be
                        opened at all (the kernel says so; `only` says it
                        again). Bind-mounted ro, or rw when the link edits.
    /conf/<id>.json     {"mode": "view"|"edit", "expires": <unix>,
                         "title": str, "name": str, "only": str | null,
                         "theme": str | null, "tokens": {css-var: value},
                         "pw": {"salt": hex, "hash": hex} | null,
                         "token_hash": hex}
    /assets/…           the platform's own frontend bundle, read-only: a
                        document opens in the REAL editor, not a textarea

Nothing under /assets is secret — it is the same JavaScript and stylesheet
every browser on the app already downloads — and mounting it rather than
copying it into the image means a frontend deploy updates this page too.

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
ASSETS = Path(os.environ.get("KB_SHARE_ASSETS", "/assets"))
PORT = int(os.environ.get("KB_SHARE_PORT", "8080"))
BRAND = os.environ.get("KB_SHARE_BRAND", "Company OS")
MAX_BODY = 4 * 1024 * 1024
COOKIE_KEY = secrets.token_bytes(32)          # per boot, in memory only
ID_RE = re.compile(r"^[a-z2-7]{16,32}$")      # base32, as the platform mints them
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
# What the editor page is allowed to fetch out of the mounted bundle. An
# allowlist of suffixes, not of names, because the chunk names carry hashes.
ASSET_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
               ".woff2": "font/woff2", ".woff": "font/woff", ".svg": "image/svg+xml"}
EDITABLE_EXT = (".md", ".txt", ".csv")


def asset_stamp() -> int:
    """One number that changes when the bundle does, for the ?v= on its URLs —
    the same trick the app plays with its own build stamp, so these files can
    be served immutable and still update the moment a deploy lands."""
    try:
        return int((ASSETS / "publicdoc.js").stat().st_mtime)
    except OSError:
        return 0


def editor_available() -> bool:
    return (ASSETS / "publicdoc.js").is_file() and (ASSETS / "style.css").is_file()


# The company's look, as the platform recorded it in the conf. A shared page
# wears the theme the company set — not this container's idea of one, and not
# the reader's device: an admin who picks Light, or tints the accent, decides
# what a client sees. Values are re-checked here because everything from a
# file gets re-checked here.
_TOKEN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_VALUE = re.compile(r"^[#A-Za-z0-9 ,.'\"_()%-]{1,120}$")
_THEMES = {"deep-blue", "dark", "light"}


def look_of(conf: dict) -> tuple[str, str]:
    """(html attribute, inline <style>) for this share's theme."""
    theme = conf.get("theme")
    attr = f" data-theme='{html.escape(theme)}'" if theme in _THEMES else ""
    tokens = conf.get("tokens")
    css = ""
    if isinstance(tokens, dict):
        rules = [f"--{k}:{v}" for k, v in list(tokens.items())[:40]
                 if isinstance(k, str) and isinstance(v, str)
                 and _TOKEN.match(k) and _VALUE.match(v)]
        if rules:
            css = ":root{" + ";".join(rules) + "}"
    return attr, css


def stamp(st: os.stat_result) -> int:
    """A file's version, in MICROSECONDS.

    Not seconds: a whole-second stamp cannot tell "I saved that" from
    "somebody else saved in the same second", which cost the page both of its
    jobs — it missed a change made within a second of loading, and its
    conflict check would have waved one writer's text over another's. Not
    nanoseconds either: 1.79e18 does not survive JSON in a browser
    (`Number.MAX_SAFE_INTEGER` is 9.0e15), so the page handed back a rounded
    number and every save came back 409."""
    return st.st_mtime_ns // 1000

try:
    from markdown_it import MarkdownIt
    # CommonMark has no tables, so this rendered `| a | b |` as a paragraph of
    # pipes. The two GFM rules are enabled by hand rather than by taking the
    # "gfm-like" preset, which also turns on linkify and then needs a package
    # that is deliberately not in this image.
    _MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    _MD.enable(["table", "strikethrough"])
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


# The same pages, wearing the platform's own theme when the bundle is there:
# a folder listing, a password prompt and "nothing here" should not look like
# a different product from the document they lead to. Tokens only — every
# colour comes from the app's stylesheet.
SHELL_CSS = """
body { font-family: var(--sans); }
header { display: flex; align-items: center; gap: .6rem; padding: .55rem .9rem;
  border-bottom: 1px solid var(--border); background: var(--panel); color: var(--muted);
  font-size: .85rem; flex: 0 0 auto; }
header b { color: var(--ink); font-weight: 600; font-size: .92rem; }
main { width: 100%; max-width: 52rem; margin: 0 auto; padding: 1.4rem 1.2rem 4rem;
  overflow: auto; }
ul.files { list-style: none; padding: 0; margin: 0; }
ul.files li { border-bottom: 1px solid var(--border); }
ul.files a { display: flex; gap: .6rem; padding: .6rem .3rem; text-decoration: none;
  color: var(--ink); border-radius: var(--r); }
ul.files a:hover { background: var(--panel2); }
ul.files span { color: var(--muted); font-size: .82rem; margin-left: auto; }
form.pw { max-width: 22rem; margin: 4rem auto; text-align: center; }
form.pw input[type=password] { width: 100%; padding: .6rem .7rem; border-radius: var(--r);
  border: 1px solid var(--border); background: var(--panel); color: var(--ink); font: inherit; }
form.pw button { margin-top: .8rem; padding: .55rem 1.2rem; border-radius: 999px; border: 0;
  background: var(--accent); color: var(--on-accent, #fff); cursor: pointer; font: inherit; }
textarea { width: 100%; min-height: 60vh; padding: 1rem; border-radius: var(--r);
  border: 1px solid var(--border); background: var(--panel); color: var(--ink);
  font: 14px/1.6 var(--mono); }
pre { background: var(--panel); border: 1px solid var(--border); border-radius: var(--r);
  padding: .9rem 1rem; overflow: auto; }
table { border-collapse: collapse; } td, th { border: 1px solid var(--border); padding: .35rem .6rem; }
img, video { max-width: 100%; border-radius: var(--r); }
blockquote { margin: 0; padding: .2rem 1rem; border-left: 3px solid var(--border); color: var(--muted); }
.bar { display: flex; gap: .6rem; align-items: center; margin: .8rem 0; }
.err { color: var(--danger); }
"""


def page(title: str, body: str, sub: str = "", conf: dict | None = None) -> bytes:
    v = asset_stamp()
    theme_attr, theme_css = look_of(conf or {})
    head = (f"<link rel=icon href='/assets/favicon.svg?v={v}'>"
            f"<link rel=stylesheet href='/assets/style.css?v={v}'>"
            f"<style>{SHELL_CSS}{theme_css}</style>"
            if editor_available() else f"<style>{CSS}</style>")
    return (f"<!doctype html><html lang=en{theme_attr}><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=referrer content=no-referrer>"
            f"<title>{html.escape(title)}</title>{head}</head><body>"
            f"<header><b>{html.escape(title)}</b><span>{html.escape(sub)}</span></header>"
            f"<main>{body}</main></body></html>").encode()


# The chrome around the editor. Everything inside the document is the app's
# own stylesheet, loaded from /assets — this is only the strip at the top.
DOC_CSS = """
body.share-doc { height: 100%; display: flex; flex-direction: column; overflow: hidden; }
.share-top { display: flex; align-items: center; gap: .6rem; padding: .55rem .9rem;
  border-bottom: 1px solid var(--border); background: var(--panel); flex: 0 0 auto;
  flex-wrap: nowrap; }
.share-top > * { flex: 0 0 auto; }
.share-top b { font-weight: 600; font-size: .92rem; flex: 0 1 auto; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.share-top .back { color: var(--muted); text-decoration: none; font-size: .82rem;
  border: 1px solid var(--border); border-radius: 999px; padding: .12rem .55rem; }
.share-top .back:hover { color: var(--ink); border-color: var(--accent); }
.share-top .where { color: var(--muted); font-size: .8rem; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; flex: 0 1 auto; min-width: 0; }
.share-top .status { margin-left: auto; font-size: .78rem; color: var(--muted);
  flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.share-top .status.ok { color: var(--ok, var(--accent)); }
.share-top .status.err { color: var(--danger); }
.share-top .badge { font-size: .7rem; letter-spacing: .04em; text-transform: uppercase;
  color: var(--muted); border: 1px solid var(--border); border-radius: 999px;
  padding: .1rem .45rem; white-space: nowrap; }
/* a phone has room for the name, the state and nothing else */
@media (max-width: 560px) {
  .share-top { gap: .45rem; padding: .45rem .6rem; }
  .share-top .where { display: none; }
  .share-top .badge { font-size: .6rem; padding: .1rem .35rem; }
  .share-top .back { padding: .12rem .45rem; }
}
#doc { flex: 1 1 auto; min-height: 0; }
#doc .cm-editor { height: 100%; }
body.share-doc noscript { display: block; padding: 1.5rem; }
"""


def doc_page(conf: dict, base: str, rel: str, text: str, mtime: int, fallback: str) -> bytes:
    """The real editor, mounted on one file.

    The document arrives inside the page (one round trip, and the text is
    already on the server's tongue), as JSON in a data block rather than
    interpolated into script source — `</script>` in a document must close
    nothing.
    """
    v = asset_stamp()
    title = conf.get("title") or conf.get("name") or BRAND
    payload = json.dumps({
        "base": base, "path": rel, "name": os.path.basename(rel),
        "mode": conf.get("mode", "view"), "mtime": mtime, "text": text,
        "rich": rel.lower().endswith(".md"),
    }).replace("<", "\\u003c")
    editable = conf.get("mode") == "edit"
    theme_attr, theme_css = look_of(conf)
    # A document reached THROUGH a folder share needs a way back to the list;
    # a single-file share has nowhere to go.
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    back = ("" if conf.get("kind") == "file" else
            f"<a class=back href='{base}/{urllib.parse.quote(parent)}'>← all files</a>")
    return (
        f"<!doctype html><html lang=en{theme_attr}><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1,"
        "viewport-fit=cover,interactive-widget=resizes-content'>"
        "<meta name=referrer content=no-referrer>"
        f"<title>{html.escape(title)}</title>"
        f"<link rel=icon href='/assets/favicon.svg?v={v}'>"
        f"<link rel=stylesheet href='/assets/style.css?v={v}'>"
        f"<style>{DOC_CSS}{theme_css}</style></head>"
        "<body class='share-doc'>"
        f"<header class=share-top>{back}<b>{html.escape(title)}</b>"
        f"<span class=where>{html.escape(rel)}</span>"
        f"<span class=badge>{'shared · you can edit' if editable else 'shared · read only'}</span>"
        "<span class=status id=status></span></header>"
        "<div id=doc></div>"
        f"<script type='application/json' id='kb-conf'>{payload}</script>"
        f"<script type=module src='/assets/publicdoc.js?v={v}'></script>"
        f"<noscript>{fallback}</noscript>"
        "</body></html>").encode()


# Raw HTML stays off — a shared document must never be able to run anything
# here — but a line break inside a table cell has no other spelling in GFM, so
# that one tag is let back through after the escaping has done its work.
_BR = re.compile(r"&lt;\s*br\s*/?\s*&gt;", re.I)


def render_markdown(text: str) -> str:
    if _MD is not None:
        return _BR.sub("<br>", _MD.render(text))
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
        # 'self' scripts are the platform's own bundle out of /assets, never
        # anything a shared document contains: markdown is rendered with raw
        # HTML off and an .html artifact is shown as source.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; img-src 'self' data:; media-src 'self'; "
                         "script-src 'self'; connect-src 'self'; font-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; form-action 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'")
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

    def ask_password(self, sid, token, bad=False, conf=None):
        body = (f"<form class=pw method=post action='/s/{html.escape(sid)}/{html.escape(token)}/__unlock'>"
                f"<p class=muted>This link is protected by a password.</p>"
                f"<input type=password name=pw autofocus autocomplete='current-password'>"
                f"{'<p class=err>Not that one.</p>' if bad else ''}"
                f"<button>Open</button></form>")
        self.send(200, page(BRAND, body, "", conf))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        # /static/ as well as /assets/: the platform's stylesheet points at
        # "/static/fonts/…" absolutely (it is served from /static there), and
        # the shared page uses that stylesheet unmodified.
        path = self.path.split("?")[0]
        for prefix in ("/assets/", "/static/"):
            if path.startswith(prefix):
                return self.asset(path[len(prefix):])
        r = self.route()
        if r is None:
            return self.fail()
        sid, token, rest, conf, q = r
        if not self.unlocked(sid, conf):
            return self.ask_password(sid, token, conf=conf)
        if rest in ("__raw", "__stat"):
            return self.file_api(rest, sid, q, conf)
        root = DATA / sid
        # A single-file share serves ONE name. What is mounted is the folder
        # the file lives in (a file mount dies the moment anything replaces
        # the file), so this is the routing half of the lock — the other half
        # is the folder's ACL, which lets this account search but not read.
        only = conf.get("only")
        if only is None and conf.get("kind") in (None, "file") and conf.get("name") \
                and (DATA / sid / conf["name"]).is_file():
            only = conf["name"]             # a conf written before `only` existed
        if only:
            if not rest:
                rest = only
            elif rest != only:
                return self.fail()
        target = safe_join(root, rest)
        if target is None or not os.path.lexists(target):
            return self.fail()
        title = conf.get("title") or conf.get("name") or BRAND
        if target.is_dir():
            if conf.get("only"):            # cannot happen through routing; refuse anyway
                return self.fail()
            return self.listing(sid, token, root, target, title, conf)
        return self.one_file(sid, token, root, target, conf, title)

    def asset(self, rel: str):
        """The platform's own bundle, read-only and by suffix. No share, no
        token: these are the same files every browser on the app downloads."""
        target = safe_join(ASSETS, urllib.parse.unquote(rel))
        if target is None or not target.is_file():
            return self.fail()
        ctype = ASSET_TYPES.get(target.suffix.lower())
        if ctype is None:
            return self.fail()
        try:
            data = target.read_bytes()
        except OSError:
            return self.fail()
        # every URL carries ?v=<stamp>, so a year is honest
        self.send(200, data, ctype, extra=[("Cache-Control", "public, max-age=31536000, immutable")])

    def file_api(self, what: str, sid: str, q: dict, conf: dict):
        """`__raw` is the document's text, `__stat` is just its timestamp —
        the two halves of noticing that somebody else has been typing."""
        rel = (q.get("path") or [""])[0]
        if conf.get("only") and rel != conf["only"]:
            return self.fail()
        target = safe_join(DATA / sid, rel)
        if target is None or not target.is_file() or not rel.lower().endswith(EDITABLE_EXT):
            return self.fail()
        try:
            st = target.stat()
            if what == "__stat":
                # NANOSECONDS. A whole-second stamp cannot tell "saved" from
                # "somebody else saved in the same second", which silently
                # cost the page both of its jobs: it never noticed a change
                # made within a second of loading, and its conflict check
                # would have let one writer overwrite the other.
                body = json.dumps({"mtime": stamp(st), "size": st.st_size}).encode()
                return self.send(200, body, "application/json",
                                 extra=[("Cache-Control", "no-store")])
            text = target.read_text(errors="replace")[:2_000_000]
        except OSError:
            return self.fail(403, "Cannot read that")
        self.send(200, text.encode(), "text/plain; charset=utf-8",
                  extra=[("Cache-Control", "no-store"), ("X-KB-Mtime", str(stamp(st)))])

    def listing(self, sid, token, root, target, title, conf=None):
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
        self.send(200, page(title, body, str(rel) if str(rel) != "." else "", conf))

    def one_file(self, sid, token, root, target, conf, title):
        name = target.name
        ctype, _ = mimetypes.guess_type(name)
        editable = conf.get("mode") == "edit" and name.lower().endswith(EDITABLE_EXT)
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
            rel = str(target.relative_to(root))
            if editable:
                body += (f"<form method=post action='{base}/__save'>"
                         f"<input type=hidden name=path value='{html.escape(rel)}'>"
                         f"<input type=hidden name=mtime value='{stamp(target.stat())}'>"
                         f"<div class=bar><span class=muted>You can edit this.</span></div>"
                         f"<textarea name=text>{html.escape(text)}</textarea>"
                         f"<button>Save</button></form>")
            # A document opens in the platform's own editor — the same rendered
            # markdown, tables and checkboxes a colleague sees — with what is
            # above as the no-JavaScript fallback. If the bundle is not mounted
            # (an older install), that fallback IS the page.
            if name.lower().endswith(EDITABLE_EXT) and editor_available():
                return self.send(200, doc_page(conf, base, rel, text,
                                               stamp(target.stat()), body))
            return self.send(200, page(title, body, name, conf))
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
        raw = self.rfile.read(length).decode("utf-8", "replace")
        as_json = "json" in (self.headers.get("Content-Type") or "").lower()
        if as_json:
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self.fail(400, "Bad request")
            form = {k: [v if isinstance(v, str) else str(v)] for k, v in body.items()}
        else:
            form = urllib.parse.parse_qs(raw)
        if rest == "__unlock":
            key = f"{sid}:{self.address_string()}"
            if too_many(key):
                return self.fail(429, "Too many attempts. Try again in a minute.")
            note_attempt(key)
            if not password_ok(conf, (form.get("pw") or [""])[0]):
                return self.ask_password(sid, token, bad=True, conf=conf)
            self.send(303, b"", extra=[("Location", f"/s/{sid}/{token}/"),
                                       ("Set-Cookie", f"kbs_{sid}={cookie_for(sid)}; Path=/s/{sid}/; "
                                                      "HttpOnly; SameSite=Lax; Secure; Max-Age=86400")])
            return
        if rest == "__save":
            if conf.get("mode") != "edit" or not self.unlocked(sid, conf):
                return self.fail(403, "This link is read-only")
            rel = (form.get("path") or [""])[0]
            if conf.get("only") and rel != conf["only"]:
                return self.fail(403, "This link is one file")
            target = safe_join(DATA / sid, rel)
            if target is None or not target.is_file() or not rel.lower().endswith(EDITABLE_EXT):
                return self.fail(400, "Cannot write that")
            try:
                was = int((form.get("mtime") or ["0"])[0])
            except ValueError:
                was = 0
            if was and stamp(target.stat()) != was:
                if as_json:
                    return self.send(409, json.dumps({"error": "changed"}).encode(),
                                     "application/json")
                return self.fail(409, "Somebody else saved while you were writing. Reload and try again.")
            try:
                target.write_text((form.get("text") or [""])[0])
            except OSError as e:
                if as_json:
                    return self.send(403, json.dumps({"error": str(e)}).encode(),
                                     "application/json")
                return self.fail(403, f"Could not save: {e}")
            if as_json:
                st = target.stat()
                return self.send(200, json.dumps(
                    {"ok": True, "mtime": stamp(st), "size": st.st_size}).encode(),
                    "application/json", extra=[("Cache-Control", "no-store")])
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
