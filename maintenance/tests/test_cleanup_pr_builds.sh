#!/usr/bin/env bash
# cleanup-pr-builds.sh against a stubbed `gh`: what it deletes, in what order, and what it
# leaves alone.
#
# The script talks to GitHub only through `gh`, so a stub on PATH that records its argv is the
# whole observation. Each case builds its own fixture directory and log. Run it from anywhere;
# it prints PASS per case and ALL PASS at the end, and exits non-zero on the first case that
# does not behave as stated.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
CLEANUP_SOURCE="$REPO_ROOT/scripts/cleanup-pr-builds.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

CASE=0
LOG_CASE=0

# A directory holding the script under test, the two-connector release-please config it reads
# the connector list from, and a `gh` that answers from files the case writes.
fixture() {
  local name="$1"
  local root="$WORK/$name"
  mkdir -p "$root/scripts" "$root/bin"
  cp "$CLEANUP_SOURCE" "$root/scripts/cleanup-pr-builds.sh"
  chmod +x "$root/scripts/cleanup-pr-builds.sh"
  printf '{"packages": {"games/minecraft": {}, "games/rust": {}}}\n' >"$root/release-please-config.json"
  cat >"$root/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG"
case "$*" in
  "api --paginate repos/"*"/releases")
    cat "$STUB_RELEASES"
    ;;
  "api -X DELETE "*)
    if [ "${STUB_FAIL_DELETE:-0}" = "1" ]; then exit 1; fi
    ;;
  "api repos/"*"/git/ref/tags/"*)
    argv="$*"
    tag="${argv##*/tags/}"
    for known in ${STUB_TAGS:-}; do
      if [ "$known" = "$tag" ]; then exit 0; fi
    done
    exit 1
    ;;
  *)
    printf 'unexpected gh call: %s\n' "$*" >&2
    exit 99
    ;;
esac
STUB
  chmod +x "$root/bin/gh"
  : >"$root/gh.log"
  printf '%s\n' "$root"
}

# The six releases every case reasons about: two for this PR's minecraft build (the release and
# a staging draft), one for another PR, one for another connector, one whose tag merely starts
# with ours, and the rolling pre-release.
releases() {
  cat <<'JSON'
[
  {"id": 1, "tag_name": "pr-12-minecraft"},
  {"id": 2, "tag_name": "pr-12-minecraft.staging-777"},
  {"id": 3, "tag_name": "pr-123-minecraft"},
  {"id": 4, "tag_name": "pr-12-rust"},
  {"id": 5, "tag_name": "pr-12-minecraft-old"},
  {"id": 6, "tag_name": "minecraft-dev"}
]
JSON
}

# expect <exit> <name> <root> [KEY=VALUE ...]
expect() {
  local want="$1" name="$2" root="$3"
  shift 3
  local got=0
  env -u GITHUB_REPOSITORY -u GH_REPO -u STUB_RELEASES -u STUB_TAGS -u STUB_FAIL_DELETE \
    PATH="$root/bin:$PATH" STUB_LOG="$root/gh.log" "$@" \
    "$root/scripts/cleanup-pr-builds.sh" 12 >"$WORK/out.$CASE" 2>"$WORK/err.$CASE" || got=$?
  if [ "$got" != "$want" ]; then
    echo "FAIL $name: expected exit $want, got $got" >&2
    cat "$WORK/out.$CASE" "$WORK/err.$CASE" >&2
    exit 1
  fi
  CASE=$((CASE + 1))
  echo "PASS $name"
}

log_is() {
  local name="$1" root="$2" expected="$3"
  printf '%s' "$expected" >"$WORK/want.$LOG_CASE"
  if ! diff -u "$WORK/want.$LOG_CASE" "$root/gh.log" >"$WORK/logdiff.$LOG_CASE" 2>&1; then
    echo "FAIL $name: unexpected gh calls" >&2
    cat "$WORK/logdiff.$LOG_CASE" >&2
    exit 1
  fi
  LOG_CASE=$((LOG_CASE + 1))
}

# 1. Only this PR's releases and tags are touched, and the staging draft goes with them.
root="$(fixture full)"
releases >"$root/releases.json"
expect 0 "only this PR's releases and tags are removed" "$root" \
  GITHUB_REPOSITORY=o/r STUB_RELEASES="$root/releases.json" STUB_TAGS="pr-12-minecraft pr-123-minecraft"
log_is "only this PR's releases and tags are removed" "$root" \
  'api --paginate repos/o/r/releases
api -X DELETE repos/o/r/releases/1
api -X DELETE repos/o/r/releases/2
api repos/o/r/git/ref/tags/pr-12-minecraft
api -X DELETE repos/o/r/git/refs/tags/pr-12-minecraft
api -X DELETE repos/o/r/releases/4
api repos/o/r/git/ref/tags/pr-12-rust
'

# 2. A PR that never produced a build is a silent no-op.
root="$(fixture nothing)"
printf '[]\n' >"$root/releases.json"
expect 0 "a PR with no builds deletes nothing" "$root" \
  GITHUB_REPOSITORY=o/r STUB_RELEASES="$root/releases.json" STUB_TAGS=""
log_is "a PR with no builds deletes nothing" "$root" \
  'api --paginate repos/o/r/releases
api repos/o/r/git/ref/tags/pr-12-minecraft
api repos/o/r/git/ref/tags/pr-12-rust
'

# 3. A failed delete fails the job rather than orphaning the rest.
root="$(fixture failing)"
releases >"$root/releases.json"
expect 1 "a failed delete stops the run" "$root" \
  GITHUB_REPOSITORY=o/r STUB_RELEASES="$root/releases.json" STUB_TAGS="pr-12-minecraft" STUB_FAIL_DELETE=1
log_is "a failed delete stops the run" "$root" \
  'api --paginate repos/o/r/releases
api -X DELETE repos/o/r/releases/1
'

# 4. Without a repository it refuses before it can delete anything anywhere.
root="$(fixture no-repo)"
releases >"$root/releases.json"
expect 1 "no repository is refused before any call" "$root" \
  STUB_RELEASES="$root/releases.json" STUB_TAGS=""
log_is "no repository is refused before any call" "$root" ''

# 5. GH_REPO wins over the ambient repository of the workflow.
root="$(fixture gh-repo)"
printf '[]\n' >"$root/releases.json"
expect 0 "GH_REPO wins over GITHUB_REPOSITORY" "$root" \
  GITHUB_REPOSITORY=o/r GH_REPO=x/y STUB_RELEASES="$root/releases.json" STUB_TAGS=""
log_is "GH_REPO wins over GITHUB_REPOSITORY" "$root" \
  'api --paginate repos/x/y/releases
api repos/x/y/git/ref/tags/pr-12-minecraft
api repos/x/y/git/ref/tags/pr-12-rust
'
if grep -q 'o/r' "$root/gh.log"; then
  echo "FAIL GH_REPO wins over GITHUB_REPOSITORY: the ambient repository was still used" >&2
  exit 1
fi

echo "ALL PASS"
