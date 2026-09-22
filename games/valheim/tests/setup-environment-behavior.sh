#!/usr/bin/env bash
# What games/valheim/scripts/setup-environment.sh has to be true of, driven through stubs.
#
# The script prepares two pinned inputs: the reference assemblies of an exact Steam depot
# manifest (fetched by `takaro-maint steam references`) and an exact BepInExPack zip
# (fetched from its versioned URL and hash-checked). Both land through the same atomic
# publication, and the behaviour that matters is what survives a failure: a run that cannot
# finish must leave whatever was already prepared byte-identical, and must never accept
# something that merely looks like an assembly.
#
# The stubs here stand in for takaro-maint and curl. sha256sum, zip extraction and the JSON
# read are real, because those are the checks being tested.
#
# There is no SteamCMD tarball and no depot fallback here: the references come from a
# pinned manifest through a pinned DepotDownloader, and a pinned build Steam does not
# serve is a re-pin, not a retry somewhere else.
set -uo pipefail

SELF="$(realpath "${BASH_SOURCE[0]}")"
COMMAND_NAME="$(basename "$0")"
REQUIRED_ASSEMBLIES=(
  assembly_valheim.dll
  assembly_utils.dll
  Splatform.dll
  UnityEngine.dll
  UnityEngine.CoreModule.dll
)
REFERENCE_CACHE_MARKER_NAME=".takaro-valheim-reference-cache"
REFERENCE_CACHE_MARKER_CONTENT="takaro-valheim-reference-cache-v1"
STUB_FP16="fp16valheimtest1"
STUB_TARGET="linux-1.0.15"
STUB_FINGERPRINT="fp16valheimtest1000000000000000000000000000000000000000000000000"
PACK_VERSION="5.4.2350"

mark_owned_reference_cache() {
  local cache_dir="$1"
  mkdir -p "$cache_dir"
  printf '%s\n' "$REFERENCE_CACHE_MARKER_CONTENT" > "$cache_dir/$REFERENCE_CACHE_MARKER_NAME"
}

create_required_assemblies() {
  local install_dir="${1:-$STUB_SERVER_DIR}"
  local managed_dir="$install_dir/valheim_server_Data/Managed"
  local assembly
  mkdir -p "$managed_dir"
  for assembly in "${REQUIRED_ASSEMBLIES[@]}"; do
    create_managed_assembly_fixture "$managed_dir/$assembly"
  done
}

create_managed_assembly_fixture() {
  local output="$1"
  mkdir -p "$(dirname "$output")"
  /bin/cp "$MANAGED_ASSEMBLY_FIXTURE" "$output"
}

create_fake_marker_assembly_fixture() {
  local output="$1"
  mkdir -p "$(dirname "$output")"
  printf 'MZ%0126dBSJB' 0 > "$output"
}

# --------------------------------------------------------------------------- the stubs

takaro_maint_stub() {
  local subcommand="${1:-} ${2:-}"
  local argument previous="" out="" dest="" target="" forced=false

  for argument in "$@"; do
    case "$previous" in
      --out) out="$argument" ;;
      --dest) dest="$argument" ;;
      --target) target="$argument" ;;
    esac
    [ "$argument" = "--force" ] && forced=true
    previous="$argument"
  done
  mkdir -p "$STUB_STATE_DIR"
  printf '%s\n' "$*" >> "$STUB_STATE_DIR/maint-calls"

  case "$subcommand" in
    "targets resolve")
      [ -n "$out" ] || return 2
      cat > "$out" <<ENV
VALHEIM_TARGET=$STUB_TARGET
VALHEIM_FINGERPRINT=$STUB_FINGERPRINT
VALHEIM_FP16=$STUB_FP16
VALHEIM_REVISION=1.0.15
VALHEIM_IMAGE=mbround18/valheim:3.8.8@sha256:0000
VALHEIM_TOOLCHAIN=mcr.microsoft.com/dotnet/sdk:8.0@sha256:0000
VALHEIM_STEAM_APP=896660
VALHEIM_STEAM_BRANCH=public
VALHEIM_STEAM_BUILDID=25390671
VALHEIM_STEAM_DEPOTS=896661:6686760496212527200
VALHEIM_BEPINEX_PACKAGE=denikson/BepInExPack_Valheim
VALHEIM_BEPINEX_PACK_VERSION=$PACK_VERSION
VALHEIM_BEPINEX_URL=https://example.invalid/package/download/denikson/BepInExPack_Valheim/$PACK_VERSION/
VALHEIM_BEPINEX_SHA256=$STUB_PACK_SHA256
VALHEIM_BEPINEX_SIZE=$STUB_PACK_SIZE
VALHEIM_ASSEMBLY_VALHEIM_SHA256=$STUB_ASSEMBLY_SHA256
VALHEIM_REFERENCE_ASSEMBLY_VALHEIM_SHA256=$STUB_ASSEMBLY_SHA256
VALHEIM_REFERENCE_ASSEMBLY_UTILS_SHA256=$STUB_ASSEMBLY_SHA256
VALHEIM_REFERENCE_SPLATFORM_SHA256=$STUB_ASSEMBLY_SHA256
VALHEIM_REFERENCE_UNITYENGINE_SHA256=$STUB_ASSEMBLY_SHA256
VALHEIM_REFERENCE_UNITYENGINE_COREMODULE_SHA256=$STUB_ASSEMBLY_SHA256
ENV
      if [ "$STUB_SCENARIO" = "missing_reference_hash" ]; then
        sed -i '/VALHEIM_REFERENCE_SPLATFORM_SHA256=/d' "$out"
      fi
      return 0
      ;;
    "steam references")
      printf '%s\n' "${target:-<no-target>}" >> "$STUB_STATE_DIR/reference-fetches"
      [ "$forced" = true ] && printf 'forced\n' >> "$STUB_STATE_DIR/reference-forces"
      steam_references_stub "$dest"
      return $?
      ;;
  esac
  printf 'unexpected takaro-maint call: %s\n' "$*" >&2
  return 99
}

steam_references_stub() {
  local dest="$1"
  local assembly count=0

  [ -n "$dest" ] || return 2
  if [ -f "$STUB_STATE_DIR/reference-fetches" ]; then
    count="$(wc -l < "$STUB_STATE_DIR/reference-fetches")"
  fi

  case "$STUB_SCENARIO" in
    manifest_unavailable)
      printf 'depot 896661 manifest 6686760496212527200 is not available\n' >&2
      return 4
      ;;
    reference_hash_mismatch)
      mkdir -p "$dest"
      for assembly in "${REQUIRED_ASSEMBLIES[@]}"; do
        create_managed_assembly_fixture "$dest/$assembly"
      done
      # A real managed assembly, but not the one the target pins: the script's own
      # sha256sum check is what has to catch this.
      printf 'tampered\n' >> "$dest/assembly_valheim.dll"
      return 0
      ;;
    stale_cache)
      if [ "$count" -le 1 ]; then
        printf '%s was built for another fingerprint; pass --force to replace it\n' "$dest" >&2
        return 7
      fi
      mkdir -p "$dest"
      for assembly in "${REQUIRED_ASSEMBLIES[@]}"; do
        create_managed_assembly_fixture "$dest/$assembly"
      done
      return 0
      ;;
    missing_required)
      mkdir -p "$dest"
      create_managed_assembly_fixture "$dest/assembly_valheim.dll"
      return 0
      ;;
    empty_required)
      mkdir -p "$dest"
      for assembly in "${REQUIRED_ASSEMBLIES[@]}"; do
        : > "$dest/$assembly"
      done
      return 0
      ;;
    corrupt_required)
      mkdir -p "$dest"
      for assembly in "${REQUIRED_ASSEMBLIES[@]}"; do
        printf 'not a managed assembly\n' > "$dest/$assembly"
      done
      return 0
      ;;
    fake_marker_required)
      mkdir -p "$dest"
      for assembly in "${REQUIRED_ASSEMBLIES[@]}"; do
        create_fake_marker_assembly_fixture "$dest/$assembly"
      done
      return 0
      ;;
    always_fail)
      printf 'simulated reference fetch failure\n' >&2
      return 1
      ;;
    *)
      mkdir -p "$dest"
      for assembly in "${REQUIRED_ASSEMBLIES[@]}"; do
        create_managed_assembly_fixture "$dest/$assembly"
      done
      return 0
      ;;
  esac
}

curl_stub() {
  local output="" argument previous=""
  for argument in "$@"; do
    [ "$previous" = "-o" ] && output="$argument"
    previous="$argument"
  done
  printf '%s\n' "$*" >> "$STUB_STATE_DIR/curl-calls"

  if [ "$STUB_SCENARIO" = "pack_download_failure" ]; then
    printf 'simulated Thunderstore download failure\n' >&2
    return 22
  fi
  [ -n "$output" ] || return 2
  mkdir -p "$(dirname "$output")"
  if [ "$STUB_SCENARIO" = "pack_hash_mismatch" ]; then
    printf 'not the pinned pack at all\n' > "$output"
    return 0
  fi
  /bin/cp "$STUB_PACK_ZIP" "$output"
}

cp_stub() {
  /bin/cp "$@"
}

mv_stub() {
  if [ "$STUB_SCENARIO" = "valheim_first_rename_interrupt" ] \
    && [ "${1:-}" = "$STUB_SERVER_DIR" ] \
    && [[ "${2:-}" == "$STUB_SERVER_DIR".backup.* ]]; then
    /bin/mv "$@"
    : > "$STUB_STATE_DIR/first-rename-interrupt-attempted"
    kill -TERM "$PPID"
    /bin/sleep 0.2
    return 71
  fi
  if [ "$STUB_SCENARIO" = "valheim_publish_interrupt" ] \
    && [[ "${1:-}" == "$STUB_SERVER_DIR".stage.* ]] \
    && [ "${2:-}" = "$STUB_SERVER_DIR" ]; then
    : > "$STUB_STATE_DIR/publish-interrupt-attempted"
    kill -TERM "$PPID"
    /bin/sleep 0.2
    return 70
  fi
  if [ "$STUB_SCENARIO" = "valheim_publish_failure" ] \
    && [[ "${1:-}" == "$STUB_SERVER_DIR".stage.* ]] \
    && [ "${2:-}" = "$STUB_SERVER_DIR" ]; then
    printf 'simulated atomic Valheim publication failure\n' >&2
    return 69
  fi
  /bin/mv "$@"
}

file_stub() {
  if [ "$STUB_SCENARIO" = "file_unavailable" ]; then
    printf "file: command unavailable\n" >&2
    return 127
  fi
  /usr/bin/file "$@"
}

case "$COMMAND_NAME" in
  takaro-maint)
    takaro_maint_stub "$@"
    exit $?
    ;;
  curl)
    curl_stub "$@"
    exit $?
    ;;
  cp)
    cp_stub "$@"
    exit $?
    ;;
  mv)
    mv_stub "$@"
    exit $?
    ;;
  file)
    file_stub "$@"
    exit $?
    ;;
  sleep)
    exit 0
    ;;
esac

# --------------------------------------------------------------------------- the harness

# Every VALHEIM_* value this harness cares about is one it sets itself. A caller may already
# have the resolved target in its environment -- build-release.sh exports it before running
# the C# suite, which runs this file -- and an inherited VALHEIM_BEPINEX_SHA256 or
# VALHEIM_TARGET would quietly replace a fixture with the real pin. So the harness starts
# from a clean slate.
while IFS= read -r inherited; do
  [ -n "$inherited" ] && unset "$inherited"
done < <(compgen -v VALHEIM_ 2>/dev/null || true)

VALHEIM_DIR="$(cd "$(dirname "$SELF")/.." && pwd)"
SETUP_SCRIPT="$VALHEIM_DIR/scripts/setup-environment.sh"
MANAGED_ASSEMBLY_FIXTURE="${MANAGED_ASSEMBLY_FIXTURE:-$VALHEIM_DIR/src/Takaro.Valheim.Core/bin/Debug/net8.0/Takaro.Valheim.Core.dll}"
if [ ! -f "$MANAGED_ASSEMBLY_FIXTURE" ]; then
  printf 'Managed assembly fixture is missing: %s\n' "$MANAGED_ASSEMBLY_FIXTURE" >&2
  exit 1
fi
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

# One BepInExPack zip per content variant, built once with fixed timestamps. The core DLLs
# are the real managed-assembly fixture unless the variant is about them being wrong.
build_pack_zip() {
  local variant="$1" out="$2" work
  work="$(mktemp -d "$TMP_ROOT/packXXXXXX")"
  mkdir -p "$work/BepInExPack_Valheim/BepInEx/core" \
    "$work/BepInExPack_Valheim/BepInEx/config" \
    "$work/BepInExPack_Valheim/doorstop_libs"
  printf 'doorstop\n' > "$work/BepInExPack_Valheim/doorstop_libs/libdoorstop_x64.so"
  printf '[Logging]\n' > "$work/BepInExPack_Valheim/BepInEx/config/BepInEx.cfg"
  printf '#!/bin/sh\nexport DOORSTOP_ENABLE=TRUE\n' > "$work/BepInExPack_Valheim/start_server_bepinex.sh"

  local core="$work/BepInExPack_Valheim/BepInEx/core"
  case "$variant" in
    empty)
      : > "$core/BepInEx.dll"
      : > "$core/0Harmony.dll"
      ;;
    corrupt)
      printf 'not a managed assembly\n' > "$core/BepInEx.dll"
      printf 'not a managed assembly\n' > "$core/0Harmony.dll"
      ;;
    fake_marker)
      create_fake_marker_assembly_fixture "$core/BepInEx.dll"
      create_fake_marker_assembly_fixture "$core/0Harmony.dll"
      ;;
    *)
      create_managed_assembly_fixture "$core/BepInEx.dll"
      create_managed_assembly_fixture "$core/0Harmony.dll"
      ;;
  esac

  local version="$PACK_VERSION"
  [ "$variant" = "wrong_version" ] && version="5.4.2333"
  printf '{"name": "BepInExPack_Valheim", "version_number": "%s"}\n' "$version" \
    > "$work/manifest.json"
  printf '# BepInExPack Valheim\n' > "$work/README.md"

  python3 - "$work" "$out" <<'PY'
import os, sys, zipfile

source, target = sys.argv[1], sys.argv[2]
names = []
for root, _, files in os.walk(source):
    for name in files:
        path = os.path.join(root, name)
        names.append((os.path.relpath(path, source).replace(os.sep, "/"), path))
with zipfile.ZipFile(target, "w") as archive:
    for relative, path in sorted(names):
        with open(path, "rb") as handle:
            archive.writestr(zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0)), handle.read())
PY
}

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

RUN_STATUS=0
RUN_OUTPUT=""
RUN_CASE_DIR=""

run_setup() {
  local name="$1"
  local scenario="$2"
  local pack_variant="${3:-valid}"
  local cache_variable="${4:-reference}"
  local case_dir="$TMP_ROOT/$name"
  local bin_dir="$case_dir/bin"
  local command pack_zip
  local -a cache_environment

  mkdir -p "$bin_dir" "$case_dir/home" "$case_dir/data" "$case_dir/server" "$case_dir/deps" "$case_dir/state"
  for command in takaro-maint curl cp mv file sleep; do
    ln -sf "$SELF" "$bin_dir/$command"
  done

  pack_zip="$case_dir/pack-${pack_variant}.zip"
  [ -f "$pack_zip" ] || build_pack_zip "$pack_variant" "$pack_zip"

  RUN_OUTPUT="$case_dir/output.log"
  RUN_CASE_DIR="$case_dir"
  case "$cache_variable" in
    reference) cache_environment=("VALHEIM_REFERENCE_CACHE_DIR=$case_dir/server") ;;
    legacy) cache_environment=("VALHEIM_SERVER_DIR=$case_dir/server") ;;
    *)
      printf 'unknown cache variable mode: %s\n' "$cache_variable" >&2
      return 2
      ;;
  esac
  env \
    PATH="$bin_dir:$PATH" \
    HOME="$case_dir/home" \
    VALHEIM_DATA_DIR="$case_dir/data" \
    VALHEIM_DEPS_DIR="$case_dir/deps" \
    TAKARO_MAINT="$bin_dir/takaro-maint" \
    STUB_SCENARIO="$scenario" \
    STUB_STATE_DIR="$case_dir/state" \
    STUB_SERVER_DIR="$case_dir/server" \
    STUB_PACK_ZIP="$pack_zip" \
    STUB_PACK_SHA256="$(sha256_of "$pack_zip")" \
    STUB_PACK_SIZE="$(wc -c < "$pack_zip" | tr -d '[:space:]')" \
    STUB_ASSEMBLY_SHA256="$(sha256_of "$MANAGED_ASSEMBLY_FIXTURE")" \
    MANAGED_ASSEMBLY_FIXTURE="$MANAGED_ASSEMBLY_FIXTURE" \
    "${cache_environment[@]}" \
    bash "$SETUP_SCRIPT" > "$RUN_OUTPUT" 2>&1
  RUN_STATUS=$?
}

pack_dir() { printf '%s/deps/bepinex/%s' "$RUN_CASE_DIR" "$STUB_FP16"; }

call_count() {
  if [ ! -f "$RUN_CASE_DIR/state/reference-fetches" ]; then
    printf '0\n'
    return
  fi
  wc -l < "$RUN_CASE_DIR/state/reference-fetches"
}

curl_count() {
  if [ ! -f "$RUN_CASE_DIR/state/curl-calls" ]; then
    printf '0\n'
    return
  fi
  wc -l < "$RUN_CASE_DIR/state/curl-calls"
}

assert_equals() {
  local expected="$1"
  local actual="$2"
  local message="$3"
  if [ "$expected" != "$actual" ]; then
    printf 'ASSERT: %s (expected=%s actual=%s)\n' "$message" "$expected" "$actual" >&2
    return 1
  fi
}

assert_nonzero() {
  local actual="$1"
  local message="$2"
  if [ "$actual" -eq 0 ]; then
    printf 'ASSERT: %s (actual exit=0)\n' "$message" >&2
    return 1
  fi
}

assert_file() {
  local path="$1"
  local message="$2"
  if [ ! -f "$path" ]; then
    printf 'ASSERT: %s (missing %s)\n' "$message" "$path" >&2
    return 1
  fi
}

assert_nonempty_file() {
  local path="$1"
  local message="$2"
  if [ ! -s "$path" ]; then
    printf 'ASSERT: %s (missing or empty %s)\n' "$message" "$path" >&2
    return 1
  fi
}

assert_output_contains() {
  local needle="$1"
  local message="$2"
  if ! grep -Fq "$needle" "$RUN_OUTPUT"; then
    printf 'ASSERT: %s (missing output: %s)\n' "$message" "$needle" >&2
    return 1
  fi
}

assert_output_lacks() {
  local needle="$1"
  local message="$2"
  if grep -Fq "$needle" "$RUN_OUTPUT"; then
    printf 'ASSERT: %s (unexpected output: %s)\n' "$message" "$needle" >&2
    return 1
  fi
}

assert_no_server_temporary_state() {
  local leaked_path
  leaked_path="$(find "$RUN_CASE_DIR" -mindepth 1 -maxdepth 1 \
    \( -name 'server.stage.*' -o -name 'server.backup.*' \) \
    -print -quit)"
  if [ -n "$leaked_path" ]; then
    printf 'ASSERT: Valheim reference-cache sibling temporary state was not cleaned up: %s\n' "$leaked_path" >&2
    return 1
  fi
}

assert_no_pack_temporary_state() {
  local leaked_path
  leaked_path="$(find "$RUN_CASE_DIR/deps/bepinex" -mindepth 1 -maxdepth 1 \
    \( -name '*.stage.*' -o -name '*.backup.*' \) \
    -print -quit 2>/dev/null)"
  if [ -n "$leaked_path" ]; then
    printf 'ASSERT: BepInExPack sibling temporary state was not cleaned up: %s\n' "$leaked_path" >&2
    return 1
  fi
}

tree_fingerprint() {
  local directory="$1"
  (
    cd "$directory" || exit 1
    find . -type f -print0 \
      | sort -z \
      | while IFS= read -r -d '' path; do
          printf '%s\0' "$path"
          sha256sum -- "$path"
        done
  ) | sha256sum | awk '{print $1}'
}

# --------------------------------------------------------------------------- the tests

test_first_attempt_success() {
  run_setup first-success first_success
  assert_equals 0 "$RUN_STATUS" "a first successful run should complete setup" || return 1
  assert_equals 1 "$(call_count)" "success should fetch the references exactly once" || return 1
  assert_nonempty_file "$RUN_CASE_DIR/server/valheim_server_Data/Managed/UnityEngine.CoreModule.dll" "required assemblies should be nonempty" || return 1
  assert_file "$RUN_CASE_DIR/server/$REFERENCE_CACHE_MARKER_NAME" "a newly published reference cache must carry its ownership marker" || return 1
  assert_equals "$REFERENCE_CACHE_MARKER_CONTENT" "$(cat "$RUN_CASE_DIR/server/$REFERENCE_CACHE_MARKER_NAME")" "the ownership marker must be written only with the completed format" || return 1
  assert_nonempty_file "$(pack_dir)/BepInExPack_Valheim/BepInEx/core/BepInEx.dll" "BepInEx reference should be nonempty" || return 1
  assert_file "$(pack_dir)/BepInExPack_Valheim/start_server_bepinex.sh" "the pack's loader entrypoint should be published" || return 1
  assert_output_contains "Reference assemblies ready" "a successful run should say where the references are" || return 1
  assert_no_pack_temporary_state || return 1
}

test_references_are_fetched_from_the_pinned_manifest_only() {
  run_setup pinned-manifest first_success
  assert_equals 0 "$RUN_STATUS" "the pinned fetch should succeed" || return 1
  assert_equals 1 "$(call_count)" "exactly one reference fetch" || return 1
  assert_equals "$STUB_TARGET" "$(tr -d '[:space:]' < "$RUN_CASE_DIR/state/reference-fetches")" "the fetch must name the catalog target" || return 1
  if grep -Fq "app_update" "$RUN_CASE_DIR/state/maint-calls"; then
    printf 'ASSERT: setup ran an app_update against the branch head\n' >&2
    return 1
  fi
  # The pack is fetched by its exact versioned URL, never through Thunderstore's `latest`.
  assert_equals 1 "$(curl_count)" "the pack should be downloaded exactly once" || return 1
  if grep -Fq "latest" "$RUN_CASE_DIR/state/curl-calls"; then
    printf 'ASSERT: the pack was fetched through a moving latest alias\n' >&2
    return 1
  fi
  grep -Fq "/${PACK_VERSION}/" "$RUN_CASE_DIR/state/curl-calls" || {
    printf 'ASSERT: the pack download did not name the pinned version\n' >&2
    return 1
  }
}

test_unavailable_manifest_fails_without_fallback_and_keeps_cache() {
  local case_dir="$TMP_ROOT/manifest-unavailable"
  local before after
  mkdir -p "$case_dir/server/valheim_server_Data/Managed"
  printf '%s\n' 'an existing cache' > "$case_dir/server/valheim_server_Data/Managed/keep.txt"
  mark_owned_reference_cache "$case_dir/server"
  before="$(tree_fingerprint "$case_dir/server")"

  run_setup manifest-unavailable manifest_unavailable
  after="$(tree_fingerprint "$RUN_CASE_DIR/server")"

  assert_nonzero "$RUN_STATUS" "an unavailable pinned manifest must fail setup" || return 1
  assert_equals 1 "$(call_count)" "an unavailable manifest must not be retried elsewhere" || return 1
  assert_equals "$before" "$after" "a failed fetch must leave the existing cache byte-identical" || return 1
  assert_output_contains "not falling back" "the failure must say it is not falling back" || return 1
  assert_output_contains "6686760496212527200" "the failure must name the pinned manifest" || return 1
  assert_output_contains "$STUB_TARGET" "the failure must name the target" || return 1
  assert_output_contains "steam pin" "the failure must name the re-pin recovery route" || return 1
  assert_no_server_temporary_state || return 1
}

test_no_windows_fallback_is_attempted() {
  run_setup no-second-platform always_fail
  assert_nonzero "$RUN_STATUS" "a failed fetch must fail setup" || return 1
  assert_equals 1 "$(call_count)" "there is no second platform to try" || return 1
  assert_output_lacks "windows" "setup must not attempt a Windows depot fallback" || return 1
  assert_output_contains "Windows depot recorded in the target" "the manual recovery route should still be named" || return 1
}

test_wrong_reference_hash_is_refused() {
  local case_dir="$TMP_ROOT/wrong-reference-hash"
  local before after
  mkdir -p "$case_dir/server/valheim_server_Data/Managed"
  printf '%s\n' 'an existing cache' > "$case_dir/server/valheim_server_Data/Managed/keep.txt"
  mark_owned_reference_cache "$case_dir/server"
  before="$(tree_fingerprint "$case_dir/server")"

  run_setup wrong-reference-hash reference_hash_mismatch
  after="$(tree_fingerprint "$RUN_CASE_DIR/server")"

  assert_nonzero "$RUN_STATUS" "an assembly that is not the pinned one must fail setup" || return 1
  assert_output_contains "is not the one ${STUB_TARGET} pins" "the refusal must name the target" || return 1
  assert_equals "$before" "$after" "a refused fetch must leave the existing cache byte-identical" || return 1
  assert_no_server_temporary_state || return 1
}

test_stale_reference_cache_is_refetched_only_when_owned() {
  local case_dir="$TMP_ROOT/stale-cache"
  mkdir -p "$case_dir/server/valheim_server_Data/Managed"
  mark_owned_reference_cache "$case_dir/server"

  run_setup stale-cache stale_cache
  assert_equals 0 "$RUN_STATUS" "a cache this script owns may be replaced" || return 1
  assert_equals 2 "$(call_count)" "a stale owned cache costs exactly one retry" || return 1
  assert_file "$RUN_CASE_DIR/state/reference-forces" "the retry must pass --force" || return 1
  assert_nonempty_file "$RUN_CASE_DIR/server/valheim_server_Data/Managed/assembly_valheim.dll" "the retry should publish the references" || return 1

  # The same conflict without the ownership marker is refused, not forced.
  local unowned="$TMP_ROOT/stale-cache-unowned"
  mkdir -p "$unowned/server/valheim_server_Data/Managed"
  printf '%s\n' 'someone else owns this' > "$unowned/server/valheim_server_Data/Managed/other.txt"
  run_setup stale-cache-unowned stale_cache
  assert_nonzero "$RUN_STATUS" "an unowned stale cache must be refused" || return 1
}

test_a_failed_fetch_does_not_accept_a_stale_managed_directory() {
  local case_dir="$TMP_ROOT/stale-managed"
  local assembly
  mkdir -p "$case_dir/server/valheim_server_Data/Managed"
  for assembly in "${REQUIRED_ASSEMBLIES[@]}"; do
    printf 'stale-valid-looking-reference\n' > "$case_dir/server/valheim_server_Data/Managed/$assembly"
  done
  mark_owned_reference_cache "$case_dir/server"

  run_setup stale-managed always_fail
  assert_nonzero "$RUN_STATUS" "a failed fetch must not accept stale assemblies" || return 1
  assert_equals 1 "$(call_count)" "a stale cache must not bypass the fetch" || return 1
}

test_owned_reference_cache_data_survives_a_refetch() {
  local case_dir="$TMP_ROOT/cache-data-survives"
  mkdir -p "$case_dir/server/worlds_local"
  printf '%s\n' "keep me" > "$case_dir/server/worlds_local/world.db"
  mark_owned_reference_cache "$case_dir/server"

  run_setup cache-data-survives first_success
  assert_equals 0 "$RUN_STATUS" "a refetch into an owned cache should succeed" || return 1
  assert_file "$RUN_CASE_DIR/server/worlds_local/world.db" "a refetch must preserve owned cache data" || return 1
  assert_file "$RUN_CASE_DIR/server/$REFERENCE_CACHE_MARKER_NAME" "a successful publication must carry a completed ownership marker" || return 1
}

test_invalid_legacy_live_server_is_refused_before_any_fetch_and_unchanged() {
  local case_dir="$TMP_ROOT/legacy-live-server"
  local before
  local after
  mkdir -p "$case_dir/server/worlds_local" "$case_dir/server/valheim_server_Data/Managed"
  printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$case_dir/server/valheim_server.x86_64"
  chmod +x "$case_dir/server/valheim_server.x86_64"
  printf '%s\n' 'irreplaceable world' > "$case_dir/server/worlds_local/world.db"
  printf '%s\n' 'invalid reference' > "$case_dir/server/valheim_server_Data/Managed/assembly_valheim.dll"
  before="$(tree_fingerprint "$case_dir/server")"

  run_setup legacy-live-server first_success valid legacy
  after="$(tree_fingerprint "$case_dir/server")"

  assert_nonzero "$RUN_STATUS" "an invalid unowned legacy live-server tree must be refused" || return 1
  assert_equals 0 "$(call_count)" "refusal must happen before anything can mutate a live server tree" || return 1
  assert_equals "$before" "$after" "every live-server file and hash must stay unchanged" || return 1
  assert_output_contains "VALHEIM_REFERENCE_CACHE_DIR" "refusal must direct the caller to a separate owned cache" || return 1
  assert_output_contains "valheim_server.x86_64" "refusal must identify the live-server marker" || return 1
  if [ -e "$RUN_CASE_DIR/server/$REFERENCE_CACHE_MARKER_NAME" ]; then
    printf 'ASSERT: refusal forged an ownership marker inside the live server\n' >&2
    return 1
  fi
  assert_no_server_temporary_state || return 1
}

test_missing_required_dlls_fail_setup() {
  run_setup missing-required missing_required
  assert_nonzero "$RUN_STATUS" "a Managed directory without every required DLL must fail" || return 1
  assert_output_contains "managed PE/CLI assembly" "the failure should name the assembly contract" || return 1
}

test_empty_required_dlls_fail_setup() {
  run_setup empty-required empty_required
  assert_nonzero "$RUN_STATUS" "zero-byte Valheim DLLs must not satisfy reference validation" || return 1
}

test_corrupt_required_dlls_fail_setup() {
  run_setup corrupt-required corrupt_required
  assert_nonzero "$RUN_STATUS" "non-managed Valheim DLL text must not satisfy reference validation" || return 1
  assert_output_contains "managed PE/CLI assembly" "Valheim corruption failure should be actionable" || return 1
}

test_fake_managed_markers_do_not_satisfy_real_assembly_validation() {
  run_setup fake-marker-required fake_marker_required
  assert_nonzero "$RUN_STATUS" "MZ and BSJB marker placement must not satisfy managed assembly validation" || return 1
}

test_empty_bepinex_dlls_fail_setup() {
  run_setup empty-bepinex first_success empty
  assert_nonzero "$RUN_STATUS" "zero-byte BepInEx DLLs must not satisfy reference validation" || return 1
  assert_output_contains "required BepInEx assembly" "BepInEx failure should name the missing or empty reference" || return 1
}

test_corrupt_bepinex_dlls_fail_setup() {
  run_setup corrupt-bepinex first_success corrupt
  assert_nonzero "$RUN_STATUS" "non-managed BepInEx DLL text must not satisfy reference validation" || return 1
  assert_output_contains "managed PE/CLI assembly" "BepInEx corruption failure should be actionable" || return 1
}

test_fake_bepinex_markers_do_not_satisfy_real_assembly_validation() {
  run_setup fake-marker-bepinex first_success fake_marker
  assert_nonzero "$RUN_STATUS" "fake BepInEx marker blobs must fail managed assembly validation" || return 1
}

test_pack_manifest_version_must_match_the_pin() {
  run_setup pack-wrong-version first_success wrong_version
  assert_nonzero "$RUN_STATUS" "a pack that is not the pinned version must fail setup" || return 1
  assert_output_contains "the pack says it is 5.4.2333" "the refusal must name what arrived" || return 1
  assert_output_contains "the target pins ${PACK_VERSION}" "the refusal must name what was pinned" || return 1
  assert_no_pack_temporary_state || return 1
}

test_pack_hash_mismatch_is_refused_and_previous_pack_kept() {
  local before after
  run_setup pack-hash-mismatch first_success
  assert_equals 0 "$RUN_STATUS" "the first run should prepare a pack to protect" || return 1
  before="$(tree_fingerprint "$(pack_dir)")"

  # The same case directory, so the prepared pack is the one at risk. The reference cache
  # is already valid, so only the pack step runs.
  rm -rf "$(pack_dir)"
  mkdir -p "$(pack_dir)/BepInExPack_Valheim/BepInEx/core"
  printf 'previous pack\n' > "$(pack_dir)/BepInExPack_Valheim/BepInEx/core/keep.txt"
  before="$(tree_fingerprint "$(pack_dir)")"
  run_setup pack-hash-mismatch pack_hash_mismatch
  after="$(tree_fingerprint "$(pack_dir)")"

  assert_nonzero "$RUN_STATUS" "a pack that is not the recorded sha256 must fail setup" || return 1
  assert_output_contains "not the sha256 the target records" "the refusal must say what was wrong" || return 1
  assert_equals "$before" "$after" "a refused pack must leave the previous pack byte-identical" || return 1
  assert_no_pack_temporary_state || return 1
}

test_pack_download_failure_preserves_previous_pack() {
  local before after
  run_setup pack-download-failure first_success
  assert_equals 0 "$RUN_STATUS" "the first run should prepare a pack to protect" || return 1
  rm -rf "$(pack_dir)"
  mkdir -p "$(pack_dir)/BepInExPack_Valheim/BepInEx/core"
  printf 'previous pack\n' > "$(pack_dir)/BepInExPack_Valheim/BepInEx/core/keep.txt"
  before="$(tree_fingerprint "$(pack_dir)")"

  run_setup pack-download-failure pack_download_failure
  after="$(tree_fingerprint "$(pack_dir)")"

  assert_nonzero "$RUN_STATUS" "a failed pack download must fail setup" || return 1
  assert_output_contains "the download failed" "the failure should say the download failed" || return 1
  assert_equals "$before" "$after" "a failed download must leave the previous pack byte-identical" || return 1
  assert_no_pack_temporary_state || return 1
}

test_failed_file_validator_is_actionable() {
  run_setup file-unavailable file_unavailable
  assert_nonzero "$RUN_STATUS" "setup must fail when its real PE/CLI validator is unavailable" || return 1
  assert_output_contains "managed PE/CLI assembly" "failed validator should identify the required assembly contract" || return 1
}

test_missing_file_command_fails_preflight() {
  local case_dir="$TMP_ROOT/file-command-absent"
  local bin_dir="$case_dir/bin"
  mkdir -p "$bin_dir" "$case_dir/home"
  ln -sf /bin/bash "$bin_dir/bash"
  ln -sf /usr/bin/dirname "$bin_dir/dirname"

  if PATH="$bin_dir" command -v file >/dev/null 2>&1; then
    printf "ASSERT: isolated preflight PATH unexpectedly contains the 'file' command\n" >&2
    return 1
  fi

  RUN_OUTPUT="$case_dir/output.log"
  RUN_CASE_DIR="$case_dir"
  /usr/bin/env \
    PATH="$bin_dir" \
    HOME="$case_dir/home" \
    /bin/bash "$SETUP_SCRIPT" > "$RUN_OUTPUT" 2>&1
  RUN_STATUS=$?

  assert_nonzero "$RUN_STATUS" "setup must fail its preflight when the file command is truly absent" || return 1
  assert_output_contains "requires the 'file' command" "missing validator preflight should be actionable" || return 1
}

test_valid_existing_cache_skips_the_fetch() {
  local case_dir="$TMP_ROOT/valid-existing"
  STUB_SERVER_DIR="$case_dir/server" create_required_assemblies "$case_dir/server"

  run_setup valid-existing always_fail valid legacy
  assert_equals 0 "$RUN_STATUS" "a fully validated existing install should be reused" || return 1
  assert_equals 0 "$(call_count)" "validated existing references should skip the fetch" || return 1
  if [ -e "$RUN_CASE_DIR/server/$REFERENCE_CACHE_MARKER_NAME" ]; then
    printf 'ASSERT: read-only reuse must not claim ownership of a caller installation\n' >&2
    return 1
  fi
}

test_a_prepared_pack_is_not_downloaded_again() {
  run_setup pack-reuse first_success
  assert_equals 0 "$RUN_STATUS" "the first run should prepare both inputs" || return 1
  assert_equals 1 "$(curl_count)" "the first run downloads the pack once" || return 1

  # The stub state carries over between runs in one case directory, so the counts are
  # cumulative: unchanged counts mean the second run fetched nothing.
  run_setup pack-reuse first_success
  assert_equals 0 "$RUN_STATUS" "a second run over prepared inputs should succeed" || return 1
  assert_equals 1 "$(curl_count)" "a validated pack must not be downloaded again" || return 1
  assert_equals 1 "$(call_count)" "a validated reference cache must not be fetched again" || return 1
}

test_a_drifted_reference_cache_is_not_reused() {
  run_setup drifted-references first_success
  assert_equals 0 "$RUN_STATUS" "the first run should prepare a reference cache" || return 1
  assert_equals 1 "$(call_count)" "the first run fetches the references once" || return 1

  # Still a real managed assembly -- just no longer the one the target pins. Validation
  # alone cannot tell the difference, which is why the pinned digest is checked again.
  printf 'tampered\n' >> "$RUN_CASE_DIR/server/valheim_server_Data/Managed/assembly_utils.dll"

  run_setup drifted-references first_success
  assert_equals 0 "$RUN_STATUS" "the second run should repair the drifted cache" || return 1
  assert_equals 2 "$(call_count)" "a cache that is not the pinned bytes must be fetched again" || return 1
  assert_output_contains "assembly_utils.dll" "the run must name the assembly that drifted" || return 1
}

test_a_missing_required_reference_hash_is_refused() {
  run_setup missing-reference-hash missing_reference_hash
  assert_equals 5 "$RUN_STATUS" "a required assembly without a recorded hash must be refused" || return 1
  assert_output_contains "Splatform.dll" "the refusal must name the unbound assembly" || return 1
  assert_output_contains "VALHEIM_REFERENCE_SPLATFORM_SHA256" "the refusal must name the missing key" || return 1
}

test_a_drifted_pack_is_downloaded_again() {
  run_setup drifted-pack first_success
  assert_equals 0 "$RUN_STATUS" "the first run should prepare a pack" || return 1
  assert_equals 1 "$(curl_count)" "the first run downloads the pack once" || return 1

  # Not one of the two core assemblies: the loader shim the game runs through, which
  # assembly validation never looks at.
  printf 'tampered\n' >> "$(pack_dir)/BepInExPack_Valheim/doorstop_libs/libdoorstop_x64.so"

  run_setup drifted-pack first_success
  assert_equals 0 "$RUN_STATUS" "the second run should replace the drifted pack" || return 1
  assert_equals 2 "$(curl_count)" "a pack that is no longer the checked zip must be downloaded again" || return 1
  assert_output_contains "no longer the ${PACK_VERSION} zip" "the run must say why the pack was rejected" || return 1
  assert_no_pack_temporary_state || return 1
}

test_failed_atomic_publication_rolls_back_and_next_run_retries() {
  local case_dir="$TMP_ROOT/atomic-publication"
  mkdir -p "$case_dir/server/worlds_local"
  printf '%s\n' "old install" > "$case_dir/server/worlds_local/world.db"
  mark_owned_reference_cache "$case_dir/server"

  run_setup atomic-publication valheim_publish_failure
  assert_nonzero "$RUN_STATUS" "a failed atomic publication must fail setup" || return 1
  assert_file "$RUN_CASE_DIR/server/worlds_local/world.db" "failed publication must restore the old install" || return 1
  assert_equals "old install" "$(cat "$RUN_CASE_DIR/server/worlds_local/world.db")" "rollback must retain old install content" || return 1
  assert_no_server_temporary_state || return 1

  run_setup atomic-publication first_success
  assert_equals 0 "$RUN_STATUS" "the next setup run must retry after failed publication" || return 1
  assert_nonempty_file "$RUN_CASE_DIR/server/valheim_server_Data/Managed/assembly_valheim.dll" "retry should publish validated references" || return 1
  assert_file "$RUN_CASE_DIR/server/worlds_local/world.db" "successful swap must preserve caller server data" || return 1
  assert_no_server_temporary_state || return 1
}

test_failed_first_publication_does_not_forge_cache_ownership() {
  run_setup failed-first-publication valheim_publish_failure

  assert_nonzero "$RUN_STATUS" "failed first reference-cache publication must fail setup" || return 1
  if [ -e "$RUN_CASE_DIR/server/$REFERENCE_CACHE_MARKER_NAME" ]; then
    printf 'ASSERT: a partial publication forged a completed cache ownership marker\n' >&2
    return 1
  fi
  assert_no_server_temporary_state || return 1
}

test_signal_during_atomic_publication_restores_old_install() {
  local case_dir="$TMP_ROOT/atomic-interrupt"
  mkdir -p "$case_dir/server/worlds_local"
  printf '%s\n' "old install" > "$case_dir/server/worlds_local/world.db"
  mark_owned_reference_cache "$case_dir/server"

  run_setup atomic-interrupt valheim_publish_interrupt
  assert_nonzero "$RUN_STATUS" "an interrupted atomic publication must fail setup" || return 1
  assert_file "$RUN_CASE_DIR/state/publish-interrupt-attempted" "test must reach the atomic publication boundary" || return 1
  assert_file "$RUN_CASE_DIR/server/worlds_local/world.db" "signal cleanup must restore the old install" || return 1
  assert_equals "old install" "$(cat "$RUN_CASE_DIR/server/worlds_local/world.db")" "signal rollback must retain old install content" || return 1
  assert_no_server_temporary_state || return 1
}

test_signal_after_first_atomic_rename_restores_old_install() {
  local case_dir="$TMP_ROOT/first-rename-interrupt"
  mkdir -p "$case_dir/server/worlds_local"
  printf '%s\n' "old install" > "$case_dir/server/worlds_local/world.db"
  mark_owned_reference_cache "$case_dir/server"

  run_setup first-rename-interrupt valheim_first_rename_interrupt
  assert_nonzero "$RUN_STATUS" "a signal after the first publication rename must fail setup" || return 1
  assert_file "$RUN_CASE_DIR/state/first-rename-interrupt-attempted" "test must interrupt immediately after the old install is renamed" || return 1
  assert_file "$RUN_CASE_DIR/server/worlds_local/world.db" "first-rename signal cleanup must restore the old install" || return 1
  assert_equals "old install" "$(cat "$RUN_CASE_DIR/server/worlds_local/world.db")" "first-rename signal rollback must retain old install content" || return 1
  assert_no_server_temporary_state || return 1
}

failures=0
for test_case in \
  test_first_attempt_success \
  test_references_are_fetched_from_the_pinned_manifest_only \
  test_unavailable_manifest_fails_without_fallback_and_keeps_cache \
  test_no_windows_fallback_is_attempted \
  test_wrong_reference_hash_is_refused \
  test_stale_reference_cache_is_refetched_only_when_owned \
  test_a_failed_fetch_does_not_accept_a_stale_managed_directory \
  test_owned_reference_cache_data_survives_a_refetch \
  test_invalid_legacy_live_server_is_refused_before_any_fetch_and_unchanged \
  test_missing_required_dlls_fail_setup \
  test_empty_required_dlls_fail_setup \
  test_corrupt_required_dlls_fail_setup \
  test_fake_managed_markers_do_not_satisfy_real_assembly_validation \
  test_empty_bepinex_dlls_fail_setup \
  test_corrupt_bepinex_dlls_fail_setup \
  test_fake_bepinex_markers_do_not_satisfy_real_assembly_validation \
  test_pack_manifest_version_must_match_the_pin \
  test_pack_hash_mismatch_is_refused_and_previous_pack_kept \
  test_pack_download_failure_preserves_previous_pack \
  test_failed_file_validator_is_actionable \
  test_missing_file_command_fails_preflight \
  test_valid_existing_cache_skips_the_fetch \
  test_a_prepared_pack_is_not_downloaded_again \
  test_a_drifted_reference_cache_is_not_reused \
  test_a_missing_required_reference_hash_is_refused \
  test_a_drifted_pack_is_downloaded_again \
  test_failed_atomic_publication_rolls_back_and_next_run_retries \
  test_failed_first_publication_does_not_forge_cache_ownership \
  test_signal_after_first_atomic_rename_restores_old_install \
  test_signal_during_atomic_publication_restores_old_install; do
  if "$test_case"; then
    printf 'PASS %s\n' "$test_case"
  else
    printf 'FAIL %s\n' "$test_case"
    failures=$((failures + 1))
  fi
done

if [ "$failures" -ne 0 ]; then
  printf '%s setup behavior test(s) failed\n' "$failures" >&2
  exit 1
fi

printf 'All setup behavior tests passed\n'
