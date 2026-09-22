import * as esbuild from "esbuild";
import { cpSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";

mkdirSync("static", { recursive: true });

await esbuild.build({
  // Two pages: the app, and the document page the public-link container
  // serves. They share `richview.js` through a split chunk, which is the
  // point — a fix to the writing surface lands in both.
  entryPoints: ["src/app.js", "src/publicdoc.js"],
  bundle: true,
  format: "esm",
  // One entry, one lazy chunk: `import("./term.js")` in app.js becomes
  // static/chunks/term-<hash>.js, fetched the first time a terminal opens.
  // The hash in the name is what lets the hub serve it immutable; the cost is
  // that a deploy (rsync --delete) removes the previous hash, so a page from
  // before the deploy asking for its FIRST terminal is told to reload
  // (newTerminal) rather than shown a blank panel.
  outdir: "static",
  entryNames: "[name]",
  chunkNames: "chunks/[name]-[hash]",
  splitting: true,
  sourcemap: false,
  // Minified + literal UTF-8 (app.html declares utf-8): ~2 MB of readable JS
  // was real parse time on every load, and every byte rides the tunnel.
  minify: true,
  charset: "utf8",
  logLevel: "info",
});

// xterm ships its CSS separately; copy it next to the bundle.
cpSync("node_modules/@xterm/xterm/css/xterm.css", "static/xterm.css");

// Bundle the brand fonts (IBM Plex, latin subset only) so the app never
// depends on a CDN — it must render identically fully offline.
mkdirSync("static/fonts", { recursive: true });
for (const f of [
  "ibm-plex-sans/files/ibm-plex-sans-latin-400-normal.woff2",
  "ibm-plex-sans/files/ibm-plex-sans-latin-500-normal.woff2",
  "ibm-plex-sans/files/ibm-plex-sans-latin-600-normal.woff2",
  "ibm-plex-mono/files/ibm-plex-mono-latin-400-normal.woff2",
  "ibm-plex-mono/files/ibm-plex-mono-latin-600-normal.woff2",
]) {
  cpSync(`node_modules/@fontsource/${f}`, `static/fonts/${f.split("/").pop()}`);
}
// Hand-authored shell: the HTML, stylesheet and brand marks live in assets/
// (tracked in git) and are copied into static/, which is entirely generated and
// gitignored. Keeping sources out of the output directory is what lets a fresh
// clone build — and stops every build from dirtying the working tree with a new
// ?v= stamp.
for (const f of ["app.html", "login.html", "style.css", "share-theme.js",
                 "favicon.svg", "logo.svg", "logo-mark.svg"]) {
  cpSync(`assets/${f}`, `static/${f}`);
}

// Cache-bust every deploy: every /static URL is served `immutable` for a year
// (hub.static_cache), which is only honest because each build makes every one
// of them a NEW URL. Stamping ?v= into the copied HTML — and into the font
// urls inside style.css, so the preload in app.html and the @font-face request
// are the same string and the browser fetches the font once — means a deploy
// lands the instant app.html (served no-store) points at the new stamp. No
// service restart or cache purge is ever needed for a frontend deploy.
const v = Date.now();
for (const f of ["static/app.html", "static/login.html", "static/style.css"]) {
  writeFileSync(f, readFileSync(f, "utf8").replace(/\?v=\d+/g, "?v=" + v));
}
console.log("build complete (assets stamped v=" + v + ")");
