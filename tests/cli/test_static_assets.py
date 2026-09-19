"""Static assets are cached for a year, and that is safe only because every
one of them is a fresh URL on every build.

  * /static/* (not the HTML shells) → public, max-age=31536000, immutable;
  * / and /login → no-store, so the stamps they carry are always current;
  * the built files agree with each other: the fonts app.html preloads are the
    exact strings style.css asks for (a preload of a different URL is a second
    download, not a saved one), the modulepreload is the script's own src, and
    every stamp in both files is the one build.
"""
import re

import httpx
from kbenv import BASE

from kb_platform.hub import STATIC_DIR


def test_stamped_assets_are_immutable_and_shells_are_not():
    c = httpx.Client(base_url=BASE, timeout=15)
    for path in ("/static/app.js", "/static/style.css",
                 "/static/fonts/ibm-plex-sans-latin-400-normal.woff2"):
        r = c.get(path)
        assert r.status_code == 200, path
        cc = r.headers.get("cache-control", "")
        assert "immutable" in cc and "max-age=31536000" in cc, f"{path}: {cc!r}"
    r = c.get("/static/app.html")
    assert "immutable" not in r.headers.get("cache-control", ""), \
        "the shell carries the stamps; caching it pins every old one"
    assert "no-store" in c.get("/login").headers.get("cache-control", "")


def test_built_html_and_css_agree_on_every_url():
    html = (STATIC_DIR / "app.html").read_text()
    css = (STATIC_DIR / "style.css").read_text()
    stamps = set(re.findall(r"\?v=(\d+)", html)) | set(re.findall(r"\?v=(\d+)", css))
    assert len(stamps) == 1, f"more than one build's stamp in play: {stamps}"
    assert "0" not in stamps, "the placeholder stamp shipped — build.mjs did not run"
    css_fonts = set(re.findall(r'url\("(/static/fonts/[^"]+)"\)', css))
    assert css_fonts, "no font urls in the built css"
    preloads = re.findall(r'<link rel="preload" as="font"[^>]*href="([^"]+)"', html)
    assert preloads, "no font preloads in the built html"
    for href in preloads:
        assert href in css_fonts, f"preload {href} is not a url style.css will request"
        assert 'crossorigin' in re.search(r'<link[^>]*href="%s"' % re.escape(href), html).group(0)
    mp = re.search(r'<link rel="modulepreload" href="([^"]+)"', html)
    src = re.search(r'<script type="module" src="([^"]+)"', html)
    assert mp and src and mp.group(1) == src.group(1), (mp and mp.group(1), src and src.group(1))
