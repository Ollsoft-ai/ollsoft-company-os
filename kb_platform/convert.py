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
import json
import logging
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from watchfiles import awatch

from . import common
from .convert_extract import (MAX_SIDECAR_CHARS, XLSX_ROW_CAP,  # noqa: F401 (re-exported)
                              cap_table_rows)

logging.basicConfig(level=logging.INFO, format="convert %(message)s")
log = logging.getLogger("kb.convert")

VERSION = "1"

# Modern formats markitdown extracts well. Legacy binary formats (.doc/.ppt/
# .xls) get an explanatory stub instead of silence — re-saving as the modern
# format is a one-click fix on the author's side and beats shipping LibreOffice.
CONVERTIBLE = {".docx", ".pptx", ".xlsx", ".pdf"}
LEGACY = {".doc", ".ppt", ".xls"}

MAX_SOURCE_BYTES = 100 * 1024 * 1024     # refuse to parse monsters
STABLE_INTERVAL = 1.0                    # seconds between size/mtime probes
STABLE_TIMEOUT = 30.0                    # give up waiting and convert anyway (hash-guarded)
SWEEP_EVERY = 600.0                      # reconciliation period; also the inotify-drop net
# Wall clock per document. Generous because it is a runaway guard, not a
# quality bar: a 38 MB scanned manual legitimately takes pdfminer ~15 minutes,
# and the work happens once per file *version* (the sidecar carries the source
# hash), at Nice=10, in a child nobody waits on. Anything still going after
# half an hour is pathological and better recorded as such than left spinning.
EXTRACT_TIMEOUT = 1800.0

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


class ExtractFailed(Exception):
    """Extraction did not produce text. The message is operator-readable and
    goes verbatim into the sidecar body, so it must say what to do next."""


class Converter:
    def __init__(self):
        self.root = common.REPO_ROOT.resolve()
        self._md_note = ""                   # learned from the extraction child
        self._lock = threading.Lock()        # sweep and worker share convert_one
        self._queue: asyncio.Queue[Path] = asyncio.Queue()
        self._queued: set[Path] = set()
        self._sig: dict[Path, tuple] = {}    # source -> (size, mtime_ns, sha) hash cache

    # -- markitdown, at arm's length ------------------------------------------
    def _extract(self, src: Path) -> str:
        """Parse `src` in a child process and return its markdown.

        The parse never runs in this process. Document parsers allocate in
        proportion to decompressed content, so one fat spreadsheet can exceed
        the unit's MemoryMax — and an in-process parse turned that into the
        whole service being OOM-killed mid-sweep, restarted, and killed again
        on the same file forever (see kb_platform/convert_extract.py). Out here
        the child dies alone and we get to write down why.

        Raises ExtractFailed for every unhappy path; the caller records it as
        `status: failed` in a sidecar stamped with the source hash, so a bad
        document is attempted once per version, not once per sweep.
        """
        fd, tmp_out = tempfile.mkstemp(prefix="kb-extract-", suffix=".md")
        os.close(fd)
        # The child imports this package; inherit our resolved sys.path so it
        # works from the service venv, a dev checkout and pytest alike.
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        try:
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "kb_platform.convert_extract",
                     str(src), tmp_out],
                    capture_output=True, text=True, env=env, timeout=EXTRACT_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise ExtractFailed(
                    f"Extraction exceeded the {EXTRACT_TIMEOUT / 60:.0f}-minute time "
                    f"budget and was stopped. The document is likely pathological "
                    f"(huge embedded tables or images) — convert it by hand if the "
                    f"text matters.") from None

            if r.returncode != 0:
                # Learn the converter even from a failure, so the sidecar blames
                # the library that actually gave up.
                self._md_note = self._child_json(r.stderr).get("note") or self._md_note
                raise ExtractFailed(self._describe_child_failure(r))

            self._md_note = self._child_json(r.stdout).get("note") or self._md_note
            with open(tmp_out, errors="replace") as f:
                return f.read()
        finally:
            try:
                os.unlink(tmp_out)
            except OSError:
                pass

    @staticmethod
    def _child_json(stream: str) -> dict:
        """The child's last JSON line. Anything else on the stream (a library
        printing a warning to stderr, a parser being chatty) is ignored."""
        for line in reversed((stream or "").strip().splitlines()):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
        return {}

    def _describe_child_failure(self, r: subprocess.CompletedProcess) -> str:
        """Turn the child's exit into a sentence worth reading in a sidecar."""
        detail = self._child_json(r.stderr).get("error", "")
        # SIGKILL with no message is the cgroup OOM killer: nothing runs in the
        # child after the kernel reaps it, so this is the only evidence there is.
        if r.returncode in (-9, 137) or r.returncode == 3:
            return ("Extraction ran out of memory and was stopped. The document's "
                    "text form is larger than the conversion memory budget "
                    "(kb-convert MemoryMax) — a very large spreadsheet or a PDF "
                    "with enormous embedded tables. The original file is intact "
                    "and untouched; only this machine-readable copy is missing.")
        if r.returncode < 0:
            return f"Extraction was killed by signal {-r.returncode}."
        return f"Conversion failed: {detail or f'extractor exited {r.returncode}'}"

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
            # Say which file we are about to parse BEFORE parsing it. If the
            # attempt ends in a way Python cannot report — a SIGKILL, a box
            # reboot — this line is the only record of what was in flight, and
            # its absence is what made the OOM loop take a subpoena to diagnose.
            rel_pre = src.relative_to(self.root) if src.is_relative_to(self.root) else src
            log.info("extracting  %s (%.1f MB)", rel_pre, st.st_size / 1e6)
            try:
                text = self._extract(src)   # child process; shapes/caps the text
                note = self._md_note
                if not text.strip():
                    status = "empty"
                    body = ("No extractable text found."
                            + (" A scanned PDF has no text layer — it needs OCR, which "
                               "kb-convert does not do (yet)." if suffix == ".pdf" else ""))
                else:
                    status, body = "ok", text
            except ExtractFailed as e:
                status, body = "failed", str(e)
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
