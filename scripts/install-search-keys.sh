#!/usr/bin/env bash
# Install (or rotate) the semantic-search provider keys and point kb-embedd at
# the provider. See docs/semantic-search.md.
#
# The keys are COMPANY credentials spent only by kb-embedd, which runs as
# kbindexer. So they live under /etc/kb as 0640 root:kbindexer: the worker can
# read them, no person and no agent can. Search requests reach the provider only
# through kb-embedd's socket, which meters and caps every call.
#
#   # Azure AI Foundry / Azure OpenAI — reads the key with `az` (never printed):
#   sudo scripts/install-search-keys.sh --from-azure <account> <resource-group> \
#        [--embed-deployment text-embedding-3-large] [--rerank-model Cohere-rerank-v4.0-pro]
#
#   # Any provider, keys by hand (read from files so they never sit in shell history):
#   sudo scripts/install-search-keys.sh --embed-provider openai \
#        --embed-url https://api.openai.com/v1 --embed-model text-embedding-3-large \
#        --embed-key-file ~/embed.key \
#        --rerank-url https://api.cohere.com/v2/rerank --rerank-model rerank-v3.5 \
#        --rerank-key-file ~/rerank.key
#
#   sudo scripts/install-search-keys.sh --off      # back to full-text only
#
# Each key is checked with one tiny request before it is installed (≈ 3 tokens
# of embedding, one rerank search unit — a fraction of a cent).
set -euo pipefail

ETC=/etc/kb
ENV_FILE=$ETC/kb.env
EMBED_KEY_FILE=$ETC/embed.key
RERANK_KEY_FILE=$ETC/rerank.key
API_VERSION=2024-10-21

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-1}"; }

[ "$(id -u)" -eq 0 ] || { echo "run me as root: sudo $0 $*" >&2; exit 1; }
[ $# -gt 0 ] || usage

EMBED_PROVIDER="" EMBED_URL="" EMBED_MODEL="" EMBED_DIMS=1024 EMBED_KEY=""
RERANK_PROVIDER="" RERANK_URL="" RERANK_MODEL="" RERANK_KEY=""
AZ_ACCOUNT="" AZ_RG="" OFF=0
EMBED_DEPLOYMENT=text-embedding-3-large
RERANK_DEPLOYMENT=Cohere-rerank-v4.0-pro

readkey() { tr -d '[:space:]' < "${1:?missing key file}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --from-azure)        AZ_ACCOUNT="${2:?}"; AZ_RG="${3:?}"; shift 3 ;;
    --embed-deployment)  EMBED_DEPLOYMENT="${2:?}"; shift 2 ;;
    --rerank-model)      RERANK_DEPLOYMENT="${2:?}"; RERANK_MODEL="${2:?}"; shift 2 ;;
    --embed-provider)    EMBED_PROVIDER="${2:?}"; shift 2 ;;
    --embed-url)         EMBED_URL="${2:?}"; shift 2 ;;
    --embed-model)       EMBED_MODEL="${2:?}"; shift 2 ;;
    --embed-dims)        EMBED_DIMS="${2:?}"; shift 2 ;;
    --embed-key-file)    EMBED_KEY="$(readkey "${2:?}")"; shift 2 ;;
    --rerank-provider)   RERANK_PROVIDER="${2:?}"; shift 2 ;;
    --rerank-url)        RERANK_URL="${2:?}"; shift 2 ;;
    --rerank-key-file)   RERANK_KEY="$(readkey "${2:?}")"; shift 2 ;;
    --off)               OFF=1; shift ;;
    -h|--help)           usage 0 ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done

# ---- the env file: replace our keys in place, keep everything else ----------
set_env() {   # set_env KEY VALUE
  local k="$1" v="$2"
  touch "$ENV_FILE"
  if grep -q "^$k=" "$ENV_FILE"; then
    local esc; esc=$(printf '%s' "$v" | sed 's/[\\&|]/\\&/g')
    sed -i "s|^$k=.*|$k=$esc|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$k" "$v" >> "$ENV_FILE"
  fi
}

restart_hint() {
  echo
  echo "kb-embedd reads this at start:  systemctl restart kb-embedd"
  echo "status:                         cat /run/kb/search/status.json"
}

if [ "$OFF" -eq 1 ]; then
  set_env KB_EMBED_PROVIDER none
  set_env KB_RERANK_PROVIDER none
  rm -f "$EMBED_KEY_FILE" "$RERANK_KEY_FILE"
  echo "== semantic search off: keys removed, providers set to none =="
  echo "   vectors already stored stay until you delete them; full-text search is unaffected"
  restart_hint
  exit 0
fi

if [ -n "$AZ_ACCOUNT" ]; then
  command -v az >/dev/null || { echo "az (Azure CLI) is not installed" >&2; exit 1; }
  # `az` runs as the invoking admin (their login), not as root.
  AZ_USER="${SUDO_USER:-root}"
  echo "== reading $AZ_ACCOUNT ($AZ_RG) with az as $AZ_USER =="
  endpoint=$(runuser -u "$AZ_USER" -- az cognitiveservices account show -n "$AZ_ACCOUNT" -g "$AZ_RG" \
               --query properties.endpoint -o tsv)
  key=$(runuser -u "$AZ_USER" -- az cognitiveservices account keys list -n "$AZ_ACCOUNT" -g "$AZ_RG" \
          --query key1 -o tsv)
  [ -n "$key" ] || { echo "could not read a key for $AZ_ACCOUNT" >&2; exit 1; }
  EMBED_PROVIDER=azure-openai
  EMBED_URL="https://$AZ_ACCOUNT.openai.azure.com"
  EMBED_MODEL="$EMBED_DEPLOYMENT"
  EMBED_KEY="$key"
  RERANK_PROVIDER=cohere
  RERANK_URL="https://$AZ_ACCOUNT.services.ai.azure.com/providers/cohere/v2/rerank"
  RERANK_MODEL="$RERANK_DEPLOYMENT"
  RERANK_KEY="$key"
  echo "   endpoint $endpoint"
fi

[ -n "$EMBED_PROVIDER" ] || { echo "no embedding provider given" >&2; usage; }
case "$EMBED_PROVIDER" in azure-openai|openai) ;; *)
  echo "--embed-provider must be azure-openai or openai (got $EMBED_PROVIDER)" >&2; exit 1 ;; esac
[ -n "$EMBED_URL" ] && [ -n "$EMBED_MODEL" ] && [ -n "$EMBED_KEY" ] \
  || { echo "the embedding provider needs a URL, a model and a key" >&2; exit 1; }
[ "$EMBED_DIMS" = 1024 ] || { echo "the index is built for 1024 dimensions" >&2; exit 1; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
chmod 700 "$TMP"
# curl reads headers from a file, so a key never appears in `ps`.
hdr() { printf '%s\n' "$1" > "$TMP/h"; chmod 600 "$TMP/h"; echo "$TMP/h"; }

echo "== checking the embedding key =="
if [ "$EMBED_PROVIDER" = azure-openai ]; then
  url="${EMBED_URL%/}/openai/deployments/$EMBED_MODEL/embeddings?api-version=$API_VERSION"
  h=$(hdr "api-key: $EMBED_KEY"); body="{\"input\":[\"probe\"],\"dimensions\":$EMBED_DIMS}"
else
  url="${EMBED_URL%/}/embeddings"
  h=$(hdr "Authorization: Bearer $EMBED_KEY")
  body="{\"input\":[\"probe\"],\"model\":\"$EMBED_MODEL\",\"dimensions\":$EMBED_DIMS}"
fi
code=$(curl -sS -m 30 -o "$TMP/out" -w '%{http_code}' -H @"$h" -H 'Content-Type: application/json' \
         -d "$body" "$url" || echo 000)
if [ "$code" != 200 ]; then
  echo "!! the embedding endpoint answered HTTP $code — nothing installed" >&2
  # Only the error code, never the body (it can echo request details).
  sed -n 's/.*"code"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/   error code: \1/p' "$TMP/out" | head -1 >&2
  exit 1
fi
dims=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["data"][0]["embedding"]))' "$TMP/out")
[ "$dims" = "$EMBED_DIMS" ] || { echo "!! the model returned $dims dimensions, not $EMBED_DIMS" >&2; exit 1; }
echo "   ok: $dims dimensions"

if [ -n "$RERANK_KEY" ]; then
  echo "== checking the rerank key =="
  case "$RERANK_URL" in *.azure.com*) h=$(hdr "api-key: $RERANK_KEY") ;;
                        *)            h=$(hdr "Authorization: Bearer $RERANK_KEY") ;; esac
  code=$(curl -sS -m 30 -o "$TMP/out" -w '%{http_code}' -H @"$h" -H 'Content-Type: application/json' \
           -d "{\"model\":\"$RERANK_MODEL\",\"query\":\"probe\",\"documents\":[\"probe\"],\"top_n\":1}" \
           "$RERANK_URL" || echo 000)
  if [ "$code" != 200 ]; then
    echo "!! the rerank endpoint answered HTTP $code — nothing installed" >&2
    sed -n 's/.*"code"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/   error code: \1/p' "$TMP/out" | head -1 >&2
    exit 1
  fi
  echo "   ok"
  RERANK_PROVIDER="${RERANK_PROVIDER:-cohere}"
fi

getent passwd kbindexer >/dev/null || { echo "no kbindexer account — is the platform installed?" >&2; exit 1; }
install -d -m 0755 "$ETC"
put() {   # put FILE KEY
  install -m 0640 -o root -g kbindexer /dev/null "$1"
  printf '%s' "$2" > "$1"
  chmod 0640 "$1"; chown root:kbindexer "$1"
}
put "$EMBED_KEY_FILE" "$EMBED_KEY"
set_env KB_EMBED_PROVIDER "$EMBED_PROVIDER"
set_env KB_EMBED_URL "$EMBED_URL"
set_env KB_EMBED_MODEL "$EMBED_MODEL"
set_env KB_EMBED_DIMS "$EMBED_DIMS"
if [ -n "$RERANK_KEY" ]; then
  put "$RERANK_KEY_FILE" "$RERANK_KEY"
  set_env KB_RERANK_PROVIDER "$RERANK_PROVIDER"
  set_env KB_RERANK_URL "$RERANK_URL"
  set_env KB_RERANK_MODEL "$RERANK_MODEL"
else
  set_env KB_RERANK_PROVIDER none
fi

echo "== installed =="
ls -l "$EMBED_KEY_FILE" ${RERANK_KEY:+"$RERANK_KEY_FILE"}
grep -E '^KB_(EMBED|RERANK)_' "$ENV_FILE"
echo
echo "Changing the model or dimensions re-embeds everything once (vectors are keyed"
echo "by model); kb-embedd prices it against the budgets in Settings → Company."
restart_hint
