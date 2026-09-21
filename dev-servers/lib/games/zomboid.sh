#!/usr/bin/env bash
# Project Zomboid: an exactly pinned Steam build plus a javaagent inside the server JVM.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 100 'zomboid|zomboid.yml|-|zomboid|8|16|connector|Project Zomboid B42 + Takaro javaagent|zomboid'

# The image mounts the install directory from here, and that is what the ledger describes.
ds_target_dest_zomboid() { printf '%s/server' "$(ds_data_dir zomboid)"; }

install_zomboid() {
    local target dest
    target="$(ds_target zomboid)"
    dest="$(ds_target_dest zomboid)"

    ds_info "Resolving the catalog target for zomboid..."
    ds_write_target_env zomboid
    mkdir -p "$dest" "${DATA}/config/Takaro"

    # The exact pinned build, by depot manifest. No SteamCMD, no first boot, no branch
    # head: the install also writes the SteamCMD stub the compose file mounts over the
    # image's own steamcmd directory, which is what stops the image replacing these bytes.
    ds_info "Installing the pinned Project Zomboid build (${target}) into ${dest}..."
    ds_maint install --game zomboid --target "$target" --dest "$dest"

    ds_info "Pulling the Project Zomboid server image..."
    ds_compose zomboid pull

    # The javaagent reads its config from compose env vars (TAKARO_*); there is no file to
    # render (an optional Zomboid/Takaro/TakaroConfig.txt can still exist, env wins).
    "${DS_DIR}/scripts/deploy-connector.sh" zomboid
}

deploy_zomboid() {
    local target tmp
    target="$(ds_target zomboid)"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' RETURN

    # Gradle always runs in the target's pinned JDK image, so there is no host-JDK branch.
    ds_info "Building the Zomboid agent for ${target} (Gradle in the pinned JDK image)..."
    ds_maint build --game zomboid --target "$target" \
        --version "$("${REPO_ROOT}/scripts/dev-version.sh" zomboid)" \
        --out "$tmp"
    ds_maint deploy --game zomboid --target "$target" \
        --dest "$(ds_target_dest zomboid)" \
        --from "${tmp}/build-manifest.json"
    ds_ok "$(ds_target_dest zomboid)/Takaro/TakaroConnector.jar"
}

ds_source_paths_zomboid() { echo "games/zomboid/mod/core games/zomboid/mod/agent games/zomboid/mod/gradle games/zomboid/mod/build.gradle.kts games/zomboid/mod/settings.gradle.kts games/zomboid/version.txt"; }
ds_success_pattern_zomboid() { echo "Identified successfully"; }
