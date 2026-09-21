#!/usr/bin/env bash
# Palworld: a third-party bridge image, pinned and checksummed.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 110 'palworld|palworld.yml|-|palworld palworld-bridge|12|6|bridge|Palworld + third-party Takaro bridge (REST only)|palworld'

install_palworld() {
    ds_info "Pulling the Palworld server image..."
    ds_compose palworld pull palworld
    mkdir -p "${DATA}/server" "${DATA}/bridge"

    # Render the bridge config BEFORE any compose up: this path is a bind-mounted
    # FILE, and Docker would silently create a directory in its place if missing.
    ds_render_config palworld

    "${DS_DIR}/scripts/deploy-connector.sh" palworld

    ds_info "First boot: downloading Palworld server files (~10 GB)..."
    ds_compose palworld up -d palworld
    ds_wait_for_file palworld "${DATA}/server/Pal" 2400 "Palworld server files" \
        || ds_die "Palworld server files never appeared; check 'dev-servers/scripts/logs.sh palworld'"
    cleanup_stop
    ds_fix_ownership "$DATA"
}

deploy_palworld() {
    # Third-party bridge; the image build downloads and checksums the pinned
    # release tarball. Nothing is built from this repo.
    ds_info "Building the third-party Palworld bridge image (pinned + checksummed)..."
    ds_compose palworld build palworld-bridge
    ds_ok "takaro-dev-palworld-bridge image"
}

ds_success_pattern_palworld() { echo "identify response from Takaro|Successfully identified with Takaro"; }
