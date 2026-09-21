#!/usr/bin/env bash
# The one way a connector's artifacts reach a GitHub release.
#
#   publish-release.sh publish --connector C --channel stable|rolling|pr|none --version V \
#       --tag TAG --dist DIR --out DIR [--reports DIR] [--pr-number N] [--mode catalog|legacy] \
#       [-- FILE...]
#
#       Assemble the complete set, publish it, and read it back from GitHub to prove it
#       landed. A connector with a catalog takes its files from the per-target build manifests
#       under --dist; one without (`--mode legacy`) publishes exactly the files it is given.
#       Nothing is ever clobbered: an identical retry is a no-op, conflicting bytes stop the
#       run, and rolling/PR builds are swapped in through a staging draft.
#
#   publish-release.sh upload  <tag> <file...>
#   publish-release.sh rolling <tag> <title> <notes> <file...>
#
#       The previous interface, kept working byte for byte for the connectors that have not
#       moved to `publish` yet. Both use `gh release` and both clobber; that is why they are
#       being retired one connector at a time.
#
# Usable locally (with an authenticated gh) or in CI (with GH_TOKEN set).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

MODE="${1:?usage: publish-release.sh <publish|upload|rolling> ...}"
shift

publish() {
  local connector="" channel="" version="" tag="" dist="" out="" reports="" pr_number="" release_mode=""
  local files=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --connector) connector="$2"; shift 2 ;;
      --channel) channel="$2"; shift 2 ;;
      --version) version="$2"; shift 2 ;;
      --tag) tag="$2"; shift 2 ;;
      --dist) dist="$2"; shift 2 ;;
      --out) out="$2"; shift 2 ;;
      --reports) reports="$2"; shift 2 ;;
      --pr-number) pr_number="$2"; shift 2 ;;
      --mode) release_mode="$2"; shift 2 ;;
      --) shift; files=("$@"); break ;;
      *) echo "Error: unknown option '$1'" >&2; exit 2 ;;
    esac
  done

  : "${connector:?--connector is required}"
  : "${channel:?--channel is required}"

  if [ "$channel" = "none" ] || [ -z "$tag" ]; then
    echo "publish-release: nothing to publish for ${connector}"
    return 0
  fi

  : "${version:?--version is required}"
  : "${dist:?--dist is required}"
  : "${out:?--out is required}"

  cd "$SCRIPT_DIR/.."
  local maint="maintenance/bin/takaro-maint"
  local run_id
  if [ -n "${GITHUB_RUN_ID:-}" ]; then
    run_id="gha-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
  else
    run_id="local-$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  local head
  head="$(git rev-parse HEAD)"

  local assemble=("$maint" release assemble --connector "$connector" --version "$version"
    --channel "$channel" --tag "$tag" --dist "$dist" --out "$out")
  if [ -n "$reports" ]; then assemble+=(--reports "$reports"); fi
  if [ -n "$release_mode" ]; then assemble+=(--mode "$release_mode"); fi
  # A stable release must be attributable to a clean commit and nothing else. A dev or PR
  # build is already stamped with a dev version, so a build script that leaves a stray file
  # behind should not stop it from publishing — the record still says the tree was dirty.
  if [ "$channel" != "stable" ]; then assemble+=(--allow-dirty); fi
  if [ "${#files[@]}" -gt 0 ]; then assemble+=("${files[@]}"); fi
  "${assemble[@]}"

  local publish_args=("$maint" release publish --connector "$connector" --channel "$channel"
    --tag "$tag" --assembled "$out" --run-id "$run_id" --target-commit "$head")
  if [ -n "$pr_number" ]; then publish_args+=(--pr-number "$pr_number"); fi
  "${publish_args[@]}"

  # Read the release back from GitHub one more time, from the outside, and keep the document.
  "$maint" release verify --tag "$tag" --connector "$connector" --expect "$out" --out "${out}.verify"

  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    # shellcheck source=scripts/comment-pr-build.sh
    . "$SCRIPT_DIR/comment-pr-build.sh"
    {
      echo "### ${connector} ${version} → \`${tag}\` (${channel})"
      echo
      release_table "$(compat_record_in "$out")"
    } >>"$GITHUB_STEP_SUMMARY"
  fi
}

case "$MODE" in
  publish)
    publish "$@"
    ;;
  upload)
    TAG="${1:?tag required}"; shift
    gh release upload "$TAG" "$@" --clobber
    ;;
  rolling)
    TAG="${1:?tag required}"; TITLE="${2:?title required}"; NOTES="${3:?notes required}"; shift 3
    gh release delete "$TAG" --cleanup-tag --yes 2>/dev/null || true
    gh release create "$TAG" "$@" \
      --prerelease \
      --title "$TITLE" \
      --notes "$NOTES" \
      --target "${GITHUB_SHA:-$(git rev-parse HEAD)}"
    ;;
  *)
    echo "Error: unknown mode '$MODE' (expected 'publish', 'upload' or 'rolling')" >&2
    exit 1
    ;;
esac
