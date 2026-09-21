#!/usr/bin/env bash
# Builds the 7D2D mod for one catalog target, stamps <version> into the staged ModInfo.xml
# and packages the Takaro mod folder deterministically as <out-dir>/<catalog artifact name>.
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
sevend2d_parse_target_flag "$@"
[ -n "${TARGET}" ] || { echo "7D2D is built per catalog target: pass --target <id>" >&2; exit 2; }
sevend2d_resolve_target "${TARGET}"

mkdir -p "${OUT_DIR}"
OUT_DIR=$(cd -- "${OUT_DIR}" && pwd)
ARTIFACT="${SEVEND2D_ARTIFACT/\{version\}/${VERSION}}"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git -C "${REPO_ROOT}" log -1 --format=%ct)}"

cd "${PROJECT_ROOT}"
# A release is built from nothing but the sources and the pinned inputs: msbuild's
# intermediate output decides what is copied into the package, so it never carries over
# from an earlier build of another target or version.
rm -rf ./mod/obj ./mod/bin ./_data/build

"${SCRIPT_DIR}/setup-environment.sh" --target "${SEVEND2D_TARGET}"
"${SCRIPT_DIR}/build-mod.sh" --target "${SEVEND2D_TARGET}"

STAGE="./_data/build/stage"
rm -rf "${STAGE}"
mkdir -p "${STAGE}"
cp -r ./_data/build/Mods/Takaro "${STAGE}/Takaro"
sed -i "s|<Version value=\"[^\"]*\" />|<Version value=\"${VERSION}\" />|" "${STAGE}/Takaro/ModInfo.xml"

# Packaged inside the toolchain image: the host has no zip, and the archive has to be
# byte-identical wherever it is built.
docker compose run --rm --build \
    --user "$(id -u):$(id -g)" \
    -e SOURCE_DATE_EPOCH \
    -e HOME=/tmp \
    -v "${OUT_DIR}:/out" \
    builder bash -c \
    ". /repo/scripts/lib/package.sh && pkg_zip /app/_data/build/stage Takaro /out/${ARTIFACT}"

# The identity the artifact carries: `takaro-maint artifact validate` reads this file,
# because a zip has no manifest to stamp.
cat > "${OUT_DIR}/${ARTIFACT}.meta.json" <<JSON
{
  "target": "${SEVEND2D_TARGET}",
  "fingerprint": "${SEVEND2D_FINGERPRINT}",
  "connectorVersion": "${VERSION}",
  "sourceRevision": "${TAKARO_SOURCE_REVISION:-$(git -C "${REPO_ROOT}" rev-parse HEAD)}",
  "game": "7d2d",
  "platform": "linux",
  "revision": "${SEVEND2D_REVISION}"
}
JSON

echo "  -> ${OUT_DIR}/${ARTIFACT}"
sha256sum "${OUT_DIR}/${ARTIFACT}"
