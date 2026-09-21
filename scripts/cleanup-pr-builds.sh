#!/usr/bin/env bash
# Deletes the disposable per-PR pre-releases (and their tags) for a closed PR. Idempotent:
# connectors that never produced a build for this PR are skipped silently. Usable locally
# (authenticated gh) or in CI (GH_TOKEN set).
#
#   cleanup-pr-builds.sh <pr-number>
#
# The connector list is derived from release-please-config.json, so a connector added there is
# cleaned up without anyone remembering to edit this file. Staging drafts left behind by a run
# that died mid-publication carry the same tag with a `.staging-<run-id>` suffix and are removed
# too; nothing outside this PR's tags is ever touched.
set -euo pipefail
cd "$(dirname "$0")/.."

PR="${1:?usage: cleanup-pr-builds.sh <pr-number>}"
REPO="${GH_REPO:-${GITHUB_REPOSITORY:?GITHUB_REPOSITORY or GH_REPO must be set}}"

RELEASES="$(gh api --paginate "repos/${REPO}/releases")"

while read -r connector; do
  TAG="pr-${PR}-${connector}"
  # Let a real delete failure (auth, rate limit, partial tag removal) fail the job instead of
  # silently orphaning the release.
  while read -r id; do
    [ -n "$id" ] || continue
    echo "Removing release ${id} (${TAG})..."
    gh api -X DELETE "repos/${REPO}/releases/${id}"
  done < <(printf '%s' "$RELEASES" | jq -r --arg t "$TAG" \
    '.[] | select(.tag_name == $t or (.tag_name | startswith($t + ".staging-"))) | .id')

  if gh api "repos/${REPO}/git/ref/tags/${TAG}" >/dev/null 2>&1; then
    echo "Removing tag ${TAG}..."
    gh api -X DELETE "repos/${REPO}/git/refs/tags/${TAG}"
  fi
done < <(jq -er '.packages | keys[] | ltrimstr("games/")' release-please-config.json)
