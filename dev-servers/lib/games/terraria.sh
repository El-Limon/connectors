#!/usr/bin/env bash
# Terraria: TShock plus the Takaro events plugin.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 10 'terraria|terraria.yml|-|terraria|1|1|plugin|TShock server + Takaro events plugin (Takaro connects over TShock REST)|terraria'

install_terraria() {
    ds_info "Pulling the TShock image..."
    ds_compose terraria pull
    mkdir -p "${DATA}/tshock" "${DATA}/worlds" "${DATA}/plugins"
    chmod -R 777 "${DATA}"

    ds_info "First boot: generating TShock config and the world..."
    ds_compose terraria up -d
    ds_wait_for_file terraria "${DATA}/tshock/config.json" 900 "TShock config" \
        || ds_die "TShock never wrote config.json; check 'dev-servers/scripts/logs.sh terraria'"
    cleanup_stop
    ds_fix_ownership "$DATA"

    # Takaro drives Terraria over the TShock REST API, not the plugin.
    ds_render_config terraria

    "${DS_DIR}/scripts/deploy-connector.sh" terraria
}

deploy_terraria() {
    local dest
    ds_info "Preparing TShock reference assemblies..."
    # games/terraria/scripts/build-mod.sh already falls back to a dotnet SDK container
    # when the host has no .NET 9 SDK, so no extra handling is needed here.
    ( cd "${REPO_ROOT}/games/terraria" && ./scripts/setup-environment.sh )
    ds_info "Building Terraria plugin..."
    ( cd "${REPO_ROOT}/games/terraria" && ./scripts/build-mod.sh )

    # The TShock image exposes /plugins as its -additionalplugins directory.
    dest="$(ds_data_dir terraria)/plugins"
    mkdir -p "$dest"
    cp "${REPO_ROOT}/games/terraria/_data/build/TakaroTerrariaEvents/TakaroTerrariaEvents.dll" "$dest/"
    ds_ok "${dest}/TakaroTerrariaEvents.dll"
}

ds_source_paths_terraria() { echo "games/terraria/mod/src games/terraria/version.txt"; }
ds_success_pattern_terraria() { echo "__NO_CONNECTOR__"; }
