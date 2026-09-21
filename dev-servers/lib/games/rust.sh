#!/usr/bin/env bash
# Rust: Carbon compiles the connector at runtime, so there is no build step.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 80 'rust|rust.yml|-|rust|8|12|connector|Rust + Carbon + TakaroConnector.cs|rust'

install_rust() {
    ds_info "Building the Rust server image (SteamCMD + Carbon, ~12 GB, slow)..."
    ds_compose rust build
    mkdir -p "${DATA}/plugins" "${DATA}/server" "${DATA}/carbon-logs"
    # Rust's connector is configured purely through environment variables, so
    # there is no config file to render.
    "${DS_DIR}/scripts/deploy-connector.sh" rust
}

deploy_rust() {
    local dest
    dest="$(ds_data_dir rust)/plugins"
    mkdir -p "$dest"
    # Carbon compiles the .cs at runtime — there is no build step.
    cp "${REPO_ROOT}/games/rust/mod/TakaroConnector.cs" "${dest}/TakaroConnector.cs"
    ds_ok "${dest}/TakaroConnector.cs"
}

ds_source_paths_rust() { echo "games/rust/mod games/rust/version.txt"; }
ds_success_pattern_rust() { echo "Identified and connected|Identified successfully"; }
