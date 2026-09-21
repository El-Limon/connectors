#!/usr/bin/env bash
# The Rust server, as this repository runs it. Mounted read-only into the runtime
# container at /takaro/start.sh and named as the container's command; the installed tree
# (pinned Steam depots + pinned Carbon, put there by `takaro-maint install`) is at /rust.
#
# This script never installs, updates or downloads anything. If /rust is empty, that is an
# install that did not happen, not something to fix at boot.
set -euo pipefail

RUST_DIR="${RUST_DIR:-/rust}"

if [ ! -f "${RUST_DIR}/RustDedicated" ]; then
    echo "[Takaro] ${RUST_DIR}/RustDedicated is missing: install the catalog target first" >&2
    exit 2
fi
if [ ! -x "${RUST_DIR}/RustDedicated" ]; then
    # Steam's manifests carry no POSIX mode, so the installer sets this; a tree that was
    # unpacked some other way says so here rather than dying inside exec.
    echo "[Takaro] ${RUST_DIR}/RustDedicated is not executable: reinstall the catalog target" >&2
    exit 2
fi

# Carbon's loader: DOORSTOP_*, LD_PRELOAD and LD_LIBRARY_PATH, derived from its own location.
if [ -f "${RUST_DIR}/carbon/tools/environment.sh" ]; then
    # shellcheck disable=SC1091  # installed by takaro-maint, not tracked here
    . "${RUST_DIR}/carbon/tools/environment.sh"
    echo "[Takaro] Carbon environment loaded"
else
    echo "[Takaro] warning: ${RUST_DIR}/carbon/tools/environment.sh is missing; Carbon will not load" >&2
fi

# Unity writes into $HOME; the base image has no home directory for an arbitrary uid.
export HOME="${HOME:-${RUST_DIR}/takaro/home}"
mkdir -p "${HOME}"

ARGS=(
    -batchmode
    -nographics
    +server.port "${RUST_SERVER_PORT:-28015}"
    +rcon.port "${RUST_RCON_PORT:-28016}"
    +rcon.web 1
    +rcon.password "${RCON_PASSWORD:-takaro123}"
    +server.hostname "${RUST_SERVER_NAME:-Takaro Dev}"
    +server.seed "${RUST_SERVER_SEED:-12345}"
    +server.worldsize "${RUST_SERVER_WORLDSIZE:-1000}"
    +server.maxplayers "${RUST_SERVER_MAXPLAYERS:-10}"
    +server.identity "${RUST_SERVER_IDENTITY:-takaro}"
    +server.secure 0
    +server.encryption 0
)

echo "[Takaro] Starting Rust server..."
cd "${RUST_DIR}"
# exec, so the container's exit code is RustDedicated's: `shutdown` is verified on it.
exec "${RUST_DIR}/RustDedicated" "${ARGS[@]}" 2>&1
