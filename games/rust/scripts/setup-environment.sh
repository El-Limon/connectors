#!/usr/bin/env bash
# Prepares everything the plugin is compile-checked against, for one catalog target:
# the pinned server assemblies (from the pinned Steam depot manifests, the Managed subset
# only) and the pinned Carbon assemblies (from the pinned release asset, by sha256).
#
# Usage: setup-environment.sh [--target <catalog target id>] [--force]
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

rust_parse_target_flag "$@"
rust_resolve_target "${TARGET}"

cd "${PROJECT_ROOT}"
echo "Setting up the Rust build environment for ${RUST_TARGET} (${RUST_FP16})..."

GAME_REFS="./_data/rust-binaries/${RUST_FP16}"
CARBON_REFS="./_data/carbon-refs/${RUST_FP16}"
mkdir -p ./_data

# Exactly the assemblies build.references selects, from the pinned depot manifests.
# Never the whole 6 GB depot set, and never a branch head.
"${TAKARO_MAINT}" steam references \
    --game rust --target "${RUST_TARGET}" --dest "${GAME_REFS}" ${FORCE:+--force}

# The one assembly whose hash decides whether this plugin can be built at all.
printf '%s  %s\n' "${RUST_ASSEMBLY_CSHARP_SHA256}" "${GAME_REFS}/Assembly-CSharp.dll" \
    | sha256sum --check --status \
    || { echo "error: ${GAME_REFS}/Assembly-CSharp.dll is not the one ${RUST_TARGET} pins" >&2; exit 5; }

# Carbon's own assemblies, from the pinned release asset. A directory left over from
# another fingerprint is refused rather than compiled against.
MARKER="${CARBON_REFS}/.takaro/carbon-references.json"
if [ -f "${MARKER}" ] && [ -z "${FORCE}" ]; then
    RECORDED=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["fingerprint"])' "${MARKER}")
    if [ "${RECORDED}" != "${RUST_FINGERPRINT}" ]; then
        echo "error: ${CARBON_REFS} holds the Carbon references for ${RECORDED:0:16}; this target is ${RUST_FP16}." >&2
        echo "       Stale Carbon reference cache. Pass --force to replace it." >&2
        exit 7
    fi
    echo "Carbon references for ${RUST_TARGET} are already in ${CARBON_REFS}"
else
    ARCHIVE="./_data/${RUST_CARBON_ASSET}"
    echo "Downloading ${RUST_CARBON_ASSET} (${RUST_CARBON_SHA256:0:16}...)"
    curl -fsSL "${RUST_CARBON_DOWNLOAD_URL}" -o "${ARCHIVE}"
    printf '%s  %s\n' "${RUST_CARBON_SHA256}" "${ARCHIVE}" \
        | sha256sum --check --status \
        || { echo "error: ${RUST_CARBON_ASSET} is not the archive ${RUST_TARGET} pins" >&2; exit 5; }

    rm -rf "${CARBON_REFS}"
    mkdir -p "${CARBON_REFS}"
    # Only carbon/managed/*.dll: the top-level framework assemblies the plugin links
    # against. The hooks, modules and native libraries are runtime-only.
    tar -xzf "${ARCHIVE}" -C "${CARBON_REFS}" --strip-components=2 \
        --wildcards --no-wildcards-match-slash --no-anchored 'carbon/managed/*.dll'
    rm -f "${ARCHIVE}"
    mkdir -p "${CARBON_REFS}/.takaro"
    CARBON_REFS="${CARBON_REFS}" RUST_FINGERPRINT="${RUST_FINGERPRINT}" \
    RUST_CARBON_ASSET="${RUST_CARBON_ASSET}" RUST_CARBON_SHA256="${RUST_CARBON_SHA256}" \
        python3 - <<'PY' > "${MARKER}"
import hashlib, json, os, pathlib
root = pathlib.Path(os.environ["CARBON_REFS"])
files = []
for path in sorted(p for p in root.glob("*.dll") if p.is_file()):
    payload = path.read_bytes()
    files.append({"path": path.name, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)})
print(json.dumps({
    "schemaVersion": 1,
    "fingerprint": os.environ["RUST_FINGERPRINT"],
    "asset": os.environ["RUST_CARBON_ASSET"],
    "sha256": os.environ["RUST_CARBON_SHA256"],
    "files": files,
}, indent=2))
PY
    echo "Carbon references: $(find "${CARBON_REFS}" -maxdepth 1 -name '*.dll' | wc -l) assemblies in ${CARBON_REFS}"
fi

echo "Environment ready: ${GAME_REFS} + ${CARBON_REFS}"
echo "Compile-check the plugin with: ./scripts/compile-check.sh --target ${RUST_TARGET}"
