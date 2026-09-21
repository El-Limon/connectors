#!/usr/bin/env bash
# Upserts a sticky PR comment describing the disposable per-PR pre-release build for a connector.
# One comment per connector, keyed by a hidden marker so repeated pushes edit it in place
# instead of spamming new comments. Usable locally (authenticated gh) or in CI (GH_TOKEN set).
#
#   comment-pr-build.sh <connector> <pr-number> <tag> <version> <assembled-dir>
#
# The table comes from the release's own compatibility record, so the comment names the same
# artifacts, hashes and verification levels the release page does. `publish-release.sh` sources
# this file for `release_table`, which is why the work below is in functions.
set -euo pipefail

# The per-target table (or, for a connector without a catalog, a link list) for one release.
release_table() {
  local record="${1:?release_table <compat-record.json>}"
  local repo tag
  repo="$(jq -r '.source.repo' "$record")"
  tag="$(jq -r '.tag' "$record")"

  if [ "$(jq -r '.mode' "$record")" = "catalog" ] && [ "$(jq -r '.targets | length' "$record")" != "0" ]; then
    printf '| target | platform / game version | artifact | sha256 | verified |\n'
    printf '|---|---|---|---|---|\n'
    jq -r '
      .targets | to_entries | sort_by(.key)[] as $t | $t.value.artifacts[]
      | "| \($t.key) | \($t.value.platform) \($t.value.revision) | [\(.name)](\(.url)) | `\(.sha256[0:12])` | "
        + (if $t.value.verification.executed
           then "\($t.value.verification.executed) (\($t.value.verification.takaro // "local"))"
           else "not required (`\($t.value.verification.required)`)" end)
        + " |"
    ' "$record"
    jq -r '
      (.aliases // {}) as $aliases
      | .assets[] | select(.kind == "alias")
      | "| \($aliases[.name].target) | (legacy name) | [\(.name)](\(.url)) | `\(.sha256[0:12])` | copy of \($aliases[.name].of) |"
    ' "$record"
  else
    jq -r '.assets | sort_by(.name)[] | "- [`\(.name)`](\(.url)) — `\(.sha256[0:12])`"' "$record"
  fi

  local extras
  extras="[SHA256SUMS](https://github.com/${repo}/releases/download/${tag}/SHA256SUMS)"
  extras="${extras} · [compat record](https://github.com/${repo}/releases/download/${tag}/$(jq -r '.self' "$record"))"
  local reports
  reports="$(jq -r '
    [.assets[] | select(.kind == "verify-report")
     | "[\(.name | sub(".*\\.verify-"; "") | sub("\\.json$"; ""))](\(.url))"] | join(", ")
  ' "$record")"
  if [ -n "$reports" ]; then
    extras="${extras} · verify reports: ${reports}"
  fi
  printf '\nAlso: %s\n' "$extras"
}

# The single compatibility record in an assembled directory.
compat_record_in() {
  local directory="${1:?compat_record_in <assembled-dir>}"
  local found
  found="$(find "$directory" -maxdepth 1 -type f -name '*.compat.json' | LC_ALL=C sort)"
  if [ "$(printf '%s\n' "$found" | grep -c .)" != "1" ]; then
    echo "Error: expected exactly one compatibility record in ${directory}" >&2
    return 1
  fi
  printf '%s\n' "$found"
}

main() {
  local connector="${1:?usage: comment-pr-build.sh <connector> <pr-number> <tag> <version> <assembled-dir>}"
  local pr="${2:?pr-number required}"
  local tag="${3:?tag required}"
  local version="${4:?version required}"
  local assembled="${5:?assembled directory required}"

  local repo="${GH_REPO:-${GITHUB_REPOSITORY:?GITHUB_REPOSITORY or GH_REPO must be set}}"
  local marker="<!-- takaro-pr-build:${connector} -->"
  local record
  record="$(compat_record_in "$assembled")"

  local body
  body="${marker}
### 🟢 ${connector} build for this PR

**Version:** \`${version}\` · **Release:** [\`${tag}\`](https://github.com/${repo}/releases/tag/${tag})

$(release_table "$record")

Direct download — no login required. This build is replaced on every push and deleted when the PR closes. _Not for production._"

  # Find an existing sticky comment for this connector (first match wins).
  local existing
  existing=$(gh api "repos/${repo}/issues/${pr}/comments" --paginate |
    jq -r --arg m "$marker" '[.[] | select(.body | startswith($m)) | .id][0] // empty')

  if [ -n "$existing" ]; then
    jq -n --arg body "$body" '{body: $body}' |
      gh api "repos/${repo}/issues/comments/${existing}" -X PATCH --input -
  else
    jq -n --arg body "$body" '{body: $body}' |
      gh api "repos/${repo}/issues/${pr}/comments" -X POST --input -
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
