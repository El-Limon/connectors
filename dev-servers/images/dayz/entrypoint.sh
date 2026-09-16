#!/usr/bin/env bash
# Prepares and launches the DayZ Linux dedicated server.
#
# Design notes:
#  * The 223350 depot needs a Steam account that OWNS DayZ; `login anonymous` is
#    refused. So the default path is "use whatever is already in /dayz" — you
#    download the depot elsewhere and copy it in. SteamCMD is
#    only attempted when STEAM_USER is set.
#  * serverDZ.cfg and battleye/beserver_x64.cfg are rendered only if absent, so a
#    hand-tuned config on the volume is never clobbered.
set -euo pipefail

INSTALL_DIR="${DAYZ_INSTALL_DIR:-/dayz}"
APP_ID="${DAYZ_APP_ID:-223350}"
PROFILE_DIR="${DAYZ_PROFILE_DIR:-${INSTALL_DIR}/profiles}"
BE_DIR="${DAYZ_BE_DIR:-${INSTALL_DIR}/battleye}"
CFG="${INSTALL_DIR}/serverDZ.cfg"

log() { printf '[dayz] %s\n' "$*"; }
die() { printf '[dayz] ERROR: %s\n' "$*" >&2; exit 1; }

# ── 1. Server files ──────────────────────────────────────────────────────────
if [ -n "${STEAM_USER:-}" ] && [ "${DAYZ_SKIP_UPDATE:-0}" != "1" ]; then
    log "STEAM_USER is set — running SteamCMD update for app ${APP_ID} into ${INSTALL_DIR}"
    # Password/Steam Guard come from an interactive first login that persists a
    # sentry in the steam home volume; we never bake credentials into the image.
    "${STEAMCMDDIR:-/home/steam/steamcmd}/steamcmd.sh" \
        +force_install_dir "${INSTALL_DIR}" \
        +login "${STEAM_USER}" ${STEAM_PASSWORD:+"${STEAM_PASSWORD}"} \
        +app_update "${APP_ID}" validate \
        +quit || die "SteamCMD update failed (owner account + Steam Guard required for app ${APP_ID})"
elif [ -n "${STEAM_USER:-}" ]; then
    log "STEAM_USER set but DAYZ_SKIP_UPDATE=1 — using the files already in ${INSTALL_DIR}"
else
    log "No STEAM_USER set — using pre-fetched server files in ${INSTALL_DIR}"
fi

if [ ! -f "${INSTALL_DIR}/DayZServer" ]; then
    die "${INSTALL_DIR}/DayZServer is missing.
  The DayZ server depot (app ${APP_ID}) cannot be downloaded with 'login anonymous':
  it requires a Steam account that owns DayZ (Bohemia issue T179224).
  Either:
    (a) pre-fetch the Linux depot elsewhere and place it in dev-servers/_data/dayz/server/, or
    (b) set STEAM_USER (and complete a one-time Steam Guard login) so this entrypoint
        can run 'steamcmd +login \$STEAM_USER +app_update ${APP_ID}'."
fi
chmod +x "${INSTALL_DIR}/DayZServer" 2>/dev/null || true

mkdir -p "${PROFILE_DIR}" "${BE_DIR}"

# ── 2. serverDZ.cfg ──────────────────────────────────────────────────────────
if [ -f "${CFG}" ]; then
    log "Keeping existing ${CFG} (enforcing env-driven keys)"
    # M2 fix: the file is kept, but the env-driven keys are re-applied on every
    # start. Otherwise a cfg rendered before DAYZ_PASSWORD existed silently keeps
    # password = "" and the .env value is ignored.
    set_cfg_str() { # key value  (value may contain / & \ -> escape for sed)
        local v; v="$(printf '%s' "$2" | sed -e 's/[\\/&]/\\&/g')"
        if grep -qE "^[[:space:]]*$1[[:space:]]*=" "${CFG}"; then
            sed -i -E "s/^[[:space:]]*$1[[:space:]]*=.*/$1 = \"${v}\";/" "${CFG}"
        else
            sed -i "1i $1 = \"${v}\";" "${CFG}"
        fi
    }
    set_cfg_num() {
        if grep -qE "^[[:space:]]*$1[[:space:]]*=" "${CFG}"; then
            sed -i -E "s/^[[:space:]]*$1[[:space:]]*=.*/$1 = $2;/" "${CFG}"
        else
            sed -i "1i $1 = $2;" "${CFG}"
        fi
    }
    set_cfg_str hostname "${DAYZ_HOSTNAME:-Takaro Dev DayZ}"
    set_cfg_str password "${DAYZ_PASSWORD:-}"
    set_cfg_str passwordAdmin "${DAYZ_ADMIN_PASSWORD:-}"
    set_cfg_num maxPlayers "${DAYZ_MAX_PLAYERS:-8}"
    set_cfg_num BattlEye "${DAYZ_BATTLEYE:-1}"
else
    log "Rendering ${CFG}"
    cat > "${CFG}" <<CFGEOF
hostname = "${DAYZ_HOSTNAME:-Takaro Dev DayZ}";
password = "${DAYZ_PASSWORD:-}";
passwordAdmin = "${DAYZ_ADMIN_PASSWORD:-}";
maxPlayers = ${DAYZ_MAX_PLAYERS:-8};

verifySignatures = 2;          // clients must run signed PBOs
forceSameBuild = 0;            // allow a client build that differs from the server's
disableVoN = 0;
vonCodecQuality = 20;
disable3rdPerson = 0;
disableCrosshair = 0;

serverTime = "SystemTime";
serverTimeAcceleration = 1;
serverNightTimeAcceleration = 1;
serverTimePersistent = 0;

guaranteedUpdates = 1;
loginQueueConcurrentPlayers = 5;
loginQueueMaxPlayers = 500;

instanceId = 1;
storageAutoFix = 1;

// Admin/diagnostic logging — the RPT/ADM logs are a game-side oracle for Takaro tests.
timeStampFormat = "Short";
logAverageFps = 1;
logMemory = 1;
logPlayers = 1;
logFile = "server_console.log";
adminLogPlayerHitsOnly = 0;
adminLogPlacement = 1;
adminLogBuildActions = 1;
adminLogPlayerList = 1;

BattlEye = ${DAYZ_BATTLEYE:-1};
steamQueryPort = ${DAYZ_QUERY_PORT:-27016};

class Missions
{
    class DayZ
    {
        template = "dayzOffline.chernarusplus";
    };
};
CFGEOF
    chmod 600 "${CFG}" || true
fi

# ── 3. BattlEye RCON ─────────────────────────────────────────────────────────
BE_CFG="${BE_DIR}/beserver_x64.cfg"
if [ -f "${BE_CFG}" ]; then
    log "Keeping existing ${BE_CFG}"
elif [ -n "${DAYZ_RCON_PASSWORD:-}" ]; then
    log "Rendering ${BE_CFG}"
    cat > "${BE_CFG}" <<BEEOF
RConPassword ${DAYZ_RCON_PASSWORD}
RConPort ${DAYZ_RCON_PORT:-2310}
RestrictRCon 0
BEEOF
    chmod 600 "${BE_CFG}" || true
    # DayZ regenerates beserver_x64.cfg from *_active_*.cfg on boot; some builds
    # read only the active file, so seed it too.
    cp "${BE_CFG}" "${BE_DIR}/beserver_x64_active_takaro.cfg" 2>/dev/null || true
    chmod 600 "${BE_DIR}/beserver_x64_active_takaro.cfg" 2>/dev/null || true
else
    log "WARNING: DAYZ_RCON_PASSWORD is empty — not rendering ${BE_CFG}; BE RCON will be unavailable."
fi

# ── 4. Launch ────────────────────────────────────────────────────────────────
cd "${INSTALL_DIR}"

ARGS=(
    "-config=$(basename "${CFG}")"
    "-port=${DAYZ_PORT:-2302}"
    "-profiles=${PROFILE_DIR}"
    "-bepath=${BE_DIR}"
    "-cpuCount=${DAYZ_CPU_COUNT:-4}"
)
if [ -n "${DAYZ_SERVERMOD:-}" ]; then
    ARGS+=("-servermod=${DAYZ_SERVERMOD}")
    if [ ! -d "${INSTALL_DIR}/${DAYZ_SERVERMOD}" ]; then
        log "WARNING: -mod=${DAYZ_SERVERMOD} but ${INSTALL_DIR}/${DAYZ_SERVERMOD} does not exist."
    fi
fi
# shellcheck disable=SC2206  # extra args are deliberately word-split
ARGS+=(${DAYZ_EXTRA_ARGS:--dologs -adminlog -netlog -freezecheck -limitFPS=60})

log "cwd=${INSTALL_DIR}"
log "Launching: ./DayZServer ${ARGS[*]}"
# M4 fix (2026-09-14): BattlEye prints "Config entry: RConPassword <pw>" to stdout,
# which leaked the RCON password into `docker logs`. Route the server's stdout and
# stderr through a line-buffered redactor. The process substitution runs sed as a
# child; `exec` below still replaces this shell, so DayZServer stays PID 1 and
# receives docker's signals directly.
exec > >(exec sed -u -E 's/(RConPassword)[[:space:]]+[^[:space:]]+/\1 <redacted>/I') 2>&1
exec ./DayZServer "${ARGS[@]}"
