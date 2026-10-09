#!/usr/bin/env bash
# The changelog lines for a range of commits, one per change, oldest first:
#
#   bash scripts/changelog.sh v1.1.0..HEAD
#
# Commit titles ARE the changelog: release.sh writes these lines into
# CHANGELOG.md, deploy.sh shows the ones a working copy runs beyond its last
# release, and Settings → About displays both. So a title is written for the
# person reading that list, not for the diff.
#
# Left out: the release commits themselves, merges, and any commit that only
# touched the README, docs, tests or CI — nothing anyone using the platform
# would notice.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RANGE="${1:?usage: changelog.sh <rev-range>}"

for h in $(git log --reverse --no-merges --format=%H "$RANGE"); do
  title="$(git show -s --format=%s "$h")"
  [[ "$title" =~ ^Release\ [0-9]+\.[0-9]+\.[0-9]+$ ]] && continue
  files="$(git show --format= --name-only "$h")"
  grep -qvE '^(README\.md|CONTRIBUTING\.md|docs/|tests/|\.github/)' <<<"$files" || continue
  echo "- $title"
done
