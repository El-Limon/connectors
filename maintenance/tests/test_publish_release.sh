#!/usr/bin/env bash
# publish-release.sh publish: which takaro-maint calls it makes, in which order, and when it
# makes none at all.
#
# Each case builds its own fixture directory holding a copy of the script and a stub
# takaro-maint that records its arguments, so nothing one case does can reach another. Run it
# from anywhere; it prints PASS per case and ALL PASS at the end, and exits non-zero on the
# first case that does not behave as stated.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid
export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null

CASE=0

# A tree that looks enough like the repository for the script: the script itself, the helper
# it sources for the job summary, and a stub takaro-maint on the path it hard-codes.
fixture() {
  local name="$1"
  local root="$WORK/$name"
  mkdir -p "$root/scripts" "$root/maintenance/bin"
  cp "$REPO_ROOT/scripts/publish-release.sh" "$root/scripts/publish-release.sh"
  cp "$REPO_ROOT/scripts/comment-pr-build.sh" "$root/scripts/comment-pr-build.sh"
  chmod +x "$root/scripts/publish-release.sh"
  cat >"$root/maintenance/bin/takaro-maint" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_LOG"
if [ -n "${STUB_FAIL_ON:-}" ] && [ "$1 $2" = "$STUB_FAIL_ON" ]; then exit "${STUB_FAIL_CODE:-7}"; fi
exit 0
STUB
  chmod +x "$root/maintenance/bin/takaro-maint"
  git init -q -b main "$root"
  git -C "$root" add -A
  git -C "$root" commit -qm fixture
  printf '%s\n' "$root"
}

# Run one case. STUB_LOG, stdout and stderr land in $WORK/{log,out,err}.$CASE.
run_publish() {
  local want="$1" name="$2" root="$3"
  shift 3
  STUB_LOG="$WORK/log.$CASE"
  : >"$STUB_LOG"
  local got=0
  env -u GITHUB_STEP_SUMMARY -u GITHUB_RUN_ID -u GITHUB_RUN_ATTEMPT \
    STUB_LOG="$STUB_LOG" "${CASE_ENV[@]}" \
    "$root/scripts/publish-release.sh" publish "$@" \
    >"$WORK/out.$CASE" 2>"$WORK/err.$CASE" || got=$?
  if [ "$got" != "$want" ]; then
    echo "FAIL $name: expected exit $want, got $got" >&2
    cat "$WORK/out.$CASE" "$WORK/err.$CASE" >&2
    exit 1
  fi
}

pass() {
  echo "PASS $1"
  CASE=$((CASE + 1))
}

fail() {
  echo "FAIL $1: $2" >&2
  cat "$WORK/out.$CASE" "$WORK/err.$CASE" "$WORK/log.$CASE" 2>/dev/null >&2 || true
  exit 1
}

contains() {
  local name="$1" haystack="$2" needle="$3"
  case "$haystack" in
    *"$needle"*) ;;
    *) fail "$name" "expected to find '$needle' in: $haystack" ;;
  esac
}

lacks() {
  local name="$1" haystack="$2" needle="$3"
  case "$haystack" in
    *"$needle"*) fail "$name" "did not expect '$needle' in: $haystack" ;;
  esac
}

CASE_ENV=()

# 1. --channel none publishes nothing, whatever else it is given.
CASE_ENV=()
root="$(fixture none)"
run_publish 0 "channel none is a no-op" "$root" \
  --connector minecraft --channel none --version 1.2.3 --tag minecraft-v1.2.3 --dist d --out o
contains "channel none is a no-op" "$(cat "$WORK/out.$CASE")" "nothing to publish for minecraft"
[ ! -s "$WORK/log.$CASE" ] || fail "channel none is a no-op" "takaro-maint was called"
pass "channel none is a no-op"

# 2. An empty tag is the documented no-op: the release workflow passes --tag "${{ inputs.tag }}"
#    verbatim, so an unset input must reach here and publish nothing rather than abort.
CASE_ENV=()
root="$(fixture emptytag)"
run_publish 0 "an empty tag is a no-op" "$root" \
  --connector minecraft --channel rolling --version 1.2.3 --tag "" --dist d --out o
contains "an empty tag is a no-op" "$(cat "$WORK/out.$CASE")" "nothing to publish for minecraft"
[ ! -s "$WORK/log.$CASE" ] || fail "an empty tag is a no-op" "takaro-maint was called"
pass "an empty tag is a no-op"

# 3. The stable happy path: assemble, publish, verify, in that order and nothing else.
CASE_ENV=()
root="$(fixture stable)"
run_publish 0 "stable assembles, publishes and verifies" "$root" \
  --connector minecraft --channel stable --version 1.2.3 --tag minecraft-v1.2.3 \
  --dist d --out o --reports r
mapfile -t lines <"$WORK/log.$CASE"
[ "${#lines[@]}" = 3 ] || fail "stable assembles, publishes and verifies" "expected 3 calls, got ${#lines[@]}"
contains "stable assembles, publishes and verifies" "${lines[0]}" "release assemble "
contains "stable assembles, publishes and verifies" "${lines[0]}" "--reports r"
lacks "stable assembles, publishes and verifies" "${lines[0]}" "--allow-dirty"
contains "stable assembles, publishes and verifies" "${lines[1]}" "release publish "
contains "stable assembles, publishes and verifies" "${lines[1]}" "--target-commit $(git -C "$root" rev-parse HEAD)"
contains "stable assembles, publishes and verifies" "${lines[1]}" "--run-id local-"
contains "stable assembles, publishes and verifies" "${lines[2]}" "release verify "
contains "stable assembles, publishes and verifies" "${lines[2]}" "--expect o --out o.verify"
pass "stable assembles, publishes and verifies"

# 4. A rolling build in CI: dirty trees are allowed and the run id comes from the workflow run.
CASE_ENV=(GITHUB_RUN_ID=42 GITHUB_RUN_ATTEMPT=2)
root="$(fixture rolling)"
run_publish 0 "a rolling build is stamped with the workflow run" "$root" \
  --connector minecraft --channel rolling --version 1.2.3-dev --tag minecraft-dev --dist d --out o
mapfile -t lines <"$WORK/log.$CASE"
contains "a rolling build is stamped with the workflow run" "${lines[0]}" "--allow-dirty"
contains "a rolling build is stamped with the workflow run" "${lines[1]}" "--run-id gha-42-2"
pass "a rolling build is stamped with the workflow run"

# 5. A PR build: the explicit file list and the mode reach assemble, the PR number reaches publish.
CASE_ENV=()
root="$(fixture pr)"
run_publish 0 "a pr build passes its files and its number through" "$root" \
  --connector minecraft --channel pr --version 1.2.3-pr7 --tag minecraft-pr7 \
  --dist d --out o --pr-number 7 --mode legacy -- a.zip b.zip
mapfile -t lines <"$WORK/log.$CASE"
contains "a pr build passes its files and its number through" "${lines[0]}" "--mode legacy --allow-dirty a.zip b.zip"
contains "a pr build passes its files and its number through" "${lines[1]}" "--pr-number 7"
lacks "a pr build passes its files and its number through" "${lines[2]}" "--pr-number"
pass "a pr build passes its files and its number through"

# 6. A failed assemble stops the run before anything is published.
CASE_ENV=(STUB_FAIL_ON="release assemble" STUB_FAIL_CODE=7)
root="$(fixture assemblefail)"
run_publish 7 "a failed assemble publishes nothing" "$root" \
  --connector minecraft --channel stable --version 1.2.3 --tag minecraft-v1.2.3 --dist d --out o
[ "$(wc -l <"$WORK/log.$CASE")" = 1 ] || fail "a failed assemble publishes nothing" "more than assemble ran"
pass "a failed assemble publishes nothing"

# 7. A failed publish is not followed by a verify that would read the previous release back.
CASE_ENV=(STUB_FAIL_ON="release publish" STUB_FAIL_CODE=9)
root="$(fixture publishfail)"
run_publish 9 "a failed publish is never verified" "$root" \
  --connector minecraft --channel stable --version 1.2.3 --tag minecraft-v1.2.3 --dist d --out o
[ "$(wc -l <"$WORK/log.$CASE")" = 2 ] || fail "a failed publish is never verified" "verify ran anyway"
pass "a failed publish is never verified"

# 8. An unknown option is a usage error, not a silently ignored flag.
CASE_ENV=()
root="$(fixture bogus)"
run_publish 2 "an unknown option is refused" "$root" --connector minecraft --bogus x
contains "an unknown option is refused" "$(cat "$WORK/err.$CASE")" "unknown option"
pass "an unknown option is refused"

# 9. A missing --connector never reaches takaro-maint.
CASE_ENV=()
root="$(fixture noconnector)"
set +e
run_publish 1 "a missing connector is refused" "$root" --channel stable --tag minecraft-v1.2.3
set -e
[ ! -s "$WORK/log.$CASE" ] || fail "a missing connector is refused" "takaro-maint was called"
pass "a missing connector is refused"

# 10. With a step summary the wrapper appends the release table for the assembled record.
CASE_ENV=(GITHUB_STEP_SUMMARY="$WORK/summary")
root="$(fixture stepsummary)"
mkdir -p "$root/o"
cat >"$root/o/minecraft.compat.json" <<'JSON'
{"mode": "legacy", "tag": "minecraft-v1.2.3", "self": "minecraft.compat.json",
 "source": {"repo": "o/r"}, "assets": []}
JSON
: >"$WORK/summary"
run_publish 0 "a step summary gets the release table" "$root" \
  --connector minecraft --channel stable --version 1.2.3 --tag minecraft-v1.2.3 --dist d --out o
contains "a step summary gets the release table" "$(cat "$WORK/summary")" "### minecraft 1.2.3"
pass "a step summary gets the release table"

echo "ALL PASS"
