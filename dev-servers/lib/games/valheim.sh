#!/usr/bin/env bash
# Valheim: an exactly pinned Steam build plus the pinned BepInExPack and the Takaro plugin.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 50 'valheim|valheim.yml|-|valheim|4|6|connector|Valheim + BepInEx + Takaro Valheim plugin|valheim'

# The image mounts the game directory from here, and that is what the ledger describes.
# Worlds live outside it (saves/), so a re-install never touches them.
ds_target_dest_valheim() { printf '%s/server' "$(ds_data_dir valheim)"; }

install_valheim() {
    local target dest data
    target="$(ds_target valheim)"
    dest="$(ds_target_dest valheim)"
    data="$(ds_data_dir valheim)"

    ds_info "Resolving the catalog target for valheim..."
    ds_write_target_env valheim
    mkdir -p "$dest" "${data}/saves" "${data}/backups" "${data}/config/bepinex"

    # The exact pinned build by depot manifest, with the pinned BepInExPack unpacked into
    # it. No SteamCMD, no Thunderstore 'latest', no first-boot download: the image's own
    # installers are switched off in compose and find everything already in place.
    ds_info "Installing the pinned Valheim build and BepInExPack (${target}) into ${dest}..."
    ds_maint install --game valheim --target "$target" --dest "$dest"

    ds_info "Pulling the Valheim server image..."
    ds_compose valheim pull

    # The connector is configured by a BepInEx .cfg and has no env-var configuration;
    # compose bind-mounts this one file into the container.
    ds_render_config valheim

    "${DS_DIR}/scripts/deploy-connector.sh" valheim
}

deploy_valheim() {
    local target tmp
    target="$(ds_target valheim)"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN

    ds_info "Building both Valheim roles for ${target} (.NET SDK in the pinned image)..."
    # --toolchain container is what the build really does (the script re-execs into the
    # pinned SDK image), and it is what build-manifest.json records.
    ds_maint build --game valheim --target "$target" \
        --version "$("${REPO_ROOT}/scripts/dev-version.sh" valheim)" \
        --out "$tmp" --toolchain container
    ds_maint deploy --game valheim --target "$target" \
        --dest "$(ds_target_dest valheim)" \
        --from "${tmp}/build-manifest.json"
    ds_ok "$(ds_target_dest valheim)/BepInEx/plugins/TakaroValheim"
}

ds_source_paths_valheim() { echo "games/valheim/mod/src games/valheim/version.txt"; }
ds_success_pattern_valheim() { echo "Takaro Valheim request received|Takaro Valheim response frame written"; }
