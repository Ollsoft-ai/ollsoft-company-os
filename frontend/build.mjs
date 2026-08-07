import * as esbuild from "esbuild";
import { cpSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";

mkdirSync("static", { recursive: true });

await esbuild.build({
  entryPoints: ["src/app.js"],
  bundle: true,
  format: "esm",
  outfile: "static/app.js",
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
for (const f of ["app.html", "login.html", "style.css",
                 "favicon.svg", "logo.svg", "logo-mark.svg"]) {
  cpSync(`assets/${f}`, `static/${f}`);
}

// Cache-bust every deploy: static assets are served without Cache-Control, so
// browsers (and any CDN edge, which caches .js/.css by extension) may hold the
// old bundle. Stamping ?v= into the copied HTML makes each build a fresh URL —
// no service restart or cache purge ever needed for a frontend deploy.
const v = Date.now();
for (const f of ["static/app.html", "static/login.html"]) {
  writeFileSync(f, readFileSync(f, "utf8").replace(/\?v=\d+/g, "?v=" + v));
}
console.log("build complete (assets stamped v=" + v + ")");
