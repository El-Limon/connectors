#!/usr/bin/env bash
# Refuses to publish a release the checkout does not actually match.
#
#   release-guard.sh <connector> <version> <tag>
#
# A recovery run is dispatched by hand with a tag and a version, and a release run is handed
# them by release-please. Either can disagree with what is checked out: a typo in the dispatch
# form, a manifest that moved on, a tag release-please has not pushed yet. Publishing anyway
# attaches one version's artifacts to another version's release, which cannot be undone once
# people have downloaded them. So every claim is checked before the build starts.
#
# An empty tag means "nothing is being published" and the guard stands down.
#
# The tag is fetched with a few retries: release-please creates the release and the tag in the
# same run that dispatches this workflow, and the tag is sometimes a second or two behind.
set -euo pipefail
cd "$(dirname "$0")/.."

CONNECTOR="${1:?usage: release-guard.sh <connector> <version> <tag>}"
VERSION="${2:?version required}"
TAG="${3-}"

ATTEMPTS="${RELEASE_GUARD_ATTEMPTS:-6}"
DELAY="${RELEASE_GUARD_DELAY:-10}"

fail() {
  echo "release-guard: $1" >&2
  exit 1
}

if [ -z "$TAG" ]; then
  echo "release-guard: no tag, nothing to guard"
  exit 0
fi

EXPECTED_TAG="${CONNECTOR}-v${VERSION}"
if [ "$TAG" != "$EXPECTED_TAG" ]; then
  fail "tag '$TAG' is not '$EXPECTED_TAG' — the tag and the version disagree"
fi

MANIFEST_VERSION="$(jq -er --arg c "$CONNECTOR" '.["games/" + $c]' .release-please-manifest.json)" ||
  fail "'games/$CONNECTOR' is not in .release-please-manifest.json"
if [ "$MANIFEST_VERSION" != "$VERSION" ]; then
  fail ".release-please-manifest.json says $CONNECTOR is $MANIFEST_VERSION, this run is for $VERSION"
fi

VERSION_FILE="games/$CONNECTOR/version.txt"
if [ -f "$VERSION_FILE" ]; then
  FILE_VERSION="$(tr -d '[:space:]' <"$VERSION_FILE")"
  if [ "$FILE_VERSION" != "$VERSION" ]; then
    fail "$VERSION_FILE says $FILE_VERSION, this run is for $VERSION"
  fi
fi

attempt=1
while :; do
  # Always re-fetch: a tag left over from an earlier run of the same checkout would otherwise
  # be trusted without ever being compared against origin.
  git fetch --force origin "refs/tags/$TAG:refs/tags/$TAG" >/dev/null 2>&1 || true
  if git rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null 2>&1; then
    break
  fi
  if [ "$attempt" -ge "$ATTEMPTS" ]; then
    fail "tag '$TAG' does not exist after $ATTEMPTS attempts"
  fi
  attempt=$((attempt + 1))
  sleep "$DELAY"
done

TAG_COMMIT="$(git rev-parse "$TAG^{commit}")"
HEAD_COMMIT="$(git rev-parse HEAD)"
if [ "$TAG_COMMIT" != "$HEAD_COMMIT" ]; then
  fail "tag '$TAG' is at $TAG_COMMIT but this checkout is at $HEAD_COMMIT — check out the tag"
fi

echo "release-guard: ok $TAG at $TAG_COMMIT"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "source_commit=$TAG_COMMIT" >>"$GITHUB_OUTPUT"
fi
