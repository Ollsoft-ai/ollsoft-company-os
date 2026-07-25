# Dictation (speech-to-text)

Speak, and the words land wherever you were already typing — a document, a
terminal, the command palette, any text field. `F9` (or `Alt+K`, or the mic button
in the topbar).

Two gestures, one key, no mode to remember:

| gesture | behaviour |
|---|---|
| **hold** the key | push-to-talk — recording ends the moment you let go |
| **tap** it once | latched — recording continues; tap again to finish |

The indicator (bottom-right) shows a pulsing dot, a live input-level meter, and a
**Stop** button, and it stays visible wherever focus is. A recording auto-stops
after 90 seconds, and on window blur, tab switch or page hide.

---

## Where the words go

Resolved when recording **starts** — the round trip takes a second or more, and by
the time the transcript lands you may have clicked elsewhere — then re-validated
at insertion. In order:

1. **Terminal focused** → `term.paste()` into the active terminal. Newlines are
   collapsed to spaces unless the foreground app enabled bracketed paste, and
   **no Enter is ever appended**: a mis-transcribed command that runs itself has
   no undo. You press Return.
2. **A text field focused** (`<input>`/`<textarea>`, including the command
   palette and dialogs) → inserted at the caret via `execCommand('insertText')`,
   which is the only method that preserves the field's native undo stack.
3. **A writable document open** → inserted at the cursor as one undo step
   (`userEvent: "input.paste"`, so it does not merge with subsequent typing). The
   editor is CRDT-backed, so it syncs to disk on its own — there is no save step.
4. **Nothing suitable** → a toast saying so. The text is never silently dropped.

The transcript is third-party text, so it is stripped of C0 control characters
(including ESC) and NFC-normalized before it touches anything. That matters most
for the terminal: without it, a hallucinated `\x1b[201~` in a transcript could
terminate xterm's bracketed paste and hand the remainder to the shell as typed
input. `term.paste()`'s own ESC→`␛` rewrite is the second layer, not the only one.

---

## Where the key lives, and why

**`/etc/kb/elevenlabs.key`, mode `0600 root:root`.** Only `kb-hub` (which runs as
root) can read it. `POST /stt` on the hub is the only way to spend it.

This is deliberately **not** the `_secrets/` mechanism that `/egress` uses. That
one is built so "can use the key" *is* "can read the file", kernel-checked — the
right model for a user's own credential. A company STT key needs the exact
inverse: everyone spends it, nobody reads it. It also cannot live in a per-user
backend, because those are spawned `runuser -u <user>` — anything the backend can
read, that user's own shell, cron job or agent can read.

The caller never chooses the upstream URL. That is the load-bearing detail: it is
why a key scoped to speech-to-text can never be pointed at `/v1/text-to-speech`,
`/v1/voices/add` or `/v1/user` by anyone who can log in.

### What a non-privileged user can and cannot do

**Can**: dictate, and thereby spend company ElevenLabs credit, bounded by a
per-user daily quota. See their own transcripts.

**Cannot**: read the key; extract it through the proxy (the `xi-api-key` header is
set server-side and no upstream response body is ever forwarded verbatim); use it
for anything but speech-to-text; follow a redirect somewhere else
(`allow_redirects=False`); forge the identity the quota is keyed on (the session
cookie is HMAC-signed); or see what anyone else dictated.

### Install or rotate the key

```bash
sudo scripts/install-dictation-key.sh sk_xxxxxxxxxxxx
# or, from the curl-config file it may already live in:
sudo scripts/install-dictation-key.sh --from ~/.secrets/elevenlabs_curl.cfg
sudo systemctl restart kb-hub          # the key is read once, at startup
```

The script validates the key against ElevenLabs before installing it. On the
ElevenLabs side, scope the key to **`speech_to_text` only** — this is a credential
every logged-in user can spend, so it should be able to do exactly one thing.

If the key is missing, `/stt` returns **503** and the mic button hides itself.
Nothing else on the platform is affected; `deploy.sh` prints a warning.

---

## What is sent upstream

`POST https://api.elevenlabs.io/v1/speech-to-text`, one stateless call per
utterance, with the browser's `audio/webm;codecs=opus` blob forwarded unmodified
(no transcoding anywhere):

| field | value | why |
|---|---|---|
| `model_id` | `scribe_v2` | current batch model; `scribe_v1` is the old one |
| `tag_audio_events` | `false` | **defaults to true** — would drop `(laughter)` into your prose |
| `timestamps_granularity` | `none` | word timings are not needed |
| `diarize` | `false` | one speaker |
| `enable_logging` | `false` | zero retention at ElevenLabs |
| `language_code` | omitted | auto-detect across ~90 languages |

Auto-detect handles mixed Czech/English in one session. To pin a language, run
**"Dictation language…"** from the command palette; it is stored per browser and
sent as `?lang=cs`.

Batch, not the realtime websocket, on purpose: the realtime endpoint takes raw
PCM/µ-law, which `MediaRecorder` cannot emit, so it would mean an AudioWorklet
plus manual PCM16 framing plus single-use-token minting — strictly more moving
parts, 77% higher cost, and no benefit for 3–15 second utterances.

### Audio does leave the box

Every dictation is uploaded to ElevenLabs. `enable_logging=false` means they do
not retain it, but the request happens — worth knowing on a platform whose whole
premise is that the kernel is the boundary. The mic button's tooltip says so.

---

## Limits and the audit trail

- **12 MiB** per recording (~50 min of 32 kbps mono opus); over that, `413`.
- Under **1 KiB** or **300 ms** is treated as a stray tap: answered `200` with an
  empty transcript, *without* spending an API call.
- **~1 hour of audio per user per UTC day** (`KB_STT_DAILY_BYTES`), then `429`.
  In-memory and reset by a hub restart — a brake, not billing.
- `/var/log/kb/stt.log` records one JSON line per call:
  `{ts, user, bytes, status, ms, chars, secs}`. **Never the transcript** — this is
  a microphone in an office, and what someone said is nobody else's business.

## Operational notes

- The microphone is held open between utterances so the next one starts instantly
  and the browser does not re-prompt (Firefox's one-time grants make re-requesting
  per utterance a re-prompt, and a prompt during a hold steals the keyup and
  strands the recording). The cost is that the browser's "in use" indicator stays
  lit; it is released after 5 idle minutes, on tab hide, and on page unload.
  **"Release the microphone"** in the command palette does it immediately.
- `getUserMedia` needs a secure context. Over the Cloudflare tunnel (https) and on
  localhost this is satisfied; over plain http to a LAN IP it is not, and the mic
  button hides itself.
- **`F9` no longer reaches the shell** while dictation is available — `mc`'s menu
  key is the notable casualty. It is one array element in `BINDINGS` (`app.js`) if
  that turns out to matter; the `?` sheet re-renders itself from the same table.
- Dictation is the one binding that deliberately works while a modal is open,
  because the command palette and the new-doc dialog are text fields.

## Tests

```bash
.venv/bin/python -m pytest tests/cli/test_stt.py tests/e2e/test_dictation.py -q

# iterate on the client without root: serve frontend/static from the source tree
# instead of /opt (which only `sudo deploy.sh` updates)
KB_DEV_BUNDLE=1 .venv/bin/python -m pytest tests/e2e/test_dictation.py -q
```

`tests/e2e/test_dictation.py` uses chromium's fake audio device and stubs `/stt`,
so it exercises the whole client path without spending anything. `test_stt.py`
covers the auth boundary, the size/quota limits, and that the key appears in no
response; its live-upstream cases are opt-in via `KB_STT_LIVE=1`.

Two things no automated test covers, worth checking by hand after a change:

1. Two dictations back-to-back **in Firefox** without ticking "Remember this
   decision" — the second must not re-prompt. That validates the held-stream
   lifecycle.
2. Actual transcription accuracy, in the languages you speak.
