"""Drive an arbitrary artifact against the real running platform.

Why this exists
---------------
An artifact's only capability is the postMessage bridge, and the host half of
that bridge lives in `frontend/src/app.js`. Any test harness that reimplements
it — the CSP header, `inArtifactScope`, the read/write/list/mkdir/delete verbs —
is a second copy that drifts, and the day it drifts is the day it stops catching
the bug you wrote it for.

So this harness reimplements nothing. It seeds an artifact into the run's own
disposable namespace and opens it in the deployed app, exactly the way a person
would: real `ARTIFACT_CSP`, real `sandbox="allow-scripts"` frame, real bridge,
real per-viewer permissions. If the platform tightens any of that, tests written
against this harness feel it immediately and for free.

Usage
-----
    from artifact_harness import place_artifact, open_artifact, watch_console

    def test_my_dashboard(browser):
        ctx = browser.new_context()
        page = login(ctx, "alice")
        errs = watch_console(page)
        path = place_artifact("dashboards/mine.html", html=MY_HTML)
        f = open_artifact(page, path)
        f.locator("#out").wait_for()
        assert "rows=3" in f.locator("#out").inner_text()
        assert not errs, errs
        ctx.close()

`place_artifact` also takes `src_dir=` to copy a whole workspace — an artifact
plus the data files it reads — which is what a tool-shaped artifact needs.

Everything it writes lands under this run's namespace, so the suite's own
teardown removes it; nothing here touches real company content.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from kbenv import AREA, doc, full, home

__all__ = ["place_artifact", "open_artifact", "watch_console", "artifact_frame_for"]

ARTIFACT_FRAME = "iframe.artifact-frame"


def place_artifact(rel_dest: str, html: str | None = None,
                   src_dir: str | Path | None = None, owner: str | None = None,
                   viewer: str | None = None) -> str:
    """Put an artifact (and optionally its whole folder) into this run's namespace.

    `rel_dest` is relative to the run's company area, e.g. "dashboards/mine.html".
    Pass `owner` (a logical user like "alice") to place it in that user's private
    area instead, which is the right place for anything permission-sensitive.

    Pass `viewer` (a logical user) when the artifact WRITES. The suite runs as
    krystof, so a placed file is owned by krystof and readable-but-not-writable
    by anyone else — the artifact loads, and every save comes back 403. Naming
    the viewer chowns the tree to them, which is the state a real artifact in
    that person's own area is in.

    Exactly one of `html` or `src_dir` must be given. With `src_dir`, the whole
    directory is copied and `rel_dest` must name the .html inside it, so an
    artifact that reads neighbouring files is tested with those files present.

    Returns the repo-relative path to hand to `open_artifact`.
    """
    if (html is None) == (src_dir is None):
        raise ValueError("pass exactly one of html= or src_dir=")

    repo_path = home(owner, rel_dest) if owner else doc(rel_dest)
    dest = full(repo_path)

    if src_dir is not None:
        src = Path(src_dir)
        if not (src / Path(rel_dest).name).is_file():
            raise FileNotFoundError(f"{src / Path(rel_dest).name} — rel_dest must name the .html inside src_dir")
        # The artifact's folder IS its permission scope, so copy the tree whole:
        # a partial copy would test a scope the real thing never has.
        shutil.copytree(src, dest.parent, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html, encoding="utf-8")

    # Placement has to make the tree readable by the VIEWER, and that is not the
    # same as chmod. A source folder may carry ACLs (a private area usually
    # does), copytree brings them along, and chmod on an ACL'd file only moves
    # the mask — the named entries and `default:other::---` survive. The result
    # is an artifact that exists, has sane-looking mode bits, and is invisible:
    # /api/tree skips any directory the viewer cannot read+execute, silently.
    top = dest.parent if src_dir else dest
    _open_up(top)
    if viewer:
        from kbenv import U
        subprocess.run(["sudo", "-n", "chown", "-R", f"{U(viewer)}:", str(top)],
                       check=False, capture_output=True)
    return repo_path


def _open_up(top: Path) -> None:
    """Strip inherited ACLs and set plain, readable modes across the tree."""
    if shutil.which("setfacl"):
        subprocess.run(["setfacl", "-R", "-b", str(top)],
                       check=False, capture_output=True)
    targets = [top] + (list(top.rglob("*")) if top.is_dir() else [])
    for p in targets:
        try:
            p.chmod(0o755 if p.is_dir() else 0o644)
        except OSError:
            pass


def open_artifact(page, repo_path: str, timeout: int = 15000):
    """Click the artifact open in the tree and return a FrameLocator for it.

    Uses the app's own tree and its own frame, so the artifact is rendered by
    the same code path a person triggers — including the CSP header and the
    sandbox attribute, which are the two things a hand-rolled harness gets wrong.

    The tree is a filesystem walk performed as the viewer, and `/api/tree`
    SKIPS any directory the viewer cannot read+execute — silently, with no error
    anywhere. So an artifact that never appears is almost always a permission
    problem on some folder in its path, not a UI fault.
    """
    from conftest import expand_folder, wait_path

    sel = f'.tree-item[data-path="{repo_path}"]'
    parts = repo_path.split("/")

    def reveal() -> bool:
        # Each ancestor has to be expanded before the next is in the DOM at all.
        for i in range(1, len(parts)):
            ancestor = "/".join(parts[:i])
            if page.locator(f'.tree-item[data-path="{ancestor}"]').count():
                try:
                    expand_folder(page, ancestor)
                except Exception:
                    pass
        return page.locator(sel).count() > 0

    deadline = time.monotonic() + max(timeout, 1000) / 1000.0
    while not reveal():
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{repo_path} never appeared in the tree.\n"
                "/api/tree walks the filesystem AS THE VIEWER and skips any directory it cannot\n"
                "read+execute, without reporting anything. Check the whole path as that user:\n"
                f"  getfacl -cpE {full(repo_path).parent}\n"
                f"  sudo -u <viewer> ls {full(repo_path).parent}\n"
                "Note that chmod on an ACL'd file only moves the mask; use `setfacl -b` to clear\n"
                "inherited entries. place_artifact() does this for you."
            )
        page.wait_for_timeout(1000)
        page.reload()
        page.wait_for_selector('[data-testid="tree"] .tree-item', timeout=timeout)

    page.click(sel)
    wait_path(page, repo_path, timeout=timeout)
    page.wait_for_selector(ARTIFACT_FRAME, timeout=timeout)
    return page.frame_locator(ARTIFACT_FRAME)


def artifact_frame_for(page):
    """The currently open artifact's frame, for tests that opened it themselves."""
    return page.frame_locator(ARTIFACT_FRAME)


def watch_console(page, ignore: tuple[str, ...] = ()) -> list[str]:
    """Collect console errors and CSP violations from the page and its frames.

    Returns a list that fills as the test runs — assert it is empty at the end.
    A blocked resource is reported by the browser as a console error, so this is
    how a test notices that an artifact reached for something the sandbox
    forbids (a CDN script, a font, a fetch) instead of silently degrading.
    """
    found: list[str] = []

    def note(text: str) -> None:
        if any(pat in text for pat in ignore):
            return
        found.append(text)

    page.on("console", lambda m: note(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: note(f"pageerror: {e}"))
    return found
