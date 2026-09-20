"""The stylesheet is themeable because it names no colours: every value is a
token from :root or a color-mix() of one, and a theme is a [data-theme] block
that redefines the tokens. This guard keeps it that way — the next hardcoded
#hex or rgba() outside the token blocks fails here, not in a light-theme bug
report. Pure text, no server."""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "frontend" / "assets" / "style.css"
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")
ALLOWED = {
    "the artifact's own page",      # .artifact-frame: the iframe content is not our chrome
}


def _blocks(css: str):
    """(selector, body) for every top-level rule (nested @media bodies are
    flattened so their rules count too)."""
    out, depth, sel, body, i = [], 0, "", "", 0
    buf = ""
    for ch in css:
        if ch == "{":
            depth += 1
            if depth == 1:
                sel, buf = buf.strip(), ""
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                out.append((sel, buf)); buf = ""
                continue
        buf += ch
    return out


def test_only_the_token_blocks_name_colours():
    css = CSS.read_text()
    css = re.sub(r"/\*.*?\*/", lambda m: "" if "the artifact's own page" not in m.group(0) else m.group(0), css, flags=re.S)
    bad = []
    for sel, body in _blocks(css):
        if sel == ":root" or sel.startswith(":root[data-theme="):
            continue
        for line in body.splitlines():
            if COLOUR.search(line) and not any(a in line for a in ALLOWED):
                bad.append(f"{sel[:60]} :: {line.strip()[:100]}")
    assert not bad, "hardcoded colours outside the token blocks:\n  " + "\n  ".join(bad)


def test_every_theme_redefines_the_same_tokens():
    """A theme that forgets a token silently inherits deep blue's value — a
    blue shadow on paper. Each theme block must set every colour token :root
    defines (fonts, radii and the topbar height are not colours)."""
    css = CSS.read_text()
    blocks = dict(_blocks(re.sub(r"/\*.*?\*/", "", css, flags=re.S)))
    root = set(re.findall(r"--([a-z][a-z0-9-]*)\s*:", blocks[":root"]))
    non_colour = {"sans", "mono", "r", "tbh", "accent-wash", "selection", "mention-wash", "mention-me-wash"}
    colours = root - non_colour
    themes = {k: v for k, v in blocks.items() if k.startswith(":root[data-theme=")}
    assert len(themes) >= 2, "expected the dark and light themes"
    for sel, body in themes.items():
        have = set(re.findall(r"--([a-z][a-z0-9-]*)\s*:", body))
        missing = sorted(colours - have)
        assert not missing, f"{sel} inherits deep blue for: {missing}"


def test_registry_themes_exist_in_the_stylesheet():
    from kb_platform import settings as s
    css = CSS.read_text()
    entry = s.BY_KEY["ui.theme"]
    for opt in entry["options"]:
        if opt == entry["default"]:
            continue                                   # the default is :root itself
        assert f':root[data-theme="{opt}"]' in css, f"no theme block for {opt}"
