#!/usr/bin/env bash
# Install (or rotate) the ElevenLabs speech-to-text key used by dictation.
#
# The key is a COMPANY credential: every logged-in user may spend it through the
# hub's /stt route, and no user may read it. So it lives beside the session key —
# 0600 root:root under /etc/kb — where only kb-hub (which runs as root) can open
# it. Per-user backends run as the logged-in user and deliberately cannot.
#
#   sudo scripts/install-dictation-key.sh sk_xxxxxxxxxxxx
#   sudo scripts/install-dictation-key.sh --from ~/.secrets/elevenlabs_curl.cfg
#
# The key needs only the `speech_to_text` permission on the ElevenLabs side.
# Scope it there too — this script cannot.
set -euo pipefail

DEST=/etc/kb/elevenlabs.key

if [ "$(id -u)" -ne 0 ]; then
  echo "run me as root: sudo $0 $*" >&2
  exit 1
fi

case "${1:-}" in
  --from)
    src="${2:?--from needs a file}"
    # Accepts a bare key or the `header = "xi-api-key: …"` curl-config form.
    KEY=$(sed -n 's/.*xi-api-key:[[:space:]]*\([A-Za-z0-9_-]\{16,\}\).*/\1/p' "$src" | head -1)
    [ -n "$KEY" ] || KEY=$(tr -d '[:space:]' < "$src")
    ;;
  "" | -h | --help)
    echo "usage: $0 <api-key>" >&2
    echo "       $0 --from <file containing the key>" >&2
    exit 1
    ;;
  *) KEY="$1" ;;
esac

if ! printf '%s' "$KEY" | grep -qE '^[A-Za-z0-9_-]{16,}$'; then
  echo "that does not look like an ElevenLabs key" >&2
  exit 1
fi

echo "== checking the key against ElevenLabs =="
code=$(curl -sS -o /tmp/kb-stt-check.$$ -w '%{http_code}' \
  -X POST https://api.elevenlabs.io/v1/speech-to-text \
  -H "xi-api-key: $KEY" \
  -F model_id=scribe_v2 -F tag_audio_events=false -F enable_logging=false \
  -F 'file=@/dev/null;filename=probe.wav;type=audio/wav' || echo 000)
# 400/422 = the key is fine and it rejected our empty probe file, which is the
# outcome we want here; 401 means the key or its permissions are wrong.
case "$code" in
  200|400|422) echo "   key accepted (HTTP $code on an empty probe — expected)" ;;
  401|403)
    echo "!! ElevenLabs rejected the key (HTTP $code):" >&2
    cat /tmp/kb-stt-check.$$ >&2; echo >&2
    echo "   the key needs the 'speech_to_text' permission" >&2
    rm -f /tmp/kb-stt-check.$$; exit 1 ;;
  *)  echo "!! unexpected response (HTTP $code) — installing anyway, verify by hand" >&2 ;;
esac
rm -f /tmp/kb-stt-check.$$

install -m 0600 -o root -g root /dev/null "$DEST"
printf '%s' "$KEY" > "$DEST"
chmod 0600 "$DEST"

echo "== installed $DEST (0600 root:root) =="
ls -l "$DEST"
echo
echo "now restart the hub so it picks the key up:"
echo "  systemctl restart kb-hub"
