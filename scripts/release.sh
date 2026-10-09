#!/usr/bin/env bash
# Cut a release: bump VERSION, move the licence's Change Date, write the
# release's section of CHANGELOG.md, tag, push.
#
#   bash scripts/release.sh 1.1.0 [--push]
#
# WHY THIS IS A SCRIPT. Under the Business Source License each version converts
# to Apache 2.0 on its own Change Date — four years out. Cutting a release
# without moving that date would publish the new code on the OLD date, earlier
# than intended, and it is not a mistake anyone would notice for years. So the
# date moves here, together with the number, or not at all.
#
# The changelog section is the commit titles since the previous tag
# (scripts/changelog.sh). Settings → About shows it on every box, so read
# `git log --format=%s <last tag>..` before cutting: a title that would read
# badly there is cheaper to fix here than in every installation.
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

# Newest first, under the file's own header: the new section goes in above
# the previous release's.
PREV="$(git describe --tags --abbrev=0 2>/dev/null || true)"
NOTES="$(bash scripts/changelog.sh "${PREV:+$PREV..}HEAD")"
[ -n "$NOTES" ] || NOTES="- No user-facing changes."
[ -f CHANGELOG.md ] || printf '# Changelog\n' > CHANGELOG.md
SECTION="$(printf '## %s — %s\n\n%s\n' "$V" "$(date -u +%Y-%m-%d)" "$NOTES")"
SECTION="$SECTION" awk '!done && /^## / { print ENVIRON["SECTION"]; print ""; done = 1 } { print }
  END { if (!done) { print ""; print ENVIRON["SECTION"] } }' CHANGELOG.md > CHANGELOG.md.tmp
mv CHANGELOG.md.tmp CHANGELOG.md

echo "version      $V"
echo "change date  $CHANGE_DATE  (LICENSE, NOTICE)"
echo "changelog    $(grep -c '^- ' <<<"$NOTES") line(s) since ${PREV:-the beginning}  (CHANGELOG.md)"
git add VERSION LICENSE NOTICE CHANGELOG.md
git commit -q -m "Release $V

Change Date for this version: $CHANGE_DATE (Business Source License 1.1)."
git tag -a "v$V" -m "Ollsoft Company OS $V"

if [ "$PUSH" -eq 1 ]; then
  git push origin HEAD "v$V"
  echo "pushed. Boxes on the stable channel pick this up on their next timer run."
else
  echo "not pushed. Review, then: git push origin HEAD v$V"
fi
