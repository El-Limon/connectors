#!/usr/bin/env bash
# dev-servers deploy steps: a successful deploy has to return, not kill the script.
#
# Each catalog-driven game used to make its scratch directory with `local tmp` plus a
# `trap 'rm -rf "$tmp"' RETURN`. Bash runs a RETURN trap again when the *caller* returns
# -- here `ds_dispatch` -- in a scope where the function's `local` is gone, so under
# `set -u` the trap expanded an unbound `$tmp` and the script died with status 1 after the
# deploy had already succeeded. `ds_scratch_dir` registers the directory for the script's
# own EXIT instead.
#
# Every case runs `ds_dispatch deploy <game>` followed by `echo after` in a
# `bash -euo pipefail` child with the tool and reporting helpers stubbed, and asserts that
# the child exits 0, that `after` was printed, and that the scratch TMPDIR is empty
# afterwards. Run it from anywhere; it prints PASS per case and ALL PASS.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

GAMES=(7d2d zomboid conan-exiles valheim terraria rust minecraft-fabric minecraft-fabric-26.1.2)

fail() { echo "FAIL $1" >&2; exit 1; }

# A tree holding a copy of the dev-servers library and a stub `scripts/dev-version.sh`;
# everything else the deploy steps reach for is stubbed in the child itself.
root="$WORK/tree"
mkdir -p "$root/dev-servers" "$root/scripts"
cp -r "$REPO_ROOT/dev-servers/lib" "$root/dev-servers/lib"
cat >"$root/scripts/dev-version.sh" <<'STUB'
#!/usr/bin/env bash
printf '0.0.0-test\n'
STUB
chmod +x "$root/scripts/dev-version.sh"

for game in "${GAMES[@]}"; do
  tmpdir="$WORK/tmp-${game//[.\/]/_}"
  mkdir -p "$tmpdir"
  out="$WORK/out-${game//[.\/]/_}"

  set +e
  TMPDIR="$tmpdir" DS_TEST_GAME="$game" DS_TEST_ROOT="$root" \
    bash -euo pipefail -c '
      REPO_ROOT="$DS_TEST_ROOT"
      # shellcheck disable=SC1090
      . "$REPO_ROOT/dev-servers/lib/common.sh"
      # The rig library is under test; everything it calls out to is not.
      ds_maint() { :; }
      ds_target() { printf "a-target\n"; }
      ds_target_dest() { printf "%s/dest\n" "$TMPDIR"; }
      ds_data_dir() { printf "%s/data/%s\n" "$TMPDIR" "$1"; }
      ds_have() { return 1; }
      ds_ok() { :; }
      ds_info() { :; }
      ds_dispatch deploy "$DS_TEST_GAME"
      echo after
    ' >"$out" 2>&1
  status=$?
  set -e

  [ "$status" -eq 0 ] || fail "$game: deploy exited $status, not 0 -- $(cat "$out")"
  grep -qx after "$out" || fail "$game: the script did not reach the statement after the deploy"
  remaining="$(find "$tmpdir" -mindepth 1 -maxdepth 1 -name 'tmp.*' -print -quit)"
  [ -z "$remaining" ] || fail "$game: the scratch directory ${remaining} was left behind"
  echo "PASS $game deploys and returns, leaving no scratch directory"
done

echo "ALL PASS"
