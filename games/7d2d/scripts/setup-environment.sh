#!/usr/bin/env bash
# Prepares everything the mod compiles against, for one catalog target:
# the pinned server assemblies (from the pinned Steam depot manifests, subset only)
# and the pinned third-party dependencies.
#
# Usage: setup-environment.sh [--target <catalog target id>]
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

sevend2d_parse_target_flag "$@"
sevend2d_resolve_target "${TARGET}"

cd "${PROJECT_ROOT}"
echo "Setting up the 7D2D build environment for ${SEVEND2D_TARGET} (${SEVEND2D_FP16})..."

mkdir -p ./_data/build ./_data/lib
REFERENCES="./_data/7dtd-binaries/${SEVEND2D_FP16}"

# Exactly the assemblies build.references selects, from the pinned depot manifests.
# Never the whole depot, and never a branch head.
"${TAKARO_MAINT}" steam references \
    --game 7d2d --target "${SEVEND2D_TARGET}" --dest "${REFERENCES}"

# The pinned third-party dependencies, verified against the catalog before they are used.
# --build keeps the toolchain image in step with Dockerfile.builder; the layers are cached,
# so an unchanged Dockerfile costs a second.
docker compose run --rm --build --user "$(id -u):$(id -g)" -e HOME=/tmp deps

# The one assembly whose hash decides whether this mod can be built at all. The catalog
# says what it must be; there is no override, because an override asserts nothing about
# the build it would let through.
printf '%s  %s\n' "${SEVEND2D_ASSEMBLY_CSHARP_SHA256}" "${REFERENCES}/Assembly-CSharp.dll" \
    | sha256sum --check --status \
    || { echo "error: ${REFERENCES}/Assembly-CSharp.dll is not the one ${SEVEND2D_TARGET} pins" >&2; exit 5; }

echo "Environment ready: ${REFERENCES}"
echo "Build the mod with: ./scripts/build-mod.sh --target ${SEVEND2D_TARGET}"
