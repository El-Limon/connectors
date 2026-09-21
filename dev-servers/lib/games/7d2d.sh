#!/usr/bin/env bash
# 7 Days to Die: the Takaro mod folder on a SteamCMD-installed server.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 90 '7d2d|7d2d.yml|-|7d2d|8|32|connector|7 Days to Die + Takaro mod|7d2d'

install_7d2d() {
    ds_info "Pulling the 7 Days to Die server image..."
    ds_compose 7d2d pull
    mkdir -p "${DATA}/ServerFiles" "${DATA}/7DaysToDie" "${DATA}/log"
    chmod -R 777 "$DATA"

    ds_info "First boot: downloading 7D2D server files (~15 GB)..."
    ds_compose 7d2d up -d
    # Wait for SteamCMD to FINALISE the install, not merely to write the binary.
    # Stopping at first-file-seen leaves StateFlags at "update required", and
    # LinuxGSM then refuses to start the server on every subsequent boot.
    ds_wait_for_condition 7d2d \
        "ds_steam_app_ready '${DATA}/ServerFiles/steamapps/appmanifest_294420.acf'" \
        5400 "7D2D server files (fully installed)" \
        || ds_die "7D2D install never completed; check 'dev-servers/scripts/logs.sh 7d2d'"
    cleanup_stop
    ds_fix_ownership "$DATA"

    # The mod reads <server cwd>/Takaro/Config.xml and has no env-var config.
    ds_render_config 7d2d

    "${DS_DIR}/scripts/deploy-connector.sh" 7d2d
}

deploy_7d2d() {
    local dest
    ds_info "Preparing 7D2D build environment (SteamCMD + deps, first run is slow)..."
    ( cd "${REPO_ROOT}/games/7d2d" && ./scripts/setup-environment.sh )
    ds_info "Building 7D2D mod (Mono/MSBuild)..."
    ( cd "${REPO_ROOT}/games/7d2d" && ./scripts/build-mod.sh )

    dest="$(ds_data_dir 7d2d)/ServerFiles/Mods/Takaro"
    mkdir -p "$dest"
    cp -r "${REPO_ROOT}/games/7d2d/_data/build/Mods/Takaro/." "$dest/"
    ds_ok "$dest"
}

ds_source_paths_7d2d() { echo "games/7d2d/mod/src games/7d2d/mod/Takaro.csproj games/7d2d/mod/ModInfo.xml games/7d2d/version.txt"; }
ds_success_pattern_7d2d() { echo "Received WebSocket request"; }
