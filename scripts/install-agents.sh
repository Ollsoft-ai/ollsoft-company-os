#!/usr/bin/env bash
# Install the agents the chat can drive (docs/agent-chat.md) into the shared
# prefix every person's backend spawns them from — and the Node they need.
#
#   sudo bash scripts/install-agents.sh                 # Node 22 + the default three
#   sudo bash scripts/install-agents.sh --node-only     # just the private Node
#   sudo bash scripts/install-agents.sh @xai-official/grok@1.0.38   # one package
#
# The Claude adapter needs Node >= 22 and Ubuntu's node is 18, so a private
# Node LTS goes under $PREFIX/node (verified against nodejs.org's checksums)
# and the platform puts its bin first on the agents' PATH. Nothing outside
# $PREFIX is touched. Re-running is safe: an up-to-date Node is kept.
set -euo pipefail

PREFIX="${KB_AGENTS_PREFIX:-/opt/kb-agents}"
NODE_MAJOR="${KB_AGENTS_NODE_MAJOR:-22}"
DEFAULT_PKGS=(@agentclientprotocol/claude-agent-acp@0.79.0 @agentclientprotocol/codex-acp@1.12.0 @google/gemini-cli@0.60.0)

node_only=0
pkgs=()
for a in "$@"; do
  case "$a" in
    --node-only) node_only=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) pkgs+=("$a") ;;
  esac
done
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo): $PREFIX is shared, root-owned code" >&2; exit 1; }

mkdir -p "$PREFIX"
chmod 755 "$PREFIX"

have_node() {
  local v
  v="$("$PREFIX/node/bin/node" -v 2>/dev/null || true)"
  [ -n "$v" ] && [ "${v#v}" != "$v" ] && [ "${v#v}" -ge "$NODE_MAJOR" ] 2>/dev/null
}
if have_node; then
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
  curl -fsSL "$base/$file" -o "$tmp/$file"
  (cd "$tmp" && grep " $file\$" SHASUMS256.txt | sha256sum -c --quiet -)
  rm -rf "$PREFIX/node.new"
  mkdir -p "$PREFIX/node.new"
  tar -xJf "$tmp/$file" -C "$PREFIX/node.new" --strip-components=1
  rm -rf "$PREFIX/node"
  mv "$PREFIX/node.new" "$PREFIX/node"
  echo "node: $("$PREFIX/node/bin/node" -v) installed at $PREFIX/node"
fi
[ "$node_only" -eq 1 ] && exit 0

[ "${#pkgs[@]}" -gt 0 ] || pkgs=("${DEFAULT_PKGS[@]}")
export PATH="$PREFIX/node/bin:$PATH"
export HOME="${HOME:-/root}" NO_UPDATE_NOTIFIER=1
echo "agents: npm install ${pkgs[*]} …"
(cd "$PREFIX" && npm install --prefix "$PREFIX" --no-audit --no-fund --loglevel=error "${pkgs[@]}")
chmod -R a+rX "$PREFIX/node_modules" 2>/dev/null || true
echo "agents: $(ls "$PREFIX/node_modules/.bin" 2>/dev/null | tr '\n' ' ')"
