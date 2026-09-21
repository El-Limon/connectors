#!/usr/bin/env bash
# dev-servers/lib/targets.sh ds_target: a failing tool and a game with no catalog target are
# different answers. Fail-open on the first one would skip every catalog check below and boot
# the rig on whatever happens to be in the data directory.
#
# Each case builds its own fixture holding a copy of the dev-servers library (common.sh sources
# lib/games/*.sh and lib/targets.sh, so the whole directory is copied) and a stub takaro-maint on the
# path the library hard-codes. Run it from anywhere; it prints PASS per case and ALL PASS.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

CASE=0

# A tree with the library and a stub takaro-maint whose behaviour the case chooses.
fixture() {
  local name="$1" mode="$2" root
  root="$WORK/$name"
  mkdir -p "$root/dev-servers" "$root/maintenance/bin"
  cp -r "$REPO_ROOT/dev-servers/lib" "$root/dev-servers/lib"
  case "$mode" in
    target)
      cat >"$root/maintenance/bin/takaro-maint" <<'STUB'
#!/usr/bin/env bash
printf '{"targets": [{"id": "fabric-26.2"}]}\n'
STUB
      ;;
    none)
      cat >"$root/maintenance/bin/takaro-maint" <<'STUB'
#!/usr/bin/env bash
printf '{"targets": []}\n'
STUB
      ;;
    fail)
      cat >"$root/maintenance/bin/takaro-maint" <<'STUB'
#!/usr/bin/env bash
echo "catalog is unreadable" >&2
exit 2
STUB
      ;;
  esac
  chmod +x "$root/maintenance/bin/takaro-maint"
  printf '%s\n' "$root"
}

# Source the library with REPO_ROOT pointed at the fixture and print ds_target's answer.
probe() {
  local root="$1"
  REPO_ROOT="$root" bash -c '
    REPO_ROOT="'"$root"'"
    # shellcheck disable=SC1090
    . "$REPO_ROOT/dev-servers/lib/common.sh"
    if value="$(ds_target minecraft-fabric)"; then
      printf "ok:%s\n" "$value"
    else
      printf "failed:%s\n" "$?"
    fi
  '
}

check() {
  local name="$1" want="$2" got="$3"
  CASE=$((CASE + 1))
  if [ "$got" != "$want" ]; then
    echo "FAIL $name: expected '$want', got '$got'" >&2
    exit 1
  fi
  echo "PASS $name"
}

# 1. A game the catalog drives resolves to its target id.
check "a catalog-driven game resolves its target" "ok:fabric-26.2" \
  "$(probe "$(fixture catalog target)" 2>/dev/null)"

# 2. A successful call with an empty list is the real "this game has no catalog target".
check "no target is an empty answer, not a failure" "ok:" \
  "$(probe "$(fixture empty none)" 2>/dev/null)"

# 3. A tool that fails is not "no target": ds_target reports failure so the caller can stop.
root_fail="$(fixture broken fail)"
check "a failing takaro-maint is a failure, not an empty target" "failed:1" \
  "$(probe "$root_fail" 2>/dev/null)"

# 4. ...and the tool's own stderr reaches the operator rather than /dev/null.
stderr="$(probe "$root_fail" 2>&1 >/dev/null)"
case "$stderr" in
  *"catalog is unreadable"*) echo "PASS the tool's stderr is shown" ;;
  *) echo "FAIL the tool's stderr was swallowed: $stderr" >&2; exit 1 ;;
esac

echo "ALL PASS"
