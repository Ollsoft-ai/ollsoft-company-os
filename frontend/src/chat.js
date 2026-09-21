// The agent chat: a view kind (views.js) that talks to an ACP agent through
// the person's backend (kb_platform/acp.py). Loaded on first use, like the
// terminal chunk — a reader never pays for it.
//
// One WebSocket per agent per page, shared by every chat tab of that agent;
// each tab owns one ACP session. The backend keeps the agent process alive
// across reloads and replays a session's updates on re-attach, so a running
// turn survives a refresh the way a shell does.
//
// What it looks like is deliberate: the transcript reads like Claude Code in
// a terminal — a ⏺ before everything the agent does, tool calls as one line
// (`Bash(ls -la)`) with a ⎿ result under it, a ✻ spinner line above the
// input, a bordered box with numbered options when the agent asks for
// permission — while the person's own messages and the composer read like
// claude.ai. Someone who lives in the terminal should feel at home at once.
import { marked } from "marked";
import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import python from "highlight.js/lib/languages/python";
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import json from "highlight.js/lib/languages/json";
import yaml from "highlight.js/lib/languages/yaml";
import markdown from "highlight.js/lib/languages/markdown";
import diff from "highlight.js/lib/languages/diff";
import sql from "highlight.js/lib/languages/sql";
import css from "highlight.js/lib/languages/css";
import xml from "highlight.js/lib/languages/xml";
import go from "highlight.js/lib/languages/go";
import rust from "highlight.js/lib/languages/rust";
import java from "highlight.js/lib/languages/java";
import plaintext from "highlight.js/lib/languages/plaintext";

for (const [n, l] of Object.entries({ bash, sh: bash, shell: bash, zsh: bash, python, py: python,
  javascript, js: javascript, jsx: javascript, typescript, ts: typescript, tsx: typescript, json, yaml, yml: yaml,
  markdown, md: markdown, diff, patch: diff, sql, css, xml, html: xml, svg: xml, go, rust, rs: rust, java,
  plaintext, text: plaintext, txt: plaintext })) hljs.registerLanguage(n, l);

let shell = null;   // what app.js hands us: openTermWith, openPath, toast, settings, icons…

// A title from a first message: the words, not the markdown around them.
// `kbOpen` is localStorage: a hand-edited or half-written record must not be
// able to throw inside the constructor (openView would close the tab, on
// every reload, with "Could not open Chat").
const strList = (v, cap) => (Array.isArray(v) ? v.filter((p) => typeof p === "string" && p).slice(0, cap) : []);
export function sanitizeCtx(c) {
  const o = c && typeof c === "object" ? c : {};
  return { off: !!o.off, mute: strList(o.mute, 200), files: strList(o.files, MAX_CTX_FILES),
           cwd: typeof o.cwd === "string" ? o.cwd : null };
}
const MAX_CTX_FILES = 20;     // the chips are a hint, not a reading list

export function plainTitle(text) {
  return String(text || "").replace(/```[\s\S]*?```/g, " ").replace(/`([^`]*)`/g, "$1")
    .replace(/[*_~#>]+/g, "").replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\s+/g, " ").trim().slice(0, 48);
}
export function init(s) { shell = s; }

// ---- markdown → safe HTML --------------------------------------------------
const renderer = {
  code({ text, lang }) {
    const l = (lang || "").trim().split(/\s+/)[0].toLowerCase();
    let body;
    try { body = l && hljs.getLanguage(l) ? hljs.highlight(text, { language: l }).value : hljs.highlightAuto(text, ["bash", "python", "javascript", "json", "diff", "markdown"]).value; }
    catch (e) { body = escapeHtml(text); }
    return `<pre class="chat-code"><div class="chat-code-bar"><span>${escapeHtml(l || "")}</span><button type="button" class="chat-copy" data-copy>copy</button></div><code class="hljs${l ? " language-" + escapeHtml(l) : ""}">${body}</code></pre>`;
  },
  link({ href, title, tokens }) {
    const text = this.parser.parseInline(tokens);
    const h = escapeHtml(href || "");
    const t = title ? ` title="${escapeHtml(title)}"` : "";
    return `<a href="${h}"${t} target="_blank" rel="noopener noreferrer">${text}</a>`;
  },
};
marked.use({ gfm: true, breaks: false, renderer });
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") { node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer"); }
});
// a task list's checkboxes stay (disabled: the transcript is not a form);
// every other input goes
DOMPurify.addHook("uponSanitizeElement", (node, data) => {
  if (data.tagName !== "input") return;
  if ((node.getAttribute("type") || "").toLowerCase() !== "checkbox") { if (node.parentNode) node.parentNode.removeChild(node); return; }
  node.setAttribute("disabled", "");
});

export function renderMarkdown(text) {
  let html;
  try { html = marked.parse(text || ""); } catch (e) { html = "<p>" + escapeHtml(text || "") + "</p>"; }
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true }, ADD_ATTR: ["target", "data-copy"], FORBID_TAGS: ["style", "form", "textarea", "select"] });
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const svg = (paths, extra) => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${extra || ""}>${paths}</svg>`;
const ICON = {
  plus: svg('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>'),
  send: svg('<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>', ' stroke-width="2.4"'),
  stop: '<svg class="icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  mic: svg('<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>', ' stroke-width="1.9"'),
  more: '<svg class="icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.9"/><circle cx="12" cy="12" r="1.9"/><circle cx="19" cy="12" r="1.9"/></svg>',
  history: svg('<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>'),
  agent: svg('<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'),
  log: svg('<line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="14" y2="18"/>'),
  check: svg('<polyline points="20 6 9 17 4 12"/>'),
  chevronDown: svg('<polyline points="6 9 12 15 18 9"/>'),
  copy: svg('<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'),
  pencil: svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
  terminal: svg('<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>'),
  file: svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>'),
  search: svg('<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.4" y2="16.4"/>'),
  globe: svg('<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>'),
  brain: svg('<path d="M12 3a4 4 0 0 0-4 4v1a3 3 0 0 0-2 5 3 3 0 0 0 1 5.5V19a3 3 0 0 0 5 2 3 3 0 0 0 5-2v-.5A3 3 0 0 0 18 13a3 3 0 0 0-2-5V7a4 4 0 0 0-4-4z"/>'),
  move: svg('<polyline points="5 9 2 12 5 15"/><polyline points="9 5 12 2 15 5"/><polyline points="15 19 12 22 9 19"/><polyline points="19 9 22 12 19 15"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/>'),
  trash: svg('<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4h6v2"/>'),
  sliders: svg('<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>'),
  eye: svg('<path d="M1.5 12S5 5.5 12 5.5 22.5 12 22.5 12 19 18.5 12 18.5 1.5 12 1.5 12z"/><circle cx="12" cy="12" r="3"/>'),
  folder: svg('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  image: svg('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/>'),
  tool: svg('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>'),
};
// the verb's picture on a tool line: Read gets a file, Bash a prompt…
const KIND_ICON = { read: "file", edit: "pencil", delete: "trash", move: "move", search: "search", execute: "terminal", think: "brain", fetch: "globe", switch_mode: "sliders" };
// what an empty chat suggests — a click puts it in the box
const SUGGESTIONS = ["Summarise what changed in the company folder this week", "Find every open to-do assigned to me", "Draft an onboarding note for a new colleague"];
// an agent's state in the picker and the menu: can it answer, what stands in the way
function agentState(a) {
  const signed = a.auth && a.auth.kind && a.auth.kind !== "none";
  if (!a.installed) return { cls: "off", text: "not installed", rank: 3 };
  if (signed) return { cls: "ok", text: "signed in · " + (a.auth.label || a.auth.kind), rank: 0 };
  if (a.hasCred || a.hasKey) return { cls: "ok", text: a.hasKey ? "API key set" : "credentials found", rank: 1 };
  if (!a.login && !(a.keys && a.keys.length)) return { cls: "ok", text: "ready", rank: 1 };   // nothing to sign in to
  return { cls: "warn", text: "needs sign-in", rank: 2 };
}

// Claude Code's vocabulary: the bullet before everything the agent does, the
// elbow before what came of it, the spark on the spinner line.
const DOT = "⏺", ELBOW = "⎿", SPARK = "✻";
const SPIN = ["✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳"];
const FOLD = 8;                       // lines of output shown before "… +N lines"
const KIND_NAME = { read: "Read", edit: "Update", delete: "Delete", move: "Move", search: "Search", execute: "Bash", think: "Think", fetch: "Fetch", switch_mode: "Mode" };
const TITLE_VERB = /^(?:edit|update|write|create|read|view|search|grep|glob|find|fetch|web ?search|web ?fetch|task|bash|run|execute|delete|remove|move|rename|think|todo ?write)\b\s*:?\s*/i;
const reducedMotion = () => !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
const plural = (n, one) => n + " " + one + (n === 1 ? "" : "s");
function lineCount(text) { if (!text) return 0; const t = text.endsWith("\n") ? text.slice(0, -1) : text; return t ? t.split("\n").length : 0; }
// an agent that wraps a tool's output in one fence meant the text, not markdown
function unfence(text) { const m = /^```[^\n]*\n([\s\S]*?)\n?```\s*$/.exec(text || ""); return m ? m[1] : (text || ""); }
function firstString(o, keys) {
  if (!o || typeof o !== "object") return "";
  for (const k of keys) if (typeof o[k] === "string" && o[k]) return o[k];
  return "";
}
function ago(t) {
  const d = Math.max(0, Date.now() / 1000 - (t || 0));
  if (d < 60) return "just now";
  if (d < 3600) return Math.round(d / 60) + " min ago";
  if (d < 86400) return Math.round(d / 3600) + " h ago";
  if (d < 7 * 86400) return Math.round(d / 86400) + " d ago";
  return new Date(t * 1000).toLocaleDateString();
}
function kb(n) { return n < 1024 ? n + " B" : (n / 1024).toFixed(n < 10240 ? 1 : 0) + " KB"; }

// ---- one socket per agent, shared by its tabs --------------------------------
const conns = new Map();   // agentId → AgentConn

class AgentConn {
  constructor(agentId) {
    this.agentId = agentId;
    this.ws = null;
    this.hello = null;          // {agent, init, auth, chats}
    this.tabs = new Set();      // chat views on this agent
    this.nextId = 1;
    this.pending = new Map();   // our request id → {resolve, reject}
    this.retries = 0;
    this.closedByUs = false;
    this.error = null;
    this.stderr = [];
    this.timer = null;
    this.connect();
  }
  connect() {
    if (this.ws && this.ws.readyState <= 1) return;
    this.error = null;
    const ws = new WebSocket(shell.wsBase() + "/acp?agent=" + encodeURIComponent(this.agentId));
    this.ws = ws;
    ws.onopen = () => { this.retries = 0; };
    ws.onmessage = (ev) => { let m; try { m = JSON.parse(ev.data); } catch (e) { return; } this.onMessage(m); };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      this.ws = null;
      for (const [, p] of this.pending) p.reject(new Error("connection lost"));
      this.pending.clear();
      for (const v of this.tabs) v.onDisconnected();
      if (this.closedByUs) return;
      const wait = Math.min(15000, 1000 * Math.pow(2, this.retries++));
      this.timer = setTimeout(() => this.connect(), wait);
    };
    ws.onerror = () => { /* onclose follows */ };
  }
  close() { this.closedByUs = true; clearTimeout(this.timer); if (this.ws) this.ws.close(); conns.delete(this.agentId); }
  send(obj) {
    if (!this.ws || this.ws.readyState !== 1) return false;
    this.ws.send(JSON.stringify(obj));
    return true;
  }
  request(method, params) {
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      if (!this.send({ jsonrpc: "2.0", id, method, params })) { reject(new Error("not connected")); return; }
      this.pending.set(id, { resolve, reject });
    });
  }
  notify(method, params) { this.send({ jsonrpc: "2.0", method, params }); }
  answer(id, result) { this.send({ jsonrpc: "2.0", id, result }); }
  onMessage(m) {
    if (m.kb === "hello") {
      this.hello = m; this.stderr = m.stderr || [];
      shell.setRepoRoot(m.cwd);
      for (const v of this.tabs) v.onHello(m);
      return;
    }
    if (m.kb === "error") { this.error = m; this.stderr = m.stderr || this.stderr; for (const v of this.tabs) v.onAgentError(m); return; }
    if (m.kb === "exit") { for (const v of this.tabs) v.onExit(m); return; }
    if (m.kb === "stderr") { this.stderr.push(m.line); if (this.stderr.length > 200) this.stderr.shift(); for (const v of this.tabs) v.onStderr(m.line); return; }
    if (m.kb === "stderr-all") { this.stderr = m.lines || []; for (const v of this.tabs) v.onStderrAll(this.stderr); return; }
    if (m.kb) {   // u / turn / req / req-done / attached / reset — routed by session
      const sid = m.sessionId;
      let hit = false;
      for (const v of this.tabs) if (v.sessionId && v.sessionId === sid) { v.onSessionMessage(m); hit = true; }
      if (!hit && m.kb === "u" && m.seq === -1) for (const v of this.tabs) v.onLooseNotification(m.m);
      return;
    }
    if ("id" in m && this.pending.has(m.id)) {
      const p = this.pending.get(m.id); this.pending.delete(m.id);
      if (m.error) { const e = new Error(m.error.message || "error"); e.code = m.error.code; e.data = m.error.data; p.reject(e); }
      else p.resolve(m.result || {});
    }
  }
}
function agentConn(agentId) {
  let c = conns.get(agentId);
  if (!c) { c = new AgentConn(agentId); conns.set(agentId, c); }
  else c.connect();
  return c;
}

// ---- the view -----------------------------------------------------------------
const views = new Map();   // tab id → ChatView

export async function open(t, spec) {
  const v = new ChatView(t, spec);
  views.set(t.id, v);
  t.chatView = v;
  await v.start();
}
export function close(t) { const v = views.get(t.id); if (v) { v.destroy(); views.delete(t.id); } }
export function activate(t) { const v = views.get(t.id); if (v) { v.scrollIfPinned(); v.docsChanged(); } }
// the window opened, closed or switched a document: every chat's context row
// follows what you have open
export function docsChanged() { for (const v of views.values()) v.docsChanged(); }
export function focus(t) { const v = views.get(t.id); if (v && v.composer) v.composer.focus(); }
// test hook (like app.js's __kbopenview): the view that owns an element, so a
// browser test can hand it session updates the echo agent never sends
if (typeof window !== "undefined")   // the module is also imported by `node --test`
  window.__kbchatview = (node) => { for (const v of views.values()) if (v.t.el === node || v.t.el.contains(node)) return v; return null; };

class ChatView {
  constructor(t, spec) {
    this.t = t;
    this.agentId = spec.agent || t.chat.agent;
    this.sessionId = spec.sessionId || t.chat.sessionId || null;
    this.title = spec.title || t.chat.title || "";
    this.conn = null;
    this.seq = 0;                 // replay cursor
    this.running = false;
    this.loading = false;         // a session/load is streaming the past back
    this.msgs = [];               // rendered items in order
    this.current = null;          // the agent message being streamed
    this.thought = null;
    this.tools = new Map();       // toolCallId → {el, data}
    this.pinned = true;           // auto-scroll while at the bottom
    this.commands = [];
    this.modes = null; this.configOptions = null;
    this.pendingPerms = new Map();
    this.attachments = [];
    // What the agent is pointed at: the documents this window has open (live,
    // so it follows you), anything you added by hand, and the folder a new
    // session starts in. Remembered with the tab.
    this.ctx = sanitizeCtx(t.chat && t.chat.ctx);
    this.ctxSig = "";
    this.spoke = false;           // has anything been said in this session yet
    this.sentEcho = null;
    this.usage = null;            // {pct, cost} from the agent's usage_update
    this.restored = !!(spec.sessionId || t.chat.sessionId);   // a chat from before: its history may live only in the agent
    this.loadedOnce = false;      // session/load asked for this session
    this.attaching = false;       // between our attach and the backend's "attached": frames wait in order
    this.buffer = [];
    this.outbox = [];             // prompts that could not go: sent when the connection is back
    this.cancelTimer = null;
    this.statusText = ""; this.statusKind = "";
    this.turnStart = 0; this.ticker = null; this.frame = 0;
    this.menu = null;
    this.emptyEl = null;
    this.build();
  }

  // -- DOM --
  build() {
    const root = this.t.el;
    root.classList.add("chat-view");
    root.innerHTML = "";
    this.body = el("div", "chat-body");
    this.log = el("div", "chat-log");
    this.log.setAttribute("data-testid", "chat-log");
    this.log.addEventListener("scroll", () => {
      this.pinned = this.log.scrollTop + this.log.clientHeight >= this.log.scrollHeight - 40;
      this.syncJump();
    });
    this.log.addEventListener("click", (e) => {
      const copy = e.target.closest("[data-copy]");
      if (copy) { const code = copy.closest("pre").querySelector("code"); navigator.clipboard.writeText(code.textContent).then(() => { copy.textContent = "copied"; setTimeout(() => { copy.textContent = "copy"; }, 1200); }, () => shell.toast("Clipboard blocked", "err")); return; }
      const path = e.target.closest("[data-open-path]");
      if (path) { e.preventDefault(); shell.openAbs(path.dataset.openPath); }
    });
    this.planEl = el("div", "chat-plan"); this.planEl.hidden = true; this.hasPlan = false;
    // scrolled up while it streams: a pill brings you back down (claude.ai's ↓)
    this.jump = el("button", "chat-jump"); this.jump.type = "button"; this.jump.hidden = true;
    this.jump.setAttribute("aria-label", "Scroll to the latest");
    this.jump.addEventListener("click", () => { this.pinned = true; this.log.scrollTop = this.log.scrollHeight; this.syncJump(); });
    this.body.append(this.log, this.jump);
    this.foot = el("div", "chat-foot");
    this.buildComposer();
    root.append(this.body, this.planEl, this.foot);
    this.body.hidden = true; this.foot.hidden = true;   // until the picker or the chat says which it is
    // "short" (a landscape phone, the chat in the panel): the composer goes
    // to one row and the plan folds away. Measured here rather than asked of
    // CSS — a height container query on this element crashes the renderer.
    this._ro = new ResizeObserver((es) => {
      for (const e of es) root.classList.toggle("short", e.contentRect.height < 460);
      if (this.composer) this.composer.placeholder = this.placeholderText();
    });
    this._ro.observe(root);
    // 1/2/3 answer a pending permission prompt, as they do in the terminal —
    // only while there is nothing typed, so a number meant for the message
    // is never taken for an answer
    root.addEventListener("keydown", (e) => this.onRootKey(e));
    this.picker = null;
  }
  buildComposer() {
    // One rounded box, as claude.ai has it: the text on top, and on its
    // bottom row the ways in (attach, the mic) on the left and the round send
    // on the right — one level, one shape, on a phone and on a desktop. The
    // status line — Claude Code's "✻ Working… (esc to interrupt · 12s)" —
    // sits just above the box while a turn runs.
    const wrap = el("div", "chat-composer");
    this.ctxRow = el("div", "chat-ctx"); this.ctxRow.hidden = true;
    this.attachRow = el("div", "chat-attach"); this.attachRow.hidden = true;
    this.slash = el("div", "chat-slash"); this.slash.hidden = true;
    this.status = el("div", "chat-status"); this.status.hidden = true;
    this.status.setAttribute("aria-live", "polite");
    const box = el("div", "chat-box");
    this.box = box;
    this.composer = document.createElement("textarea");
    this.composer.className = "chat-input";
    this.composer.rows = 1;
    this.composer.placeholder = this.placeholderText();
    this.composer.setAttribute("data-testid", "chat-input");
    this.composer.setAttribute("aria-label", "Message");
    this.composer.addEventListener("input", () => { this.autosize(); this.slashHint(); this.syncSend(); });
    this.composer.addEventListener("keydown", (e) => {
      if (this.slash && this.slash.hidden === false) {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); this.slashMove(e.key === "ArrowDown" ? 1 : -1); return; }
        if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) { e.preventDefault(); this.slashPick(); return; }
        if (e.key === "Escape") { this.slash.hidden = true; return; }
      }
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.sendPrompt(); }
      if (e.key === "Escape" && this.running) { this.cancel(); }
    });
    this.composer.addEventListener("paste", (e) => {
      const items = [...(e.clipboardData && e.clipboardData.items || [])].filter((i) => i.type.startsWith("image/"));
      if (!items.length) return;
      e.preventDefault();
      for (const it of items) { const f = it.getAsFile(); if (f) this.attachFile(f); }
    });
    const row = el("div", "chat-box-row");
    const left = el("div", "chat-box-left");
    // ＋ — a picture from the camera roll or the disk (a paste works too)
    const pick = document.createElement("input");
    pick.type = "file"; pick.accept = "image/*"; pick.multiple = true; pick.hidden = true;
    pick.addEventListener("change", () => { for (const f of pick.files || []) this.attachFile(f); pick.value = ""; });
    this.addWrap = el("div", "chat-menu-wrap chat-add-wrap");
    const add = el("button", "chat-round chat-add"); add.type = "button";
    add.title = "Add a file, a folder or an image";
    add.setAttribute("aria-label", "Add context"); add.setAttribute("data-testid", "chat-add");
    add.setAttribute("aria-haspopup", "menu"); add.setAttribute("aria-expanded", "false");
    add.innerHTML = ICON.plus;
    add.addEventListener("pointerdown", (e) => e.preventDefault());
    add.addEventListener("click", () => this.toggleAddMenu(add, () => pick.click()));
    this.addWrap.append(add);
    left.append(this.addWrap, pick);
    // the agent, where claude.ai keeps its model: a chip at the box's foot
    // with the mode beside its name — its menu switches agent or mode, and
    // it is there whatever size the chat is
    this.agentChip = el("button", "chat-agent-chip"); this.agentChip.type = "button";
    this.agentChip.setAttribute("data-testid", "chat-agent-chip");
    this.agentChip.setAttribute("aria-haspopup", "menu"); this.agentChip.setAttribute("aria-expanded", "false");
    this.agentChip.addEventListener("pointerdown", (e) => e.preventDefault());
    this.agentChip.addEventListener("click", () => this.toggleAgentMenu());
    left.append(this.agentChip);
    this.renderChip();
    const right = el("div", "chat-box-right");
    // the mic, as the editor's dock and the terminal's keybar have it: the
    // words land in the box (dictation targets the focused field)
    if (shell.dictation && shell.dictation.ready && shell.dictation.ready()) {
      const mic = el("button", "chat-round chat-mic"); mic.type = "button"; mic.title = "Dictate (F9)";
      mic.setAttribute("aria-label", "Dictate"); mic.setAttribute("data-testid", "chat-mic"); mic.setAttribute("data-mic", "");
      mic.innerHTML = ICON.mic;
      mic.addEventListener("pointerdown", (e) => e.preventDefault());   // keep the box focused
      mic.addEventListener("click", () => { this.composer.focus(); shell.dictation.toggle(); });
      right.append(mic);
    }
    this.sendBtn = el("button", "chat-send");
    this.sendBtn.type = "button"; this.sendBtn.setAttribute("data-testid", "chat-send");
    this.sendBtn.setAttribute("aria-label", "Send"); this.sendBtn.title = "Send (Enter)";
    this.sendBtn.addEventListener("pointerdown", (e) => e.preventDefault());
    this.sendBtn.addEventListener("click", () => {
      const has = !!(this.composer.value.trim() || this.attachments.length);
      return this.running && !has ? this.cancel() : this.sendPrompt();
    });
    // ⋯ — new chat, rename, recent chats, the agent's log; in the composer
    // because the strip above already carries the chat's name
    this.moreWrap = el("div", "chat-menu-wrap chat-more-wrap");
    const more = el("button", "chat-round chat-more-btn"); more.type = "button";
    more.title = "New chat, recent chats, agent, log"; more.setAttribute("aria-label", "More");
    more.setAttribute("aria-haspopup", "menu"); more.setAttribute("aria-expanded", "false");
    more.innerHTML = ICON.more;
    more.addEventListener("pointerdown", (e) => e.preventDefault());
    more.addEventListener("click", () => this.toggleMenu(this.conn && this.conn.hello, more, this.authLabel || this.agentName()));
    this.moreWrap.append(more);
    left.append(this.moreWrap);
    right.append(this.sendBtn);
    row.append(left, right);
    box.append(this.ctxRow, this.attachRow, this.composer, row);
    wrap.append(this.slash, this.status, box);
    this.foot.append(wrap);
    this.renderCtx();
    this.syncSend();
  }
  attachFile(f) {
    if (!f || !f.type.startsWith("image/")) return;
    const r = new FileReader();
    r.onload = () => { const data = String(r.result).split(",")[1]; this.attachments.push({ type: "image", mimeType: f.type, data, name: f.name || "image" }); this.renderAttachments(); this.syncSend(); };
    r.readAsDataURL(f);
  }
  // The send button: an arrow while there is something to send, dim while
  // there is not, a stop square while the agent works.
  syncSend() {
    const b = this.sendBtn;
    if (!b) return;
    const has = !!(this.composer.value.trim() || this.attachments.length);
    if (this.running && has) {
      // Something is written: the button is Send, and the message waits for
      // the turn to end (Claude Code's "Queue a message…"). Esc still stops.
      b.innerHTML = ICON.send;
      b.classList.remove("stop"); b.disabled = false;
      b.title = "Queue it (Enter) — it goes when this turn ends";
      b.setAttribute("aria-label", "Queue the message");
    } else if (this.running) {
      b.innerHTML = ICON.stop;
      b.classList.add("stop"); b.disabled = false; b.title = "Stop (Esc)"; b.setAttribute("aria-label", "Stop");
    } else {
      b.innerHTML = ICON.send;
      b.classList.remove("stop"); b.disabled = !has; b.title = "Send (Enter)"; b.setAttribute("aria-label", "Send");
    }
  }
  autosize() {
    const c = this.composer;
    c.style.height = "auto";
    c.style.height = Math.min(220, c.scrollHeight) + "px";
  }
  onRootKey(e) {
    if (e.key < "1" || e.key > "9" || e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t !== this.composer && t && t.matches && t.matches("input, select, textarea")) return;
    if (this.composer.value) return;
    const live = [...this.pendingPerms.values()].filter((p) => p.pick);
    const p = live[0];   // the one asked first, as Claude Code queues them
    if (p && p.pick(Number(e.key))) e.preventDefault();
  }

  // -- the status line: "✻ Working… (esc to interrupt · 12s · context 42%)" --
  setStatus(text, kind) {
    this.statusText = text || ""; this.statusKind = kind || "";
    if (kind === "busy") this.startTicker(); else this.stopTicker();
    this.renderStatus();
  }
  startTicker() {
    if (this.ticker) return;
    this.frame = 0;
    this.ticker = setInterval(() => { this.frame++; this.renderStatus(); }, reducedMotion() ? 1000 : 120);
  }
  stopTicker() { if (this.ticker) { clearInterval(this.ticker); this.ticker = null; } }
  usageText() {
    const u = this.usage; if (!u) return "";
    const parts = [];
    if (u.pct !== null && u.pct !== undefined) parts.push("context " + u.pct + "%");
    if (u.cost) parts.push(u.cost);
    return parts.join(" · ");
  }
  renderStatus() {
    const s = this.status;
    const kind = this.statusKind;
    s.className = "chat-status" + (kind ? " " + kind : "");
    s.textContent = "";
    if (kind === "busy") {
      s.append(el("span", "chat-spin", reducedMotion() ? SPARK : SPIN[this.frame % SPIN.length]), " ");
      s.append(el("span", "chat-status-text", this.statusText || "Working…"));
      const meta = [];
      if (this.running) meta.push(Math.max(0, Math.round((Date.now() - this.turnStart) / 1000)) + "s");
      const u = this.usageText(); if (u) meta.push(u);
      if (this.running && !(shell.isMobile && shell.isMobile())) meta.push("esc to interrupt");
      if (meta.length) s.append(" ", el("span", "chat-status-meta", "(" + meta.join(" · ") + ")"));
      s.hidden = false;
    } else if (this.statusText) {
      s.append(el("span", "chat-status-text", this.statusText));
      s.hidden = false;
    } else if (this.usageText()) {
      s.append(el("span", "chat-status-meta", this.usageText()));
      s.hidden = false;
    } else s.hidden = true;
  }
  setRunning(on) {
    this.running = on;
    if (this.composer) this.composer.placeholder = this.placeholderText();
    this.syncSend();
    this.t.el.classList.toggle("running", on);
    if (on) { this.turnStart = Date.now(); this.setStatus("Working…", "busy"); }
    else if (this.statusKind !== "err") this.setStatus("");
  }
  scrollIfPinned() { if (this.pinned) this.log.scrollTop = this.log.scrollHeight; this.syncJump(); }
  syncJump() {
    if (!this.jump) return;
    const away = !this.pinned && this.log.scrollHeight - this.log.clientHeight > 60;
    this.jump.hidden = !away;
    if (away) this.jump.textContent = this.running ? "↓ New replies" : "↓";
  }
  rename(title, tell) {
    this.title = title || this.title;
    this.t.chat.title = this.title;
    shell.renameTab(this.t, this.title ? this.title.slice(0, 40) : (this.sessionId ? this.agentName() : "New chat"));
    // the recent-chats list shows what the tab shows
    if (tell && this.conn && this.sessionId && this.title) this.conn.send({ kb: "title", sessionId: this.sessionId, title: this.title });
    // …and so does the sidebar, once the backend has written it down
    if (this.sessionId && this.title && shell.chatsChanged) { clearTimeout(this._chatsT); this._chatsT = setTimeout(() => shell.chatsChanged(), 700); }
  }
  agentName() { const a = this.conn && this.conn.hello && this.conn.hello.agent; return a ? a.name : (shell.agentName(this.agentId) || "Agent"); }
  // "Message Claude Code…" where it fits; a phone, or a chat in a column,
  // gets the short one — a textarea wraps its placeholder, and a wrapped
  // placeholder in a one-line box is cut in half
  // the chip names the agent, so the box never repeats it; a phone's box is
  // ~100px wide, and a textarea wraps a placeholder it cannot fit
  placeholderText() {
    if (this.running) return "Queue a message…";
    return (shell.isMobile && shell.isMobile()) ? "Ask…" : "Ask anything…";
  }
  // everything in the transcript goes through here: the first item replaces
  // the welcome hint
  push(node) {
    if (this.emptyEl) { this.emptyEl.remove(); this.emptyEl = null; }
    this.log.append(node);
  }
  clearLog() { this.log.innerHTML = ""; this.emptyEl = null; this.tools.clear(); this.msgs = []; }
  showChat() {
    if (this.picker) { this.picker.remove(); this.picker = null; }
    this.body.hidden = false; this.foot.hidden = false; this.planEl.hidden = !this.hasPlan;
  }
  showEmpty() {
    if (this.log.childElementCount || this.emptyEl) return;
    const e = el("div", "chat-empty");
    // the person by their first name — a test account's prefix stripped
    const who = ((shell.userName && shell.userName()) || "").replace(/^kbt_[a-z0-9]+_/, "").split(/[._-]+/)[0];
    const nice = who ? who.charAt(0).toUpperCase() + who.slice(1) : "";
    const h = new Date().getHours();
    const greet = h < 5 ? "Still up" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
    e.append(shell.agentAvatar(this.agentId, "lg"));
    e.append(el("div", "chat-empty-title", greet + (nice ? ", " + nice : "") + "."));
    e.append(el("div", "chat-empty-sub", "Ask " + this.agentName() + " anything about the knowledgebase — it works as you, in your files."));
    const chips = el("div", "chat-empty-chips");
    for (const sug of SUGGESTIONS) {
      const b = el("button", "chat-sug", sug); b.type = "button";
      b.addEventListener("click", () => { this.composer.value = sug; this.autosize(); this.syncSend(); this.composer.focus(); });
      chips.append(b);
    }
    e.append(chips, el("div", "chat-empty-hint", "/ for commands · Shift+Enter for a new line"));
    this.emptyEl = e;
    this.log.append(e);
    this.log.scrollTop = 0;   // a short chat must not open below its own greeting
  }

  // -- lifecycle --
  async start() {
    this.t.chat.agent = this.agentId;
    this.rename(this.title);
    if (this.sessionId) { this.showChat(); this.connect(); return; }
    // A new chat goes straight to your agent when it is installed and has a
    // way in (a credential file, a key, or it reported being signed in); the
    // picker is for the first time, and for switching.
    let ready = false;
    try {
      const data = await (await fetch("/api/acp/agents")).json();
      shell.setRepoRoot(data.cwd);
      const a = (data.agents || []).find((x) => x.id === this.agentId);
      const signed = a && a.auth && a.auth.kind && a.auth.kind !== "none";
      ready = !!(a && a.installed && (signed || a.hasCred || a.hasKey));
    } catch (e) { ready = false; }
    if (ready) await this.startWith(this.agentId);
    else await this.showPicker();
  }
  connect() {
    this.conn = agentConn(this.agentId);
    this.conn.tabs.add(this);
    this.renderChip();
    this.setStatus("Connecting…", "busy");
    if (this.conn.hello) this.onHello(this.conn.hello);
    else if (this.conn.error) this.onAgentError(this.conn.error);
  }
  destroy() {
    this.stopTicker();
    this.closeMenu();
    this.closeAgentMenu();
    if (this.jumpT) clearTimeout(this.jumpT);
    if (this._ro) { this._ro.disconnect(); this._ro = null; }
    if (this.conn) {
      this.conn.tabs.delete(this);
      if (this.sessionId) this.conn.send({ kb: "detach", sessionId: this.sessionId });
      if (!this.conn.tabs.size) this.conn.close();
    }
  }
  onDisconnected() { this.setStatus("Reconnecting…", "err"); }
  onExit(m) { this.setStatus("The agent exited (" + m.code + ") — send again to restart it", "err"); this.setRunning(false); this.dead = true; }
  onStderr() { /* the details panel reads conn.stderr on demand */ }
  onStderrAll() { }
  onAgentError(m) {
    this.setStatus("", "");
    this.showPicker({ error: "The agent could not start: " + (m.message || "unknown error"), stderr: m.stderr });
  }
  onHello(h) {
    this.dead = false;
    this.renderHead(h);
    // a chat from before remembers where it was started: show that folder
    const known = (h.chats || []).find((c) => c.id === this.sessionId);
    if (known && typeof known.cwd === "string") {
      const rel = shell.relPath(known.cwd);
      const here = rel === known.cwd ? null : rel;      // outside the tree: not ours to show
      if (here !== this.ctx.cwd) { this.ctx.cwd = here; this.renderCtx(); }
    }
    if (this.sessionId) {
      this.attach(this.seq);
      this.setStatus("", "");
    }
  }
  // Where this chat's agent stands. A session that exists keeps the folder it
  // was created with (the backend remembers it); a new one takes the folder
  // the ＋ menu picked, else the knowledgebase root.
  sessionCwd() {
    const chats = (this.conn && this.conn.hello && this.conn.hello.chats) || [];
    const known = this.sessionId ? chats.find((c) => c.id === this.sessionId) : null;
    if (known && typeof known.cwd === "string" && known.cwd.startsWith("/")) return known.cwd;
    return this.ctx.cwd ? shell.abs(this.ctx.cwd) : shell.repoRoot();
  }
  // Attach to the session's log. Until the backend answers "attached", every
  // frame waits: the replay and the live stream may cross, and a live chunk
  // drawn before the replay would sit above the whole transcript.
  attach(have) {
    this.attaching = true; this.buffer = []; this.dropBuffer = false;
    this.conn.send({ kb: "attach", sessionId: this.sessionId, have });
  }
  onLooseNotification(m) {
    if (m.method === "_auth/status_update" && this.conn.hello) { this.conn.hello.auth = m.params.authStatus || m.params; this.renderHead(this.conn.hello); }
  }
  // The head: the title, who you are talking to, the mode, and one ⋯ menu
  // with the rest. On a phone the pill moves into the menu (CSS hides it).
  // The tab strip names the chat (the window system's title bar); what is
  // left of the old head is the ⋯ menu, which lives in the composer.
  renderHead(h) {
    if (this.composer) this.composer.placeholder = this.placeholderText();
    if (!this.title) shell.renameTab(this.t, h.agent.name);
    this.authLabel = h.agent.name + (h.auth && h.auth.label ? " · " + h.auth.label : "");
    this.closeMenu();
    this.renderModes();
  }
  // One popup at a time, whichever button opened it: the ⋯ and the ＋ share
  // this so a second click (or the other button) closes the first.
  popMenu(wrap, btn, build) {
    const again = this.menuBtn === btn && this.menu;
    this.closeMenu();
    if (again) return;
    const m = el("div", "chat-menu"); m.setAttribute("role", "menu");
    const api = {
      item: (icon, text, fn, on) => {
        const b = el("button", "chat-menu-item" + (on ? " on" : "")); b.type = "button";
        b.setAttribute("role", "menuitem");
        b.innerHTML = icon; b.append(el("span", "", text));
        if (on) b.insertAdjacentHTML("beforeend", ICON.check);
        b.addEventListener("click", () => { this.closeMenu(); fn(); });
        m.append(b); return b;
      },
      head: (text) => m.append(el("div", "chat-menu-h", text)),
      id: (text) => m.append(el("div", "chat-menu-id", text)),
      sep: () => m.append(el("div", "chat-menu-sep")),
      note: (text) => m.append(el("div", "chat-menu-note", text)),
    };
    build(api);
    wrap.append(m);
    this.menu = m; this.menuBtn = btn;
    btn.setAttribute("aria-expanded", "true");
    this.menuOff = (e) => { if (!wrap.contains(e.target)) this.closeMenu(); };
    this.menuKey = (e) => { if (e.key === "Escape") { this.closeMenu(); this.composer.focus(); } };
    document.addEventListener("pointerdown", this.menuOff, true);
    document.addEventListener("keydown", this.menuKey, true);
    const first = m.querySelector("button");
    if (first) first.focus();
  }
  toggleMenu(h, btn, label) {
    this.popMenu(this.moreWrap, btn, ({ item, id }) => {
      id(label);
      item(ICON.plus, "New chat", () => shell.openChat({ agent: this.agentId }));
      if (this.sessionId) item(ICON.pencil, "Rename", () => this.renameDialog());
      item(ICON.history, "Recent chats", () => this.showHistory());
      item(ICON.agent, "Agent & sign-in", () => this.showPicker({ keep: true }));
      item(ICON.log, "Agent log", () => this.showPicker({ keep: true, section: "log" }));
    });
  }
  // ＋ — what the agent should look at, and where it should stand
  toggleAddMenu(btn, pickImage) {
    this.popMenu(this.addWrap, btn, ({ item, head, sep, note }) => {
      head("Context");
      item(ICON.file, "Add a file…", () => this.addCtxFile());
      item(ICON.folder, this.ctx.cwd === null ? "Work in a folder…" : "Change the working folder…",
           () => this.pickCwd());
      item(ICON.eye, this.ctx.off ? "Include what I have open" : "Including what I have open",
           () => { this.ctx.off = !this.ctx.off; this.ctx.mute = []; this.saveCtx(); }, !this.ctx.off);
      sep();
      head("Attach");
      item(ICON.image, "An image…", pickImage);
      if (!this.sessionId && this.ctx.cwd)
        note("A chat keeps the folder it starts in.");
    });
  }
  async renameDialog() {
    const t = await shell.prompt("Name this chat", this.title || "", { title: "Rename", ok: "Rename" });
    if (t === null || t === undefined) return;
    const clean = plainTitle(t);
    if (clean) this.rename(clean, true);
  }
  closeMenu() {
    if (!this.menu) return;
    this.menu.remove(); this.menu = null;
    if (this.menuBtn) this.menuBtn.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", this.menuOff, true);
    document.removeEventListener("keydown", this.menuKey, true);
  }

  // -- the first screen: which agent, are you signed in, recent chats --
  async showPicker(opts = {}) {
    let data;
    try { data = await (await fetch("/api/acp/agents")).json(); } catch (e) { data = { agents: [], default: this.agentId }; }
    shell.setRepoRoot(data.cwd);
    const showHidden = /\bdev\b/.test(location.hash) || (data.agents || []).some((a) => a.hidden && a.id === this.agentId);
    data.agents = (data.agents || []).filter((a) => !a.hidden || showHidden);
    const chats = await fetch("/api/acp/chats").then((r) => r.json()).then((j) => j.chats || []).catch(() => []);
    const box = el("div", "chat-picker");
    box.setAttribute("data-testid", "chat-picker");
    if (opts.error) {
      const e = el("div", "chat-error");
      e.append(el("div", "", opts.error));
      if (opts.stderr && opts.stderr.length) { const pre = el("pre", "chat-stderr", opts.stderr.slice(-12).join("\n")); e.append(pre); }
      box.append(e);
    }
    const isAdmin = shell.isAdmin();
    const state = agentState;
    // the one you can talk to first, then the ones a sign-in away, then the
    // ones an admin must install; the agent of this chat leads its group
    const cur = (a) => (a.id === this.agentId ? 0 : 1);
    // an employee sees only what can be used or signed into; an admin also
    // gets the rest, folded away, to install
    const usable = data.agents.filter((a) => a.installed);
    const missing = data.agents.filter((a) => !a.installed && a.npm !== false);
    const byHand = data.agents.filter((a) => !a.installed && a.npm === false);
    const agents = usable.slice().sort((x, y) => state(x).rank - state(y).rank || cur(x) - cur(y));
    // an agent installed per person is listed for everyone, with its note
    const missingList = (isAdmin ? missing.slice() : []).concat(byHand);
    const agentsSection = () => {
      const frag = document.createDocumentFragment();
      frag.append(el("h3", "", "Agents"));
      const list = el("div", "chat-agents");
      for (const a of agents) {
        const st = state(a);
        const card = el("div", "chat-agent" + (a.id === this.agentId ? " chosen" : ""));
        card.dataset.agent = a.id;
        const row = el("div", "chat-agent-row");
        card.append(row);
        row.append(el("span", "chat-agent-dot " + st.cls));
        const main = el("div", "chat-agent-main");
        const id = el("div", "chat-agent-id");
        id.append(el("strong", "", a.name), el("span", "chat-agent-vendor", a.vendor || ""));
        main.append(id, el("div", "chat-agent-status " + st.cls, st.text));
        row.append(main);
        const acts = el("div", "chat-agent-actions");
        if (a.installed) {
          // the first thing to press is the one that works: a chat when the
          // agent can answer, the sign-in when it cannot yet
          const canLogin = a.login && a.login.how === "terminal" && a.loginCommand;
          const start = el("button", st.rank === 2 && canLogin ? "" : "primary", a.id === this.agentId && this.sessionId ? "Use" : "Start chat");
          start.type = "button"; start.setAttribute("data-testid", "chat-start-" + a.id);
          start.addEventListener("click", () => this.startWith(a.id));
          if (!(st.rank === 2 && canLogin)) acts.append(start);
          if (canLogin) {
            const b = el("button", st.rank === 2 ? "primary" : "", st.rank === 0 ? "Sign in again" : "Sign in");
            b.type = "button"; b.title = a.login.note || "";
            b.addEventListener("click", () => this.signIn(a));
            acts.append(b);
            if (st.rank === 2) acts.append(start);
          }
          if (a.keys && a.keys.length) {
            const b = el("button", "", a.hasKey ? "Change API key" : "Paste API key");
            b.type = "button";
            b.addEventListener("click", () => this.pasteKey(a));
            acts.append(b);
          }
        } else if (!a.npm) {
          // not an npm package an admin installs for everyone: it lives in
          // the person's own home, so the person installs it themselves
          acts.append(el("span", "chat-agent-ask", "install it in your own home"));
        } else if (isAdmin) {
          const b = el("button", "primary", "Install");
          b.type = "button"; b.title = "npm install into " + data.prefix + " (admins only)";
          b.addEventListener("click", () => this.install(a, b));
          acts.append(b);
        } else {
          acts.append(el("span", "chat-agent-ask", "ask an admin to install it"));
        }
        row.append(acts);
        if (a.installed && st.rank === 2 && a.login && a.login.note) card.append(el("div", "chat-agent-note", a.login.note));
        else if (!a.installed && a.install && a.install.note) card.append(el("div", "chat-agent-note", a.install.note));
        list.append(card);
      }
      if (!agents.length) list.append(el("div", "chat-agent-note", isAdmin ? "No agent is installed on this server yet — install one below." : "No agent is installed on this server yet. Ask an admin."));
      frag.append(list);
      if (missingList.length) {
        const det = el("details", "chat-install-det");
        det.append(el("summary", "", "Install more agents (" + missingList.length + ")"));
        const more = el("div", "chat-agents");
        for (const a of missingList) {
          const card = el("div", "chat-agent"); card.dataset.agent = a.id;
          const row = el("div", "chat-agent-row");
          row.append(el("span", "chat-agent-dot off"));
          const main = el("div", "chat-agent-main");
          const id = el("div", "chat-agent-id"); id.append(el("strong", "", a.name), el("span", "chat-agent-vendor", a.vendor || ""));
          main.append(id, el("div", "chat-agent-status off", "not installed"));
          row.append(main);
          const acts = el("div", "chat-agent-actions");
          const b = el("button", "primary", "Install"); b.type = "button";
          b.title = "npm install into " + data.prefix + " (admins only)";
          b.addEventListener("click", () => this.install(a, b));
          acts.append(b); row.append(acts); card.append(row); more.append(card);
        }
        det.append(more);
        frag.append(det);
      }
      return frag;
    };
    const historySection = () => {
      const frag = document.createDocumentFragment();
      if (!chats.some((x) => !(x.id === this.sessionId && !x.title))) return frag;
      frag.append(el("h3", "", "Recent chats"));
      const list = el("div", "chat-history");
      // this very chat, still empty, is not "recent"
      for (const c of chats.filter((x) => !(x.id === this.sessionId && !x.title)).slice(0, 40)) {
        const row = el("button", "chat-history-row"); row.type = "button";
        const when = new Date((c.updatedAt || 0) * 1000);
        row.title = (c.title || "New chat") + " · " + when.toLocaleString();
        row.append(el("span", "chat-history-title", c.title || "New chat"),
          el("span", "chat-history-meta", (shell.agentName(c.agent) || c.agent) + " · " + ago(c.updatedAt)));
        row.addEventListener("click", () => this.resume(c));
        list.append(row);
      }
      frag.append(list);
      return frag;
    };
    if (!opts.keep) {
      box.append(el("h2", "chat-picker-title", "Start a chat"));
      box.append(el("p", "chat-picker-lead", "Agents run as you, in the knowledgebase: what you can read, they can read; what you can write, they can write. Sign in once per agent; the sign-in happens in a terminal tab."));
    }
    if (opts.section === "history") { box.append(historySection(), agentsSection()); }
    else { box.append(agentsSection(), historySection()); }
    if (opts.keep) {
      const det = el("details", "chat-log-det");
      const lines = this.conn ? this.conn.stderr.slice(-80) : [];
      det.append(el("summary", "", "Agent log (stderr)"), el("pre", "chat-stderr", lines.length ? lines.join("\n") : "(nothing yet)"));
      det.open = opts.section === "log";
      box.append(det);
      const back = el("button", "chat-back", "← Back to the chat"); back.type = "button";
      back.addEventListener("click", () => { this.showChat(); this.composer.focus(); });
      box.append(back);
      if (opts.section === "log") requestAnimationFrame(() => det.scrollIntoView({ block: "start" }));
    }
    if (this.picker) this.picker.remove();
    this.picker = box;
    this.body.hidden = true; this.foot.hidden = true; this.planEl.hidden = true;
    this.t.el.append(box);
  }
  async startWith(agentId) {
    if (this.conn && this.agentId !== agentId) { this.conn.tabs.delete(this); if (!this.conn.tabs.size) this.conn.close(); this.conn = null; }
    this.agentId = agentId;
    this.t.chat.agent = agentId;
    this.showChat();
    this.rename(this.title);
    this.connect();
    if (!this.sessionId) await this.newSession();
    else this.composer.focus();
  }
  // The agent you chat with becomes the one the chat button opens next time:
  // on this device at once, and as your setting (which roams) when the
  // registry knows the agent — the test double never is.
  rememberAgent() {
    try { localStorage.setItem("kbChatAgent", this.agentId); } catch (e) { /* private mode */ }
    try {
      const st = shell.settings;
      const known = (st.entry("ai.agent") || {}).options || [];
      if (st && known.includes(this.agentId) && st.get("ai.agent") !== this.agentId) st.set("ai.agent", this.agentId);
    } catch (e) { /* a viewer without the setting, an old backend */ }
  }
  // One session start at a time: a message typed while the agent is still
  // answering session/new waits for that session instead of making a second
  // one (which then stole the transcript and the updates).
  newSession() {
    if (this.starting) return this.starting;
    this.starting = this._newSession().finally(() => { this.starting = null; });
    return this.starting;
  }
  async _newSession() {
    this.setStatus("Starting a session…", "busy");
    try {
      const res = await this.waitHello().then(() => this.conn.request("session/new", { cwd: this.sessionCwd(), mcpServers: [] }));
      this.sessionId = res.sessionId;
      this.t.chat.sessionId = res.sessionId;
      this.seq = 0;
      this.clearLog();
      this.modes = res.modes || null; this.configOptions = res.configOptions || null;
      this.renderModes();
      this.restored = false; this.loadedOnce = true;   // born here: nothing to load
      this.attach(0);
      this.setStatus("");
      this.showEmpty();
      shell.saveSession();
      this.rememberAgent();
      this.composer.focus();
    } catch (e) {
      if (e.code === -32000) { this.setStatus("", ""); this.showPicker({ error: this.agentName() + " needs you to sign in first." }); return; }
      this.setStatus(e.message || "could not start a session", "err");
    }
  }
  waitHello() {
    return new Promise((resolve, reject) => {
      const tick = (n) => {
        if (this.conn && this.conn.hello) return resolve();
        if (this.conn && this.conn.error) return reject(new Error(this.conn.error.message));
        if (n > 600) return reject(new Error("the agent did not start"));
        setTimeout(() => tick(n + 1), 200);
      };
      tick(0);
    });
  }
  async resume(c) {
    this.showChat();
    if (this.sessionId && this.conn) {
      // this tab already holds a chat: open the old one beside it
      shell.openChat({ agent: c.agent, sessionId: c.id, title: c.title });
      return;
    }
    this.agentId = c.agent; this.t.chat.agent = c.agent;
    this.sessionId = c.id; this.t.chat.sessionId = c.id;
    this.rename(c.title || "");
    this.loadedOnce = true;
    this.restored = true;      // it has a past: its folder is the agent's, not ours to change
    this.connect();
    await this.loadHistory();
    this.rememberAgent();
  }
  async loadHistory() {
    // The backend replays what it still holds on attach; if that does not reach
    // the beginning (a fresh process, a long chat), ask the agent for the whole
    // conversation — session/load streams it back as updates.
    try { await this.waitHello(); } catch (e) { return; }
    const caps = (this.conn.hello.init && this.conn.hello.init.agentCapabilities) || {};
    if (!caps.loadSession) return;
    this.setStatus("Loading the conversation…", "busy");
    this.clearLog(); this.seq = 0;
    this.loading = true;
    try {
      const res = await this.conn.request("session/load", { sessionId: this.sessionId, cwd: this.sessionCwd(), mcpServers: [] });
      this.modes = res.modes || this.modes; this.configOptions = res.configOptions || this.configOptions;
      this.renderModes();
      this.setStatus("");
    } catch (e) {
      if (e.code === -32000) this.showPicker({ error: this.agentName() + " needs you to sign in first." });
      else this.setStatus("Could not load: " + e.message, "err");
    }
    this.loading = false;
    this.finishThought();
    if (!this.log.querySelector(".chat-msg, .chat-tool, .chat-agent-block")) {
      // an agent that keeps no transcript (or a chat that predates it): say so,
      // instead of an empty screen that looks like a broken one
      this.addNote("This agent kept no transcript of this chat — what you write now starts a new one here.");
    }
    this.showEmpty();
    shell.saveSession();
  }
  async showHistory() { await this.showPicker({ keep: true, section: "history" }); }
  signIn(a) {
    shell.toast("Signing in to " + a.name + " in a terminal tab. Come back here when it says you are signed in.", "ok");
    shell.openTermWith(a.loginCommand);
    // after the login the running agent must restart to pick the credentials up
    this.awaitingLogin = a.id;
  }
  async pasteKey(a) {
    const key = await shell.prompt("Paste the API key for " + a.name + " (" + a.keys[0] + "). It is stored in your own settings folder, readable only by you.", "", { title: a.name + " API key", ok: "Save", password: true });
    if (key === null || key === undefined) return;
    const r = await fetch("/api/acp/key", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agent: a.id, key }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { shell.toast(j.error || "could not save the key", "err"); return; }
    shell.toast(key.trim() ? "Key saved" : "Key removed", "ok");
    if (this.conn && this.conn.agentId === a.id) { this.conn.close(); this.conn = null; }
    await this.showPicker({ keep: !!this.sessionId });
  }
  async install(a, btn) {
    btn.disabled = true; btn.textContent = "Installing…";
    const r = await fetch("/admin/agents/install", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: a.id }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { btn.disabled = false; btn.textContent = "Install"; shell.toast(j.error || "install failed", "err"); return; }
    const poll = async () => {
      const s = await fetch("/admin/agents").then((x) => x.json()).catch(() => null);
      if (!s || !s.job) return;
      if (s.job.running) { setTimeout(poll, 3000); return; }
      shell.toast(s.job.ok ? a.name + " installed" : "install failed — see the log", s.job.ok ? "ok" : "err");
      if (!s.job.ok) console.error(s.job.log);
      await this.showPicker({ keep: !!this.sessionId });
    };
    setTimeout(poll, 3000);
  }

  // -- modes and config options in the head --
  // the modes and the agent's own switches live in the chip's menu; the chip
  // names the current mode beside the agent
  renderModes() {
    this.renderChip();
    if (this.agentMenu) this.openAgentMenu(true);
  }
  currentModeName() {
    const m = this.modes; if (!m || !m.availableModes) return "";
    const cur = m.availableModes.find((x) => x.id === m.currentModeId);
    return cur ? cur.name : "";
  }
  renderChip() {
    const b = this.agentChip; if (!b) return;
    b.innerHTML = "";
    const name = this.agentName(), mode = this.currentModeName();
    b.append(shell.agentAvatar(this.agentId, "sm"), el("span", "chat-agent-chip-name", name));
    if (mode) b.append(el("span", "chat-agent-chip-mode", "· " + mode));
    b.insertAdjacentHTML("beforeend", ICON.chevronDown);
    b.title = name + (mode ? " · " + mode : "") + " — change the agent or the mode";
    b.setAttribute("aria-label", b.title);
  }
  async toggleAgentMenu() { if (this.agentMenu) { this.closeAgentMenu(); return; } await this.openAgentMenu(); }
  async openAgentMenu(refresh) {
    if (!refresh) {
      // the agents and their sign-in state, fresh each time the menu opens
      try { const d = await (await fetch("/api/acp/agents")).json(); this.agentsCache = d; shell.setRepoRoot(d.cwd); }
      catch (e) { this.agentsCache = this.agentsCache || { agents: [] }; }
    }
    if (this.agentMenu) this.agentMenu.remove();
    const m = el("div", "chat-agent-menu"); m.setAttribute("role", "menu"); m.setAttribute("data-testid", "chat-agent-menu");
    const showHidden = /\bdev\b/.test(location.hash) || this.agentId === "echo";
    const agents = (this.agentsCache.agents || []).filter((a) => a.installed && (!a.hidden || showHidden));
    const radio = (on, txt) => {
      const b = el("button", "chat-menu-item" + (on ? " on" : "")); b.type = "button";
      b.setAttribute("role", "menuitemradio"); b.setAttribute("aria-checked", on ? "true" : "false");
      b.append(txt);
      if (on) b.insertAdjacentHTML("beforeend", ICON.check);
      return b;
    };
    m.append(el("div", "chat-menu-h", "Agent"));
    for (const a of agents) {
      const st = agentState(a);
      const txt = el("span", "chat-agent-item-txt");
      txt.append(el("span", "chat-agent-item-name", a.name), el("span", "chat-agent-item-st " + st.cls, st.text));
      const b = radio(a.id === this.agentId, txt);
      b.classList.add("chat-agent-item"); b.dataset.agent = a.id;
      b.prepend(shell.agentAvatar(a.id));
      b.addEventListener("click", () => { this.closeAgentMenu(); this.pickAgent(a, st); });
      m.append(b);
    }
    if (!agents.length) m.append(el("div", "chat-menu-note", shell.isAdmin() ? "No agent installed yet — Manage agents below." : "No agent installed yet — ask an admin."));
    // Claude Code offers its permission modes twice — as ACP `modes` and as a
    // config option also called "Mode". One section, not two.
    const dupMode = (this.configOptions || []).some((o) => String(o.name || "").trim().toLowerCase() === "mode");
    if (!dupMode && this.modes && this.modes.availableModes && this.modes.availableModes.length) {
      m.append(el("div", "chat-menu-h", "Mode"));
      const grp = el("div", "chat-mode-group"); grp.setAttribute("data-testid", "chat-mode"); grp.setAttribute("role", "group");
      for (const md of this.modes.availableModes) {
        const b = radio(md.id === this.modes.currentModeId, el("span", "chat-agent-item-txt", md.name));
        b.dataset.mode = md.id; b.title = md.description || "";
        b.addEventListener("click", () => this.setMode(md.id));
        grp.append(b);
      }
      m.append(grp);
    }
    // the agent's own switches (Codex's reasoning effort, …)
    for (const o of this.configOptions || []) {
      if (o.type === "select") {
        m.append(el("div", "chat-menu-h", o.name));
        const flat = []; for (const x of o.options || []) { if (x.options) flat.push(...x.options); else flat.push(x); }
        for (const x of flat) {
          const b = radio(x.value === o.currentValue, el("span", "chat-agent-item-txt", x.name || x.value));
          b.addEventListener("click", () => this.conn.request("session/set_config_option", { sessionId: this.sessionId, configId: o.id, value: x.value })
            .then((r) => { this.configOptions = r.configOptions || this.configOptions; this.renderModes(); }).catch((e) => shell.toast(e.message, "err")));
          m.append(b);
        }
      } else if (o.type === "boolean") {
        const b = radio(!!o.currentValue, el("span", "chat-agent-item-txt", o.name));
        b.setAttribute("role", "menuitemcheckbox");
        b.addEventListener("click", () => this.conn.request("session/set_config_option", { sessionId: this.sessionId, configId: o.id, type: "boolean", value: !o.currentValue })
          .then(() => { o.currentValue = !o.currentValue; this.renderModes(); }).catch((e) => shell.toast(e.message, "err")));
        m.append(b);
      }
    }
    m.append(el("div", "chat-menu-sep"));
    // where ⋯ has no room of its own (a phone's one-row composer), its
    // items live at the foot of this menu: one control, everything in it
    if (this.moreWrap && getComputedStyle(this.moreWrap).display === "none") {
      const it = (icon, text, fn) => {
        const b = el("button", "chat-menu-item"); b.type = "button"; b.innerHTML = icon;
        b.append(el("span", "chat-agent-item-txt", text));
        b.addEventListener("click", () => { this.closeAgentMenu(); fn(); });
        m.append(b);
      };
      it(ICON.plus, "New chat", () => shell.openChat({ agent: this.agentId }));
      if (this.sessionId) it(ICON.pencil, "Rename", () => this.renameDialog());
      it(ICON.history, "Recent chats", () => this.showHistory());
      it(ICON.log, "Agent log", () => this.showPicker({ keep: true, section: "log" }));
      m.append(el("div", "chat-menu-sep"));
    }
    const cur = agents.find((a) => a.id === this.agentId);
    if (cur && agentState(cur).rank === 2 && cur.login && cur.login.how === "terminal" && cur.loginCommand) {
      const b = el("button", "chat-menu-item"); b.type = "button";
      b.append(el("span", "chat-agent-item-txt", "Sign in to " + cur.name + "…"));
      b.addEventListener("click", () => { this.closeAgentMenu(); this.signIn(cur); });
      m.append(b);
    }
    const manage = el("button", "chat-menu-item"); manage.type = "button";
    manage.append(el("span", "chat-agent-item-txt", "Manage agents & sign-in…"));
    manage.addEventListener("click", () => { this.closeAgentMenu(); this.showPicker({ keep: true }); });
    m.append(manage);
    // …on the page, not in the pane: the chat clips its overflow, and in
    // the terminal panel the list lost its first agents to that edge
    document.body.append(m);
    this.agentMenu = m; this.agentChip.setAttribute("aria-expanded", "true");
    this.placeAgentMenu();
    if (!refresh) {
      this.agentMenuMove = () => this.placeAgentMenu();
      window.addEventListener("resize", this.agentMenuMove);
      window.addEventListener("scroll", this.agentMenuMove, true);
    }
    if (!refresh) {
      this.agentMenuOff = (e) => { if (!m.contains(e.target) && !this.agentChip.contains(e.target)) this.closeAgentMenu(); };
      this.agentMenuKey = (e) => {
        if (e.key === "Escape") { this.closeAgentMenu(); this.composer.focus(); return; }
        if (this.agentMenu && this.agentMenuNav(e)) e.preventDefault();
      };
      document.addEventListener("pointerdown", this.agentMenuOff, true);
      document.addEventListener("keydown", this.agentMenuKey, true);
      const first = m.querySelector(".chat-menu-item.on") || m.querySelector(".chat-menu-item");
      if (first) first.focus();
    }
  }
  // above the chip, never off the screen, never taller than the room it has
  placeAgentMenu() {
    const m = this.agentMenu; if (!m) return;
    const r = this.agentChip.getBoundingClientRect();
    const room = r.top - 12;
    m.style.maxHeight = Math.max(160, Math.min(room, window.innerHeight * 0.6, 28 * 16)) + "px";
    const w = m.getBoundingClientRect().width;
    m.style.left = Math.round(Math.max(8, Math.min(r.left, window.innerWidth - w - 8))) + "px";
    m.style.bottom = Math.round(window.innerHeight - r.top + 6) + "px";
  }
  // the arrows walk the menu, as a menu's do
  agentMenuNav(e) {
    const items = [...this.agentMenu.querySelectorAll(".chat-menu-item")];
    if (!items.length) return false;
    const i = items.indexOf(document.activeElement);
    let j = null;
    if (e.key === "ArrowDown") j = i < 0 ? 0 : (i + 1) % items.length;
    else if (e.key === "ArrowUp") j = i < 0 ? items.length - 1 : (i - 1 + items.length) % items.length;
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = items.length - 1;
    if (j === null) return false;
    items[j].focus();
    return true;
  }
  closeAgentMenu() {
    if (!this.agentMenu) return;
    this.agentMenu.remove(); this.agentMenu = null;
    this.agentChip.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", this.agentMenuOff, true);
    document.removeEventListener("keydown", this.agentMenuKey, true);
    if (this.agentMenuMove) {
      window.removeEventListener("resize", this.agentMenuMove);
      window.removeEventListener("scroll", this.agentMenuMove, true);
      this.agentMenuMove = null;
    }
  }
  setMode(id) {
    if (!this.conn || !this.sessionId || !this.modes) return;
    this.conn.request("session/set_mode", { sessionId: this.sessionId, modeId: id })
      .then(() => { this.modes.currentModeId = id; this.renderModes(); }).catch((e) => shell.toast(e.message, "err"));
  }
  // Another agent: an empty chat simply becomes that agent's; one with
  // messages keeps them, and the new agent gets a new chat beside it.
  pickAgent(a, st) {
    if (a.id === this.agentId) return;
    if (st.rank === 2 && a.login && a.login.how === "terminal" && a.loginCommand) { this.signIn(a); return; }
    const empty = !this.log.querySelector(".chat-msg, .chat-tool");
    if (empty) {
      this.sessionId = null; this.t.chat.sessionId = null; this.seq = 0;
      this.modes = null; this.configOptions = null; this.restored = false;
      this.startWith(a.id);
    } else {
      // a chat with a history stays with the agent that wrote it; the new
      // one opens beside it — say so, or the click looks like it did nothing
      const was = this.agentName();
      shell.openChat({ agent: a.id });
      shell.toast("New chat with " + a.name + " — this one stays with " + was, "ok");
    }
  }

  // -- sending --
  async sendPrompt() {
    const text = this.composer.value.trim();
    if ((!text && !this.attachments.length) || !this.conn) return;
    if (this.starting) await this.starting;
    if (!this.sessionId) { await this.newSession(); if (!this.sessionId) return; }
    if (this.dead) {
      // the old connection must not report its own closing as ours
      this.conn.tabs.delete(this); this.conn.close(); this.conn = null;
      this.connect();
      try { await this.waitHello(); } catch (e) { return; }
      this.attach(this.seq); this.dead = false;
    }
    const prompt = [];
    if (text) prompt.push({ type: "text", text });
    const ctx = this.ctxItems();
    for (const c of ctx)
      prompt.push({ type: "resource_link", uri: "file://" + shell.abs(c.path),
                    name: shell.baseName(c.path), title: c.path });
    for (const a of this.attachments) prompt.push({ type: "image", data: a.data, mimeType: a.mimeType });
    this.composer.value = ""; this.autosize(); this.slash.hidden = true;
    this.attachments = []; this.renderAttachments(); this.syncSend();
    this.spoke = true;
    const wrap = this.addUser(text, prompt.filter((p) => p.type === "image"), ctx);
    if (!this.title && text) this.rename(plainTitle(text), true);
    const item = { prompt, text, wrap };
    // one turn at a time: a message written while the agent is working waits
    // its turn rather than racing it
    if (this.running) { this.holdQueued(item); return; }
    await this.deliver(item);
  }
  // One prompt to the agent. A connection that is down holds it (the bubble
  // says "Not sent") and sends it when the agent is back; one that drops
  // mid-flight leaves the outcome unknown, so that one waits for a hand.
  async deliver(item) {
    if (!this.conn || !this.conn.ws || this.conn.ws.readyState !== 1) { this.holdUnsent(item, false); return; }
    this.sentEcho = item.text;
    this.setRunning(true);
    this.current = null; this.thought = null;
    try {
      const res = await this.conn.request("session/prompt", { sessionId: this.sessionId, prompt: item.prompt });
      this.endTurn(res.stopReason);
    } catch (e) {
      this.setRunning(false);
      if (e.code === -32000) { this.showPicker({ error: this.agentName() + " needs you to sign in first." }); return; }
      if (e.message === "not connected") { this.holdUnsent(item, false); return; }
      if (e.message === "connection lost") { this.holdUnsent(item, true); return; }
      this.setStatus(e.message, "err");
    }
  }
  // Waiting for the turn in front of it. The bubble says so, and the item
  // goes the moment the agent finishes (or is stopped).
  holdQueued(item) {
    this.queued = this.queued || [];
    this.queued.push(item);
    item.wrap.classList.add("queued");
    if (!item.wrap.querySelector(".chat-queued")) {
      const mark = el("div", "chat-queued", "Queued — goes when this turn ends");
      const x = el("button", "chat-queued-x", "Cancel"); x.type = "button";
      x.addEventListener("click", () => {
        this.queued = this.queued.filter((q) => q !== item);
        item.wrap.remove();
      });
      mark.append(x);
      item.wrap.append(mark);
    }
  }
  async flushQueued() {
    const waiting = this.queued || [];
    this.queued = [];
    for (const item of waiting) {
      if (!item.wrap.isConnected) continue;          // cancelled while it waited
      item.wrap.classList.remove("queued");
      const mark = item.wrap.querySelector(".chat-queued");
      if (mark) mark.remove();
      await this.deliver(item);
    }
  }
  holdUnsent(item, manual) {
    item.manual = manual;
    if (!this.outbox.includes(item)) this.outbox.push(item);
    item.wrap.classList.add("unsent");
    let mark = item.wrap.querySelector(".chat-unsent");
    if (!mark) {
      mark = el("div", "chat-unsent");
      const b = el("button", "chat-unsent-retry", "Retry"); b.type = "button";
      b.addEventListener("click", () => { item.manual = false; this.flushOutbox(); });
      mark.append(el("span", "chat-unsent-text", ""), b);
      item.wrap.append(mark);
    }
    mark.querySelector(".chat-unsent-text").textContent = manual ? "The connection dropped while sending · " : "Not sent · ";
    this.setStatus(manual ? "The connection dropped — check the reply, then retry if it never came" : "Not connected — it goes when the agent is back", "err");
    this.syncJump();
  }
  async flushOutbox() {
    if (this.flushing) return;
    this.flushing = true;
    try {
      while (this.outbox.length && this.conn && this.conn.ws && this.conn.ws.readyState === 1) {
        const item = this.outbox.find((x) => !x.manual);
        if (!item) break;
        this.outbox.splice(this.outbox.indexOf(item), 1);
        item.wrap.classList.remove("unsent");
        const mark = item.wrap.querySelector(".chat-unsent"); if (mark) mark.remove();
        if (this.statusKind === "err") this.setStatus("");
        await this.deliver(item);
        if (this.outbox.includes(item)) break;   // held again
      }
    } finally { this.flushing = false; }
  }
  cancel() {
    if (!this.conn || !this.sessionId) return;
    this.conn.notify("session/cancel", { sessionId: this.sessionId });
    this.setStatus("Stopping…", "busy");
    // an agent that never confirms the stop: the turn is over here anyway,
    // as Claude Code gives up on it — the composer comes back
    clearTimeout(this.cancelTimer);
    this.cancelTimer = setTimeout(() => { if (this.running) this.endTurn("cancelled"); }, 5000);
  }
  endTurn(stopReason) {
    clearTimeout(this.cancelTimer); this.cancelTimer = null;
    this.finishThought();
    this.setRunning(false);
    this.sentEcho = null;
    this.current = null; this.thought = null;
    // a tool the agent left "running" is not: the turn has ended
    const left = stopReason === "cancelled" ? "cancelled" : stopReason === "error" ? "failed" : "completed";
    for (const [, tc] of this.tools) if (tc.data.status === "pending" || tc.data.status === "in_progress") this.updateTool({ toolCallId: tc.data.toolCallId, status: left });
    if (stopReason && stopReason !== "end_turn") this.addNote(stopReason === "cancelled" ? "Interrupted" : "Stopped: " + stopReason);
    if (this.queued && this.queued.length) this.flushQueued();
  }
  renderAttachments() {
    this.attachRow.innerHTML = "";
    this.attachRow.hidden = !this.attachments.length;
    this.attachments.forEach((a, i) => {
      const chip = el("span", "chat-chip");
      const img = document.createElement("img"); img.src = "data:" + a.mimeType + ";base64," + a.data; img.alt = a.name;
      const x = el("button", "chat-chip-x", "×"); x.type = "button"; x.setAttribute("aria-label", "Remove"); x.addEventListener("click", () => { this.attachments.splice(i, 1); this.renderAttachments(); this.syncSend(); });
      chip.append(img, x); this.attachRow.append(chip);
    });
  }

  // -- context: what the agent is pointed at ------------------------------
  // Not a copy of the text — the agent runs as you, in the same
  // knowledgebase, so it reads the file itself. Every prompt carries the
  // paths as resource_link blocks (the way an editor sends an @-mention).
  ctxAuto() {
    if (this.ctx.off || !shell.openDocs) return [];
    const mine = new Set(this.ctx.files);
    return shell.openDocs()
      .filter((d) => !mine.has(d.path) && !this.ctx.mute.includes(d.path))
      .slice(0, 4);
  }
  ctxItems() {
    return [...this.ctxAuto().map((d) => ({ path: d.path, auto: true })),
            ...this.ctx.files.map((path) => ({ path, auto: false }))];
  }
  saveCtx() {
    // a muted document that is no longer open has nothing to mute: forget it,
    // so re-opening the file brings its chip back
    const open = new Set((shell.openDocs ? shell.openDocs() : []).map((d) => d.path));
    this.ctx.mute = this.ctx.mute.filter((p) => open.has(p));
    this.t.chat.ctx = { off: this.ctx.off, mute: this.ctx.mute.slice(),
                        files: this.ctx.files.slice(), cwd: this.ctx.cwd };
    if (shell.saveSession) shell.saveSession();
    this.renderCtx();
  }
  // the window's open documents changed under us
  docsChanged() {
    const sig = this.ctxAuto().map((d) => d.path).join("\u0000");
    if (sig !== this.ctxSig) this.renderCtx();
  }
  renderCtx() {
    if (!this.ctxRow) return;
    // a restored chip for a folder that has since been deleted (the backend
    // would fall back to the root anyway, so the chip would be a lie)
    if (this.ctx.cwd && !this.sessionId && shell.hasPath && shell.knowsTree && shell.knowsTree()
        && !shell.hasPath(this.ctx.cwd)) this.ctx.cwd = null;
    const items = this.ctxItems();
    this.ctxSig = items.filter((i) => i.auto).map((i) => i.path).join("\u0000");
    this.ctxRow.innerHTML = "";
    this.ctxRow.hidden = !items.length && this.ctx.cwd === null;
    if (this.ctx.cwd !== null) {
      const name = this.ctx.cwd ? shell.baseName(this.ctx.cwd) : "the knowledgebase";
      const chip = this.ctxChip(ICON.folder, name, "cwd");
      chip.title = "Working folder: " + (this.ctx.cwd || "the whole knowledgebase")
        + (this.sessionId ? " — set when this chat started" : "");
      chip.firstChild.addEventListener("click", () => this.pickCwd());
      const x = chip.querySelector(".chat-ctx-x");
      // a live session IS standing in that folder — the chip states a fact,
      // and a × that only hid the fact would be a lie
      if (this.sessionId) x.remove();
      else x.addEventListener("click", () => { this.ctx.cwd = null; this.saveCtx(); });
      this.ctxRow.append(chip);
    }
    for (const it of items) {
      const chip = this.ctxChip(ICON.file, shell.baseName(it.path), it.auto ? "auto" : "");
      chip.title = it.path + (it.auto ? " — open in the window" : "");
      chip.firstChild.addEventListener("click", () => shell.openPath && shell.openPath(it.path));
      chip.querySelector(".chat-ctx-x").addEventListener("click", () => {
        if (it.auto) this.ctx.mute.push(it.path);
        else this.ctx.files = this.ctx.files.filter((p) => p !== it.path);
        this.saveCtx();
      });
      this.ctxRow.append(chip);
    }
  }
  ctxChip(icon, name, cls) {
    const chip = el("span", "chat-ctx-chip " + (cls || ""));
    const b = el("button", "chat-ctx-open"); b.type = "button";
    b.innerHTML = icon; b.append(el("span", "chat-ctx-name", name));
    const x = el("button", "chat-ctx-x", "×"); x.type = "button";
    x.setAttribute("aria-label", "Remove " + name); x.title = "Remove";
    chip.append(b, x);
    return chip;
  }
  async addCtxFile() {
    const path = await shell.pickPath({ kind: "file", placeholder: "Filter files…" });
    if (!path) return;
    if (this.ctx.files.length >= MAX_CTX_FILES) {
      shell.toast("That is enough context for one chat (" + MAX_CTX_FILES + " files)", "err");
      return;
    }
    this.ctx.mute = this.ctx.mute.filter((p) => p !== path);
    if (!this.ctx.files.includes(path)) this.ctx.files.push(path);
    this.saveCtx();
    this.composer.focus();
  }
  // The folder a session runs in is fixed when the session starts (ACP takes
  // it in session/new), so changing it on a chat that has already spoken
  // opens a new one rather than quietly lying about where the agent stands.
  async pickCwd() {
    const path = await shell.pickPath({ kind: "dir", placeholder: "Filter folders…" });
    if (path === null) return;
    const rel = path || null;
    if (rel === this.ctx.cwd) return;
    // a chat that has already spoken keeps its folder: the agent is standing
    // there. Picking another one opens a chat that starts there.
    if (this.spoke || this.restored) {
      shell.openChat({ agent: this.agentId, ctx: { cwd: rel } });
      shell.toast("New chat in " + (rel ? shell.baseName(rel) : "the knowledgebase")
                  + " — a chat keeps the folder it started in", "ok");
      return;
    }
    this.ctx.cwd = rel;
    this.saveCtx();
    // the session opened when the chat did, standing in the old folder, and
    // nothing has been said in it: make it again, here.
    const stale = this.sessionId;
    if (stale) {
      if (this.conn) this.conn.send({ kb: "detach", sessionId: stale });
      this.sessionId = null; this.t.chat.sessionId = null; this.seq = 0;
      this.modes = null; this.configOptions = null;
      shell.saveSession();                       // …before anything can fail
      await this.newSession();
      // only once the replacement exists: a forget with no new session would
      // leave the tab pointing at a chat nobody can find again
      if (this.sessionId)
        fetch("/api/acp/forget", { method: "POST", headers: { "content-type": "application/json" },
                                   body: JSON.stringify({ id: stale }) })
          .then(() => shell.chatsChanged && shell.chatsChanged()).catch(() => {});
    }
    this.composer.focus();
  }

  // -- slash commands: Claude Code's popup, /name on the left, what it does on the right --
  slashHint() {
    const v = this.composer.value;
    if (!v.startsWith("/") || v.includes("\n") || !this.commands.length) { this.slash.hidden = true; return; }
    const q = v.slice(1).toLowerCase();
    const hits = this.commands.filter((c) => c.name.toLowerCase().startsWith(q)).slice(0, 8);
    if (!hits.length) { this.slash.hidden = true; return; }
    this.slash.innerHTML = "";
    hits.forEach((c, i) => {
      const row = el("div", "chat-slash-row" + (i === 0 ? " sel" : ""));
      row.append(el("span", "chat-slash-name", "/" + c.name), el("span", "chat-slash-desc", c.description || ""));
      if (c.input && c.input.hint) row.append(el("span", "chat-slash-hint", c.input.hint));
      row.addEventListener("mousedown", (e) => { e.preventDefault(); this.slashPick(i); });
      this.slash.append(row);
    });
    this.slash.hidden = false;
  }
  slashMove(d) {
    const rows = [...this.slash.children]; if (!rows.length) return;
    let i = rows.findIndex((r) => r.classList.contains("sel"));
    rows[i].classList.remove("sel"); i = (i + d + rows.length) % rows.length; rows[i].classList.add("sel");
    rows[i].scrollIntoView({ block: "nearest" });
  }
  slashPick(i) {
    const rows = [...this.slash.children]; if (!rows.length) return;
    const row = i === undefined ? rows.find((r) => r.classList.contains("sel")) : rows[i];
    const name = row.querySelector(".chat-slash-name").textContent;
    const cmd = this.commands.find((c) => "/" + c.name === name);
    this.composer.value = name + " ";
    this.slash.hidden = true;
    if (cmd && !(cmd.input && cmd.input.hint)) this.sendPrompt(); else this.composer.focus();
  }

  // -- incoming --
  onSessionMessage(m) {
    if (m.kb === "reset") {
      // the log no longer reaches back: session/load brings the whole chat,
      // so the replay that follows the reset is not wanted twice
      this.dropBuffer = true; this.seq = m.seq || 0; this.loadedOnce = true; this.loadHistory(); return;
    }
    if (m.kb === "attached") {
      this.attaching = false;
      const frames = this.dropBuffer ? [] : this.buffer.slice();
      this.buffer = []; this.dropBuffer = false;
      // the replay and what streamed meanwhile, in the log's order, once each
      frames.sort((a, b) => (typeof a.seq === "number" && a.seq >= 0 ? a.seq : Infinity) - (typeof b.seq === "number" && b.seq >= 0 ? b.seq : Infinity));
      for (const f of frames) this.onSessionMessage(f);
      if (m.meta && (m.meta.modes || m.meta.configOptions)) {
        this.modes = m.meta.modes || this.modes; this.configOptions = m.meta.configOptions || this.configOptions;
        this.renderModes();
      }
      // a chat from before whose agent process is new (an idle stop, a
      // restart): the process has no log of it — ask the agent for the
      // conversation, as Recent chats does
      const caps = (this.conn && this.conn.hello && this.conn.hello.init && this.conn.hello.init.agentCapabilities) || {};
      if (this.restored && !this.loadedOnce && m.seq === 0 && !m.running && caps.loadSession) { this.loadedOnce = true; this.loadHistory(); return; }
      if (m.running && !this.running) this.setRunning(true);
      if (!m.running && !this.loading) { this.finishThought(); this.showEmpty(); }
      if (this.outbox.length) this.flushOutbox();
      return;
    }
    if (this.attaching) { this.buffer.push(m); return; }
    if (typeof m.seq === "number" && m.seq >= 0) {
      if (m.seq < this.seq) return;   // seen already (a replay crossing the live stream)
      this.seq = m.seq + 1;
    }
    if (m.kb === "turn") { if (this.running || m.stopReason) this.endTurn(m.stopReason || (m.error ? "error" : "end_turn")); if (m.error) this.setStatus(m.error.message || "error", "err"); return; }
    if (m.kb === "req") { this.onAgentRequest(m.m); return; }
    if (m.kb === "req-done") {
      const p = this.pendingPerms.get(m.id);
      if (p) { p.settle(m.label, m.kind ? (String(m.kind).startsWith("allow") ? "ok" : "no") : undefined); this.pendingPerms.delete(m.id); }
      return;
    }
    if (m.kb === "u") { this.onUpdate(m.m); }
  }
  onUpdate(n) {
    if (n.method !== "session/update") return;
    const u = n.params.update || {};
    switch (u.sessionUpdate) {
      case "user_message_chunk": this.appendUserChunk(u); break;
      case "agent_message_chunk": this.appendAgentChunk(u); break;
      case "agent_thought_chunk": this.appendThought(u); break;
      case "tool_call": this.addTool(u); break;
      case "tool_call_update": this.updateTool(u); break;
      case "plan": this.renderPlan(u.entries || []); break;
      case "available_commands_update": this.commands = u.availableCommands || []; break;
      case "current_mode_update": if (this.modes) { this.modes.currentModeId = u.currentModeId; this.renderModes(); } break;
      case "config_option_update": this.configOptions = u.configOptions || this.configOptions; this.renderModes(); break;
      case "session_info_update": if (u.title) this.rename(u.title); break;
      case "usage_update": this.renderUsage(u); break;
      default: break;
    }
    // a turn that was running when the page opened streams on without our
    // having sent anything: the chunks say so (a replay of the past does not)
    if (u.sessionUpdate && u.sessionUpdate !== "user_message_chunk" && !this.running && !this.loading && u.sessionUpdate.endsWith("_chunk")) this.setRunning(true);
    this.scrollIfPinned();
  }
  contentText(c) { return c && c.type === "text" ? c.text : ""; }
  appendUserChunk(u) {
    const c = u.content;
    if (this.running && this.sentEcho !== null) return;   // our own prompt, echoed back
    if (this.replayUser && (u.messageId === undefined || u.messageId === this.replayUser.id)) {
      if (c.type === "text") { this.replayUser.text += c.text; this.replayUser.body.textContent = this.replayUser.text; }
      return;
    }
    const wrap = this.addUser(c.type === "text" ? c.text : "", c.type === "image" ? [c] : []);
    this.replayUser = { id: u.messageId, text: c.type === "text" ? c.text : "", body: wrap.querySelector(".chat-text") };
    this.current = null;
  }
  addUser(text, images, ctx) {
    this.finishThought();
    this.replayUser = null;
    const wrap = el("div", "chat-msg user");
    const body = el("div", "chat-text", text); body.dir = "auto";
    wrap.append(body);
    if (ctx && ctx.length) {
      const row = el("div", "chat-msg-ctx");
      for (const c of ctx) {
        const b = el("button", "chat-ctx-chip sent"); b.type = "button"; b.title = c.path;
        b.innerHTML = ICON.file; b.append(el("span", "chat-ctx-name", shell.baseName(c.path)));
        b.addEventListener("click", () => shell.openPath && shell.openPath(c.path));
        row.append(b);
      }
      wrap.append(row);
    }
    for (const im of images || []) { const img = document.createElement("img"); img.className = "chat-img"; img.src = "data:" + im.mimeType + ";base64," + im.data; wrap.append(img); }
    this.push(wrap);
    this.pinned = true; this.scrollIfPinned();
    return wrap;
  }
  // an agent message: a ⏺ in the gutter, the text beside it — no bubble
  agentBlock(id) {
    const wrap = el("div", "chat-msg agent");
    const body = el("div", "chat-md"); body.dir = "auto";
    // claude.ai's row under an answer, reduced to the one that matters here
    const act = el("div", "chat-msg-actions");
    const cp = el("button", "chat-msg-act"); cp.type = "button"; cp.title = "Copy"; cp.setAttribute("aria-label", "Copy the answer");
    cp.innerHTML = ICON.copy;
    cp.addEventListener("click", () => navigator.clipboard.writeText(body.innerText).then(() => shell.toast("Copied", "ok"), () => shell.toast("Clipboard blocked", "err")));
    act.append(cp);
    wrap.append(el("span", "chat-dot", DOT), body, act);
    this.push(wrap);
    return { id, text: "", body, raf: 0 };
  }
  appendAgentChunk(u) {
    this.replayUser = null;
    this.finishThought();
    const c = u.content || {};
    if (c.type === "image") { const b = this.agentBlock(); const img = document.createElement("img"); img.className = "chat-img"; img.src = "data:" + c.mimeType + ";base64," + c.data; b.body.append(img); this.current = null; return; }
    if (c.type === "resource_link" || c.type === "resource") { const b = this.agentBlock(); b.body.append(this.resourceEl(c)); this.current = null; return; }
    if (!this.current || (u.messageId !== undefined && this.current.id !== u.messageId)) this.current = this.agentBlock(u.messageId);
    this.current.text += c.text || "";
    this.flushCurrent();
  }
  flushCurrent() {
    const cur = this.current;
    if (!cur || cur.raf) return;
    cur.raf = requestAnimationFrame(() => { cur.raf = 0; cur.body.innerHTML = renderMarkdown(cur.text); this.scrollIfPinned(); });
  }
  // "✻ Thinking…" with the thought streaming under it, dim and italic; when
  // the agent moves on it folds to "✻ Thought for 4s" and opens on a click
  appendThought(u) {
    this.replayUser = null;
    const text = this.contentText(u.content);
    if (!this.thought || (u.messageId !== undefined && this.thought.id !== u.messageId)) {
      this.finishThought();
      const det = document.createElement("details");
      det.className = "chat-thought"; det.open = true;
      const sum = el("summary", "");
      const label = el("span", "chat-thought-label", "Thinking…");
      sum.append(el("span", "chat-spark", SPARK), label);
      const body = el("div", "chat-md chat-thought-body");
      det.append(sum, body);
      const th = { id: u.messageId, text: "", body, det, label, raf: 0, t0: performance.now(), userToggled: false, done: false };
      sum.addEventListener("click", () => { th.userToggled = true; });
      sum.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") th.userToggled = true; });
      this.push(det);
      this.thought = th;
      this.current = null;
    }
    const th = this.thought;
    th.text += text;
    if (!th.raf) th.raf = requestAnimationFrame(() => { th.raf = 0; th.body.innerHTML = renderMarkdown(th.text); this.scrollIfPinned(); });
  }
  finishThought() {
    const th = this.thought;
    this.thought = null;
    if (!th || th.done) return;
    th.done = true;
    const secs = Math.round((performance.now() - th.t0) / 1000);
    th.label.textContent = secs >= 1 ? "Thought for " + secs + "s" : "Thought";
    if (!th.userToggled) th.det.open = false;
  }
  addNote(text) { const n = el("div", "chat-note"); n.append(el("span", "chat-elbow", ELBOW), el("span", "", text)); this.push(n); }
  renderUsage(u) {
    const pct = u.size ? Math.round(100 * (u.used || 0) / u.size) : null;
    const cost = u.cost && u.cost.amount !== undefined ? u.cost.amount.toFixed(3) + " " + (u.cost.currency || "") : "";
    this.usage = { pct, cost: cost.trim() };
    this.renderStatus();
  }

  // -- tool calls: one line, Claude Code's way — ⏺ Bash(ls -la), then ⎿ what came of it --
  addTool(u) {
    this.current = null; this.finishThought(); this.replayUser = null;
    let tc = this.tools.get(u.toolCallId);
    if (!tc) {
      const card = el("div", "chat-tool");
      card.dataset.tool = u.toolCallId;
      card.setAttribute("data-testid", "chat-tool");
      const head = el("div", "chat-tool-head");
      const title = el("span", "chat-tool-title");
      head.append(el("span", "chat-dot", DOT), title);
      const res = el("div", "chat-tool-res"); res.hidden = true;
      const out = el("div", "chat-tool-out");
      res.append(el("span", "chat-elbow", ELBOW), out);
      card.append(head, res);
      this.push(card);
      tc = { el: card, title, res, out, data: { toolCallId: u.toolCallId, status: "pending", content: [] }, perm: null, open: false };
      head.addEventListener("click", () => { if (!card.classList.contains("has-more")) return; tc.open = !tc.open; this.renderToolBody(tc); });
      this.tools.set(u.toolCallId, tc);
    }
    this.updateTool(u);
  }
  updateTool(u) {
    const tc = this.tools.get(u.toolCallId);
    if (!tc) { this.addTool({ toolCallId: u.toolCallId, title: u.title || "tool", kind: u.kind, status: u.status || "pending", content: u.content, locations: u.locations, rawInput: u.rawInput, rawOutput: u.rawOutput }); return; }
    const d = tc.data;
    for (const k of ["title", "kind", "status", "name", "locations", "rawInput", "rawOutput"]) if (u[k] !== undefined && u[k] !== null) d[k] = u[k];
    if (Array.isArray(u.content)) d.content = u.content;
    const { name, arg } = this.toolLabel(d);
    tc.title.textContent = "";
    tc.title.insertAdjacentHTML("afterbegin", ICON[KIND_ICON[d.kind] || "tool"]);
    if (name) tc.title.append(el("span", "chat-tool-name", name), "(", el("span", "chat-tool-arg", arg), ")");
    else tc.title.append(el("span", "chat-tool-name", arg));
    tc.title.title = name ? name + "(" + arg + ")" : arg;
    tc.el.dataset.status = d.status || "pending";
    this.renderToolBody(tc);
  }
  // Read(company/notes.md), Bash(ls -la), Update(path), Search(pattern),
  // Fetch(url): the verb from the kind, the argument from the raw input,
  // the locations or the diff — the title when nothing better is known.
  toolLabel(d) {
    const ri = d.rawInput && typeof d.rawInput === "object" ? d.rawInput : null;
    const kind = d.kind || "other";
    const diffs = (d.content || []).filter((c) => c.type === "diff");
    const loc = d.locations && d.locations[0];
    const rawPath = (diffs[0] && diffs[0].path) || firstString(ri, ["file_path", "filePath", "path", "file", "notebook_path"]) || (loc && loc.path) || "";
    const path = rawPath ? shell.relPath(rawPath) : "";
    const oneLine = (s) => { s = String(s || "").trim(); const i = s.indexOf("\n"); return i < 0 ? s : s.slice(0, i) + " …"; };
    let name = KIND_NAME[kind] || "", arg = "";
    // the title as the argument, minus a verb of its own: "Edit x.md" under
    // our "Update" would read Update(Edit x.md)
    const title = () => { const t = String(d.title || ""); if (!name) return t; const s = t.replace(TITLE_VERB, "").trim(); return s || t; };
    switch (kind) {
      case "execute": arg = firstString(ri, ["command", "cmd"]) || (typeof d.rawInput === "string" ? d.rawInput : "") || title(); break;
      case "edit": if (diffs.length && diffs.every((c) => c.oldText == null)) name = "Write"; arg = path || title(); break;
      case "read": case "delete": arg = path || title(); break;
      case "move": { const to = firstString(ri, ["new_path", "newPath", "destination", "to"]); arg = (path || title()) + (to ? " → " + shell.relPath(to) : ""); break; }
      case "search": arg = firstString(ri, ["pattern", "query", "glob", "regex", "q"]) || title(); break;
      case "fetch": arg = firstString(ri, ["url", "uri"]) || title(); break;
      case "think": arg = firstString(ri, ["description", "prompt"]) || title(); break;
      case "switch_mode": arg = firstString(ri, ["mode", "modeId", "mode_id"]) || title(); break;
      default: name = ""; arg = d.title || d.name || "tool";
    }
    return { name, arg: oneLine(arg) || "…" };
  }
  renderToolBody(tc) {
    const d = tc.data, out = tc.out;
    out.textContent = "";
    const kind = d.kind || "other";
    const diffs = (d.content || []).filter((c) => c.type === "diff");
    const texts = [], others = [];
    for (const c of d.content || []) {
      if (c.type === "content") { const cc = c.content || {}; if (cc.type === "text") texts.push(cc.text || ""); else others.push(cc); }
      else if (c.type === "terminal") others.push(c);
    }
    let text = unfence(texts.join("\n"));
    if (text.length > 40000) text = text.slice(0, 40000) + "\n…";
    const n = lineCount(text);
    const running = d.status === "pending" || d.status === "in_progress";
    const failed = d.status === "failed" || d.status === "cancelled";
    let more = false;   // is there anything a click on the line reveals?
    const toggle = (label) => {
      const b = el("button", "chat-more", label); b.type = "button";
      b.addEventListener("click", (e) => { e.stopPropagation(); tc.open = !tc.open; this.renderToolBody(tc); });
      return b;
    };
    for (const df of diffs) {
      const lines = lineDiff(df.oldText == null ? "" : df.oldText, df.newText || "", df.oldText == null);
      const add = lines.filter((l) => l[0] === "+").length, del = lines.filter((l) => l[0] === "-").length;
      out.append(el("div", "chat-tool-sum", df.oldText == null ? "Wrote " + plural(lineCount(df.newText || ""), "line") : "Updated with " + plural(add, "addition") + " and " + plural(del, "removal")));
      out.append(this.diffEl(df, lines));
    }
    if (text) {
      const inline = failed || kind === "execute" || kind === "edit" || kind === "delete" || kind === "move" || kind === "switch_mode";
      if (inline) {
        // the output itself, the first lines of it; the rest a click away
        const pre = el("pre", "chat-tool-text" + (failed ? " err" : ""));
        if (n > FOLD) {
          more = true;
          if (tc.open) { pre.textContent = text; pre.classList.add("tall"); out.append(pre, toggle("collapse")); }
          else { pre.textContent = text.split("\n").slice(0, FOLD).join("\n"); out.append(pre, toggle("… +" + plural(n - FOLD, "line"))); }
        } else { pre.textContent = text; out.append(pre); }
      } else {
        // a line that says what came back; the text behind it
        more = true;
        const sum = el("button", "chat-tool-sum chat-tool-toggle");
        sum.type = "button"; sum.setAttribute("aria-expanded", tc.open ? "true" : "false");
        const empty = !text.trim() || /^(no matches|no results|nothing found)\b/i.test(text.trim());
        const what = kind === "read" ? (empty ? "Empty file" : "Read " + plural(n, "line"))
          : kind === "search" ? (empty ? "No matches" : "Found " + plural(n, "result"))
          : kind === "fetch" ? "Received " + kb(text.length)
          : "Done · " + plural(n, "line");
        sum.append(el("span", "", what), el("span", "chat-tool-caret", tc.open ? " ▾" : " ▸"));
        sum.addEventListener("click", (e) => { e.stopPropagation(); tc.open = !tc.open; this.renderToolBody(tc); });
        out.append(sum);
        if (tc.open) {
          if (kind === "read" || kind === "search") { const pre = el("pre", "chat-tool-text tall", text); out.append(pre); }
          else { const md = el("div", "chat-md chat-tool-detail"); md.innerHTML = renderMarkdown(text); out.append(md); }
        }
      }
    }
    for (const cc of others) {
      if (cc.type === "image") { const img = document.createElement("img"); img.className = "chat-img"; img.src = "data:" + cc.mimeType + ";base64," + cc.data; out.append(img); }
      else if (cc.type === "terminal") out.append(el("div", "chat-tool-sum", "terminal " + cc.terminalId));
      else out.append(this.resourceEl(cc));
    }
    // where it touched, when the line does not already say (a search's hits)
    if (d.locations && d.locations.length && !diffs.length && kind !== "read" && kind !== "edit" && kind !== "delete" && !(kind === "search" && text)) {
      const locs = el("div", "chat-tool-locs");
      for (const l of d.locations.slice(0, 12)) { const a = el("a", "chat-path", shell.relPath(l.path) + (l.line ? ":" + l.line : "")); a.href = "#"; a.dataset.openPath = l.path; locs.append(a); }
      if (d.locations.length > 12) locs.append(el("span", "chat-tool-sum", "+" + (d.locations.length - 12) + " more"));
      out.append(locs);
    }
    // the question goes UNDER the diff or the output it is about — you
    // cannot answer "do you want to proceed?" before you have seen it
    if (tc.perm) out.append(tc.perm.el);
    if (!out.childNodes.length || (tc.perm && out.childNodes.length === 1)) {
      if (tc.perm && !tc.perm.done) { /* the prompt says what is going on */ }
      else if (running) out.append(el("div", "chat-tool-sum", "Running…"));
      else if (failed) out.append(el("div", "chat-tool-sum err", d.status === "cancelled" ? "Interrupted" : "Failed"));
      else if (kind === "execute") out.append(el("div", "chat-tool-sum", "(no output)"));
    }
    tc.res.hidden = !out.childNodes.length;
    tc.el.classList.toggle("has-more", more);
    tc.el.classList.toggle("open", !!tc.open);
  }
  resourceEl(c) {
    const r = c.type === "resource" ? c.resource : c;
    const uri = (r && r.uri) || "";
    const a = el("a", "chat-path", c.name || c.title || uri.replace(/^file:\/\//, ""));
    a.href = uri; a.target = "_blank"; a.rel = "noopener";
    if (uri.startsWith("file://")) { a.dataset.openPath = decodeURIComponent(uri.slice(7)); a.href = "#"; }
    return a;
  }
  diffEl(c, lines) {
    const wrap = el("div", "chat-diff");
    const head = el("div", "chat-diff-head");
    const a = el("a", "chat-path", shell.relPath(c.path)); a.href = "#"; a.dataset.openPath = c.path; a.title = "Open " + shell.relPath(c.path);
    head.append(a);
    wrap.append(head);
    const pre = el("pre", "chat-diff-body");
    lines = lines || lineDiff(c.oldText == null ? "" : c.oldText, c.newText || "", c.oldText == null);
    if (lines.length && lines[lines.length - 1][1] === "") lines = lines.slice(0, -1);   // the newline at the end of the file
    let shown = 0;
    for (const [tag, text] of lines) {
      if (shown++ > 1500) { pre.append(el("div", "chat-diff-line ctx", "… (" + (lines.length - shown) + " more lines)")); break; }
      const ln = el("div", "chat-diff-line " + (tag === "+" ? "add" : tag === "-" ? "del" : "ctx"), (tag || " ") + " " + text);
      pre.append(ln);
    }
    wrap.append(pre);
    return wrap;
  }

  // -- permission and elicitation requests from the agent --
  onAgentRequest(req) {
    if (req.method === "session/request_permission") this.askPermission(req);
    else if (req.method === "elicitation/create") this.askElicitation(req);
  }
  // Claude Code's prompt: the tool line above, a bordered box with the
  // question and numbered options — 1, 2, 3 on the keyboard pick them
  askPermission(req) {
    const p = req.params || {};
    const tcu = p.toolCall || {};
    let tc = tcu.toolCallId ? this.tools.get(tcu.toolCallId) : null;
    if (!tc && tcu.toolCallId) { this.addTool({ ...tcu, toolCallId: tcu.toolCallId, title: tcu.title || "permission" }); tc = this.tools.get(tcu.toolCallId); }
    else if (tc) this.updateTool(tcu);
    const box = el("div", "chat-perm");
    box.setAttribute("data-testid", "chat-perm");
    const what = tcu.title || (tc && tc.data.title) || "";
    if (!tc && what) box.append(el("div", "chat-perm-what", what));
    box.append(el("div", "chat-perm-q", "Do you want to proceed?"));
    const row = el("div", "chat-perm-row"); row.setAttribute("role", "group");
    const order = { allow_once: 0, allow_always: 1, reject_once: 2, reject_always: 3 };
    const opts = (p.options || []).slice().sort((x, y) => (order[x.kind] ?? 9) - (order[y.kind] ?? 9));
    const buttons = [];
    opts.forEach((o, i) => {
      const b = el("button", "chat-perm-btn " + (String(o.kind).startsWith("allow") ? "allow" : "reject"));
      b.type = "button"; b.dataset.kind = o.kind;
      b.append(el("span", "chat-perm-n", (i + 1) + "."), el("span", "chat-perm-label", o.name));
      b.addEventListener("click", () => {
        this.conn.answer(req.id, { outcome: { outcome: "selected", optionId: o.optionId } });
        settle(o.name, String(o.kind).startsWith("allow") ? "ok" : "no");
      });
      row.append(b); buttons.push(b);
    });
    box.append(row);
    const hint = shell.isMobile && shell.isMobile() ? null : el("div", "chat-perm-hint", (opts.length > 1 ? "1–" + opts.length : "1") + " to choose · Esc to interrupt");
    if (hint) box.append(hint);
    let settled = false;
    const settle = (label, kind) => {
      if (settled) return;                       // the backend's req-done follows our own click
      settled = true;
      row.textContent = "";
      row.append(el("span", "chat-perm-answer " + (kind || ""), (kind === "ok" ? "✓ " : kind === "no" ? "✗ " : "· ") + (label || "answered elsewhere")));
      if (hint) hint.remove();
      box.classList.add("done");
      if (tc && tc.perm) { tc.perm.done = true; this.renderToolBody(tc); }
      this.pendingPerms.delete(req.id);
      this.refreshPermHints();
    };
    this.pendingPerms.set(req.id, { settle: (label, kind) => settle(label, kind), pick: (n) => { const b = buttons[n - 1]; if (!b || settled) return false; b.click(); return true; }, hint, n: opts.length });
    this.refreshPermHints();
    if (tc) { tc.perm = { el: box, done: false }; this.renderToolBody(tc); }
    else this.push(box);
    this.pinned = true; this.scrollIfPinned();
    shell.attention(this.t);
  }
  // the keys go to the prompt asked first; the others say so until their turn
  refreshPermHints() {
    let first = true;
    for (const p of this.pendingPerms.values()) {
      if (!p.pick || !p.hint) continue;
      p.hint.textContent = first ? (p.n > 1 ? "1–" + p.n : "1") + " to choose · Esc to interrupt" : "answer the one above first";
      first = false;
    }
  }
  askElicitation(req) {
    const p = req.params || {};
    const box = el("div", "chat-perm chat-elicit");
    box.append(el("div", "chat-perm-q", p.message || "The agent needs an answer"));
    const done = (result) => { this.conn.answer(req.id, result); box.classList.add("done"); box.querySelectorAll("button, input, select").forEach((x) => { x.disabled = true; }); };
    if (p.mode === "url") {
      const a = el("a", "chat-path", p.url); a.href = p.url; a.target = "_blank"; a.rel = "noopener";
      box.append(a);
      const row = el("div", "chat-perm-row");
      const ok = el("button", "chat-perm-btn allow", "I did it"); ok.type = "button"; ok.addEventListener("click", () => done({ action: "accept" }));
      const no = el("button", "chat-perm-btn reject", "Cancel"); no.type = "button"; no.addEventListener("click", () => done({ action: "cancel" }));
      row.append(ok, no); box.append(row);
    } else {
      const schema = (p.requestedSchema && p.requestedSchema.properties) || {};
      const form = el("div", "chat-form");
      const fields = {};
      for (const [k, def] of Object.entries(schema)) {
        const lab = el("label", "chat-field");
        lab.append(el("span", "", def.title || k));
        let input;
        if (def.enum) { input = document.createElement("select"); for (const v of def.enum) { const op = document.createElement("option"); op.value = v; op.textContent = v; input.append(op); } }
        else if (def.type === "boolean") { input = document.createElement("input"); input.type = "checkbox"; lab.classList.add("check"); }
        else { input = document.createElement("input"); input.type = def.type === "number" || def.type === "integer" ? "number" : "text"; }
        fields[k] = { input, def };
        lab.append(input); form.append(lab);
      }
      box.append(form);
      const row = el("div", "chat-perm-row");
      const ok = el("button", "chat-perm-btn allow", "Send"); ok.type = "button";
      ok.addEventListener("click", () => {
        const content = {};
        for (const [k, f] of Object.entries(fields)) {
          content[k] = f.def.type === "boolean" ? f.input.checked : (f.def.type === "number" || f.def.type === "integer") ? Number(f.input.value) : f.input.value;
        }
        done({ action: "accept", content });
      });
      const no = el("button", "chat-perm-btn reject", "Decline"); no.type = "button"; no.addEventListener("click", () => done({ action: "decline" }));
      row.append(ok, no); box.append(row);
    }
    this.pendingPerms.set(req.id, { settle: () => { box.classList.add("done"); box.querySelectorAll("button").forEach((x) => { x.disabled = true; }); } });
    this.push(box);
    this.pinned = true; this.scrollIfPinned();
    shell.attention(this.t);
  }

  // -- the plan: Claude Code's todo list, under the transcript --
  renderPlan(entries) {
    this.planEl.innerHTML = "";
    this.hasPlan = !!entries.length;
    this.planEl.hidden = !this.hasPlan || !!this.picker;
    if (!entries.length) return;
    const done = entries.filter((e) => e.status === "completed").length;
    const det = document.createElement("details");
    det.open = done < entries.length;
    det.append(el("summary", "", "Plan · " + done + "/" + entries.length));
    const ul = el("ul", "chat-plan-list");
    for (const e of entries) {
      const li = el("li", "chat-plan-item " + (e.status || "pending") + " p-" + (e.priority || "medium"));
      li.append(el("span", "chat-plan-mark", e.status === "completed" ? "☒" : e.status === "in_progress" ? "◐" : "☐"), el("span", "chat-plan-text", e.content));
      ul.append(li);
    }
    det.append(ul);
    this.planEl.append(det);
  }
}

// A small line diff: common prefix/suffix, then an LCS on what changed —
// capped, because a whole-file rewrite is better shown than diffed.
export function lineDiff(oldText, newText, allNew) {
  const a = oldText.split("\n"), b = newText.split("\n");
  if (allNew) return b.map((l) => ["+", l]);
  let s = 0; while (s < a.length && s < b.length && a[s] === b[s]) s++;
  let e = 0; while (e < a.length - s && e < b.length - s && a[a.length - 1 - e] === b[b.length - 1 - e]) e++;
  const am = a.slice(s, a.length - e), bm = b.slice(s, b.length - e);
  const out = [];
  const ctx = (arr, from, to) => { for (let i = from; i < to; i++) out.push([" ", arr[i]]); };
  ctx(a, Math.max(0, s - 3), s);
  if (am.length * bm.length > 250000) {
    for (const l of am) out.push(["-", l]);
    for (const l of bm) out.push(["+", l]);
  } else {
    const n = am.length, m = bm.length;
    const dp = new Array((n + 1) * (m + 1)).fill(0);
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
      dp[i * (m + 1) + j] = am[i] === bm[j] ? dp[(i + 1) * (m + 1) + j + 1] + 1 : Math.max(dp[(i + 1) * (m + 1) + j], dp[i * (m + 1) + j + 1]);
    let i = 0, j = 0;
    while (i < n && j < m) {
      if (am[i] === bm[j]) { out.push([" ", am[i]]); i++; j++; }
      else if (dp[(i + 1) * (m + 1) + j] >= dp[i * (m + 1) + j + 1]) { out.push(["-", am[i++]]); }
      else { out.push(["+", bm[j++]]); }
    }
    while (i < n) out.push(["-", am[i++]]);
    while (j < m) out.push(["+", bm[j++]]);
  }
  ctx(a, a.length - e, Math.min(a.length, a.length - e + 3));
  return out;
}
