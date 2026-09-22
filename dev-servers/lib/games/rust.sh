#!/usr/bin/env bash
# Rust: an exactly pinned Steam build plus the pinned Carbon framework, and a plugin
# Carbon compiles at load.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.
#
# The install is one tree under _data/rust/rust_dedicated, described by the ledger.
# An existing rig keeps its saves with
#   dev-servers/scripts/install.sh rust --force
#   mv dev-servers/_data/rust/server/takaro dev-servers/_data/rust/rust_dedicated/server/takaro

ds_register 80 'rust|rust.yml|-|rust|8|12|connector|Rust + Carbon + TakaroConnector.cs|rust'

# The container mounts the whole installed tree at /rust, and that is what the ledger
# describes: the Steam depots, Carbon, the plugin and the world saves under server/.
ds_target_dest_rust() { printf '%s/rust_dedicated' "$(ds_data_dir rust)"; }

install_rust() {
    local target dest
    target="$(ds_target rust)"
    dest="$(ds_target_dest rust)"

    ds_info "Resolving the catalog target for rust..."
    ds_write_target_env rust
    mkdir -p "$dest"

    # The exact pinned build, by depot manifest, plus the pinned Carbon archive by its
    # sha256. Nothing here updates from the Steam branch head or downloads Carbon's
    # moving release alias, and the runtime image installs nothing at all.
    ds_info "Installing the pinned Rust build and Carbon (${target}) into ${dest}..."
    ds_maint install --game rust --target "$target" --dest "$dest"

    ds_info "Pulling the Rust runtime image..."
    ds_compose rust pull

    # The connector is configured purely through environment variables, so there is no
    # config file to render.
    "${DS_DIR}/scripts/deploy-connector.sh" rust
}

deploy_rust() {
    local target tmp
    target="$(ds_target rust)"
    ds_scratch_dir tmp

    # "Building" a Rust connector is compile-checking the source against the pinned game
    # and Carbon assemblies and stamping the version into it; Carbon compiles the result.
    ds_info "Building the Rust plugin for ${target} (compile-check in the pinned SDK image)..."
    ds_maint build --game rust --target "$target" \
        --version "$("${REPO_ROOT}/scripts/dev-version.sh" rust)" \
        --out "$tmp"
    ds_maint deploy --game rust --target "$target" \
        --dest "$(ds_target_dest rust)" \
        --from "${tmp}/build-manifest.json"
    ds_ok "$(ds_target_dest rust)/carbon/plugins/TakaroConnector.cs"
}

ds_source_paths_rust() { echo "games/rust/mod games/rust/version.txt"; }
ds_success_pattern_rust() { echo "Identified successfully"; }
