#!/usr/bin/env bash
# Valheim: BepInEx plus the Takaro plugin, built in a toolchain image.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 50 'valheim|valheim.yml|-|valheim|4|6|connector|Valheim + BepInEx + Takaro Valheim plugin|valheim'

install_valheim() {
    ds_info "Pulling the Valheim server image..."
    ds_compose valheim pull
    mkdir -p "${DATA}/config" "${DATA}/server"

    ds_info "First boot: downloading Valheim and installing BepInEx..."
    ds_compose valheim up -d
    ds_wait_for_file valheim "${DATA}/config/bepinex/BepInEx.cfg" 1800 "BepInEx install" \
        || ds_die "BepInEx never installed; check 'dev-servers/scripts/logs.sh valheim'"
    cleanup_stop
    ds_fix_ownership "$DATA"

    ds_render_config valheim

    "${DS_DIR}/scripts/deploy-connector.sh" valheim
}

ds_valheim_builder_image() {
    local tag="takaro-dev-valheim-builder"
    if ! docker image inspect "$tag" >/dev/null 2>&1; then
        ds_info "Building the Valheim toolchain image (one time)..." >&2
        docker build -q -t "$tag" "${DS_DIR}/images/valheim-builder" >&2
    fi
    printf '%s' "$tag"
}

deploy_valheim() {
    local stage dest version builder
    version="$(cat "${REPO_ROOT}/games/valheim/version.txt")"

    # The reference cache MUST stay separate from the runnable server:
    # games/valheim/scripts/setup-environment.sh refuses to write into a live install.
    stage="${REPO_ROOT}/games/valheim/_data/dist"
    mkdir -p "$stage"
    builder="$(ds_valheim_builder_image)"

    ds_info "Preparing Valheim compile references (SteamCMD + BepInEx)..."
    ds_toolchain_run "$builder" "${REPO_ROOT}/games/valheim" ./scripts/setup-environment.sh

    # Preferred path: the connector's own release script, which runs the full
    # test suite first and so also catches regressions.
    ds_info "Building Valheim connector v${version} via scripts/build-release.sh..."
    if ds_toolchain_run "$builder" "${REPO_ROOT}/games/valheim" \
           ./scripts/build-release.sh "$version" "$stage"; then
        rm -rf "${stage:?}/TakaroValheim"
        python3 -c '
import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as zf:
    zf.extractall(sys.argv[2])
' "${stage}/takaro-valheim-plugin.zip" "$stage"
    else
        # Fall back to publishing the dedicated-server plugin directly. A
        # dedicated server needs only the DLLs, not the release archives, so an
        # unrelated packaging/docs test failure should not block a dev server.
        ds_warn "build-release.sh failed. Falling back to a direct plugin publish."
        ds_warn "This SKIPS the connector's test suite — fix the failing test before a real release."
        rm -rf "${stage:?}/TakaroValheim"
        ds_toolchain_run "$builder" "${REPO_ROOT}/games/valheim" \
            dotnet publish src/Takaro.Valheim.Plugin/Takaro.Valheim.Plugin.csproj \
                -c Release -f net472 -o "${stage}/TakaroValheim" \
                -p:EnableValheimPluginBuild=true \
                -p:BepInExReferencePath="${REPO_ROOT}/games/valheim/_data/deps/bepinex/BepInExPack_Valheim/BepInEx/core" \
                -p:ValheimReferencePath="${REPO_ROOT}/games/valheim/_data/server/valheim_server_Data/Managed" \
                -p:TakaroValheimReleaseVersion="$version" \
                -p:TakaroValheimBepInExVersion="$version" \
                -p:Version="$version" \
            || ds_die "Valheim plugin build failed"
    fi

    dest="$(ds_data_dir valheim)/config/bepinex/plugins"
    mkdir -p "$dest"
    rm -rf "${dest:?}/TakaroValheim"
    cp -r "${stage}/TakaroValheim" "${dest}/TakaroValheim"
    # A stale chainloader cache makes BepInEx skip the updated plugin.
    rm -f "$(ds_data_dir valheim)/server/bepinex/BepInEx/cache/chainloader_typeloader.dat"
    ds_ok "${dest}/TakaroValheim"
}

ds_source_paths_valheim() { echo "games/valheim/mod/src games/valheim/version.txt"; }
ds_success_pattern_valheim() { echo "Takaro Valheim request received|Takaro Valheim response frame written"; }
