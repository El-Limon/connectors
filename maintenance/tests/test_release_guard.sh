#!/usr/bin/env bash
# release-guard.sh against real git repositories: a bare origin and a clone of it.
#
# Each case builds its own pair, so nothing one case does can reach another. Run it from
# anywhere; it prints PASS per case and ALL PASS at the end, and exits non-zero on the first
# case that does not behave as stated.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD_SOURCE="$REPO_ROOT/scripts/release-guard.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid
export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export RELEASE_GUARD_ATTEMPTS=3 RELEASE_GUARD_DELAY=1

CASE=0

# A checkout that agrees with itself: manifest 1.2.3, version.txt 1.2.3, one commit pushed.
fixture() {
  local name="$1" version="${2:-1.2.3}"
  local origin="$WORK/$name.git" clone="$WORK/$name"
  git init -q --bare "$origin"
  git init -q -b main "$clone"
  mkdir -p "$clone/scripts/lib" "$clone/games/minecraft"
  cp "$GUARD_SOURCE" "$clone/scripts/release-guard.sh"
  chmod +x "$clone/scripts/release-guard.sh"
  printf '{"games/minecraft": "%s"}\n' "$version" >"$clone/.release-please-manifest.json"
  printf '%s\n' "$version" >"$clone/games/minecraft/version.txt"
  git -C "$clone" add -A
  git -C "$clone" commit -qm "fixture"
  git -C "$clone" remote add origin "$origin"
  git -C "$clone" push -q origin main
  printf '%s\n' "$clone"
}

tag_origin() {
  local clone="$1" tag="$2" commit="${3:-HEAD}"
  git -C "$clone" push -q origin "$commit:refs/tags/$tag"
}

expect() {
  local want="$1" name="$2" clone="$3"
  shift 3
  local got=0
  "$clone/scripts/release-guard.sh" "$@" >"$WORK/out.$CASE" 2>"$WORK/err.$CASE" || got=$?
  if [ "$got" != "$want" ]; then
    echo "FAIL $name: expected exit $want, got $got" >&2
    cat "$WORK/out.$CASE" "$WORK/err.$CASE" >&2
    exit 1
  fi
  CASE=$((CASE + 1))
  echo "PASS $name"
}

# 1. Everything agrees.
clone="$(fixture agree)"
tag_origin "$clone" minecraft-v1.2.3
expect 0 "a tag, a manifest and a checkout that agree" "$clone" minecraft 1.2.3 minecraft-v1.2.3
grep -q "release-guard: ok minecraft-v1.2.3" "$WORK/out.0"

# 2. An empty tag means nothing is published.
clone="$(fixture empty)"
expect 0 "an empty tag stands down" "$clone" minecraft 1.2.3 ""
grep -q "nothing to guard" "$WORK/out.1"

# 3. The tag does not match the version it claims to release.
clone="$(fixture shape)"
tag_origin "$clone" minecraft-v1.2.3
expect 1 "a tag that is not <connector>-v<version>" "$clone" minecraft 1.2.3 minecraft-1.2.3

# 4. The manifest moved on.
clone="$(fixture manifest)"
printf '{"games/minecraft": "9.9.9"}\n' >"$clone/.release-please-manifest.json"
git -C "$clone" commit -qam "bump manifest only"
git -C "$clone" push -q origin main
tag_origin "$clone" minecraft-v1.2.3
expect 1 "a manifest that disagrees with the version" "$clone" minecraft 1.2.3 minecraft-v1.2.3

# 5. version.txt moved on.
clone="$(fixture versionfile)"
printf '9.9.9\n' >"$clone/games/minecraft/version.txt"
git -C "$clone" commit -qam "bump version.txt only"
git -C "$clone" push -q origin main
tag_origin "$clone" minecraft-v1.2.3
expect 1 "a version.txt that disagrees with the version" "$clone" minecraft 1.2.3 minecraft-v1.2.3

# 6. The tag never appears.
clone="$(fixture absent)"
expect 1 "a tag that never appears" "$clone" minecraft 1.2.3 minecraft-v1.2.3
grep -q "after 3 attempts" "$WORK/err.5"

# 7. The tag is at another commit.
clone="$(fixture elsewhere)"
tag_origin "$clone" minecraft-v1.2.3
printf 'later\n' >"$clone/games/minecraft/later.txt"
git -C "$clone" add -A
git -C "$clone" commit -qm "move HEAD past the tag"
expect 1 "a tag that points somewhere else" "$clone" minecraft 1.2.3 minecraft-v1.2.3
grep -q "check out the tag" "$WORK/err.6"

# 8. The tag turns up while the guard is retrying — release-please's own race.
clone="$(fixture late)"
(
  sleep 1.5
  git -C "$clone" push -q origin HEAD:refs/tags/minecraft-v1.2.3
) &
late_pid=$!
expect 0 "a tag that arrives during the retries" "$clone" minecraft 1.2.3 minecraft-v1.2.3
wait "$late_pid"

# 9. The commit the guard proved is handed to the workflow.
clone="$(fixture output)"
tag_origin "$clone" minecraft-v1.2.3
export GITHUB_OUTPUT="$WORK/gha-output"
expect 0 "the proven commit is written to GITHUB_OUTPUT" "$clone" minecraft 1.2.3 minecraft-v1.2.3
grep -q "^source_commit=$(git -C "$clone" rev-parse HEAD)$" "$WORK/gha-output"

echo "ALL PASS"
