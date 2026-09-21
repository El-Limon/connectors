#!/usr/bin/env bash
# Conan Exiles: a TypeScript sidecar beside the dedicated server.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 120 'conan-exiles|conan-exiles.yml|-|conan-exiles conan-bridge|12|6|sidecar|Conan Exiles + Takaro TypeScript sidecar|conan-exiles'

install_conan_exiles() {
    ds_info "Building the Conan Exiles server image..."
    ds_compose conan-exiles build conan-exiles
    mkdir -p "${DATA}/server" "${DATA}/bridge" "${DATA}/logs"

    ds_info "Downloading Conan Exiles game files (~35 GB, this takes a long time)..."
    # CONAN_INSTALL_ONLY makes the entrypoint exit after SteamCMD instead of
    # launching the server, so install stays a foreground, one-shot step.
    ds_compose conan-exiles run --rm \
        -e CONAN_INSTALL_ONLY=1 --no-deps conan-exiles
    ds_fix_ownership "$DATA"

    ds_render_config conan-exiles

    mkdir -p "${DATA}/server/ConanSandbox/Saved/Logs"
    "${DS_DIR}/scripts/deploy-connector.sh" conan-exiles
}

deploy_conan_exiles() {
    # The sidecar runs straight from the connector directory, which compose
    # bind-mounts read-only. Building here is the whole deploy.
    ds_info "Building Conan Exiles sidecar (npm)..."
    if ds_have npm; then
        ( cd "${REPO_ROOT}/games/conan-exiles/bridge" && npm ci && npm run build )
    else
        ds_info "No host Node — building in node:22-slim"
        ds_toolchain_run node:22-slim "${REPO_ROOT}/games/conan-exiles/bridge" \
            sh -c "npm ci && npm run build"
    fi
    [ -f "${REPO_ROOT}/games/conan-exiles/bridge/dist/index.js" ] || ds_die "games/conan-exiles/bridge/dist/index.js missing after build"
    ds_ok "${REPO_ROOT}/games/conan-exiles/bridge/dist (mounted read-only into the bridge container)"
}

ds_source_paths_conan_exiles() { echo "games/conan-exiles/bridge/src games/conan-exiles/bridge/package.json games/conan-exiles/bridge/tsconfig.json"; }
ds_success_pattern_conan_exiles() { echo "Identified with Takaro as gameServerId="; }
