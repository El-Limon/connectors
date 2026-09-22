#!/usr/bin/env bash
# scripts/lib/target.sh: the one resolution every game's scripts use.
#
# Each case asserts what target resolution exports, what it refuses, and whether a
# temporary environment file survives a failure. The fixture holds the real library and
# a stub `takaro-maint` whose behaviour the case chooses.
# The expressions below are single-quoted on purpose: they are run inside the fixture's
# own shell, after its copy of the library is sourced, not expanded by this one.
# shellcheck disable=SC2016
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FAILED=0

check() {
  local what="$1" want="$2" got="$3"
  if [ "$want" = "$got" ]; then
    echo "PASS $what"
  else
    echo "FAIL $what: wanted [$want], got [$got]" >&2
    FAILED=1
  fi
}

# A tree with the real library and a stub takaro-maint on the path the library derives.
fixture() {
  local name="$1" mode="$2" root
  root="$WORK/$name"
  mkdir -p "$root/scripts/lib" "$root/maintenance/bin" "$root/games/stub/scripts"
  cp "$REPO_ROOT/scripts/lib/target.sh" "$root/scripts/lib/target.sh"
  cp "$REPO_ROOT/games/rust/scripts/lib-target.sh" "$root/games/stub/scripts/lib-target.sh"
  case "$mode" in
    ok)
      cat >"$root/maintenance/bin/takaro-maint" <<'STUB'
#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in --out) shift; out="$1" ;; esac
  shift
done
printf 'STUB_TARGET=linux-1.0\n' >"$out"
printf 'STUB_FINGERPRINT=abc\n' >>"$out"
printf 'STUB_URL=https://example.invalid/a?b=c&d=e\n' >>"$out"
printf 'OTHER_KEY=not-for-this-prefix\n' >>"$out"
printf 'wrote 4 keys\n'
STUB
      ;;
    fail)
      cat >"$root/maintenance/bin/takaro-maint" <<'STUB'
#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in --out) shift; out="$1" ;; esac
  shift
done
printf 'STUB_TARGET=half-written\n' >"$out"
echo "the catalog is unreadable" >&2
exit 2
STUB
      ;;
    never)
      cat >"$root/maintenance/bin/takaro-maint" <<'STUB'
#!/usr/bin/env bash
echo "the stub was called" >&2
exit 9
STUB
      ;;
  esac
  chmod +x "$root/maintenance/bin/takaro-maint"
  printf '%s\n' "$root"
}

# Run one expression against a fixture's library, in its own shell.
run() {
  local root="$1" expression="$2"
  shift 2
  env "$@" bash -c ". '$root/scripts/lib/target.sh'; $expression"
}

# 1. What it exports: every key of the prefix, and nothing outside it.
root_ok="$(fixture ok ok)"
check "the prefixed keys are exported" "linux-1.0 abc" \
  "$(run "$root_ok" 'takaro_resolve_target stub STUB >/dev/null; printf "%s %s" "$STUB_TARGET" "$STUB_FINGERPRINT"')"
check "a key outside the prefix is not" "" \
  "$(run "$root_ok" 'takaro_resolve_target stub STUB >/dev/null; printf "%s" "${OTHER_KEY:-}"')"
# A value may hold `=` of its own -- a URL query, a dependency coordinate.
check "a value containing = survives whole" "https://example.invalid/a?b=c&d=e" \
  "$(run "$root_ok" 'takaro_resolve_target stub STUB >/dev/null; printf "%s" "$STUB_URL"')"

# 2. The temporary env file is removed whether the tool succeeded or not. It carries the
#    whole resolved target, and TMPDIR is shared.
check "the env file is removed on success" "0" \
  "$(run "$root_ok" 'TMPDIR=$PWD/tmp; mkdir -p "$TMPDIR"; takaro_resolve_target stub STUB >/dev/null; ls "$TMPDIR" | wc -l')"

root_fail="$(fixture failing fail)"
check "a failing tool returns 2" "2" \
  "$(run "$root_fail" 'takaro_resolve_target stub STUB >/dev/null 2>&1; printf "%s" "$?"')"
check "the env file is removed on failure too" "0" \
  "$(run "$root_fail" 'TMPDIR=$PWD/tmp; mkdir -p "$TMPDIR"; takaro_resolve_target stub STUB >/dev/null 2>&1; ls "$TMPDIR" | wc -l')"
stderr="$(run "$root_fail" 'takaro_resolve_target stub STUB' 2>&1 >/dev/null || true)"
case "$stderr" in
  *"could not resolve the stub catalog target"*) echo "PASS the failure names the game" ;;
  *) echo "FAIL the failure message was unhelpful: $stderr" >&2; FAILED=1 ;;
esac

# 3. The fast path: a build that re-execs inside a toolchain image with no takaro-maint in
#    it hands the resolved keys over as environment, and nothing is run.
root_never="$(fixture pre-resolved never)"
check "pre-exported keys resolve without the tool" "linux-1.0" \
  "$(run "$root_never" 'takaro_resolve_target stub STUB && printf "%s" "$STUB_TARGET"' \
     STUB_TARGET=linux-1.0 STUB_FINGERPRINT=abc)"
check "and with the same target named explicitly" "linux-1.0" \
  "$(run "$root_never" 'takaro_resolve_target stub STUB linux-1.0 && printf "%s" "$STUB_TARGET"' \
     STUB_TARGET=linux-1.0 STUB_FINGERPRINT=abc)"
# The stub here refuses to run at all, so "the tool was called" is what its message proves.
check "a different target is resolved rather than assumed" "2 called" \
  "$(run "$root_never" 'takaro_resolve_target stub STUB other-1.0 2>&1 >/dev/null | grep -q "the stub was called" \
       && echo called || echo quiet' STUB_TARGET=linux-1.0 STUB_FINGERPRINT=abc \
     | { read -r called; printf "%s %s" "$(run "$root_never" \
         'takaro_resolve_target stub STUB other-1.0 >/dev/null 2>&1; printf "%s" "$?"' \
         STUB_TARGET=linux-1.0 STUB_FINGERPRINT=abc)" "$called"; })"
check "a target without its fingerprint is not trusted" "2" \
  "$(run "$root_never" 'takaro_resolve_target stub STUB >/dev/null 2>&1; printf "%s" "$?"' STUB_TARGET=linux-1.0)"

# 4. The flag parser.
check "--target x" "x" "$(run "$root_ok" 'takaro_parse_target_flag --target x; printf "%s" "$TARGET"')"
check "--target=x" "x" "$(run "$root_ok" 'takaro_parse_target_flag --target=x; printf "%s" "$TARGET"')"
check "no flags at all" "" "$(run "$root_ok" 'takaro_parse_target_flag; printf "%s" "$TARGET"')"
# `${1:?}` aborts the shell it runs in rather than returning, so what this asserts is that
# a `--target` with nothing after it never reaches a resolution with an empty target.
bare=0
bare_message="$(run "$root_ok" 'takaro_parse_target_flag --target' 2>&1 >/dev/null)" || bare=$?
check "a bare --target stops the script" "yes" "$([ "$bare" -ne 0 ] && echo yes || echo no)"
case "$bare_message" in
  *"--target needs a catalog target id"*) echo "PASS the bare --target message says what is missing" ;;
  *) echo "FAIL the bare --target message was unhelpful: $bare_message" >&2; FAILED=1 ;;
esac
check "an unknown argument returns 2" "2" \
  "$(run "$root_ok" 'takaro_parse_target_flag --nope >/dev/null 2>&1; printf "%s" "$?"')"

# 5. Every game's shim parses, and defines the three names its own scripts call.
for shim in "$REPO_ROOT"/games/*/scripts/lib-target.sh; do
  game="$(basename "$(dirname "$(dirname "$shim")")")"
  bash -n "$shim" || { echo "FAIL $game: lib-target.sh does not parse" >&2; FAILED=1; }
  prefix="$(sed -n 's/^\([a-z0-9]*\)_repo_root().*/\1/p' "$shim")"
  found="$(bash -c ". '$shim'; for name in ${prefix}_repo_root ${prefix}_resolve_target ${prefix}_parse_target_flag; do
      declare -F \"\$name\" >/dev/null || { echo \"missing \$name\"; exit 1; }
    done; echo ok")"
  check "$game defines its three target functions" "ok" "$found"
  check "$game resolves to the repository root" "$REPO_ROOT" "$(bash -c ". '$shim'; ${prefix}_repo_root")"
done

[ "$FAILED" -eq 0 ] || { echo "SOME FAILED" >&2; exit 1; }
echo "ALL PASS"
