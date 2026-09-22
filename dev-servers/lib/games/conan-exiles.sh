#!/usr/bin/env bash
# Conan Exiles: an exactly pinned Steam build plus the Takaro TypeScript sidecar.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 120 'conan-exiles|conan-exiles.yml|-|conan-exiles conan-bridge|12|6|sidecar|Conan Exiles + Takaro TypeScript sidecar|conan-exiles'

# The compose file mounts the install at /conan from here, and that is what the ledger
# describes; the rendered bridge config lives beside it under bridge/.
ds_target_dest_conan_exiles() { printf '%s/server' "$(ds_data_dir conan-exiles)"; }

install_conan_exiles() {
    local target dest
    target="$(ds_target conan-exiles)"
    dest="$(ds_target_dest conan-exiles)"

    ds_info "Resolving the catalog target for conan-exiles..."
    ds_write_target_env conan-exiles
    mkdir -p "$dest" "${DATA}/bridge"

    # The exact pinned build, by depot manifest: the game's Linux content and the
    # Steamworks redistributable it needs. No Steam client, no updater, no branch head.
    ds_info "Installing the pinned Conan Exiles build (${target}) into ${dest}..."
    ds_maint install --game conan-exiles --target "$target" --dest "$dest"

    # One image serves both containers, and the catalog pins it by digest.
    ds_info "Pulling the pinned image..."
    ds_compose conan-exiles pull

    # The sidecar is configured by a file; the server reads Game.ini, which the
    # entrypoint writes on first boot.
    ds_render_config conan-exiles
    mkdir -p "${dest}/ConanSandbox/Saved/Logs"

    "${DS_DIR}/scripts/deploy-connector.sh" conan-exiles
}

deploy_conan_exiles() {
    local target tmp
    target="$(ds_target conan-exiles)"
    ds_scratch_dir tmp

    ds_info "Building the Conan Exiles sidecar for ${target} (Node in the pinned image)..."
    ds_maint build --game conan-exiles --target "$target" \
        --version "$("${REPO_ROOT}/scripts/dev-version.sh" conan-exiles)" \
        --out "$tmp"
    ds_maint deploy --game conan-exiles --target "$target" \
        --dest "$(ds_target_dest conan-exiles)" \
        --from "${tmp}/build-manifest.json"
    ds_ok "$(ds_target_dest conan-exiles)/TakaroBridge/TakaroConanExiles"
}

ds_source_paths_conan_exiles() { echo "games/conan-exiles/bridge/src games/conan-exiles/bridge/package.json games/conan-exiles/bridge/tsconfig.json"; }
ds_success_pattern_conan_exiles() { echo "Identified with Takaro as gameServerId="; }
