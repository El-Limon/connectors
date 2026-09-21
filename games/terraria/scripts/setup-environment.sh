#!/usr/bin/env bash
# Extracts the assemblies the plugin compiles against out of the exact image the catalog
# target pins, and refuses anything that is not what it pins.
#
# The references are the image's own files, by digest, not a download and not a tag: the
# plugin is compiled against the same bytes the server will run, and `takaro-maint verify`
# re-hashes them inside the booted container.
#
# Usage: setup-environment.sh [--target <catalog target id>] [--force]
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

FORCE=""
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1 ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done
terraria_parse_target_flag "${ARGS[@]+"${ARGS[@]}"}"
terraria_resolve_target "${TARGET}"

cd "${PROJECT_ROOT}"
REFS="_data/refs/${TERRARIA_FP16}"
MARKER="${REFS}/.takaro/references.json"

IFS=';' read -r -a REFERENCE_PATHS <<< "${TERRARIA_REFERENCES}"

# The hash the catalog pins for one reference path, by its file name. The key grammar is
# the adapter's: upper-case the dependency name and replace every other character with _.
reference_sha256() {
    local name key
    name="$(basename "$1")"
    key="TERRARIA_DEP_$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')_SHA256"
    printf '%s\n' "${!key:-}"
}

references_intact() {
    local path expected
    for path in "${REFERENCE_PATHS[@]}"; do
        expected="$(reference_sha256 "$path")"
        [ -n "$expected" ] || return 1
        [ -f "${REFS}/$(basename "$path")" ] || return 1
        printf '%s  %s\n' "$expected" "${REFS}/$(basename "$path")" | sha256sum --check --status || return 1
    done
}

if [ -f "${MARKER}" ] && [ -z "${FORCE}" ]; then
    marker_fp=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' "${MARKER}")
    if [ "${marker_fp}" != "${TERRARIA_FINGERPRINT}" ]; then
        echo "error: stale reference cache in games/terraria/${REFS}: it holds ${marker_fp:0:16}," >&2
        echo "       not ${TERRARIA_TARGET} (${TERRARIA_FP16}); pass --force to replace it" >&2
        exit 7
    fi
    if references_intact; then
        echo "References for ${TERRARIA_TARGET} (${TERRARIA_FP16}) are up-to-date: games/terraria/${REFS}"
        exit 0
    fi
fi

rm -rf "${REFS}"
mkdir -p "${REFS}/.takaro"

# By digest. A wrong or withdrawn digest fails here; there is no tag to fall back to,
# because a fallback would compile the plugin against bytes nobody pinned.
if ! container=$(docker create "${TERRARIA_IMAGE}" 2>&1); then
    echo "error: docker could not create a container from ${TERRARIA_IMAGE}" >&2
    echo "       ${container}" >&2
    echo "       not falling back to a floating tag" >&2
    exit 4
fi
cleanup() { docker rm -f "${container}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

for path in "${REFERENCE_PATHS[@]}"; do
    docker cp "${container}:${path}" "${REFS}/$(basename "$path")"
done

# The catalog says what each assembly must be. There is no override: an override asserts
# nothing about the build it would let through.
for path in "${REFERENCE_PATHS[@]}"; do
    expected="$(reference_sha256 "$path")"
    if [ -z "${expected}" ]; then
        echo "error: the catalog target ${TERRARIA_TARGET} pins no sha256 for ${path}" >&2
        exit 5
    fi
    if ! printf '%s  %s\n' "${expected}" "${REFS}/$(basename "$path")" | sha256sum --check --status; then
        echo "error: ${REFS}/$(basename "$path") is not the one ${TERRARIA_TARGET} pins" >&2
        exit 5
    fi
done

python3 - "${MARKER}" "${TERRARIA_FINGERPRINT}" "${TERRARIA_IMAGE}" "${REFS}" "${TERRARIA_REFERENCES}" <<'PY'
import hashlib, json, os, sys

marker, fingerprint, image, refs, references = sys.argv[1:6]
files = []
for path in references.split(";"):
    name = os.path.basename(path)
    blob = open(os.path.join(refs, name), "rb").read()
    files.append({"path": name, "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)})
document = {"fingerprint": fingerprint, "image": image, "files": files}
with open(marker, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

echo "References ready for ${TERRARIA_TARGET} (${TERRARIA_FP16}): games/terraria/${REFS}"
echo "Build the plugin with: ./scripts/build-mod.sh --target ${TERRARIA_TARGET}"
