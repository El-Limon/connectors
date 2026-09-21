#!/usr/bin/env bash
# 7 Days to Die: an exactly pinned Steam build plus the Takaro mod folder.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 90 '7d2d|7d2d.yml|-|7d2d|8|32|connector|7 Days to Die + Takaro mod|7d2d'

# A variable name cannot start with a digit, so this game's env prefix is spelled out.
ds_target_prefix_7d2d() { printf 'SEVEND2D'; }

# The image mounts serverfiles/ from here, and that is what the ledger describes.
ds_target_dest_7d2d() { printf '%s/ServerFiles' "$(ds_data_dir 7d2d)"; }

install_7d2d() {
    local target dest
    target="$(ds_target 7d2d)"
    dest="$(ds_target_dest 7d2d)"

    ds_info "Resolving the catalog target for 7d2d..."
    ds_write_target_env 7d2d
    mkdir -p "$dest" "${DATA}/7DaysToDie" "${DATA}/log"

    # The exact pinned build, by depot manifest. No SteamCMD, no first boot, no branch
    # head: the install writes DONT_REMOVE.txt, which is what stops the image's own
    # LinuxGSM auto-install from ever replacing these bytes.
    ds_info "Installing the pinned 7D2D build (${target}) into ${dest}..."
    ds_maint install --game 7d2d --target "$target" --dest "$dest"

    ds_info "Pulling the 7 Days to Die server image..."
    ds_compose 7d2d pull

    # The mod reads <server cwd>/Takaro/Config.xml and has no env-var config.
    ds_render_config 7d2d

    "${DS_DIR}/scripts/deploy-connector.sh" 7d2d
}

deploy_7d2d() {
    local target tmp
    target="$(ds_target 7d2d)"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN

    ds_info "Building the 7D2D mod for ${target} (Mono/MSBuild in the pinned image)..."
    ds_maint build --game 7d2d --target "$target" \
        --version "$("${REPO_ROOT}/scripts/dev-version.sh" 7d2d)" \
        --out "$tmp"
    ds_maint deploy --game 7d2d --target "$target" \
        --dest "$(ds_target_dest 7d2d)" \
        --from "${tmp}/build-manifest.json"
    ds_ok "$(ds_target_dest 7d2d)/Mods/Takaro"
}

ds_source_paths_7d2d() { echo "games/7d2d/mod/src games/7d2d/mod/Takaro.csproj games/7d2d/mod/ModInfo.xml games/7d2d/version.txt catalog/7d2d"; }
ds_success_pattern_7d2d() { echo "Received WebSocket request"; }
