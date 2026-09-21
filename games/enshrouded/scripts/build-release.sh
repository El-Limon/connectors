#!/usr/bin/env bash
# Builds both Enshrouded connector components for one catalog target and packages them
# deterministically as <out-dir>/<catalog artifact name> plus a .meta.json each.
#
#   TakaroEnshrouded/          the native dbghelp.dll proxy plugin the game server loads
#   TakaroEnshroudedSidecar/   the Node sidecar that speaks the Takaro protocol
#
# Usage: build-release.sh <version> <out-dir> --target <catalog target id>
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
REPO_ROOT=$(cd -- "${PROJECT_ROOT}/../.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

VERSION="${1:?usage: build-release.sh <version> <out-dir> --target <id>}"
OUT_DIR="${2:?usage: build-release.sh <version> <out-dir> --target <id>}"
shift 2
enshrouded_parse_target_flag "$@"
[ -n "${TARGET}" ] || { echo "Enshrouded is built per catalog target: pass --target <id>" >&2; exit 2; }
enshrouded_resolve_target "${TARGET}"

# The version reaches a JSON document, a file name and a command inside the builder
# container, so it is checked once here rather than escaped three times.
case "${VERSION}" in
  *[!A-Za-z0-9._+-]*|"")
    echo "refusing version '${VERSION}': use letters, digits and . _ + - only" >&2
    exit 2
    ;;
esac

mkdir -p "${OUT_DIR}"
OUT_DIR=$(cd -- "${OUT_DIR}" && pwd)
PLUGIN_ZIP="${ENSHROUDED_ARTIFACT_SERVER_PLUGIN/\{version\}/${VERSION}}"
SIDECAR_ZIP="${ENSHROUDED_ARTIFACT_SIDECAR/\{version\}/${VERSION}}"
export PLUGIN_ZIP SIDECAR_ZIP VERSION
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "${REPO_ROOT}" log -1 --format=%ct)}"

# A release is built from nothing but the sources and the pinned inputs, so no object file
# or dist/ from an earlier build of another target or version can reach the archive.
rm -rf "${PROJECT_ROOT}/mod/build" "${PROJECT_ROOT}/mod/build-debug" \
       "${PROJECT_ROOT}/sidecar/dist" "${PROJECT_ROOT}/_data/build"

echo "Building the Enshrouded connector ${VERSION} for ${ENSHROUDED_TARGET} (${ENSHROUDED_FP16})..."
enshrouded_builder_image || { echo "the pinned builder image did not build" >&2; exit 6; }

# Everything is compiled and packaged inside the toolchain image: the host has neither zig
# nor zip, and the archives have to be byte-identical wherever they are built. The names
# are handed over as environment entries and quoted inside the container, so the program
# the container runs is the same one whatever the version string contains.
docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp \
    -e ZIG=/opt/zig/zig \
    -e SOURCE_DATE_EPOCH \
    -e VERSION \
    -e PLUGIN_ZIP \
    -e SIDECAR_ZIP \
    -e npm_config_cache=/tmp/.npm \
    -v "${REPO_ROOT}:/repo" \
    -v "${OUT_DIR}:/out" \
    -w /repo/games/enshrouded \
    "${BUILDER_IMAGE}" bash -euo pipefail -c '
      ./mod/build.sh
      cd sidecar && npm ci --no-audit --no-fund && npm run build && cd ..

      STAGE=_data/build/stage
      rm -rf "$STAGE"
      mkdir -p "$STAGE/TakaroEnshrouded" "$STAGE/TakaroEnshroudedSidecar"

      cp mod/build/dbghelp.dll "$STAGE/TakaroEnshrouded/"
      sed "s/@VERSION@/${VERSION}/g" scripts/templates/plugin-README.txt \
        > "$STAGE/TakaroEnshrouded/README.txt"

      cp -R sidecar/dist "$STAGE/TakaroEnshroudedSidecar/dist"
      rm -rf "$STAGE/TakaroEnshroudedSidecar/dist/__tests__" "$STAGE/TakaroEnshroudedSidecar/dist/testing"
      cp sidecar/package.json sidecar/package-lock.json sidecar/.env.example \
         "$STAGE/TakaroEnshroudedSidecar/"
      # The source .dockerignore excludes dist/, which is exactly what the release build
      # copies in, so the zip carries its own.
      cp sidecar/.dockerignore.release "$STAGE/TakaroEnshroudedSidecar/.dockerignore"
      # The zip ships the release Dockerfile AS Dockerfile, so "docker build ." inside the
      # unpacked folder works: the source Dockerfile compiles from src/, which the zip has not got.
      cp sidecar/Dockerfile.release "$STAGE/TakaroEnshroudedSidecar/Dockerfile"
      sed "s/@VERSION@/${VERSION}/g" scripts/templates/sidecar-README.release.txt \
        > "$STAGE/TakaroEnshroudedSidecar/README.release.txt"

      . /repo/scripts/lib/package.sh
      pkg_zip "$STAGE" TakaroEnshrouded "/out/${PLUGIN_ZIP}"
      pkg_zip "$STAGE" TakaroEnshroudedSidecar "/out/${SIDECAR_ZIP}"
    '

# The identity each artifact carries: `takaro-maint artifact validate` reads these files,
# because a zip has no manifest to stamp.
SOURCE_REVISION="${TAKARO_SOURCE_REVISION:-$(git -C "${REPO_ROOT}" rev-parse HEAD)}"
for artifact in "${PLUGIN_ZIP}" "${SIDECAR_ZIP}"; do
  cat > "${OUT_DIR}/${artifact}.meta.json" <<JSON
{
  "target": "${ENSHROUDED_TARGET}",
  "fingerprint": "${ENSHROUDED_FINGERPRINT}",
  "connectorVersion": "${VERSION}",
  "sourceRevision": "${SOURCE_REVISION}",
  "game": "enshrouded",
  "platform": "proton",
  "revision": "${ENSHROUDED_REVISION}"
}
JSON
  echo "  -> ${OUT_DIR}/${artifact}"
  sha256sum "${OUT_DIR}/${artifact}"
done
