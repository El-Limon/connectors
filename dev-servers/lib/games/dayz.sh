#!/usr/bin/env bash
# DayZ: the one game whose server files this rig cannot fetch itself.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 60 'dayz|dayz.yml|-|dayz dayz-takaro|6|4|sidecar|DayZ (Linux, app 223350) + @TakaroIntegration mod + Takaro TypeScript sidecar|dayz'

install_dayz() {
    local DATA="${DS_DATA}/dayz"
    mkdir -p "${DATA}/server" "${DATA}/profiles/TakaroIntegration" "${DATA}/bridge" "${DATA}/mod"

    # DayZ is the one game whose server files this rig cannot fetch itself: Steam
    # app 223350 refuses `login anonymous` (Bohemia T179224), so the Linux depot is
    # downloaded elsewhere with an account that owns DayZ and dropped into _data.
    if [ ! -f "${DATA}/server/DayZServer" ]; then
        ds_die "DayZ server files are missing from ${DATA}/server/.
  Steam app 223350 cannot be downloaded anonymously. Fetch the Linux depot with an
  account that owns DayZ and place it in ${DATA}/server/ (DayZServer must be at the top),
  or set STEAM_USER on the dayz service and let images/dayz/entrypoint.sh run SteamCMD."
    fi

    local mod_dir="${DAYZ_MOD_DIR:-../_data/dayz/mod/@takarointegration}"
    case "$mod_dir" in ../*) mod_dir="${DS_COMPOSE_DIR}/${mod_dir}" ;; esac
    [ -d "$mod_dir" ] || ds_warn "mod folder ${mod_dir} does not exist yet (DAYZ_MOD_DIR); the server will start without the Takaro mod."

    ds_info "Building the DayZ server image and the Takaro sidecar image..."
    ds_compose dayz build

    ds_fix_ownership "${DATA}"
}
