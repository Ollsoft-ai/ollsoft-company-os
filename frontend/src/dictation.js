// ═══ Dictation ══════════════════════════════════════════════════════════════
// Speak, and the words land wherever you were already typing — a document, a
// terminal, the command palette, any text field.
//
// This module owns the microphone and nothing else. It does not know that
// CodeMirror or xterm exist: `initDictation` is handed a `resolveTarget` and an
// `insert` callback, and app.js decides where text goes. That split is what
// keeps the audio lifecycle (the part with all the failure modes) testable and
// separate from the routing.
//
// The audio never touches the browser's API key, because there isn't one: the
// blob is POSTed to the hub's /stt route, which holds the ElevenLabs credential
// as root and is the only thing that can spend it.
//
// Two gestures, one key, no mode to remember — the convention Discord and Slack
// both converged on:
//   • tap  → latched. Recording continues; tap again to finish.
//   • hold → push-to-talk. Recording ends the moment you let go.

const HOLD_MS = 450;        // held longer than this and the release ends it
// The cap exists so an accidentally-latched mic cannot run forever, not to
// bound how long a thought is allowed to be. Ten minutes, because the audio is
// vaulted to IndexedDB second by second while recording (see the vault below)
// — a long dictation risks nothing — and ten minutes of 32 kbps opus is ~2.4 MB,
// far under the server's 12 MiB limit.
const MAX_MS = 600_000;
const MIN_MS = 300;         // shorter than this was a mis-hit, not speech
const MIN_BYTES = 1024;     // ditto, measured after encoding
const SLICE_MS = 1000;      // MediaRecorder timeslice: how much a tab crash can lose
const IDLE_RELEASE_MS = 5 * 60_000;   // drop the mic so the browser's in-use dot goes away
const STOP_GRACE_MS = 1500;           // MediaRecorder.onstop watchdog

// Bare values, never {exact:…} — an exact sampleRate throws OverconstrainedError
// on PipeWire, which is what Ubuntu 24.04 runs.
const CONSTRAINTS = {
  audio: { echoCancellation: true, noiseSuppression: true,
           autoGainControl: true, channelCount: 1 },
  video: false,
};

// Ordered by what the batch STT endpoint ingests happiest. The empty string is
// the "let the browser pick" fallback; we read rec.mimeType afterwards anyway.
const MIMES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm", ""];

let hooks = null;           // { resolveTarget, insert, toast, button }
let stream = null;          // held for the session; tracks disabled when idle
let audioCtx = null, meter = null;
let rec = null, chunks = [], recMime = "audio/webm";
let state = "idle";         // idle | recording | sending
let target = null;          // resolved at record START and stashed — see below
let startedAt = 0, keyDown = false, latched = false, pressCode = null;
let maxTimer = null, idleTimer = null, stopMeter = null;
let lastBlob = null;        // kept for "Retry the last dictation"
let lastRecId = null;       // its vault id, so a retry updates the same record
let recId = null, chunkSeq = 0;   // the vault identity of the CURRENT recording
let reduceMotion = false;
let stoppedOffScreen = false;   // a recording auto-finished because the app was left

export function dictationReady() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
            typeof MediaRecorder !== "undefined" && window.isSecureContext);
}

export function dictationState() { return state; }

// ---- the audio vault --------------------------------------------------------
// The recording is the only part of a dictation that cannot be re-created:
// the transcript can be re-requested, but five minutes of speech cannot be
// re-spoken. So the audio is written to IndexedDB WHILE recording — one chunk
// per second — and only ever deleted deliberately. Whatever fails afterwards
// (the upload, the STT service, the network, this tab), the audio is on disk,
// listed in Dictation history with download / transcribe-again buttons.
//
// Two stores rather than one growing blob: re-putting the whole recording on
// every slice would write O(n²) bytes over a long dictation (~700 MB of flash
// traffic for ten minutes). Appending each 4 KB slice under a [recId, seq] key
// keeps writes linear, and IndexedDB returns the range in key order, so the
// blob is just `new Blob(getAll(range))` — opus/webm slices from one
// MediaRecorder concatenate into a valid stream.
//
// Every vault call is best-effort and swallows its errors: dictation must keep
// working in a browser with IndexedDB unavailable (Firefox private windows);
// it just loses the safety net, same as the localStorage transcript history.
const VAULT_DB = "kbDictAudio";
const VAULT_TTL_DONE = 24 * 3600 * 1000;      // transcribed: kept a day, like the text
const VAULT_TTL_KEPT = 7 * 24 * 3600 * 1000;  // NOT transcribed: a week to rescue it
const VAULT_MAX = 100;                        // count cap; transcribed evict first

let vaultDbP = null;
function vaultOpen() {
  if (vaultDbP) return vaultDbP;
  vaultDbP = new Promise((resolve, reject) => {
    let req;
    try { req = indexedDB.open(VAULT_DB, 1); } catch (e) { reject(e); return; }
    req.onupgradeneeded = () => {
      req.result.createObjectStore("recs", { keyPath: "id" });
      req.result.createObjectStore("chunks", { keyPath: ["rid", "seq"] });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    req.onblocked = () => reject(new Error("blocked"));
  });
  // A failed open must not be cached forever — a transient lock (another tab
  // upgrading) would otherwise disable the vault for the whole session.
  vaultDbP.catch(() => { vaultDbP = null; });
  return vaultDbP;
}

function vaultReq(store, mode, fn) {
  return vaultOpen().then((db) => new Promise((resolve, reject) => {
    const tx = db.transaction(store, mode);
    const r = fn(tx.objectStore(store));
    tx.oncomplete = () => resolve(r ? r.result : undefined);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  })).catch(() => null);
}

const chunkRange = (rid) => IDBKeyRange.bound([rid, -Infinity], [rid, Infinity]);
const vaultPutMeta = (m) => vaultReq("recs", "readwrite", (s) => s.put(m));
const vaultGetMeta = (id) => vaultReq("recs", "readonly", (s) => s.get(id));
const vaultAllMeta = () =>
  vaultReq("recs", "readonly", (s) => s.getAll()).then((a) => (Array.isArray(a) ? a : []));
const vaultAddChunk = (rid, seq, data) =>
  vaultReq("chunks", "readwrite", (s) => s.put({ rid, seq, data }));
const vaultChunks = (rid) =>
  vaultReq("chunks", "readonly", (s) => s.getAll(chunkRange(rid)))
    .then((a) => (Array.isArray(a) ? a : []));

async function vaultDelete(id) {
  await vaultReq("recs", "readwrite", (s) => s.delete(id));
  await vaultReq("chunks", "readwrite", (s) => s.delete(chunkRange(id)));
}

async function vaultMark(id, status) {
  if (!id) return;
  const m = await vaultGetMeta(id);
  if (m && m.status !== status) { m.status = status; await vaultPutMeta(m); }
}

async function vaultBlob(id) {
  const m = await vaultGetMeta(id);
  if (!m) return null;
  const parts = await vaultChunks(id);
  if (!parts.length) return null;
  return new Blob(parts.map((c) => c.data), { type: m.mime || "audio/webm" });
}

// Runs once per page load. A record still marked "recording" is a tab that
// crashed or was killed mid-dictation — the chunks that made it to disk ARE
// the recording, so it is promoted to "interrupted" (rescuable), never dropped.
async function vaultSweep() {
  const all = await vaultAllMeta();
  const now = Date.now();
  const live = [];
  for (const m of all) {
    if (m.status === "recording") { m.status = "interrupted"; await vaultPutMeta(m); }
    const ttl = m.status === "done" ? VAULT_TTL_DONE : VAULT_TTL_KEPT;
    if (now - m.t > ttl) await vaultDelete(m.id); else live.push(m);
  }
  // Over the count cap, transcribed recordings go first — their text is safe
  // in the history; un-transcribed audio is the last thing to evict.
  live.sort((a, b) => (a.status === "done") - (b.status === "done") || b.t - a.t);
  for (const m of live.slice(VAULT_MAX)) await vaultDelete(m.id);
}

// ---- the vault's public face (Dictation history in app.js) ------------------
export async function listRecordings() {
  const all = await vaultAllMeta();
  const out = [];
  for (const m of all.sort((a, b) => b.t - a.t)) {
    const parts = await vaultChunks(m.id);
    out.push({ id: m.id, t: m.t, mime: m.mime, status: m.status, ms: m.ms || 0,
               bytes: parts.reduce((n, c) => n + ((c.data && c.data.size) || 0), 0) });
  }
  return out;
}

export function recordingBlob(id) { return vaultBlob(id); }

export function deleteRecording(id) { return vaultDelete(id); }

export async function transcribeRecording(id) {
  if (state !== "idle") return false;
  const blob = await vaultBlob(id);
  if (!blob) { if (hooks && hooks.toast) hooks.toast("That recording is gone", "err"); return false; }
  // Resolved NOW, same rule as recording start: the words land where the user
  // is working at the moment they act, not where they were minutes ago.
  target = hooks.resolveTarget ? hooks.resolveTarget() : null;
  await transcribeAndInsert(blob, id);
  return true;
}

// ---- UI: a fixed overlay, because dictation fires while focus is elsewhere ---
const $ = (s) => document.querySelector(s);

function paint(label) {
  const panel = $("#ptt");
  if (panel) {
    panel.hidden = state === "idle";
    panel.dataset.state = state;
  }
  const lbl = $("#ptt-label");
  if (lbl && label) lbl.textContent = label;
  if (hooks && hooks.button) {
    hooks.button.classList.toggle("rec", state === "recording");
    hooks.button.classList.toggle("busy", state === "sending");
    hooks.button.setAttribute("aria-pressed", state === "recording" ? "true" : "false");
  }
  // One announcement per state change, never per frame.
  const st = $("#ptt-status");
  if (st && label) st.textContent = label;
}

function fail(msg) {
  if (hooks && hooks.toast) hooks.toast(msg, "err");
  const el = $("#ptt-error");
  if (el) el.textContent = msg;
}

// ---- the microphone ---------------------------------------------------------
// Held open for the session. This is the single most important latency and
// permission decision in the module: Firefox's one-time grants and Chrome's
// per-gesture checks both mean that re-calling getUserMedia per utterance can
// re-prompt, and a prompt during a hold steals the keyup and strands the
// recording. We take the cost — the browser's "in use" indicator stays lit —
// and release the mic after five idle minutes to give it back.
async function ensureWarm() {
  if (stream && stream.getAudioTracks().some((t) => t.readyState === "live")) {
    for (const t of stream.getAudioTracks()) t.enabled = true;
    return stream;
  }
  releaseMicNow();
  try {
    stream = await navigator.mediaDevices.getUserMedia(CONSTRAINTS);
  } catch (e) {
    if (e && e.name === "OverconstrainedError") {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } else {
      throw e;
    }
  }
  for (const t of stream.getAudioTracks()) {
    t.enabled = true;
    // Unplugged headset, or the OS handed the device to something else.
    t.addEventListener("ended", () => { stream = null; if (state === "recording") stopPress(true); });
  }
  buildMeter();
  return stream;
}

export function releaseMicNow() {
  if (stopMeter) { stopMeter(); stopMeter = null; }
  if (stream) { for (const t of stream.getTracks()) t.stop(); }
  stream = null; meter = null;
  if (audioCtx) { audioCtx.close().catch(() => {}); audioCtx = null; }
}

function armIdleRelease() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (state === "idle") releaseMicNow(); }, IDLE_RELEASE_MS);
}

function buildMeter() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const src = audioCtx.createMediaStreamSource(stream);
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    // Terminate the graph through a silenced gain so the node is actually pulled.
    // Connecting a mic to destination directly is an instant feedback howl.
    const mute = audioCtx.createGain();
    mute.gain.value = 0;
    src.connect(analyser); analyser.connect(mute); mute.connect(audioCtx.destination);
    meter = { analyser, buf: new Float32Array(analyser.fftSize) };
  } catch (e) { meter = null; }
}

function startMeter() {
  const bar = $("#ptt-bar");
  if (!bar || !meter || reduceMotion) {
    if (bar && reduceMotion) bar.style.transform = "scaleX(1)";
    return () => { if (bar) bar.style.transform = "scaleX(0)"; };
  }
  let raf = 0, smoothed = 0;
  const ATTACK = 0.6, RELEASE = 0.12;   // fast rise, slow fall — reads as a VU meter
  const tick = () => {
    meter.analyser.getFloatTimeDomainData(meter.buf);
    let sum = 0;
    for (let i = 0; i < meter.buf.length; i++) sum += meter.buf[i] * meter.buf[i];
    const rms = Math.sqrt(sum / meter.buf.length);
    // dBFS → 0..1, floored at −60 dB; speech peaks land around −20..−6.
    const db = 20 * Math.log10(Math.max(rms, 1e-8));
    const level = Math.min(1, Math.max(0, (db + 60) / 60));
    smoothed += (level - smoothed) * (level > smoothed ? ATTACK : RELEASE);
    bar.style.transform = "scaleX(" + smoothed.toFixed(3) + ")";   // compositor-only
    raf = requestAnimationFrame(tick);
  };
  raf = requestAnimationFrame(tick);
  return () => { cancelAnimationFrame(raf); bar.style.transform = "scaleX(0)"; };
}

function pickMime() {
  for (const m of MIMES) {
    if (m === "") return "";
    try { if (MediaRecorder.isTypeSupported(m)) return m; } catch (e) { /* older browser */ }
  }
  return "";
}

// ---- record -----------------------------------------------------------------
async function beginRecording() {
  // Resolve the destination NOW, not when the transcript lands. The round trip
  // is a second or more, and by then the user may have clicked somewhere else —
  // and a first-time permission prompt moves focus too.
  target = hooks.resolveTarget ? hooks.resolveTarget() : null;

  let s;
  try {
    s = await ensureWarm();
  } catch (e) {
    state = "idle"; paint("");
    const n = (e && e.name) || "";
    if (n === "NotAllowedError" || n === "SecurityError") {
      // Never auto-retry: Chrome starts auto-blocking origins whose prompts get
      // repeatedly dismissed. The next attempt must be a fresh explicit gesture.
      fail("Microphone blocked. Click the mic (Chrome) or padlock (Firefox) icon "
           + "in the address bar and allow it, then try again.");
    } else if (n === "NotFoundError" || n === "DevicesNotFoundError") {
      fail("No microphone found.");
    } else if (n === "NotReadableError" || n === "TrackStartError") {
      fail("The microphone is in use by another app.");
    } else {
      fail("Could not open the microphone" + (n ? " (" + n + ")" : "") + ".");
    }
    return false;
  }

  const want = pickMime();
  try {
    rec = want ? new MediaRecorder(s, { mimeType: want, audioBitsPerSecond: 32000 })
               : new MediaRecorder(s);
  } catch (e) {
    try { rec = new MediaRecorder(s); } catch (e2) { fail("This browser cannot record audio."); return false; }
  }
  // Trust what the recorder actually produced, never what we asked for.
  recMime = (rec.mimeType || want || "audio/webm").split(";")[0].trim() || "audio/webm";
  chunks = [];
  startedAt = Date.now();
  recId = "d" + startedAt.toString(36) + Math.random().toString(36).slice(2, 8);
  chunkSeq = 0;
  const rid = recId;
  vaultPutMeta({ id: rid, t: startedAt, mime: recMime, status: "recording", ms: 0 });
  rec.ondataavailable = (e) => {
    if (!(e.data && e.data.size)) return;
    chunks.push(e.data);
    // Persist every slice the moment it exists. From here on, a crash, a killed
    // tab or a dead battery costs at most the last second of speech — the rest
    // is already on disk and shows up in Dictation history as "interrupted".
    vaultAddChunk(rid, chunkSeq++, e.data);
  };
  // A recorder that dies mid-capture (codec failure, device yanked at the wrong
  // moment) must finish like any other stop: the vaulted chunks are the recording.
  rec.onerror = () => { if (state === "recording") finishRecording("Recording error — saved what was captured"); };
  rec.start(SLICE_MS);   // timeslice: chunks stream into the vault as they exist

  state = "recording";
  stopMeter = startMeter();
  paint(latched ? "Listening… press again to finish" : "Listening…");
  clearTimeout(maxTimer);
  maxTimer = setTimeout(() => {
    if (state === "recording") { finishRecording("Stopped after 10 minutes"); }
  }, MAX_MS);
  return true;
}

// The final slice only exists once `onstop` has fired. Reading the chunks
// synchronously after stop() loses the tail of the audio — this is the single
// most likely bug in the whole feature, so it is structured to be impossible.
function collectBlob() {
  return new Promise((resolve) => {
    if (!rec || rec.state === "inactive") {
      resolve(chunks.length ? new Blob(chunks, { type: recMime }) : null);
      return;
    }
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve(chunks.length ? new Blob(chunks, { type: recMime }) : null);
    };
    rec.onstop = finish;
    setTimeout(finish, STOP_GRACE_MS);   // watchdog: a wedged recorder must not hang the UI
    try { rec.stop(); } catch (e) { finish(); }
  });
}

async function finishRecording(note) {
  if (state !== "recording") return;
  const elapsed = Date.now() - startedAt;
  clearTimeout(maxTimer);
  if (stopMeter) { stopMeter(); stopMeter = null; }
  state = "sending";
  paint("Transcribing…");

  const blob = await collectBlob();
  // Idle the mic without dropping it — keeps the permission and the low latency,
  // and stops the browser indicator from implying we are always listening.
  if (stream) for (const t of stream.getAudioTracks()) t.enabled = false;
  armIdleRelease();
  rec = null; chunks = [];
  // …unless this finish happened OFF-SCREEN (the 10 min cap, or the OS killing
  // the capture): a disabled-but-live track still lights Android's system-wide
  // "microphone in use" dot, and a green dot with nothing recording reads as
  // eavesdropping. The blob is already collected, so the upload below loses
  // nothing; the next recording re-opens the mic (at worst re-prompting — the
  // honest trade). Flag it so the return to the app explains what happened —
  // any toast fired now would expire unseen.
  if (document.hidden) { stoppedOffScreen = true; releaseMicNow(); }

  if (note && hooks.toast) hooks.toast(note, "err");

  const id = recId; recId = null;
  if (!blob || blob.size < MIN_BYTES || elapsed < MIN_MS) {
    if (id) vaultDelete(id);   // a mis-hit, not speech — not worth vault space
    state = "idle"; paint("");
    if (hooks.toast) hooks.toast("Too short — hold the key while you speak", "err");
    return;
  }
  // Record how long it was while we know; history shows it next to the size.
  vaultGetMeta(id).then((m) => { if (m) { m.ms = elapsed; return vaultPutMeta(m); } });
  lastBlob = blob;
  lastRecId = id;
  await transcribeAndInsert(blob, id);
}

// Any exit that is not a confirmed transcript marks the vault record "failed":
// still listed in Dictation history, still downloadable, still one click from
// another attempt. The audio is only ever released by success (kept a day),
// expiry, or an explicit delete.
async function transcribeAndInsert(blob, id) {
  state = "sending"; paint("Transcribing…");
  let j = null;
  try {
    const lang = (localStorage.getItem("kbDictateLang") || "").trim();
    const url = "/stt" + (/^[a-z]{2,3}$/.test(lang) ? "?lang=" + lang : "");
    const r = await fetch(url, { method: "POST",
                                 headers: { "content-type": blob.type.split(";")[0] || "audio/webm" },
                                 body: blob });
    j = await r.json().catch(() => null);
    if (!r.ok) {
      vaultMark(id, "failed");
      state = "idle"; paint("");
      fail((j && j.error) || "Transcription failed (HTTP " + r.status + ")");
      hintRetry();
      return;
    }
  } catch (e) {
    vaultMark(id, "failed");
    state = "idle"; paint("");
    fail("Could not reach the server.");
    hintRetry();
    return;
  }
  state = "idle"; paint("");

  const text = sanitize((j && j.text) || "");
  if (!text) {
    // An empty transcript from real speech is a failure of the STT, not of the
    // speaker — keep the audio so it can be downloaded or tried again.
    vaultMark(id, "failed");
    if (hooks.toast) hooks.toast("Nothing was said — the audio is kept in Dictation history", "err");
    return;
  }
  vaultMark(id, "done");
  lastBlob = null; lastRecId = null;
  hooks.insert(target, text, id);
}

function hintRetry() {
  if (hooks.toast) hooks.toast(
    "The recording is safe in Dictation history — download it or transcribe it again from there.", "err");
}

// The transcript is third-party text on its way to a shell. Everything hostile it
// could carry is a control character, so only \n and \t survive.
//
// CR is the one that matters and the one that is easy to miss: xterm's paste path
// turns \r\n into \r and passes a bare \r straight through, and a \r in a PTY *is*
// Enter — so a transcript containing one would execute whatever was on the command
// line. Fold every line ending to \n first, then strip the rest of C0 (which
// includes \x1b, so no escape sequence can survive either).
export function sanitize(text) {
  return String(text).normalize("NFC")
    .replace(/\r\n?/g, "\n")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "")
    .replace(/[ \t]+\n/g, "\n");
}

export async function retryDictation() {
  if (state !== "idle") return;
  if (lastBlob) { await transcribeAndInsert(lastBlob, lastRecId); return; }
  // After a reload the in-memory blob is gone, but the vault is not: retry the
  // most recent recording that never produced a transcript.
  const all = await vaultAllMeta();
  const cand = all.filter((m) => m.status !== "done").sort((a, b) => b.t - a.t)[0];
  if (!cand) { if (hooks && hooks.toast) hooks.toast("Nothing to retry", "err"); return; }
  await transcribeRecording(cand.id);
}

// ---- the two gestures -------------------------------------------------------
// Called on the FIRST keydown (auto-repeat is filtered by `keyDown`) and on
// pointerdown on the mic button.
//
// `pressLive` makes each press's release count exactly ONCE. A touch tap on the
// button delivers pointerup AND (implicit pointer capture) lostpointercapture —
// and Firefox for Android can deliver the second one late, after the menu that
// held the button has closed. A duplicate release measured >450 ms after the
// press reads as a hold ending, which finished the recording an instant after
// the tap had latched it — "recording stops by itself on Firefox".
let pressLive = false;

export function startPress() {
  if (!dictationReady()) return;
  if (state === "sending") return;                 // a transcript is in flight
  if (state === "recording") { finishRecording(); return; }   // second tap ends a latch
  latched = false;
  pressLive = true;
  beginRecording();
}

// Called on keyup / pointerup. `abort` short-circuits the hold logic.
export function stopPress(abort) {
  if (state !== "recording") { pressLive = false; return; }
  if (abort) { finishRecording(); return; }
  if (!pressLive) return;                          // duplicate release of the same press
  pressLive = false;
  const held = Date.now() - startedAt;
  if (held >= HOLD_MS) {
    finishRecording();                             // it was a hold: release ends it
  } else {
    latched = true;                                // it was a tap: keep listening
    paint("Listening… press again to finish");
  }
}

// The plain toggle, for the button click and the command palette.
export function toggleDictation() {
  if (state === "recording") finishRecording();
  else startPress();
}

// ---- wiring ----------------------------------------------------------------
export function initDictation(h) {
  hooks = h;
  // Rescue first: recordings stranded by a crash become "interrupted" (listed
  // in history with transcribe/download), and expired audio is cleared out.
  vaultSweep();
  // Test hook: is the microphone actually open (a LIVE track — what lights the
  // OS mic indicator), as opposed to merely permitted? The stream is module-
  // private, so the release-on-hide behaviour is unobservable without this.
  window.__kbmicLive = () =>
    !!(stream && stream.getAudioTracks().some((t) => t.readyState === "live"));
  window.__kbdictStream = () => stream;   // test hook: to emulate the OS reclaiming the mic
  const rm = window.matchMedia("(prefers-reduced-motion: reduce)");
  reduceMotion = rm.matches;
  rm.addEventListener("change", (e) => { reduceMotion = e.matches; });

  // Both halves of the gesture are handled HERE, in the capture phase, not
  // through app.js's bubble-phase dispatcher. Three reasons, all load-bearing:
  //
  //  • Auto-repeat. Holding a key emits keydown ~30×/s. The bubble dispatcher
  //    calls run() for every one of them, and a toggle called 30×/s is a
  //    recording that flickers on and off. Only the event knows: `e.repeat`.
  //  • keyup at all. The dispatcher is keydown-only by design, and xterm and
  //    CodeMirror both stop propagation of keys they consume — a keyup we never
  //    see is a recording that never stops.
  //  • Modals. wireShortcuts() deliberately gives up the keyboard while a modal
  //    is open, but the command palette and the new-doc dialog are text fields,
  //    and dictating into them is half the point of the feature.
  //
  // preventDefault() is what keeps this from firing twice: the bubble
  // dispatcher's first line is `if (e.defaultPrevented) return`. The BINDINGS
  // entry in app.js still exists — it is what documents the key in the help
  // sheet, offers it in the palette, and tells xterm to yield it instead of
  // sending \x1b[20~ to the shell.
  window.addEventListener("keydown", (e) => {
    if (!isDictateKey(e)) return;
    e.preventDefault();
    if (e.repeat || keyDown) return;     // OS auto-repeat, not a new press
    keyDown = true;
    pressCode = e.code;
    startPress();
  }, true);

  // Match the keyup on the PHYSICAL key that started the press and nothing else.
  // Alt+K released as Alt-then-K delivers a keyup with altKey already false, so
  // re-running isDictateKey() here would miss it — and a missed keyup is a
  // recording that runs until the 10-minute cap.
  window.addEventListener("keyup", (e) => {
    if (!keyDown || e.code !== pressCode) return;
    keyDown = false;
    pressCode = null;
    stopPress(false);
  }, true);

  // Every way a keyup can fail to arrive. Each one strands a recording, so each
  // one gets a guard rather than a comment.
  //
  // blur ends only a KEY-held recording (the stranded-keyup case it exists
  // for). A latched recording survives it: mobile browsers blur the window for
  // their own chrome — Firefox for Android does it for the mic-permission
  // doorhanger and its "recording" notification, i.e. moments after recording
  // starts — and the tab going properly hidden is handled below.
  window.addEventListener("blur", () => {
    const wasKey = keyDown;
    keyDown = false; pressCode = null;
    if (wasKey && state === "recording") finishRecording();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      // Coming back to a recording that was finished for you looks like the
      // feature silently died — say what happened, once, on return. (A toast
      // fired while hidden would have expired long before anyone saw it.)
      if (stoppedOffScreen) {
        stoppedOffScreen = false;
        // lastBlob is cleared on a successful transcription and kept on a
        // failed one — it distinguishes "already in history" from "retry me".
        if (hooks.toast) hooks.toast(
          "Recording stopped when the app left the screen — " +
          (lastBlob ? "it is saved in Dictation history; transcribe it from there"
                    : "the transcript is in the dictation history"));
      }
      return;
    }
    keyDown = false; pressCode = null;
    // A recording deliberately KEEPS RUNNING off-screen — dictating a note
    // while reading something in another app is half the point of latching,
    // and the phone's mic dot is then telling the truth. (A key-held recording
    // never gets here: switching apps blurs the window, and the blur handler
    // above already finished it — its keyup is gone for good.) The recording
    // stays bounded: the 10-minute cap still fires off-screen, and if the OS
    // or the browser kills the capture in the background, the track's "ended"
    // guard finishes it — both land in finishRecording, which sees
    // document.hidden, releases the mic, and queues the explanation below.
    if (state === "recording") return;
    // idle or sending: nothing is listening and the blob (if any) is already
    // collected — give the device back so the phone's mic indicator dies with
    // the app leaving the screen.
    releaseMicNow();
  });
  window.addEventListener("pagehide", releaseMicNow);

  const stopBtn = $("#ptt-stop");
  if (stopBtn) stopBtn.addEventListener("click", () => finishRecording());

  // Press-and-hold works on the button too, so a user whose window manager eats
  // F-keys still gets push-to-talk.
  const b = h.button;
  if (b) {
    b.addEventListener("pointerdown", (e) => { e.preventDefault(); startPress(); });
    b.addEventListener("pointerup", () => stopPress(false));
    b.addEventListener("pointercancel", () => stopPress(false));
    b.addEventListener("lostpointercapture", () => stopPress(false));
    // Keyboard activation of the button itself (Enter/Space) is a plain toggle.
    b.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDictation(); }
    });
  }
}

// Which physical keys mean "dictate". `code`, not `key`: the character changes
// under a modifier or a Czech/Dvorak layout, and comparing `key` between keydown
// and keyup is the classic stuck-recording bug.
export const DICTATE_CODES = ["F9"];
function isDictateKey(e) {
  if (DICTATE_CODES.includes(e.code)) return !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey;
  return e.code === "KeyK" && e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey;
}
