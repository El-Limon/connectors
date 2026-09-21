#!/usr/bin/env bash
# Kept so a caller that still asks for "the bridge release" gets the whole set.
#
# Both roles are built and packaged together now — the plugin and the bridge are one
# target's artifacts and a release that carries one of them is incomplete — so this is a
# thin wrapper around build-release.sh rather than a second build path.
#
# Usage: build-bridge-release.sh <version> <out-dir> [--target <catalog target id>]
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

VERSION="${1:?usage: build-bridge-release.sh <version> <out-dir> [--target <id>]}"
OUT_DIR="${2:?usage: build-bridge-release.sh <version> <out-dir> [--target <id>]}"
shift 2

exec "${SCRIPT_DIR}/build-release.sh" "${VERSION}" "${OUT_DIR}" "$@"
