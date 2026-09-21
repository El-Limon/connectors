#!/usr/bin/env bash
# scripts/lib/package.sh: two honest builds of the same content produce the same archive bytes.
#
# The two staging trees below differ in every way a build environment normally differs — a
# different absolute path, files created in the opposite order, different mtimes, different
# permissions — and nothing else. If any of that reaches the archive, the `cmp` fails.
#
# Needs zip, tar, gzip, git and sha256sum. Prints PASS per case and ALL PASS at the end.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
LIBRARY="$REPO_ROOT/scripts/lib/package.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid
export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null

pass() { echo "PASS $1"; }

fail() {
  echo "FAIL $1" >&2
  exit 1
}

# Two stages holding identical content, built as differently as a build environment can manage.
stage_forwards() {
  local stage="$1" marker="${2:-alpha}"
  mkdir -p "$stage/TakaroPlugin/lib" "$stage/TakaroPlugin/docs"
  printf 'plugin %s\n' "$marker" >"$stage/TakaroPlugin/plugin.cfg"
  printf 'library bytes\n' >"$stage/TakaroPlugin/lib/takaro.so"
  printf 'readme\n' >"$stage/TakaroPlugin/docs/README.md"
  chmod 600 "$stage/TakaroPlugin/plugin.cfg"
  touch -d @1000000000 "$stage/TakaroPlugin/plugin.cfg"
}

stage_backwards() {
  local stage="$1" marker="${2:-alpha}"
  mkdir -p "$stage/TakaroPlugin/docs" "$stage/TakaroPlugin/lib"
  printf 'readme\n' >"$stage/TakaroPlugin/docs/README.md"
  printf 'library bytes\n' >"$stage/TakaroPlugin/lib/takaro.so"
  printf 'plugin %s\n' "$marker" >"$stage/TakaroPlugin/plugin.cfg"
  chmod 777 "$stage/TakaroPlugin/lib/takaro.so"
  touch -d @1700000000 "$stage/TakaroPlugin/docs/README.md"
  touch -d @1600000000 "$stage/TakaroPlugin/lib/takaro.so"
}

# -- 1 & 2: a fixed SOURCE_DATE_EPOCH ----------------------------------------------------

# shellcheck source=/dev/null
. "$LIBRARY"
export SOURCE_DATE_EPOCH=1600000000

mkdir -p "$WORK/build-one" "$WORK/a-much-longer-second-build-path"
stage_forwards "$WORK/build-one"
stage_backwards "$WORK/a-much-longer-second-build-path"

pkg_zip "$WORK/build-one" TakaroPlugin "$WORK/out/one.zip"
pkg_tar_gz "$WORK/build-one" TakaroPlugin "$WORK/out/one.tar.gz"
(cd "$WORK/a-much-longer-second-build-path" && pkg_zip . TakaroPlugin "$WORK/out/two.zip")
(cd / && pkg_tar_gz "$WORK/a-much-longer-second-build-path" TakaroPlugin "$WORK/out/two.tar.gz")

cmp -s "$WORK/out/one.zip" "$WORK/out/two.zip" || fail "two clean builds produced different zip bytes"
pass "zip bytes survive a different path, order, mtime and mode"

cmp -s "$WORK/out/one.tar.gz" "$WORK/out/two.tar.gz" ||
  fail "two clean builds produced different tar.gz bytes"
pass "tar.gz bytes survive a different path, order, mtime and mode"

# -- 3: content still decides ------------------------------------------------------------

mkdir -p "$WORK/build-three"
stage_forwards "$WORK/build-three" beta
pkg_zip "$WORK/build-three" TakaroPlugin "$WORK/out/three.zip"
pkg_tar_gz "$WORK/build-three" TakaroPlugin "$WORK/out/three.tar.gz"

if cmp -s "$WORK/out/one.zip" "$WORK/out/three.zip"; then
  fail "a one-byte content change did not change the zip"
fi
if cmp -s "$WORK/out/one.tar.gz" "$WORK/out/three.tar.gz"; then
  fail "a one-byte content change did not change the tar.gz"
fi
pass "a one-byte content change changes both archives"

# -- 4: the epoch derived from a repository ----------------------------------------------

unset SOURCE_DATE_EPOCH
GIT_HOME="$WORK/from-git"
mkdir -p "$GIT_HOME/scripts/lib"
cp "$LIBRARY" "$GIT_HOME/scripts/lib/package.sh"
git init -q -b main "$GIT_HOME"
git -C "$GIT_HOME" add -A
GIT_AUTHOR_DATE="@1500000000 +0000" GIT_COMMITTER_DATE="@1500000000 +0000" \
  git -C "$GIT_HOME" commit -qm "the library, at a known commit time"

# Sourcing the copy is what makes pkg_epoch look at that repository rather than this one.
(
  # shellcheck source=/dev/null
  . "$GIT_HOME/scripts/lib/package.sh"
  [ "$(pkg_epoch)" = "1500000000" ] || {
    echo "FAIL pkg_epoch took $(pkg_epoch) instead of the repository's commit time" >&2
    exit 1
  }
  mkdir -p "$WORK/build-four" "$WORK/build-five"
  stage_forwards "$WORK/build-four"
  stage_backwards "$WORK/build-five"
  pkg_zip "$WORK/build-four" TakaroPlugin "$WORK/out/four.zip"
  pkg_zip "$WORK/build-five" TakaroPlugin "$WORK/out/five.zip"
  cmp -s "$WORK/out/four.zip" "$WORK/out/five.zip" || {
    echo "FAIL the repository-derived epoch did not produce identical bytes" >&2
    exit 1
  }
)
pass "the epoch falls back to the repository's commit time"

# -- 5: SHA256SUMS ------------------------------------------------------------------------

mkdir -p "$WORK/sums-one" "$WORK/sums-two"
printf 'b\n' >"$WORK/sums-one/beta.bin"
printf 'a\n' >"$WORK/sums-one/alpha.bin"
printf 'a\n' >"$WORK/sums-two/alpha.bin"
printf 'b\n' >"$WORK/sums-two/beta.bin"
pkg_sha256sums "$WORK/sums-one"
pkg_sha256sums "$WORK/sums-two"

cmp -s "$WORK/sums-one/SHA256SUMS" "$WORK/sums-two/SHA256SUMS" ||
  fail "SHA256SUMS depends on the order the files were created"
if grep -q "SHA256SUMS" "$WORK/sums-one/SHA256SUMS"; then fail "SHA256SUMS lists itself"; fi
[ "$(wc -l <"$WORK/sums-one/SHA256SUMS")" = "2" ] || fail "SHA256SUMS does not list both files"
pass "SHA256SUMS is sorted, complete and excludes itself"

echo "ALL PASS"
