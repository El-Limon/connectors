#!/usr/bin/env bash
# RuneScape: Dragonwilds — an LD_PRELOAD plugin and a TypeScript sidecar.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 70 'dragonwilds|dragonwilds.yml|-|dragonwilds dragonwilds-takaro|4|8|sidecar|RuneScape: Dragonwilds (Linux, app 4019830) + Takaro LD_PRELOAD plugin + TypeScript sidecar|dragonwilds-dev'

install_dragonwilds() {
    # The dev rig uses its own project name, container and data dir
    # (takaro-dev-dragonwilds / _data/dragonwilds-dev).
    mkdir -p "${DS_DATA}/dragonwilds-dev" "${DS_DATA}/dragonwilds-plugin" "${DS_DATA}/dragonwilds-sidecar"

    ds_info "Building the patched Dragonwilds server image (LD_PRELOAD on the game binary only)..."
    ds_compose dragonwilds build dragonwilds

    ds_info "First boot: downloading Dragonwilds server files (SteamCMD app 4019830, ~5.2 GB)..."
    ds_info "The first SteamCMD attempt often fails with 'Missing configuration'; the wrapper retries."
    ds_compose dragonwilds up -d dragonwilds

    # The .sym file (301 MB) is what the Takaro plugin resolves engine functions from, so it
    # is part of "installed", not an optional extra. Wait for the binary AND its symbols.
    local bin="${DS_DATA}/dragonwilds-dev/RSDragonwilds/Binaries/Linux/RSDragonwildsServer-Linux-Shipping"
    ds_wait_for_condition dragonwilds \
        "[ -s '${bin}' ] && [ -s '${bin}.sym' ]" \
        5400 "Dragonwilds server binary + .sym" \
        || ds_die "Dragonwilds server files never appeared; check 'dev-servers/scripts/logs.sh dragonwilds'"
    # SteamCMD writes the binary before it finalises the manifest; wait for StateFlags 4 too.
    ds_wait_for_condition dragonwilds \
        "ds_steam_app_ready '${DS_DATA}/dragonwilds-dev/steamapps/appmanifest_4019830.acf'" \
        1800 "Dragonwilds install manifest (fully installed)" \
        || ds_warn "appmanifest never reached StateFlags 4 — the server may re-validate on next boot"

    ds_fix_ownership "${DS_DATA}/dragonwilds-dev"

    # The plugin is built and deployed separately (deploy-connector.sh dragonwilds); it is a
    # no-op until the connector source tree exists.
    "${DS_DIR}/scripts/deploy-connector.sh" dragonwilds
}

DRAGONWILDS_SRC="${DRAGONWILDS_SRC:-${REPO_ROOT}/games/dragonwilds}"

deploy_dragonwilds() {
    local build="${DRAGONWILDS_SRC}/plugin/build.sh" dest="${DS_DATA}/dragonwilds-plugin"

    if [ ! -x "$build" ] && [ ! -f "$build" ]; then
        ds_warn "Dragonwilds plugin source not present yet (${build}) — nothing to deploy."
        ds_info "plugin source not present yet"
        return 0
    fi

    ds_info "Building libtakaro-dragonwilds.so..."
    ( cd "${DRAGONWILDS_SRC}/plugin" && bash ./build.sh ) || ds_die "Dragonwilds plugin build failed"
    local so="${DRAGONWILDS_SRC}/plugin/dist/libtakaro-dragonwilds.so"
    [ -f "$so" ] || ds_die "expected ${so} after build.sh"

    if [ -d "${DRAGONWILDS_SRC}/sidecar" ]; then
        ds_info "Rebuilding the Dragonwilds sidecar image..."
        ds_compose dragonwilds --profile sidecar build dragonwilds-takaro \
            || ds_die "Dragonwilds sidecar image build failed"
    else
        ds_warn "sidecar source not present yet (${DRAGONWILDS_SRC}/sidecar) — skipping its image build"
    fi

    # The running game holds the .so open via LD_PRELOAD, so it cannot be replaced in place:
    # stop → swap → start.
    local was_running=0
    if ds_is_running dragonwilds; then
        was_running=1
        ds_info "Stopping the rig to replace the plugin (the game process holds the .so open)..."
        ds_compose dragonwilds stop
    fi

    mkdir -p "$dest"
    cp "$so" "${dest}/libtakaro-dragonwilds.so.new"
    mv -f "${dest}/libtakaro-dragonwilds.so.new" "${dest}/libtakaro-dragonwilds.so"
    chmod 644 "${dest}/libtakaro-dragonwilds.so"
    ds_ok "${dest}/libtakaro-dragonwilds.so"

    if [ "$was_running" = "1" ]; then
        ds_info "Restarting the rig..."
        ds_compose dragonwilds start
    fi
}

ds_source_paths_dragonwilds() { echo "games/dragonwilds/mod games/dragonwilds/sidecar/src games/dragonwilds/version.txt"; }
