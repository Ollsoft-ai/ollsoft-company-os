"""kb-convert's extraction child — one document, one process, then exit.

Why a whole process for one `md.convert()` call: the document parsers allocate
in proportion to the *decompressed* content, not the file on disk. A 54 MB
xlsx of incident rows expands past 1.9 GB of Python objects, which is over
kb-convert.service's MemoryMax — so the cgroup OOM killer fired, and because
the parse ran in-process it took the whole service with it. systemd restarted
it, the boot sweep walked the tree in the same order, reached the same file,
and died again: a 15-minute crash loop that also meant every convertible file
*after* the poison one in walk order never got a sidecar.

Isolating the parse fixes both halves:

  * the child is the biggest thing in the cgroup and pins its own
    oom_score_adj to the maximum, so when the limit is hit the kernel reaps
    *it* and the parent — a few tens of MB of asyncio — keeps running;
  * the parent turns that death into `status: failed` in the sidecar, which
    carries the source hash, so the file is never retried until it changes.
    One bad document costs one conversion attempt, not the service.

This needs `OOMPolicy=continue` on the unit: systemd's default (`stop`) treats
*any* process in the cgroup being OOM-killed as the unit failing, which would
defeat the isolation.

Big text never crosses the pipe. The extracted markdown goes to a file the
parent names, already row-capped and truncated, so neither side can be blown
up by the output; stdout carries a single JSON line of metadata.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_SIDECAR_CHARS = 2_000_000            # cap derived text (the index parses every line)
XLSX_ROW_CAP = 500                       # per sheet; big data belongs in Postgres
XLSX_COL_CAP = 40                        # a sidecar is for orientation, not for pivoting
XLSX_CHAR_BUDGET = 120_000               # stop *reading* here — see extract_xlsx

TRUNCATION_NOTE = ("\n\n> ⚠ Truncated — the extracted text exceeds the "
                   "2,000,000-character sidecar cap.")


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


def _cell(v) -> str:
    """One spreadsheet cell as table-safe markdown text."""
    if v is None:
        return ""
    return str(v).replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


def extract_xlsx(src: Path) -> str:
    """Read a spreadsheet lazily and stop early — never load the whole book.

    This is the guardrail that matters for the format that caused the outage.
    Handing an xlsx to markitdown materialises *every* row of *every* sheet as
    Python objects before anything gets capped, so a 54 MB incident export
    became ~1.9 GB of RAM and took the service down. Trimming the markdown
    afterwards was always too late: the damage is done during the parse.

    So the cap moves to the read itself. openpyxl's read_only mode is a
    streaming (SAX) reader, so `iter_rows` pulls a row at a time and we simply
    stop — at XLSX_ROW_CAP rows per sheet, XLSX_COL_CAP columns, or
    XLSX_CHAR_BUDGET characters overall, whichever comes first. Peak memory is
    a function of the cap, not of the file, so a 5 MB sheet and a 500 MB sheet
    cost the same. Nobody ever needed the whole table in a sidecar: it exists
    so search and agents know the file's shape and vocabulary, and the original
    is one click away for the actual numbers.

    Two caps rather than one because sheets fail in two directions: a narrow
    sheet can afford all 500 rows, while a 60-column CRM export spends the
    whole character budget in a few dozen. Whichever binds first, binds.
    """
    import openpyxl

    # A file OBJECT, not the path: openpyxl picks its reader from the
    # filename's extension, and the parent hands us a /proc/self/fd/N pin
    # (no extension) so the document cannot be swapped mid-parse. Reading the
    # handle decides the format from the zip content instead, which is the
    # more honest test anyway.
    fh = open(src, "rb")
    wb = openpyxl.load_workbook(fh, read_only=True, data_only=True)
    out: list[str] = []
    used = 0
    stopped_early = False
    try:
        for ws in wb.worksheets:
            out.append(f"\n## {ws.title}\n")
            rows = 0
            for row in ws.iter_rows(values_only=True):
                if rows >= XLSX_ROW_CAP or used >= XLSX_CHAR_BUDGET:
                    stopped_early = True
                    out.append(f"\n> … stopped after {rows} rows — a sidecar samples a "
                               f"sheet, it does not copy it. Open the original file (or "
                               f"load it into Postgres) for the full data.\n")
                    break
                cells = [_cell(v) for v in row[:XLSX_COL_CAP]]
                while cells and not cells[-1]:
                    cells.pop()                    # trailing empties carry nothing
                if not cells:
                    continue                       # skip blank rows, don't spend budget
                line = "| " + " | ".join(cells) + " |"
                out.append(line)
                if rows == 0:                      # first non-empty row heads the table
                    out.append("|" + "---|" * len(cells))
                used += len(line)
                rows += 1
            if rows == 0:
                out.append("_(empty sheet)_")
    finally:
        wb.close()                                 # releases the streaming file handles
        fh.close()

    if stopped_early:
        out.append(f"\n> ⚠ This sheet sample is capped at {XLSX_ROW_CAP} rows per sheet "
                   f"and {XLSX_COL_CAP} columns.")
    return "\n".join(out)


def shape_text(text: str, suffix: str) -> tuple[str, bool]:
    """Last bounding pass before the text leaves this process. Returns
    (text, truncated). Done here rather than in the parent so the handoff file
    is bounded before it is ever written.

    `.xlsx` is exempt from the row cap because `extract_xlsx` already stopped
    reading at the cap — re-trimming bounded output would only mangle the note
    explaining the cut. The other formats come from markitdown, which returns
    everything it found, so a docx or PDF with a monstrous embedded table still
    needs the row cap here."""
    if suffix != ".xlsx":
        text = cap_table_rows(text)
    if len(text) > MAX_SIDECAR_CHARS:
        return text[:MAX_SIDECAR_CHARS] + TRUNCATION_NOTE, True
    return text, False


def _volunteer_for_the_oom_killer():
    """Make this process the cgroup's preferred OOM victim.

    Under memory pressure the kernel picks the highest oom_score in the memcg,
    which is already this process (it holds the parse), but saying so
    explicitly removes the doubt — the parent must survive to record *why* the
    conversion failed. Unprivileged processes may only raise this value, so it
    is safe and needs no capabilities."""
    try:
        Path("/proc/self/oom_score_adj").write_text("1000\n")
    except OSError:
        pass                              # advisory only; the cgroup still bounds us


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("usage: python -m kb_platform.convert_extract <source> <out-file> "
              "[<original-name>]",
              file=sys.stderr)
        return 64
    src, out = Path(argv[1]), Path(argv[2])
    _volunteer_for_the_oom_killer()

    # argv[3] is the document's real NAME, given when argv[1] is a
    # /proc/self/fd/N pin. The parent opens the source once, O_NOFOLLOW, and
    # hands us that fd so a swap between its check and our open cannot
    # redirect us at another file (convert.py:_convert_locked). A pin has no
    # extension, and every parser below dispatches on the suffix.
    suffix = Path(argv[3] if len(argv) == 4 else src).suffix.lower()
    note = ""
    try:
        if suffix == ".xlsx":
            # Bounded by construction — never hand a spreadsheet to markitdown.
            import openpyxl
            note = f" (openpyxl {openpyxl.__version__}, streamed)"
            text = extract_xlsx(src)
        else:
            from markitdown import MarkItDown  # heavy import, hence the lazy child
            try:
                from importlib.metadata import version
                note = f" (markitdown {version('markitdown')})"
            except Exception:
                note = " (markitdown)"
            res = MarkItDown(enable_plugins=False).convert(str(src))
            text = getattr(res, "markdown", None) or getattr(res, "text_content", "") or ""
    except MemoryError:
        # Allocation refused cleanly instead of being reaped — same meaning.
        print(json.dumps({"status": "oom", "note": note}), file=sys.stderr)
        return 3
    except BaseException as e:
        # A corrupt upload, an unsupported variant, a parser assertion: all the
        # parent needs is a short reason to put in the sidecar. `note` rides
        # along even here so a failed sidecar names the library that actually
        # gave up, not whichever one the previous file happened to use.
        print(json.dumps({"status": "error", "note": note,
                          "error": f"{type(e).__name__}: {str(e)[:500]}"}), file=sys.stderr)
        return 2

    text, truncated = shape_text(text, suffix)
    try:
        out.write_text(text, encoding="utf-8", errors="replace")
    except OSError as e:
        print(json.dumps({"status": "error", "error": f"cannot write handoff file: {e}"}),
              file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", "note": note,
                      "chars": len(text), "truncated": truncated}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
