"""kb-convert — shadows office/PDF binaries with hidden, read-only markdown.

Agents and search cannot see inside a .docx/.pptx/.xlsx/.pdf: file tools read
text, ripgrep reads text, and the indexer parses only markdown. So this
service keeps a machine-extracted sidecar next to every such binary —
`report.docx` -> `.report.docx.md` — in the same directory, dot-prefixed:

  * same directory  => the kernel answers "who can read this?" identically for
    sidecar and source (ancestor traversal is shared; the file ACL is cloned
    from the source and stripped to read-only);
  * dot prefix      => the tree and quick-open hide it (the frontend already
    filters dot-entries), while the index and content search still surface it
    — the indexer makes exactly one exception for these
    (common.is_derived_sidecar).

Sidecars are owned by the service user with no group/other write bit, so for
every human and agent they are read-only (kernel-enforced; syncd's
write-permission check then serves them as live read-only documents).

Runs as `kbindexer`, like the indexer, and for the same reason: that user sits
in the content groups, so exactly what can be indexed can be converted — one
service account to remember when granting a project group. No database access.
Everything it writes is derived: delete every sidecar and restart the service,
and identical files come back (modulo the conversion timestamp).
"""
from __future__ import annotations

import asyncio
import grp
import hashlib
import logging
import os
import pwd
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from watchfiles import awatch

from . import common

logging.basicConfig(level=logging.INFO, format="convert %(message)s")
log = logging.getLogger("kb.convert")

VERSION = "1"

# Modern formats markitdown extracts well. Legacy binary formats (.doc/.ppt/
# .xls) get an explanatory stub instead of silence — re-saving as the modern
# format is a one-click fix on the author's side and beats shipping LibreOffice.
CONVERTIBLE = {".docx", ".pptx", ".xlsx", ".pdf"}
LEGACY = {".doc", ".ppt", ".xls"}

MAX_SOURCE_BYTES = 100 * 1024 * 1024     # refuse to parse monsters
MAX_SIDECAR_CHARS = 2_000_000            # cap derived text (the index parses every line)
XLSX_ROW_CAP = 500                       # per sheet; big data belongs in Postgres
STABLE_INTERVAL = 1.0                    # seconds between size/mtime probes
STABLE_TIMEOUT = 30.0                    # give up waiting and convert anyway (hash-guarded)
SWEEP_EVERY = 600.0                      # reconciliation period; also the inotify-drop net

# Editor/agent litter that must never trigger (or receive) a conversion:
# Office lock files, atomic-write temps, LibreOffice locks.
_SKIP_PREFIXES = ("~$", ".~lock")
_SKIP_SUFFIXES = (".tmp", ".kbtmp", ".crdownload", ".partial")


def sidecar_path(src: Path) -> Path:
    return src.parent / f".{src.name}.md"


def source_name_of(sidecar_name: str) -> str:
    """`.report.docx.md` -> `report.docx` (caller checks is_derived_sidecar)."""
    return sidecar_name[1:-3]


def is_litter(name: str) -> bool:
    return name.startswith(_SKIP_PREFIXES) or name.lower().endswith(_SKIP_SUFFIXES)


def cap_table_rows(text: str, cap: int = XLSX_ROW_CAP) -> str:
    """Trim each markdown table to `cap` rows. Spreadsheets are the one format
    whose text form can explode; a note marks the cut so nobody mistakes the
    excerpt for the whole sheet."""
    out, run = [], 0
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            run += 1
            if run == cap + 1:
                out.append(f"| … remaining rows omitted (sidecar caps tables at {cap} rows; "
                           f"query the full sheet from the original file) … |")
            if run > cap:
                continue
        else:
            run = 0
        out.append(line)
    return "\n".join(out)


def compose_sidecar(source_name: str, sha: str, status: str, body: str,
                    converter_note: str = "") -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "---\n"
        f"derived_from: {source_name}\n"
        f"source_sha256: {sha}\n"
        f"converted_at: {ts}\n"
        f"converter: kb-convert/{VERSION}{converter_note}\n"
        f"status: {status}\n"
        "---\n\n"
        f"> Machine-extracted text of `{source_name}`. Read-only — kb-convert "
        "regenerates this file whenever the source changes.\n\n"
        f"{body}\n"
    )


def parse_sidecar_hash(text: str) -> str | None:
    """Pull source_sha256 out of a sidecar's frontmatter (head of file only)."""
    for i, line in enumerate(text.splitlines()[:10]):
        if line.startswith("source_sha256:"):
            return line.split(":", 1)[1].strip() or None
        if line == "---" and i > 0:      # closing fence: no hash in this file
            break
    return None


def parse_getfacl(out: str) -> tuple[str | None, str | None, dict, dict]:
    """(group_perm, mask, named_users, named_groups) from `getfacl -cE` output.
    Same parsing shape as the indexer's acl_info — the two must agree on what
    "who can read this?" means."""
    group_perm = mask = None
    named_u: dict[str, str] = {}
    named_g: dict[str, str] = {}
    for raw in out.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != 3:
            continue
        typ, name, perms = parts
        if typ == "group" and name == "":
            group_perm = perms
        elif typ == "mask":
            mask = perms
        elif typ == "user" and name:
            named_u[name] = perms
        elif typ == "group" and name:
            named_g[name] = perms
    return group_perm, mask, named_u, named_g


def build_acl_spec(owner: str, group: str, grp_read: bool, oth_read: bool,
                   named_users: list[str], named_groups: list[str]) -> str:
    """The sidecar's whole permission model in one setfacl --set spec:
    owner (the service user) may rewrite it, the source's readers may read it,
    nobody may write it. g:: stays empty so the setgid directory's group grants
    nothing the source didn't."""
    parts = ["u::rw-", "g::---", f"o::{'r--' if oth_read else '---'}", "m::r--",
             f"u:{owner}:r--"]
    if grp_read:
        parts.append(f"g:{group}:r--")
    parts += [f"u:{n}:r--" for n in named_users if n != owner]
    parts += [f"g:{n}:r--" for n in named_groups if not (grp_read and n == group)]
    return ",".join(parts)


def source_acl_spec(src: Path) -> str:
    """Read the source's effective READ audience (owner + group-if-readable +
    named ACL readers + world-if-readable) and express it as a read-only ACL
    for the sidecar."""
    st = src.stat()
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    mode = stat.S_IMODE(st.st_mode)
    oth_read = bool(mode & 0o004)
    try:
        out = subprocess.run(["getfacl", "-cE", "--absolute-names", "--", str(src)],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    group_perm, mask, named_u, named_g = parse_getfacl(out)
    if mask is None:
        # no extended ACL: st_mode's group bits are the truth
        grp_read = bool(mode & 0o040)
        nu, ng = [], []
    else:
        mr = "r" in mask
        grp_read = bool(group_perm and "r" in group_perm and mr)
        nu = sorted(n for n, pm in named_u.items() if "r" in pm and mr)
        ng = sorted(n for n, pm in named_g.items() if "r" in pm and mr)
    return build_acl_spec(owner, group, grp_read, oth_read, nu, ng)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Converter:
    def __init__(self):
        self.root = common.REPO_ROOT.resolve()
        self._md = None                      # lazy MarkItDown instance
        self._md_note = ""
        self._lock = threading.Lock()        # sweep and worker share convert_one
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._queued: set[Path] = set()
        self._sig: dict[Path, tuple] = {}    # source -> (size, mtime_ns, sha) hash cache

    # -- markitdown -----------------------------------------------------------
    def _extract(self, src: Path) -> str:
        if self._md is None:
            from markitdown import MarkItDown   # heavy import, once, on demand
            self._md = MarkItDown(enable_plugins=False)
            try:
                from importlib.metadata import version
                self._md_note = f" (markitdown {version('markitdown')})"
            except Exception:
                self._md_note = " (markitdown)"
        res = self._md.convert(str(src))
        return getattr(res, "markdown", None) or getattr(res, "text_content", "") or ""

    # -- the one place a sidecar is written ------------------------------------
    def convert_one(self, src: Path) -> str | None:
        """Create/refresh the sidecar for `src`. Returns the status written, or
        None if the sidecar was already current (or src vanished/is unreadable).
        Safe to call repeatedly — the source hash makes it idempotent."""
        with self._lock:
            return self._convert_locked(src)

    def _convert_locked(self, src: Path) -> str | None:
        try:
            st = src.lstat()
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):     # symlinks/dirs never convert
            return None
        side = sidecar_path(src)

        # cheap staleness gate: stat signature -> cached hash -> sidecar header
        sig_key = (st.st_size, st.st_mtime_ns)
        cached = self._sig.get(src)
        if cached and cached[:2] == sig_key:
            sha = cached[2]
        else:
            try:
                sha = sha256_file(src)
            except OSError:
                return None                   # unreadable for us: kernel's call
            self._sig[src] = (*sig_key, sha)
        try:
            with open(side, errors="replace") as f:
                if parse_sidecar_hash(f.read(600)) == sha:
                    return None               # sidecar already matches this source
        except OSError:
            pass

        suffix = src.suffix.lower()
        note = self._md_note
        t0 = time.monotonic()
        if st.st_size > MAX_SOURCE_BYTES:
            status = "too-large"
            body = (f"Source is {st.st_size / 1e6:.0f} MB — above the "
                    f"{MAX_SOURCE_BYTES // (1024 * 1024)} MB conversion cap.")
        elif suffix in LEGACY:
            status = "unsupported"
            body = (f"Legacy format `{suffix}` is not converted. Re-save the file as "
                    f"`{suffix}x` (File → Save As in Office) and the text will appear here.")
        else:
            try:
                text = self._extract(src)
                note = self._md_note
                if suffix == ".xlsx":
                    text = cap_table_rows(text)
                if len(text) > MAX_SIDECAR_CHARS:
                    text = (text[:MAX_SIDECAR_CHARS]
                            + "\n\n> ⚠ Truncated — the extracted text exceeds the "
                              "2,000,000-character sidecar cap.")
                if not text.strip():
                    status = "empty"
                    body = ("No extractable text found."
                            + (" A scanned PDF has no text layer — it needs OCR, which "
                               "kb-convert does not do (yet)." if suffix == ".pdf" else ""))
                else:
                    status, body = "ok", text
            except Exception as e:  # a corrupt upload must not kill the service
                status = "failed"
                body = f"Conversion failed: {type(e).__name__}: {str(e)[:500]}"

        rel = src.relative_to(self.root) if src.is_relative_to(self.root) else src
        try:
            spec = source_acl_spec(src)
            tmp = side.with_name(side.name + ".kbtmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(compose_sidecar(src.name, sha, status, body, note))
            except Exception:
                tmp.unlink(missing_ok=True)
                raise
            r = subprocess.run(["setfacl", "--set", spec, "--", str(tmp)],
                               capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                # fail CLOSED: better an owner-only sidecar than an over-shared one
                os.chmod(tmp, 0o600)
                log.warning("setfacl failed for %s (%s) — sidecar left owner-only",
                            rel, r.stderr.strip())
            os.replace(tmp, side)
        except OSError as e:
            log.warning("cannot write sidecar for %s: %s", rel, e)
            return None
        log.info("%-11s %s (%.1fs)", status, rel, time.monotonic() - t0)
        return status

    def remove_sidecar_for(self, src: Path):
        self._sig.pop(src, None)
        try:
            sidecar_path(src).unlink()
            log.info("removed     %s", sidecar_path(src).relative_to(self.root))
        except OSError:
            pass

    # -- reconciliation sweep ---------------------------------------------------
    def sweep(self):
        """Walk the whole tree: convert anything missing/stale, delete orphaned
        sidecars. Runs at boot and periodically — like the indexer's fallback
        rescan, this is what makes dropped inotify events invisible."""
        for dirpath, dirnames, filenames in os.walk(self.root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "_secrets"]
            names = set(filenames)
            for fn in filenames:
                p = Path(dirpath) / fn
                if is_litter(fn):
                    continue
                if not fn.startswith(".") and p.suffix.lower() in CONVERTIBLE | LEGACY:
                    try:
                        self.convert_one(p)
                    except Exception:
                        log.exception("sweep: %s", p)
                elif common.is_derived_sidecar(fn) and source_name_of(fn) not in names:
                    try:
                        p.unlink()            # orphan: its source is gone
                        log.info("removed     %s (orphan)", p.relative_to(self.root))
                    except OSError:
                        pass

    async def sweep_loop(self):
        while True:
            await asyncio.sleep(SWEEP_EVERY)
            try:
                await asyncio.to_thread(self.sweep)
            except Exception:
                log.exception("sweep loop")

    # -- event handling -----------------------------------------------------------
    async def wait_stable(self, p: Path):
        """Hold off until size+mtime stop moving: Word saves via a temp-file
        dance and network-drive writes land in chunks — converting a
        half-written file wastes a cycle and logs a scary 'failed'."""
        last, deadline = None, time.monotonic() + STABLE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                st = p.stat()
            except OSError:
                return
            sig = (st.st_size, st.st_mtime_ns)
            if sig == last:
                return
            last = sig
            await asyncio.sleep(STABLE_INTERVAL)

    async def worker(self):
        while True:
            p = await self._queue.get()
            self._queued.discard(p)
            await self.wait_stable(p)
            try:
                await asyncio.to_thread(self.convert_one, p)
            except Exception:
                log.exception("convert: %s", p)

    def _enqueue(self, p: Path):
        if p not in self._queued:
            self._queued.add(p)
            self._queue.put_nowait(p)

    async def watch_loop(self):
        async for changes in awatch(self.root, recursive=True,
                                    ignore_permission_denied=True):
            for _change, fspath in changes:
                p = Path(fspath)
                name = p.name
                if is_litter(name):
                    continue
                try:
                    rel = str(p.resolve().relative_to(self.root))
                except (ValueError, OSError):
                    continue
                if common.is_secret_path(rel):
                    continue
                if any(seg.startswith(".") for seg in rel.split("/")[:-1]):
                    continue                  # .claude/.git machinery
                if not name.startswith(".") and p.suffix.lower() in CONVERTIBLE | LEGACY:
                    if p.exists():
                        self._enqueue(p)
                    else:
                        self.remove_sidecar_for(p)
                elif common.is_derived_sidecar(name) and not p.exists():
                    src = p.parent / source_name_of(name)
                    if src.exists():
                        self._enqueue(src)    # someone deleted the sidecar: regrow it

    async def run(self):
        log.info("boot sweep of %s", self.root)
        await asyncio.to_thread(self.sweep)
        log.info("watching")
        await asyncio.gather(self.watch_loop(), self.worker(), self.sweep_loop())


def main():
    asyncio.run(Converter().run())


if __name__ == "__main__":
    main()
