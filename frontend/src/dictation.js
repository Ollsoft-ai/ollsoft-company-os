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
const MAX_MS = 90_000;      // hard cap: nobody meant to record for two minutes
const MIN_MS = 300;         // shorter than this was a mis-hit, not speech
const MIN_BYTES = 1024;     // ditto, measured after encoding
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
let reduceMotion = false;

export function dictationReady() {
  return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
            typeof MediaRecorder !== "undefined" && window.isSecureContext);
}

export function dictationState() { return state; }

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
  rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
  rec.start();   // no timeslice: one blob, delivered on stop

  state = "recording";
  startedAt = Date.now();
  stopMeter = startMeter();
  paint(latched ? "Listening… press again to finish" : "Listening…");
  clearTimeout(maxTimer);
  maxTimer = setTimeout(() => {
    if (state === "recording") { finishRecording("Stopped after 90 seconds"); }
  }, MAX_MS);
  return true;
}

// The blob only exists once `onstop` has fired. Reading it synchronously after
// stop() loses 100% of the audio when there is no timeslice — this is the single
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

  if (note && hooks.toast) hooks.toast(note, "err");

  if (!blob || blob.size < MIN_BYTES || elapsed < MIN_MS) {
    state = "idle"; paint("");
    if (hooks.toast) hooks.toast("Too short — hold the key while you speak", "err");
    return;
  }
  lastBlob = blob;
  await transcribeAndInsert(blob);
}

async function transcribeAndInsert(blob) {
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
      state = "idle"; paint("");
      fail((j && j.error) || "Transcription failed (HTTP " + r.status + ")");
      if (r.status !== 503 && r.status !== 429) hintRetry();
      return;
    }
  } catch (e) {
    state = "idle"; paint("");
    fail("Could not reach the server — your recording is kept, run “Retry the last dictation”.");
    return;
  }
  state = "idle"; paint("");

  const text = sanitize((j && j.text) || "");
  if (!text) {
    if (hooks.toast) hooks.toast("Nothing was said", "err");
    return;
  }
  lastBlob = null;
  hooks.insert(target, text);
}

function hintRetry() {
  if (hooks.toast) hooks.toast("Your recording is kept — run “Retry the last dictation”.", "err");
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
  if (!lastBlob) { if (hooks && hooks.toast) hooks.toast("Nothing to retry", "err"); return; }
  if (state !== "idle") return;
  await transcribeAndInsert(lastBlob);
}

// ---- the two gestures -------------------------------------------------------
// Called on the FIRST keydown (auto-repeat is filtered by `keyDown`) and on
// pointerdown on the mic button.
export function startPress() {
  if (!dictationReady()) return;
  if (state === "sending") return;                 // a transcript is in flight
  if (state === "recording") { finishRecording(); return; }   // second tap ends a latch
  latched = false;
  beginRecording();
}

// Called on keyup / pointerup. `abort` short-circuits the hold logic.
export function stopPress(abort) {
  if (state !== "recording") return;
  if (abort) { finishRecording(); return; }
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
  // recording that runs until the 90 s cap.
  window.addEventListener("keyup", (e) => {
    if (!keyDown || e.code !== pressCode) return;
    keyDown = false;
    pressCode = null;
    stopPress(false);
  }, true);

  // Every way a keyup can fail to arrive. Each one strands a recording, so each
  // one gets a guard rather than a comment.
  window.addEventListener("blur", () => {
    keyDown = false; pressCode = null;
    if (state === "recording") finishRecording();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) return;
    keyDown = false; pressCode = null;
    if (state === "recording") finishRecording();
    else if (state === "idle") releaseMicNow();
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
