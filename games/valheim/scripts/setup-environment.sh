#!/usr/bin/env bash
# Prepares everything the Valheim connector compiles against, for one catalog target:
#
#   * the reference assemblies `build.references` selects, fetched from the pinned Steam
#     depot manifests by `takaro-maint steam references` -- never a branch head, never a
#     whole depot, and never an update to whatever Steam serves today;
#   * the BepInExPack the target pins, downloaded from its exact versioned URL and checked
#     against the sha256 in the record -- never Thunderstore's moving `latest`.
#
# Both land through the same three-step publication this script has always used: stage in a
# sibling directory, validate what was staged, then swap it in atomically with the previous
# copy kept until the swap succeeds. A failed or interrupted preparation therefore leaves
# whatever was already there byte-identical.
#
# Usage: setup-environment.sh [--target <catalog target id>]
set -euo pipefail

# The assembly validator first: every later check is only as real as `file`, so a missing
# validator fails the run before anything is fetched or replaced.
if ! command -v file >/dev/null 2>&1; then
  echo "Valheim reference setup requires the 'file' command to validate real PE/CLI assemblies." >&2
  exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

valheim_parse_target_flag "$@"
valheim_resolve_target "${TARGET}"

cd "${PROJECT_ROOT}"

DATA_DIR="${VALHEIM_DATA_DIR:-_data}"
# The reference cache is per fingerprint, so two targets never share one directory. The two
# legacy overrides keep working: they are how the behaviour harness and an existing
# checkout point the script somewhere else.
REFERENCE_CACHE_DIR="${VALHEIM_REFERENCE_CACHE_DIR:-${VALHEIM_SERVER_DIR:-${DATA_DIR}/references/${VALHEIM_FP16}}}"
SERVER_DIR="$REFERENCE_CACHE_DIR"
DEPS_DIR="${VALHEIM_DEPS_DIR:-${DATA_DIR}/deps}"
PACK_DIR="${DEPS_DIR}/bepinex/${VALHEIM_FP16}"
PACK_CORE_DIR="${PACK_DIR}/BepInExPack_Valheim/BepInEx/core"
LOADER_VERSION_FILE="${PACK_DIR}/.takaro/loader-version"
# Written from the staged pack the moment its zip has been hash-checked, and re-checked
# before the prepared pack is reused. The zip itself is not kept, so this is how a later
# run knows the prepared tree is still the content of the sha256 the target records.
PACK_CHECKSUM_FILE=".takaro/pack.sha256"

REQUIRED_VALHEIM_ASSEMBLIES=(
  assembly_valheim.dll
  assembly_utils.dll
  Splatform.dll
  UnityEngine.dll
  UnityEngine.CoreModule.dll
)
REQUIRED_BEPINEX_ASSEMBLIES=(
  BepInEx.dll
  0Harmony.dll
)
REFERENCE_CACHE_MARKER_NAME=".takaro-valheim-reference-cache"
REFERENCE_CACHE_MARKER_CONTENT="takaro-valheim-reference-cache-v1"

mkdir -p "$(dirname "$SERVER_DIR")" "$(dirname "$PACK_DIR")"

# Where a publication currently is, for the cleanup handler. Both publications use the
# same three slots, so the helpers below take the variable *names* and read and write them
# indirectly -- which is why shellcheck cannot see these being used.
# shellcheck disable=SC2034
ACTIVE_SERVER_STAGE=""
# shellcheck disable=SC2034
ACTIVE_SERVER_BACKUP=""
# shellcheck disable=SC2034
ACTIVE_SERVER_FINAL=""
# shellcheck disable=SC2034
ACTIVE_PACK_STAGE=""
# shellcheck disable=SC2034
ACTIVE_PACK_BACKUP=""
# shellcheck disable=SC2034
ACTIVE_PACK_FINAL=""
ACTIVE_PACK_ARCHIVE=""

restore_publication_state() {
  # One rollback for both publications: move the backup back over whatever is in the way,
  # so a signal landing between the two renames cannot leave the destination half-swapped.
  local backup_var="$1" final_var="$2" stage_var="$3" subject="$4"
  local backup="${!backup_var}" final="${!final_var}" stage="${!stage_var}"

  if [ -n "$backup" ] && [ -e "$backup" ] && [ -n "$final" ]; then
    if [ -e "$final" ] && ! rm -rf "$final"; then
      echo "Interrupted ${subject} publication could not remove the uncommitted replacement; the previous copy remains preserved at $backup." >&2
    elif mv "$backup" "$final"; then
      printf -v "$backup_var" '%s' ""
    else
      echo "Interrupted ${subject} publication could not restore the previous copy; it remains preserved at $backup." >&2
    fi
  fi
  if [ -n "$stage" ]; then
    rm -rf "$stage"
    printf -v "$stage_var" '%s' ""
  fi
  printf -v "$final_var" '%s' ""
}

cleanup_server_publication_state() {
  restore_publication_state ACTIVE_SERVER_BACKUP ACTIVE_SERVER_FINAL ACTIVE_SERVER_STAGE "Valheim reference"
}

cleanup_pack_publication_state() {
  restore_publication_state ACTIVE_PACK_BACKUP ACTIVE_PACK_FINAL ACTIVE_PACK_STAGE "BepInExPack"
  if [ -n "$ACTIVE_PACK_ARCHIVE" ]; then
    rm -f "$ACTIVE_PACK_ARCHIVE"
    ACTIVE_PACK_ARCHIVE=""
  fi
}

cleanup_on_exit() {
  local status=$?
  cleanup_server_publication_state
  cleanup_pack_publication_state
  trap - EXIT
  exit "$status"
}

trap cleanup_on_exit EXIT
trap 'exit 130' HUP INT TERM

curl_retry() {
  curl --retry 5 --retry-delay 2 --retry-all-errors "$@"
}

is_managed_pe_assembly() {
  local assembly_path="$1"
  local description

  [ -f "$assembly_path" ] || return 1
  if ! description="$(LC_ALL=C file -b -- "$assembly_path")"; then
    return 1
  fi

  case "$description" in
    *PE32*Mono/.Net\ assembly*) return 0 ;;
    *) return 1 ;;
  esac
}

validate_managed_assemblies() {
  local managed_dir="$1"
  local assembly

  [ -d "$managed_dir" ] || return 1
  for assembly in "${REQUIRED_VALHEIM_ASSEMBLIES[@]}"; do
    if ! is_managed_pe_assembly "$managed_dir/$assembly"; then
      echo "Required Valheim assembly is not a managed PE/CLI assembly: $managed_dir/$assembly" >&2
      return 1
    fi
  done
}

validate_bepinex_assemblies() {
  local core_dir="$1"
  local assembly

  [ -d "$core_dir" ] || return 1
  for assembly in "${REQUIRED_BEPINEX_ASSEMBLIES[@]}"; do
    if ! is_managed_pe_assembly "$core_dir/$assembly"; then
      echo "Downloaded required BepInEx assembly is not a managed PE/CLI assembly: $core_dir/$assembly" >&2
      return 1
    fi
  done
}

references_match_pin() {
  # The one pinned digest the target records for the reference set. The fetch path checks
  # it before publishing, and the reuse path checks it again: bytes that were validated
  # once are not thereby the bytes this target pins, and a cache is only a cache of this
  # target while assembly_valheim.dll still hashes to what the record says.
  local managed_dir="$1"

  [ -n "${VALHEIM_ASSEMBLY_VALHEIM_SHA256:-}" ] || return 0
  printf '%s  %s\n' "$VALHEIM_ASSEMBLY_VALHEIM_SHA256" "$managed_dir/assembly_valheim.dll" \
    | sha256sum --check --status
}

reference_cache_is_owned() {
  local cache_dir="$1"
  local marker="$cache_dir/$REFERENCE_CACHE_MARKER_NAME"
  local marker_content

  [ -f "$marker" ] || return 1
  IFS= read -r marker_content < "$marker" || return 1
  [ "$marker_content" = "$REFERENCE_CACHE_MARKER_CONTENT" ]
}

reference_cache_has_entries() {
  local cache_dir="$1"
  [ -d "$cache_dir" ] || return 1
  [ -n "$(find -H "$cache_dir" -mindepth 1 -maxdepth 1 -print -quit)" ]
}

ensure_reference_cache_write_is_safe() {
  local cache_dir="$1"

  if [ -e "$cache_dir" ] && [ ! -d "$cache_dir" ]; then
    echo "Valheim reference cache target is not a directory; refusing to mutate it: $cache_dir" >&2
    echo "Set VALHEIM_REFERENCE_CACHE_DIR to a separate empty directory used only for compile references." >&2
    return 1
  fi

  if ! reference_cache_has_entries "$cache_dir"; then
    return 0
  fi

  if reference_cache_is_owned "$cache_dir"; then
    return 0
  fi

  echo "Valheim reference cache target is non-empty and unowned; refusing to mutate it: $cache_dir" >&2
  if [ -e "$cache_dir/valheim_server.x86_64" ]; then
    echo "Detected live-server marker: $cache_dir/valheim_server.x86_64" >&2
  fi
  echo "Validated assemblies may be reused read-only, but invalid caller/live-server files are never replaced." >&2
  echo "Set VALHEIM_REFERENCE_CACHE_DIR to a separate empty directory used only for compile references." >&2
  echo "Legacy VALHEIM_SERVER_DIR remains read-only unless it is empty or already carries the Takaro reference-cache ownership marker." >&2
  return 1
}

publish_directory() {
  # stage -> final, with the previous copy kept until the swap has succeeded.
  local stage_dir="$1" final_dir="$2" backup_var="$3" final_var="$4" stage_var="$5" subject="$6"
  local backup_dir="${final_dir}.backup.$$.${RANDOM}"
  local publish_status

  mkdir -p "$(dirname "$final_dir")"
  publish_status=0
  if [ -e "$final_dir" ]; then
    # Recorded before the first rename, so a signal cannot land in the gap between the old
    # copy moving aside and the cleanup handler learning where it went.
    printf -v "$backup_var" '%s' "$backup_dir"
    printf -v "$final_var" '%s' "$final_dir"
    # Each rename's own status, captured directly: `if ! mv` would report the negation's.
    mv "$final_dir" "$backup_dir" || publish_status=$?
    if [ "$publish_status" -ne 0 ]; then
      echo "Could not move the existing ${subject} aside for atomic publication: $final_dir" >&2
      restore_publication_state "$backup_var" "$final_var" "$stage_var" "$subject"
      return "$publish_status"
    fi
  else
    printf -v "$final_var" '%s' "$final_dir"
  fi

  mv "$stage_dir" "$final_dir" || publish_status=$?
  if [ "$publish_status" -eq 0 ]; then
    printf -v "$stage_var" '%s' ""
    backup_dir="${!backup_var}"
    printf -v "$backup_var" '%s' ""
    printf -v "$final_var" '%s' ""
    [ -n "$backup_dir" ] && rm -rf "$backup_dir"
    return 0
  fi

  echo "Could not atomically publish the validated ${subject} to $final_dir; restoring the previous copy." >&2
  restore_publication_state "$backup_var" "$final_var" "$stage_var" "$subject"
  return "$publish_status"
}

reference_fetch_failed() {
  # One place that says what went wrong and what a human does about it, because a pinned
  # manifest that Steam no longer serves is not something a retry or another platform fixes.
  local status="$1"

  echo "Could not fetch the Valheim compile references for ${VALHEIM_TARGET}." >&2
  case "$status" in
    4)
      echo "Steam did not serve depot manifest ${VALHEIM_STEAM_DEPOTS} of app ${VALHEIM_STEAM_APP} (branch ${VALHEIM_STEAM_BRANCH})." >&2
      echo "This build is pinned to exactly those bytes and is not falling back to the branch head or to another platform." >&2
      ;;
    5)
      echo "The depot served bytes that do not match the hashes ${VALHEIM_TARGET} records." >&2
      ;;
    7)
      echo "The reference cache at ${SERVER_DIR} belongs to another fingerprint and is not owned by this script." >&2
      ;;
  esac
  echo "Recovery: re-pin with 'takaro-maint steam pin --game valheim --target ${VALHEIM_TARGET} --metadata --record-files <path> --write'," >&2
  echo "or recover the compile references by hand from the Windows depot recorded in the target's support notes." >&2
  echo "Expected reference directory: ${SERVER_DIR}/valheim_server_Data/Managed" >&2
  return "$status"
}

install_valheim_references() {
  local server_install_dir="$SERVER_DIR"
  local managed_dir stage_dir stage_managed_dir status=0 drifted=false

  if [[ "$server_install_dir" != /* ]]; then
    server_install_dir="$(pwd)/$server_install_dir"
  fi
  managed_dir="$server_install_dir/valheim_server_Data/Managed"

  if validate_managed_assemblies "$managed_dir"; then
    if references_match_pin "$managed_dir"; then
      echo "Reusing validated Valheim references read-only at $managed_dir."
      return 0
    fi
    echo "Cached $managed_dir/assembly_valheim.dll is not the one ${VALHEIM_TARGET} pins; re-fetching." >&2
    drifted=true
  fi

  if ! ensure_reference_cache_write_is_safe "$server_install_dir"; then
    return 1
  fi

  if ! stage_dir="$(mktemp -d "${server_install_dir}.stage.XXXXXX")"; then
    echo "Could not create a sibling Valheim staging directory for $server_install_dir." >&2
    return 1
  fi
  # shellcheck disable=SC2034 # read indirectly by the cleanup handler
  ACTIVE_SERVER_STAGE="$stage_dir"
  # The stage starts as a copy of whatever is there, so unrelated files a caller keeps
  # beside the references survive a re-fetch, and a cache left by another fingerprint is
  # visible to the fetch (and refused by it) rather than silently overwritten.
  if [ -d "$server_install_dir" ] && ! cp -a "$server_install_dir/." "$stage_dir/"; then
    echo "Could not stage the existing Valheim reference cache before the fetch: $server_install_dir" >&2
    return 1
  fi
  stage_managed_dir="$stage_dir/valheim_server_Data/Managed"
  # Drifted bytes are not repaired in place: the fetch is given an empty directory, so what
  # it publishes is the depot's content and nothing that was already there. Only the
  # assemblies go -- anything else a caller keeps in an owned cache is still staged above.
  if [ "$drifted" = true ]; then
    rm -rf "$stage_managed_dir"
  fi

  echo "Fetching the Valheim compile references pinned by ${VALHEIM_TARGET} (${VALHEIM_FP16})..."
  # `if ! cmd` would report the negation's status, not the tool's, and the exit code is
  # exactly what decides between a re-pin, a refusal and a retry here.
  status=0
  "$TAKARO_MAINT" steam references \
    --game valheim --target "$VALHEIM_TARGET" --dest "$stage_managed_dir" || status=$?
  # A cache left behind by another fingerprint is the one recoverable conflict, and only
  # when this script is the thing that wrote it.
  if [ "$status" -eq 7 ] && reference_cache_is_owned "$stage_dir"; then
    echo "Replacing a reference cache this script owns and re-fetching..."
    status=0
    "$TAKARO_MAINT" steam references \
      --game valheim --target "$VALHEIM_TARGET" --dest "$stage_managed_dir" --force || status=$?
  fi
  if [ "$status" -ne 0 ]; then
    reference_fetch_failed "$status"
    return "$status"
  fi

  if ! validate_managed_assemblies "$stage_managed_dir"; then
    return 1
  fi

  # The one assembly whose hash decides whether this plugin can be built at all. There is
  # no override: an override would assert nothing about the build it let through.
  if ! references_match_pin "$stage_managed_dir"; then
    echo "error: $stage_managed_dir/assembly_valheim.dll is not the one ${VALHEIM_TARGET} pins" >&2
    return 5
  fi

  if ! printf '%s\n' "$REFERENCE_CACHE_MARKER_CONTENT" > "$stage_dir/$REFERENCE_CACHE_MARKER_NAME"; then
    echo "Could not mark the validated Valheim reference cache complete: $stage_dir" >&2
    return 1
  fi

  if ! publish_directory "$stage_dir" "$server_install_dir" \
    ACTIVE_SERVER_BACKUP ACTIVE_SERVER_FINAL ACTIVE_SERVER_STAGE "Valheim reference cache"; then
    return 1
  fi
  echo "Valheim compile-reference cache installed for ${VALHEIM_TARGET}."
}

pack_download_failed() {
  local reason="$1"
  echo "Could not prepare BepInExPack ${VALHEIM_BEPINEX_PACKAGE} ${VALHEIM_BEPINEX_PACK_VERSION}: ${reason}" >&2
  echo "The pinned package is ${VALHEIM_BEPINEX_URL}" >&2
  echo "Nothing was replaced; any previously prepared pack at ${PACK_DIR} is unchanged." >&2
  echo "Recovery: re-record the package with 'takaro-maint catalog validate --online' after re-pinning the target." >&2
}

record_loader_version() {
  # The BepInEx loader's own assembly version -- the number a server operator sees in the
  # log banner -- as opposed to the pack version Thunderstore publishes. They are different
  # facts and the packaged manifest.json states both.
  local core_dir="$1" out_file="$2" version=""

  mkdir -p "$(dirname "$out_file")"
  if command -v dotnet >/dev/null 2>&1; then
    # The SDK writes unrelated chatter ("An issue was encountered verifying workloads") to
    # stdout, so the version is picked out by shape rather than by being the only line.
    version="$(dotnet msbuild "${SCRIPT_DIR}/bepinex-loader-version.proj" \
      -nologo -verbosity:minimal \
      -p:BepInExReferencePath="$core_dir" 2>/dev/null \
      | grep -oE '^[[:space:]]*[0-9]+(\.[0-9]+){1,3}[[:space:]]*$' | tail -n 1 | tr -d '[:space:]' || true)"
  fi
  case "$version" in
    [0-9]*) printf '%s\n' "$version" > "$out_file" ;;
    *) printf 'unknown\n' > "$out_file" ;;
  esac
}

# The pack is a third-party zip, so it is unpacked by a reader that refuses an entry
# escaping the staging directory. Python rather than unzip: takaro-maint already requires
# python3, and one dependency that is always there beats two that are not (this script also
# runs on a CI runner and inside the portable maintenance image).
extract_pack() {
    python3 - "$1" "$2" <<'PYTHON'
import os
import sys
import zipfile

archive, destination = sys.argv[1], sys.argv[2]
root = os.path.realpath(destination)
os.makedirs(root, exist_ok=True)
with zipfile.ZipFile(archive) as pack:
    for entry in pack.namelist():
        target = os.path.realpath(os.path.join(root, entry))
        if target != root and not target.startswith(root + os.sep):
            sys.exit(f"{entry} would be written outside {destination}")
    pack.extractall(root)
PYTHON
}

# What the pack says its own version is, straight out of the Thunderstore manifest.
read_pack_version() {
    python3 - "$1" <<'PYTHON'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("version_number", ""))
PYTHON
}

record_pack_checksums() {
  # Every file the pack publishes, hashed. `.takaro/` is this script's own bookkeeping and
  # is excluded: it is written after the download, so it is not part of what arrived.
  local pack_root="$1"
  ( cd "$pack_root" \
    && mkdir -p "$(dirname "$PACK_CHECKSUM_FILE")" \
    && find . -type f -not -path "./.takaro/*" -print0 \
      | LC_ALL=C sort -z \
      | xargs -0 -r sha256sum > "$PACK_CHECKSUM_FILE" )
}

pack_matches_pin() {
  # What the fetch path proved, re-proved: the tree still declares the pinned version and
  # still hashes as the checked zip's content. A pack that was corrupted, truncated or
  # hand-edited since is not reused, however managed its assemblies look.
  local pack_root="$1" declared

  [ -f "$pack_root/$PACK_CHECKSUM_FILE" ] || return 1
  [ -f "$pack_root/manifest.json" ] || return 1
  declared="$(read_pack_version "$pack_root/manifest.json" 2>/dev/null || true)"
  [ "$declared" = "$VALHEIM_BEPINEX_PACK_VERSION" ] || return 1
  ( cd "$pack_root" && sha256sum --check --status "$PACK_CHECKSUM_FILE" )
}

install_bepinex_pack() {
  local stage_dir archive manifest declared

  if [ -f "$PACK_CORE_DIR/BepInEx.dll" ] && validate_bepinex_assemblies "$PACK_CORE_DIR"; then
    if pack_matches_pin "$PACK_DIR"; then
      echo "Reusing the validated BepInExPack at $PACK_DIR."
      return 0
    fi
    echo "The prepared pack at $PACK_DIR is no longer the ${VALHEIM_BEPINEX_PACK_VERSION} zip ${VALHEIM_TARGET} pins; re-downloading." >&2
  fi

  if ! stage_dir="$(mktemp -d "${PACK_DIR}.stage.XXXXXX")"; then
    echo "Could not create a sibling BepInExPack staging directory for $PACK_DIR." >&2
    return 1
  fi
  ACTIVE_PACK_STAGE="$stage_dir"
  archive="$stage_dir/pack.zip"
  ACTIVE_PACK_ARCHIVE="$archive"

  echo "Downloading BepInExPack ${VALHEIM_BEPINEX_PACK_VERSION} for ${VALHEIM_TARGET}..."
  if ! curl_retry -fsSL "$VALHEIM_BEPINEX_URL" -o "$archive"; then
    pack_download_failed "the download failed"
    return 4
  fi

  if ! printf '%s  %s\n' "$VALHEIM_BEPINEX_SHA256" "$archive" | sha256sum --check --status; then
    pack_download_failed "the downloaded zip is not the sha256 the target records"
    return 5
  fi
  if [ -n "${VALHEIM_BEPINEX_SIZE:-}" ]; then
    local size
    size="$(wc -c < "$archive" | tr -d '[:space:]')"
    if [ "$size" != "$VALHEIM_BEPINEX_SIZE" ]; then
      pack_download_failed "the downloaded zip is ${size} bytes, the target records ${VALHEIM_BEPINEX_SIZE}"
      return 5
    fi
  fi

  if ! extract_pack "$archive" "$stage_dir/pack"; then
    pack_download_failed "the zip could not be extracted"
    return 5
  fi
  rm -f "$archive"
  ACTIVE_PACK_ARCHIVE=""

  manifest="$stage_dir/pack/manifest.json"
  if [ ! -f "$manifest" ]; then
    pack_download_failed "the zip carries no manifest.json; it is not a Thunderstore package"
    return 5
  fi
  declared="$(read_pack_version "$manifest" 2>/dev/null || true)"
  if [ "$declared" != "$VALHEIM_BEPINEX_PACK_VERSION" ]; then
    pack_download_failed "the pack says it is ${declared:-<unversioned>}, the target pins ${VALHEIM_BEPINEX_PACK_VERSION}"
    return 5
  fi

  if ! validate_bepinex_assemblies "$stage_dir/pack/BepInExPack_Valheim/BepInEx/core"; then
    return 1
  fi
  record_loader_version "$stage_dir/pack/BepInExPack_Valheim/BepInEx/core" "$stage_dir/pack/.takaro/loader-version"
  if ! record_pack_checksums "$stage_dir/pack"; then
    pack_download_failed "the extracted pack could not be checksummed"
    return 5
  fi

  if ! publish_directory "$stage_dir/pack" "$PACK_DIR" \
    ACTIVE_PACK_BACKUP ACTIVE_PACK_FINAL ACTIVE_PACK_STAGE "BepInExPack"; then
    return 1
  fi
  rm -rf "$stage_dir"
  # shellcheck disable=SC2034 # read indirectly by the cleanup handler
  ACTIVE_PACK_STAGE=""
  echo "BepInExPack ${VALHEIM_BEPINEX_PACK_VERSION} installed for ${VALHEIM_TARGET}."
}

install_valheim_references
install_bepinex_pack

echo "Reference assemblies ready:"
echo "  Target:  ${VALHEIM_TARGET} (${VALHEIM_FP16})"
echo "  Valheim: ${SERVER_DIR}/valheim_server_Data/Managed"
echo "  BepInEx: ${PACK_CORE_DIR}"
echo "  Loader:  $(cat "$LOADER_VERSION_FILE" 2>/dev/null || echo unknown)"
