"""What an artifact gets from the app besides the file verbs: the address's
#fragment, the app's theme (live), its own per-viewer memory, and outside
links copied instead of opened."""
import httpx
import pytest
from artifact_harness import place_artifact
from conftest import BASE, CREDS, login
from kbenv import U

PROBE = """<!doctype html><meta charset="utf-8">
<pre id="hash"></pre><pre id="theme"></pre><pre id="state">state …</pre>
<a id="ext" href="https://example.com/page?x=1">an outside page</a>
<script>
const show=()=>{document.getElementById("hash").textContent="hash="+location.hash;};
show(); addEventListener("hashchange", show);
const th=()=>{const r=document.documentElement;document.getElementById("theme").textContent=
  "theme="+(r.getAttribute("data-kb-theme")||"")+" mode="+(r.getAttribute("data-kb-mode")||"")+
  " bg="+getComputedStyle(r).getPropertyValue("--kb-bg").trim().toLowerCase();};
addEventListener("message",e=>{if(e.data&&e.data.type==="kb-theme")setTimeout(th,0);});
let _i=0,_p={};addEventListener("message",e=>{const m=e.data||{};if(m.type==="kb-result"&&_p[m.id]){_p[m.id](m);delete _p[m.id];}});
const send=(msg)=>new Promise(r=>{const id=++_i;_p[id]=r;parent.postMessage({id,...msg},"*");});
(async()=>{const g=await send({type:"kb-state-get"});const n=((g.state&&g.state.n)||0)+1;
  const s=await send({type:"kb-state-set",state:{n}});
  document.getElementById("state").textContent="state n="+n+" ok="+!!s.ok;})();
</script>"""


def api(user):
    c = httpx.Client(base_url=BASE, timeout=15)
    c.post("/login", data={"username": U(user), "password": CREDS[user]})
    return c


@pytest.fixture(autouse=True)
def _reset():
    api("bob").post("/api/settings", json={"unset": ["ui.theme", "ui.theme.custom"]})
    yield
    api("bob").post("/api/settings", json={"unset": ["ui.theme", "ui.theme.custom"]})


def text(loc, want, page, timeout=15000):
    for _ in range(timeout // 250):
        t = loc.inner_text()
        if want in t:
            return t
        page.wait_for_timeout(250)
    raise AssertionError(f"wanted {want!r}, have {loc.inner_text()!r}")


def test_fragment_theme_state_and_outside_links(browser):
    path = place_artifact("bridge_extras/probe.html", html=PROBE)
    # this viewer's memory of the page starts empty
    assert api("bob").post("/api/artifact/state", json={"artifact": path, "state": None}).status_code == 200
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    page = login(ctx, "bob")
    page.goto(BASE + "/" + path + "#sec-2")
    f = page.frame_locator("iframe.artifact-frame")
    # 1. the address's #fragment reaches the page, and a new one arrives without a reload
    text(f.locator("#hash"), "hash=#sec-2", page)
    text(f.locator("#state"), "state n=1 ok=true", page)
    page.evaluate("() => { location.hash = '#sec-3'; }")
    text(f.locator("#hash"), "hash=#sec-3", page)
    assert "n=1" in f.locator("#state").inner_text(), "a new fragment must not reload the page"
    # 2. the theme, and live when it changes
    text(f.locator("#theme"), "theme=deep-blue mode=dark bg=#0d1626", page)
    page.evaluate("() => window.__kbsettings.set('ui.theme', 'solarized-light')")
    text(f.locator("#theme"), "theme=solarized-light mode=light bg=#fdf6e3", page)
    # 3. an outside link is copied, never opened: the frame stays on the page
    f.locator("#ext").click()
    page.wait_for_selector('[data-testid="toast"]:has-text("Link copied")', timeout=10000)
    assert page.evaluate("async () => await navigator.clipboard.readText()") == "https://example.com/page?x=1"
    assert f.locator("#hash").is_visible()
    # 4. the page's memory survives a reload, for this viewer only
    page.reload()
    text(page.frame_locator("iframe.artifact-frame").locator("#state"), "state n=2 ok=true", page)
    assert api("carol").post("/api/artifact/state", json={"artifact": path}).json().get("state") is None, \
        "another viewer has their own"
    r = api("bob").post("/api/artifact/state", json={"artifact": path, "state": {"big": "x" * 17000}})
    assert r.status_code == 413, r.text
    api("bob").post("/api/artifact/state", json={"artifact": path, "state": None})
    ctx.close()
