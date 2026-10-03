#!/usr/bin/env bash
# Cut a release: bump VERSION, move the licence's Change Date, tag, push.
#
#   bash scripts/release.sh 1.1.0 [--push]
#
# WHY THIS IS A SCRIPT. Under the Business Source License each version converts
# to Apache 2.0 on its own Change Date — four years out. Cutting a release
# without moving that date would publish the new code on the OLD date, earlier
# than intended, and it is not a mistake anyone would notice for years. So the
# date moves here, together with the number, or not at all.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
V="${1:?usage: release.sh <major.minor.patch> [--push]}"
PUSH=0; [ "${2:-}" = "--push" ] && PUSH=1
[[ "$V" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be N.N.N" >&2; exit 2; }

git diff --quiet && git diff --cached --quiet || { echo "working tree is dirty" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/v$V" >/dev/null && { echo "v$V already exists" >&2; exit 1; }

CHANGE_DATE="$(date -u -d '+4 years' +%Y-%m-%d)"
echo "$V" > VERSION
sed -i -E "s/^(Change Date: *)[0-9]{4}-[0-9]{2}-[0-9]{2}/\\1$CHANGE_DATE/" LICENSE
grep -q "Change Date: *$CHANGE_DATE" LICENSE || { echo "LICENSE Change Date not updated" >&2; exit 1; }
sed -i -E "s|On [0-9]{4}-[0-9]{2}-[0-9]{2}, or four years|On $CHANGE_DATE, or four years|" NOTICE

echo "version      $V"
echo "change date  $CHANGE_DATE  (LICENSE, NOTICE)"
git add VERSION LICENSE NOTICE
git commit -q -m "Release $V

Change Date for this version: $CHANGE_DATE (Business Source License 1.1)."
git tag -a "v$V" -m "Ollsoft Company OS $V"

if [ "$PUSH" -eq 1 ]; then
  git push origin HEAD "v$V"
  echo "pushed. Boxes on the stable channel pick this up on their next timer run."
else
  echo "not pushed. Review, then: git push origin HEAD v$V"
fi
