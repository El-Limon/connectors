#!/usr/bin/env bash
# Builds the Node bridge for one catalog target, in that target's pinned Node image.
#
# The pinned image is the same one `takaro-maint verify` runs the bridge in and the same
# one the dev rig starts, so "it compiled" and "it runs" are claims about one runtime.
# `npm ci` installs exactly what package-lock.json pins, integrity included.
#
# Usage: build-bridge.sh [--target <catalog target id>]
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
REPO_ROOT=$(cd -- "${PROJECT_ROOT}/../.." && pwd)
# shellcheck source=lib-target.sh
. "${SCRIPT_DIR}/lib-target.sh"

terraria_parse_target_flag "$@"
terraria_resolve_target "${TARGET}"

cd "${PROJECT_ROOT}/bridge"
rm -rf dist node_modules

# One container run, because `npm ci --omit=dev` has to happen after the tests and the
# compile: the release ships production dependencies only, but the tests need the dev ones.
docker run --rm \
    --user "$(id -u):$(id -g)" \
    -v "${REPO_ROOT}:${REPO_ROOT}" \
    -w "${REPO_ROOT}/games/terraria/bridge" \
    -e HOME=/tmp \
    -e npm_config_cache=/tmp/npm \
    "${TERRARIA_BRIDGE_IMAGE}" \
    sh -c 'npm ci && npm test && npm run build && npm ci --omit=dev'

echo "Built games/terraria/bridge/dist with production node_modules (${TERRARIA_BRIDGE_IMAGE})"
