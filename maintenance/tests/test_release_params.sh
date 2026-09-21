#!/usr/bin/env bash
# release-params.sh against real git repositories: one fixture per trigger it has to tell apart.
#
# Each case builds its own repository with the commit subject it needs, runs the script with a
# clean environment plus exactly the variables the trigger sets, and compares the three
# key=value lines with the expected ones. Run it from anywhere; it prints PASS per case and
# ALL PASS at the end, and exits non-zero on the first case that does not behave as stated.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PARAMS_SOURCE="$REPO_ROOT/scripts/release-params.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid
export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null

CASE=0
DEV_VERSION="1.2.3-dev.abc1234"

# A repository holding the script under test, a dev-version.sh that always prints the same
# string, and one commit whose subject the case chooses.
fixture() {
  local name="$1" subject="$2"
  local root="$WORK/$name"
  mkdir -p "$root/scripts"
  cp "$PARAMS_SOURCE" "$root/scripts/release-params.sh"
  chmod +x "$root/scripts/release-params.sh"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "%s"\n' "$DEV_VERSION" >"$root/scripts/dev-version.sh"
  chmod +x "$root/scripts/dev-version.sh"
  git init -q -b main "$root"
  git -C "$root" commit -q --allow-empty -m "$subject"
  printf '%s\n' "$root"
}

# expect <exit> <name> <root> <expected-stdout-or-empty> [KEY=VALUE ...]
expect() {
  local want="$1" name="$2" root="$3" expected="$4"
  shift 4
  local got=0
  env -u IN_TAG -u IN_VERSION -u EVENT -u PR_NUMBER "$@" \
    "$root/scripts/release-params.sh" minecraft >"$WORK/out.$CASE" 2>"$WORK/err.$CASE" || got=$?
  if [ "$got" != "$want" ]; then
    echo "FAIL $name: expected exit $want, got $got" >&2
    cat "$WORK/out.$CASE" "$WORK/err.$CASE" >&2
    exit 1
  fi
  if [ -n "$expected" ]; then
    printf '%s\n' "$expected" >"$WORK/want.$CASE"
    if ! diff -u "$WORK/want.$CASE" "$WORK/out.$CASE" >"$WORK/diff.$CASE" 2>&1; then
      echo "FAIL $name: unexpected output" >&2
      cat "$WORK/diff.$CASE" >&2
      exit 1
    fi
  fi
  CASE=$((CASE + 1))
  echo "PASS $name"
}

# 1. release-please's own call: the tag and version it decided win.
root="$(fixture stable "feat: something")"
expect 0 "a provided tag publishes a stable release" "$root" \
  "version=1.2.3
tag=minecraft-v1.2.3
publish=stable" \
  IN_TAG=minecraft-v1.2.3 IN_VERSION=1.2.3

# 2. A tag without the version it belongs to is a caller bug, not a rolling build.
root="$(fixture no-version "feat: something")"
expect 1 "a tag without a version is refused" "$root" "" IN_TAG=minecraft-v1.2.3

# 3. A pull request builds the disposable per-PR pre-release.
root="$(fixture pr "feat: something")"
expect 0 "a pull request publishes the per-PR pre-release" "$root" \
  "version=$DEV_VERSION
tag=pr-7-minecraft
publish=pr" \
  EVENT=pull_request PR_NUMBER=7

# 4. Without a PR number there is no tag to publish to.
root="$(fixture pr-no-number "feat: something")"
expect 1 "a pull request without a number is refused" "$root" "" EVENT=pull_request

# 5. An ordinary push to main updates the rolling pre-release.
root="$(fixture rolling "feat: something")"
expect 0 "an ordinary push rolls the dev pre-release" "$root" \
  "version=$DEV_VERSION
tag=minecraft-dev
publish=rolling" \
  EVENT=push

# 6. The merge of a Release PR publishes nothing: release-please calls back for that one.
root="$(fixture release-merge "chore(main): release minecraft 1.2.3")"
expect 0 "the release PR merge publishes nothing" "$root" \
  "version=$DEV_VERSION
tag=
publish=none" \
  EVENT=push

# 7. The regex is anchored on the release-please subject; an ordinary chore is a normal push.
root="$(fixture chore-push "chore: release-notes tidy")"
expect 0 "an ordinary chore commit still rolls" "$root" \
  "version=$DEV_VERSION
tag=minecraft-dev
publish=rolling" \
  EVENT=push

# 8. release-please's workflow_call arrives on the same commit as the merge push; the tag wins.
root="$(fixture merge-with-tag "chore(main): release minecraft 1.2.3")"
expect 0 "a provided tag beats the release-merge push" "$root" \
  "version=1.2.3
tag=minecraft-v1.2.3
publish=stable" \
  EVENT=push IN_TAG=minecraft-v1.2.3 IN_VERSION=1.2.3

# 9. A local invocation with no CI context publishes nothing.
root="$(fixture local "feat: something")"
expect 0 "a local invocation publishes nothing" "$root" \
  "version=$DEV_VERSION
tag=
publish=none"

echo "ALL PASS"
