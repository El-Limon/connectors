#!/usr/bin/env bash
# Installs/updates Conan Exiles into the mounted volume, writes the RCON
# settings the Takaro sidecar needs, then launches the native Linux server.
set -euo pipefail

INSTALL_DIR="${CONAN_INSTALL_DIR:-/conan}"
APP_ID="${CONAN_APP_ID:-443030}"
CONFIG_DIR="${INSTALL_DIR}/ConanSandbox/Saved/Config/LinuxServer"

echo "[conan] Updating Steam app ${APP_ID} into ${INSTALL_DIR} (this takes a while on first run)..."
# force_install_dir must come BEFORE login (SteamCMD warns otherwise). The
# earlier "Missing file permissions" failure was caused by running as root,
# not by this ordering.
"${STEAMCMDDIR:-/home/steam/steamcmd}/steamcmd.sh" \
    +force_install_dir "${INSTALL_DIR}" \
    +login anonymous \
    +app_update "${APP_ID}" validate \
    +quit

if [ ! -f "${INSTALL_DIR}/ConanSandboxServer.sh" ]; then
    echo "[conan] ERROR: ConanSandboxServer.sh missing after SteamCMD update." >&2
    echo "[conan] The Linux dedicated-server build may not be available for this app." >&2
    exit 1
fi

chmod +x "${INSTALL_DIR}/ConanSandboxServer.sh" 2>/dev/null || true
mkdir -p "${CONFIG_DIR}"

# RCON is what the Takaro sidecar drives. RconMaxKarma is raised because Conan
# throttles repeated RCON, and the sidecar polls; see games/conan-exiles/README.md.
GAME_INI="${CONFIG_DIR}/Game.ini"
if ! grep -q '^\[RconPlugin\]' "${GAME_INI}" 2>/dev/null; then
    echo "[conan] Writing [RconPlugin] settings to ${GAME_INI}"
    cat >> "${GAME_INI}" <<INI

[RconPlugin]
RconEnabled=1
RconPassword=${RCON_PASSWORD}
RconPort=${CONAN_RCON_PORT:-25575}
RconMaxKarma=1000
INI
fi

if [ "${CONAN_INSTALL_ONLY:-0}" = "1" ]; then
    echo "[conan] CONAN_INSTALL_ONLY set — game files are in place, not launching."
    exit 0
fi

echo "[conan] Starting Conan Exiles dedicated server..."
cd "${INSTALL_DIR}"
exec ./ConanSandboxServer.sh \
    -log \
    -server \
    -nosteamclient \
    -Port="${CONAN_GAME_PORT:-7777}" \
    -QueryPort="${CONAN_QUERY_PORT:-27015}" \
    -RconEnabled=1 \
    -RconPassword="${RCON_PASSWORD}" \
    -RconPort="${CONAN_RCON_PORT:-25575}"
