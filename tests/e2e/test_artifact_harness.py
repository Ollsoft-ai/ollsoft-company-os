"""The artifact harness, proving itself.

Doubles as the worked example referenced from the kb-artifacts skill: this is
what an agent should copy when it wants to test an artifact it just wrote.
"""
from artifact_harness import open_artifact, place_artifact, watch_console
from conftest import login
from kbenv import full

# A miniature artifact that drives every file verb the bridge exposes and
# reports the outcome as text. It also reaches for a CDN script, which the CSP
# must block — that line is deliberate, and the test asserts on it.
PROBE = """<!doctype html><meta charset="utf-8">
<script src="https://cdnjs.cloudflare.com/ajax/libs/dagre/0.8.5/dagre.min.js"></script>
<pre id="out">running</pre>
<script>
let _i=0,_p={};onmessage=e=>{const m=e.data||{};if(m.type==="kb-result"&&_p[m.id]){_p[m.id](m);delete _p[m.id];}};
const send=(msg)=>new Promise(r=>{const id=++_i;_p[id]=r;parent.postMessage({id,...msg},"*");});
const here=new URL(location.href).searchParams.get("path").replace(/\\/[^/]*$/,"");
(async()=>{
  const say=[];
  say.push("cdn="+(typeof dagre==="undefined"?"blocked":"LOADED"));
  say.push("mkdir="+(!(await send({type:"kb-mkdir",path:here+"/probe/deep"})).error));
  say.push("write="+(!(await send({type:"kb-write",path:here+"/probe/deep/a.md",content:"# hi"})).error));
  const l=await send({type:"kb-list",path:here,depth:3});
  say.push("list="+((l.entries||[]).some(e=>e.path.endsWith("probe/deep/a.md"))));
  const rd=await send({type:"kb-read",path:here+"/probe/deep/a.md"});
  say.push("read="+(rd.content==="# hi"));
  const out=await send({type:"kb-read",path:"users/root/nope.md"});
  say.push("escape="+(out.error?"refused":"ALLOWED"));
  const self=await send({type:"kb-delete",path:here,recursive:true});
  say.push("selfdelete="+(self.error?"refused":"ALLOWED"));
  say.push("rm="+(!(await send({type:"kb-delete",path:here+"/probe",recursive:true})).error));
  say.push("done");
  document.getElementById("out").textContent=say.join(" ");
})();
</script>"""


def test_harness_opens_an_artifact_and_the_bridge_behaves(browser):
    ctx = browser.new_context()
    page = login(ctx, "alice")
    errors = watch_console(page)

    path = place_artifact("dashboards/harness_probe.html", html=PROBE)
    frame = open_artifact(page, path)

    out = frame.locator("#out")
    out.wait_for(timeout=15000)
    text = ""
    for _ in range(40):
        text = out.inner_text()
        if "done" in text:
            break
        page.wait_for_timeout(250)
    assert "done" in text, f"artifact never finished: {text!r}"

    # the sandbox, as the platform actually configures it
    assert "cdn=blocked" in text, f"a CDN <script src> loaded — CSP is not applied: {text!r}"
    assert "escape=refused" in text, f"read escaped the artifact's folder: {text!r}"
    assert "selfdelete=refused" in text, f"an artifact deleted its own folder: {text!r}"

    # the file verbs
    for k in ("mkdir=true", "write=true", "list=true", "read=true", "rm=true"):
        assert k in text, f"{k} missing: {text!r}"
    assert not full(path.rsplit("/", 1)[0] + "/probe").exists(), "the probe subtree survived"

    # the blocked CDN must be visible to the test author, not silent
    assert any("Content Security Policy" in e or "Refused to load" in e for e in errors), \
        f"the CSP block was not reported on the console: {errors}"
    ctx.close()


def test_place_artifact_can_seed_a_whole_workspace(browser, tmp_path):
    """An artifact that reads neighbouring files must be tested with them present."""
    src = tmp_path / "ws"
    (src / "data").mkdir(parents=True)
    (src / "data" / "rows.md").write_text("alpha\nbeta\ngamma\n")
    (src / "viewer.html").write_text("""<!doctype html><meta charset="utf-8"><pre id="out">…</pre>
<script>
let _i=0,_p={};onmessage=e=>{const m=e.data||{};if(m.type==="kb-result"&&_p[m.id]){_p[m.id](m);delete _p[m.id];}};
const send=(m)=>new Promise(r=>{const id=++_i;_p[id]=r;parent.postMessage({id,...m},"*");});
const here=new URL(location.href).searchParams.get("path").replace(/\\/[^/]*$/,"");
(async()=>{const r=await send({type:"kb-read",path:here+"/data/rows.md"});
 document.getElementById("out").textContent="rows="+(r.content||"").trim().split("\\n").length;})();
</script>""")

    ctx = browser.new_context()
    page = login(ctx, "alice")
    path = place_artifact("dashboards/wsviewer/viewer.html", src_dir=src)
    frame = open_artifact(page, path)
    out = frame.locator("#out")
    out.wait_for(timeout=15000)
    for _ in range(40):
        if "rows=" in out.inner_text():
            break
        page.wait_for_timeout(250)
    assert "rows=3" in out.inner_text(), f"neighbouring data file was not readable: {out.inner_text()!r}"
    ctx.close()
