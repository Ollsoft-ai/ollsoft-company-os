#!/usr/bin/env bash
# Install the agents the chat can drive (docs/agent-chat.md) into the shared
# prefix every person's backend spawns them from — and the Node they need.
#
#   sudo bash scripts/install-agents.sh                 # Node 22 + the default three
#   sudo bash scripts/install-agents.sh --node-only     # just the private Node
#   sudo bash scripts/install-agents.sh @xai-official/grok@1.0.38   # one package
#   sudo bash scripts/install-agents.sh --refresh       # what an update runs: the
#        newest Node of the same major, and every agent already installed here
#        moved to the version this release pins (kb_platform/acp.py CATALOGUE)
#
# The Claude adapter needs Node >= 22 and Ubuntu's node is 18, so a private
# Node LTS goes under $PREFIX/node (verified against nodejs.org's checksums)
# and the platform puts its bin first on the agents' PATH. Nothing outside
# $PREFIX is touched. Re-running is safe: an up-to-date Node is kept.
set -euo pipefail

PREFIX="${KB_AGENTS_PREFIX:-/opt/kb-agents}"
NODE_MAJOR="${KB_AGENTS_NODE_MAJOR:-22}"
DEFAULT_PKGS=(@agentclientprotocol/claude-agent-acp@0.107.0 @agentclientprotocol/codex-acp@1.12.0 @google/gemini-cli@0.60.0)

node_only=0
refresh=0
pkgs=()
for a in "$@"; do
  case "$a" in
    --node-only) node_only=1 ;;
    --refresh) refresh=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) pkgs+=("$a") ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo): $PREFIX is shared, root-owned code" >&2; exit 1; }

if [ "$refresh" -eq 1 ]; then
  [ "${#pkgs[@]}" -eq 0 ] || { echo "--refresh takes no packages" >&2; exit 2; }
  # the pins of the release being installed: this script's own checkout
  here="$(cd "$(dirname "$0")/.." && pwd)"
  py="${KB_VENV_PY:-/opt/kb-venv/bin/python}"
  [ -x "$py" ] || py=python3
  mapfile -t pkgs < <(cd "$here" && KB_AGENTS_PREFIX="$PREFIX" "$py" -m kb_platform.acp installed-pins)
  if [ "${#pkgs[@]}" -eq 0 ] && [ ! -x "$PREFIX/node/bin/node" ]; then
    echo "agents: none installed in $PREFIX — nothing to refresh"
    exit 0
  fi
fi

mkdir -p "$PREFIX"
chmod 755 "$PREFIX"

have_node() {
  local v
  v="$("$PREFIX/node/bin/node" -v 2>/dev/null || true)"
  v="${v%%.*}"
  [ -n "$v" ] && [ "${v#v}" != "$v" ] && [ "${v#v}" -ge "$NODE_MAJOR" ] 2>/dev/null
}
if have_node && [ "$refresh" -eq 0 ]; then
  echo "node: $("$PREFIX/node/bin/node" -v) at $PREFIX/node (kept)"
else
  arch="$(uname -m)"
  case "$arch" in
    x86_64) narch=x64 ;;
    aarch64|arm64) narch=arm64 ;;
    *) echo "no Node build for $arch" >&2; exit 1 ;;
  esac
  base="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"
  tmp="$(mktemp -d "$PREFIX/.node.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT
  echo "node: fetching the latest v${NODE_MAJOR}.x for linux-$narch …"
  curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
  file="$(grep -oE "node-v${NODE_MAJOR}\.[0-9]+\.[0-9]+-linux-${narch}\.tar\.xz" "$tmp/SHASUMS256.txt" | head -1)"
  [ -n "$file" ] || { echo "no linux-$narch tarball in $base" >&2; exit 1; }
  want="${file#node-}"; want="${want%-linux-*}"
  if have_node && [ "$("$PREFIX/node/bin/node" -v)" = "$want" ]; then
    echo "node: $want at $PREFIX/node (current)"
  else
  curl -fsSL "$base/$file" -o "$tmp/$file"
  (cd "$tmp" && grep " $file\$" SHASUMS256.txt | sha256sum -c --quiet -)
  rm -rf "$PREFIX/node.new"
  mkdir -p "$PREFIX/node.new"
  tar -xJf "$tmp/$file" -C "$PREFIX/node.new" --strip-components=1
  rm -rf "$PREFIX/node"
  mv "$PREFIX/node.new" "$PREFIX/node"
  echo "node: $("$PREFIX/node/bin/node" -v) installed at $PREFIX/node"
  fi
fi
[ "$node_only" -eq 1 ] && exit 0
if [ "$refresh" -eq 1 ] && [ "${#pkgs[@]}" -eq 0 ]; then echo "agents: none installed — Node only"; exit 0; fi

[ "${#pkgs[@]}" -gt 0 ] || pkgs=("${DEFAULT_PKGS[@]}")
export PATH="$PREFIX/node/bin:$PATH"
export HOME="${HOME:-/root}" NO_UPDATE_NOTIFIER=1
echo "agents: npm install ${pkgs[*]} …"
(cd "$PREFIX" && npm install --prefix "$PREFIX" --no-audit --no-fund --loglevel=error "${pkgs[@]}")
chmod -R a+rX "$PREFIX/node_modules" 2>/dev/null || true
echo "agents: $(ls "$PREFIX/node_modules/.bin" 2>/dev/null | tr '\n' ' ')"
