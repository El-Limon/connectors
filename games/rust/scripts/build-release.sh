#!/usr/bin/env bash
# Builds the Rust connector for one catalog target. Carbon compiles the plugin at load, so
# the released artifact is the .cs source itself; what "build" means here is:
#   1. prepare the pinned game and Carbon assemblies,
#   2. compile-check the source against them (the authoritative build for the fingerprint),
#   3. stage the source with <version> in its [Info] attribute and an identity header,
# under the catalog's own artifact name, with a .meta.json and a .provenance.json beside it.
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

# What goes into [Info(...)]. Oxide's VersionNumber -- which Carbon uses -- parses that
# string as three integers, so a dev or PR version ("0.0.5-dev.abc1234") throws inside the
# attribute's constructor and Carbon refuses the whole file with
# "Invalid plugin format in 'TakaroConnector.cs'". The numeric head is what the attribute
# gets; the exact build is in the artifact's name, its header and its .provenance.json, none
# of which any framework parses.
INFO_VERSION=$(printf '%s' "${VERSION}" | sed -E 's/^([0-9]+(\.[0-9]+)*).*$/\1/')
case "${INFO_VERSION}" in
  ''|*[!0-9.]*) INFO_VERSION="0.0.0" ;;
esac
# ... and it wants exactly three of them.
while [ "$(printf '%s' "${INFO_VERSION}" | tr -cd '.' | wc -c)" -lt 2 ]; do
    INFO_VERSION="${INFO_VERSION}.0"
done
INFO_VERSION=$(printf '%s' "${INFO_VERSION}" | cut -d. -f1-3)

cd "${PROJECT_ROOT}"
"${SCRIPT_DIR}/setup-environment.sh" --target "${RUST_TARGET}"
"${SCRIPT_DIR}/compile-check.sh" --target "${RUST_TARGET}"

SOURCE_REVISION="${TAKARO_SOURCE_REVISION:-$(git -C "${REPO_ROOT}" rev-parse HEAD)}"

# The identity the source itself carries, for anyone reading the file on a server. No
# timestamps: two builds of one commit have to be byte-identical.
{
    printf '// takaro-rust-plugin %s (plugin version %s) built for catalog target %s (Rust build %s, Carbon %s)\n' \
        "${VERSION}" "${INFO_VERSION}" "${RUST_TARGET}" "${RUST_STEAM_BUILDID}" "${RUST_CARBON_SHA256:0:16}"
    printf '// fingerprint %s | source %s | Assembly-CSharp %s | depots %s\n' \
        "${RUST_FINGERPRINT}" "${SOURCE_REVISION}" "${RUST_ASSEMBLY_CSHARP_SHA256}" "${RUST_STEAM_DEPOTS}"
    sed "s/\[Info(\"TakaroConnector\", \"Takaro\", \"[^\"]*\")\]/[Info(\"TakaroConnector\", \"Takaro\", \"${INFO_VERSION}\")]/" \
        mod/TakaroConnector.cs
} > "${OUT_DIR}/${ARTIFACT}"

# What this build was made from, in full. It is deliberately *not* the .meta.json:
# `takaro-maint build --out DIR` writes its own generic sidecar under that exact name when
# it copies the artifact into a release directory, so anything recorded only there is lost.
# The adapter carries this file along instead, and `publish assemble` re-derives the same
# pins into the compat record.
cat > "${OUT_DIR}/${ARTIFACT}.provenance.json" <<JSON
{
  "schemaVersion": 1,
  "target": "${RUST_TARGET}",
  "fingerprint": "${RUST_FINGERPRINT}",
  "connectorVersion": "${VERSION}",
  "pluginVersion": "${INFO_VERSION}",
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

# `takaro-maint artifact validate` reads this file: a .cs has no manifest to stamp. It says
# only which target these bytes belong to, because that is all a generic sidecar can also
# say -- the build's own pins are in the .provenance.json beside it.
cat > "${OUT_DIR}/${ARTIFACT}.meta.json" <<JSON
{
  "target": "${RUST_TARGET}",
  "fingerprint": "${RUST_FINGERPRINT}",
  "connectorVersion": "${VERSION}",
  "provenance": "${ARTIFACT}.provenance.json"
}
JSON

echo "  -> ${OUT_DIR}/${ARTIFACT}"
echo "  -> ${OUT_DIR}/${ARTIFACT}.provenance.json"
sha256sum "${OUT_DIR}/${ARTIFACT}"
