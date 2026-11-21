// Added by the platform to every artifact it serves (user_server.artifact_raw):
// the page's few links to the app around it. The page is sandboxed and cannot
// move the app, use the clipboard on its own or know the app's look, so each of
// these is a message the app checks and answers with the viewer's own access.
//
// Links. A click on a link to a knowledgebase file opens that file in the app,
// the way a link in a document does: `<a href="/company/notes/x.md">`, a
// relative `<a href="plan.md">`, `…/x.md#L12` for a line, or `…/page.html#id`
// for a place in another artifact (its #fragment is handed to that page). A
// click on an outside address (http, https) copies it instead — the app never
// opens outside sites from an artifact; the viewer pastes it where they choose.
// In-page anchors, other schemes, modified clicks and links the page handles
// itself (preventDefault) are left alone.
//
// Theme. The app tells the page its theme — at once and whenever it changes —
// as `--kb-<token>` custom properties on <html> (`--kb-bg`, `--kb-ink`,
// `--kb-accent`, …) and `data-kb-theme` / `data-kb-mode` (light or dark) on it.
// A page that ignores them looks exactly as it did; one that wants to match the
// app writes `background: var(--kb-bg, #fff)`. The raw `kb-theme` message
// reaches the page's own listeners too.
(function () {
  var DIR = __KB_ARTIFACT_DIR__;   // this page's own folder, repo-relative
  var FRAG = /^#[A-Za-z0-9._~-]{1,128}$/;

  function target(href) {
    if (!href || href.charAt(0) === "#" || href.slice(0, 2) === "//" || /^[a-z][a-z0-9+.-]*:/i.test(href)) return null;
    var m = /#L(\d+)$/i.exec(href);
    var line = m ? parseInt(m[1], 10) : 0;
    var at = href.indexOf("#");
    var hash = at >= 0 ? href.slice(at) : "";
    href = href.split("#")[0].split("?")[0];
    if (!href) return null;
    var parts = href.charAt(0) === "/" || !DIR ? [] : DIR.split("/");
    var segs = href.split("/");
    for (var i = 0; i < segs.length; i++) {
      var s;
      try { s = decodeURIComponent(segs[i]); } catch (e) { return null; }
      if (s === "" || s === ".") continue;
      if (s === "..") { if (!parts.length) return null; parts.pop(); continue; }
      parts.push(s);
    }
    if (!parts.length) return null;
    var t = { path: parts.join("/"), line: line };
    if (!m && FRAG.test(hash) && /\.html$/i.test(t.path)) t.frag = hash;
    return t;
  }

  // An outside address: http(s), or protocol-relative. The resolved href, so
  // what is copied is what the link really points at.
  function external(a, href) {
    if (!href || !/^(https?:)?\/\//i.test(href)) return null;
    var u = a.href || href;
    return /^https?:\/\/\S+$/i.test(u) && u.length <= 4096 ? u : null;
  }

  // on window, bubbling: every handler of the page itself has run by now
  window.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var href = a.getAttribute("href");
    var ext = external(a, href);
    if (ext) {
      e.preventDefault();
      parent.postMessage({ type: "kb-clipboard", text: ext, link: true }, "*");
      return;
    }
    var t = target(href);
    if (!t) return;
    e.preventDefault();
    var msg = { type: "kb-open", path: t.path, line: t.line };
    if (t.frag) msg.frag = t.frag;
    parent.postMessage(msg, "*");
  });

  function applyTheme(d) {
    var root = document.documentElement;
    if (!root || typeof d.theme !== "string") return;
    root.setAttribute("data-kb-theme", d.theme);
    root.setAttribute("data-kb-mode", d.mode === "light" ? "light" : "dark");
    var tk = d.tokens || {};
    for (var k in tk) {
      if (Object.prototype.hasOwnProperty.call(tk, k) && /^[a-z][a-z0-9-]{0,40}$/.test(k) && typeof tk[k] === "string")
        root.style.setProperty("--kb-" + k, tk[k]);
    }
  }
  window.addEventListener("message", function (e) {
    if (e.source === parent && e.data && e.data.type === "kb-theme") applyTheme(e.data);
  });
  // ask at once rather than wait for the frame's load, so it usually lands
  // before the first paint
  parent.postMessage({ type: "kb-theme-get" }, "*");
})();
