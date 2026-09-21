#!/usr/bin/env bash
# `docker compose` for the 7D2D build services, with the catalog target resolved first.
#
# The compose file mounts the target's reference directory by fingerprint, so every
# compose command needs SEVEND2D_* in the environment.
#
# Usage: compose.sh [--target <id>] <docker compose args...>
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

TARGET=""
if [ "${1:-}" = "--target" ]; then
    TARGET="${2:?--target needs a catalog target id}"
    shift 2
fi
sevend2d_resolve_target "${TARGET}"

cd "${PROJECT_ROOT}"
exec docker compose "$@"
