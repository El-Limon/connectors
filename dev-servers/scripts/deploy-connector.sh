#!/usr/bin/env bash
# Builds a connector from this working tree using the repo's own build scripts
# and copies the artifact into dev-servers/_data/<game>/.
#
# Usage: deploy-connector.sh <game>
#
# Run this on its own after editing connector code — no reinstall needed.
# Most games then need a restart: dev-servers/scripts/start.sh <game>
set -euo pipefail
# shellcheck source=../lib/common.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)/common.sh"

GAME="${1:?usage: deploy-connector.sh <game>}"
ds_validate_game "$GAME"

ds_load_env

ds_dispatch deploy "$GAME"

# Record what was just deployed so sync-connectors.sh can detect future drift.
ds_record_fingerprint "$GAME"
