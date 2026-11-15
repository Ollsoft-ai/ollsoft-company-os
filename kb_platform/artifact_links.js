// Added by the platform to every artifact it serves (user_server.artifact_raw).
// A click on a link to a knowledgebase file opens that file in the app, the
// way a link in a document does: `<a href="/company/notes/x.md">`, a relative
// `<a href="plan.md">`, or `…/x.md#L12` for a line. The page is sandboxed and
// cannot move the app itself, so the click becomes a `kb-open` message; the
// app checks the path and opens it with the viewer's own access. Nothing comes
// back to the page. In-page anchors, external addresses, modified clicks and
// links the page handles itself (preventDefault) are left alone.
(function () {
  var DIR = __KB_ARTIFACT_DIR__;   // this page's own folder, repo-relative

  function target(href) {
    if (!href || href.charAt(0) === "#" || href.slice(0, 2) === "//" || /^[a-z][a-z0-9+.-]*:/i.test(href)) return null;
    var m = /#L(\d+)$/i.exec(href);
    var line = m ? parseInt(m[1], 10) : 0;
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
    return parts.length ? { path: parts.join("/"), line: line } : null;
  }

  // on window, bubbling: every handler of the page itself has run by now
  window.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var t = target(a.getAttribute("href"));
    if (!t) return;
    e.preventDefault();
    parent.postMessage({ type: "kb-open", path: t.path, line: t.line }, "*");
  });
})();
