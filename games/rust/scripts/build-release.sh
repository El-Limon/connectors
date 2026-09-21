#!/usr/bin/env bash
# Builds the Rust connector for one catalog target. Carbon compiles the plugin at load, so
# the released artifact is the .cs source itself; what "build" means here is:
#   1. prepare the pinned game and Carbon assemblies,
#   2. compile-check the source against them (the authoritative build for the fingerprint),
#   3. stage the source with <version> in its [Info] attribute and an identity header,
# under the catalog's own artifact name, with a .meta.json beside it.
#
# Usage: build-release.sh <version> <out-dir> [--target <catalog target id>]
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
REPO_ROOT=$(cd -- "${PROJECT_ROOT}/../.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

VERSION="${1:?usage: build-release.sh <version> <out-dir> [--target <id>]}"
OUT_DIR="${2:?usage: build-release.sh <version> <out-dir> [--target <id>]}"
shift 2
rust_parse_target_flag "$@"
rust_resolve_target "${TARGET}"

# The version reaches a sed program and a JSON document, so it is checked once here rather
# than escaped twice.
case "${VERSION}" in
  *[!A-Za-z0-9._+-]*|"")
    echo "refusing version '${VERSION}': use letters, digits and . _ + - only" >&2
    exit 2
    ;;
esac

mkdir -p "${OUT_DIR}"
OUT_DIR=$(cd -- "${OUT_DIR}" && pwd)
ARTIFACT="${RUST_ARTIFACT/\{version\}/${VERSION}}"

cd "${PROJECT_ROOT}"
"${SCRIPT_DIR}/setup-environment.sh" --target "${RUST_TARGET}"
"${SCRIPT_DIR}/compile-check.sh" --target "${RUST_TARGET}"

SOURCE_REVISION="${TAKARO_SOURCE_REVISION:-$(git -C "${REPO_ROOT}" rev-parse HEAD)}"

# The identity the source itself carries, for anyone reading the file on a server. No
# timestamps: two builds of one commit have to be byte-identical.
{
    printf '// takaro-rust-plugin %s built for catalog target %s (Rust build %s, Carbon %s)\n' \
        "${VERSION}" "${RUST_TARGET}" "${RUST_STEAM_BUILDID}" "${RUST_CARBON_SHA256:0:16}"
    printf '// fingerprint %s | source %s | Assembly-CSharp %s | depots %s\n' \
        "${RUST_FINGERPRINT}" "${SOURCE_REVISION}" "${RUST_ASSEMBLY_CSHARP_SHA256}" "${RUST_STEAM_DEPOTS}"
    sed "s/\[Info(\"TakaroConnector\", \"Takaro\", \"[^\"]*\")\]/[Info(\"TakaroConnector\", \"Takaro\", \"${VERSION}\")]/" \
        mod/TakaroConnector.cs
} > "${OUT_DIR}/${ARTIFACT}"

# `takaro-maint artifact validate` reads this file: a .cs has no manifest to stamp.
cat > "${OUT_DIR}/${ARTIFACT}.meta.json" <<JSON
{
  "target": "${RUST_TARGET}",
  "fingerprint": "${RUST_FINGERPRINT}",
  "connectorVersion": "${VERSION}",
  "sourceRevision": "${SOURCE_REVISION}",
  "game": "rust",
  "platform": "carbon",
  "revision": "${RUST_REVISION}",
  "carbon": {
    "tag": "${RUST_CARBON_TAG}",
    "asset": "${RUST_CARBON_ASSET}",
    "sha256": "${RUST_CARBON_SHA256}",
    "size": ${RUST_CARBON_SIZE}
  },
  "gameBuild": {
    "app": ${RUST_STEAM_APP},
    "branch": "${RUST_STEAM_BRANCH}",
    "buildid": ${RUST_STEAM_BUILDID},
    "depots": "${RUST_STEAM_DEPOTS}"
  },
  "assemblyCSharpSha256": "${RUST_ASSEMBLY_CSHARP_SHA256}",
  "toolchain": "${RUST_TOOLCHAIN}"
}
JSON

echo "  -> ${OUT_DIR}/${ARTIFACT}"
sha256sum "${OUT_DIR}/${ARTIFACT}"
