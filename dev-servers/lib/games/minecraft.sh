#!/usr/bin/env bash
# Minecraft: four rig games on one compose file, one per catalog target or platform.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 20 'minecraft-paper|minecraft.yml|paper|paper|3|2|connector|Paper 1.21.x + Takaro Paper plugin|minecraft/paper'
ds_register 30 'minecraft-neoforge|minecraft.yml|neoforge|neoforge|3|2|connector|NeoForge 1.21.x + Takaro NeoForge mod|minecraft/neoforge'
ds_register 40 'minecraft-fabric|minecraft.yml|fabric|fabric|3|2|connector|Fabric (catalog target) + Takaro Fabric mod|minecraft/fabric'
ds_register 45 'minecraft-fabric-26.1.2|minecraft.yml|fabric-26-1-2|fabric-26-1-2|3|2|connector|Fabric 26.1.2 (catalog target) + Takaro Fabric mod|minecraft/fabric-26.1.2'

install_minecraft() {
    local platform="$1"
    mkdir -p "$DATA"

    if [ -n "$(ds_target "$GAME")" ]; then
        # Catalog-driven: the exact server jar, loader launcher and API jar the
        # connector was built against, verified by hash and recorded in a ledger.
        ds_info "Resolving the catalog target for ${GAME}..."
        ds_write_target_env "$GAME"
        ds_info "Installing pinned server files into ${DATA}..."
        ds_maint install --game minecraft --target "$(ds_target "$GAME")" --dest "$DATA"
    fi

    ds_info "Pulling the Minecraft server image..."
    ds_compose "$GAME" pull "$platform"
    # Minecraft env vars override the connector's file config, and compose
    # already supplies them, so there is nothing to render here either.
    "${DS_DIR}/scripts/deploy-connector.sh" "$GAME"
}

# One entry point per rig game; the shared step takes the platform it was registered with.
install_minecraft_paper() { install_minecraft paper; }
install_minecraft_neoforge() { install_minecraft neoforge; }
install_minecraft_fabric() { install_minecraft fabric; }
install_minecraft_fabric_26_1_2() { install_minecraft fabric-26-1-2; }

deploy_minecraft() {
    local platform="$1" dest jar subdir target tmp toolchain

    target="$(ds_target "minecraft-${platform}")"
    if [ -n "$target" ]; then
        # Catalog-driven: build the target's artifact and deploy it by manifest row, so
        # the jar in mods/ is the one the ledger records and nothing else is guessed at.
        toolchain=container
        if ds_have java && java -version 2>&1 | grep -qE '"(2[5-9]|[3-9][0-9])'; then
            toolchain=host
        fi
        tmp="$(mktemp -d)"
        trap 'rm -rf "$tmp"' RETURN
        ds_info "Building Minecraft target ${target} (${toolchain} toolchain)..."
        ds_maint build --game minecraft --target "$target" \
            --version "$("${REPO_ROOT}/scripts/dev-version.sh" minecraft)" \
            --out "$tmp" --toolchain "$toolchain"
        ds_maint deploy --game minecraft --target "$target" \
            --dest "$(ds_data_dir "minecraft-${platform}")" \
            --from "${tmp}/build-manifest.json"
        ds_ok "$(ds_data_dir "minecraft-${platform}")/mods"
        return 0
    fi

    # TODO(#151): paper and neoforge keep the old gradle+copy path until they
    # become catalog targets.
    ds_info "Building Minecraft ${platform} module (gradle)..."
    # Minecraft 26.2 (fabric) needs a JDK 25 toolchain; paper/neoforge still
    # target Java 21 class files but build fine on the same JDK 25.
    if ds_have java && java -version 2>&1 | grep -qE '"(2[5-9]|[3-9][0-9])'; then
        ( cd "${REPO_ROOT}/games/minecraft/mod" && ./gradlew ":${platform}:build" )
    else
        ds_info "No host JDK 25+ — building in eclipse-temurin:25-jdk"
        ds_toolchain_run eclipse-temurin:25-jdk "${REPO_ROOT}/games/minecraft/mod" \
            ./gradlew ":${platform}:build" --no-daemon
    fi

    jar="$(find "${REPO_ROOT}/games/minecraft/mod/${platform}/build/libs" \
        -name "takaro-${platform}-*.jar" \
        -not -name '*-dev-shadow*' -not -name '*-sources*' 2>/dev/null | head -1)"
    [ -n "$jar" ] || ds_die "no JAR built for ${platform}"

    # Paper loads plugins/, the mod loaders load mods/.
    [ "$platform" = "paper" ] && subdir="plugins" || subdir="mods"
    dest="$(ds_data_dir "minecraft-${platform}")/${subdir}"
    mkdir -p "$dest"
    cp "$jar" "${dest}/TakaroMinecraft.jar"
    ds_ok "${dest}/TakaroMinecraft.jar"
}

deploy_minecraft_paper() { deploy_minecraft paper; }
deploy_minecraft_neoforge() { deploy_minecraft neoforge; }
deploy_minecraft_fabric() { deploy_minecraft fabric; }
deploy_minecraft_fabric_26_1_2() { deploy_minecraft fabric-26.1.2; }

ds_source_paths_minecraft_paper() { echo "games/minecraft/mod/core games/minecraft/mod/paper games/minecraft/mod/gradle games/minecraft/mod/build.gradle.kts games/minecraft/mod/settings.gradle.kts"; }
ds_source_paths_minecraft_neoforge() { echo "games/minecraft/mod/core games/minecraft/mod/neoforge games/minecraft/mod/gradle games/minecraft/mod/build.gradle.kts games/minecraft/mod/settings.gradle.kts"; }
ds_source_paths_minecraft_fabric() { echo "games/minecraft/mod/core games/minecraft/mod/fabric games/minecraft/mod/targets games/minecraft/mod/buildSrc games/minecraft/mod/gradle games/minecraft/mod/build.gradle.kts games/minecraft/mod/settings.gradle.kts catalog/minecraft"; }
ds_source_paths_minecraft_fabric_26_1_2() { echo "games/minecraft/mod/core games/minecraft/mod/fabric games/minecraft/mod/targets games/minecraft/mod/buildSrc games/minecraft/mod/gradle games/minecraft/mod/build.gradle.kts games/minecraft/mod/settings.gradle.kts catalog/minecraft"; }

ds_success_pattern_minecraft_paper() { echo "Identified successfully"; }
ds_success_pattern_minecraft_neoforge() { echo "Identified successfully"; }
ds_success_pattern_minecraft_fabric() { echo "Identified successfully"; }
ds_success_pattern_minecraft_fabric_26_1_2() { echo "Identified successfully"; }

# A rig game id is not an env prefix: these are the names the compose files read.
ds_target_prefix_minecraft_paper() { printf 'MC_PAPER'; }
ds_target_prefix_minecraft_neoforge() { printf 'MC_NEOFORGE'; }
ds_target_prefix_minecraft_fabric() { printf 'MC_FABRIC'; }
ds_target_prefix_minecraft_fabric_26_1_2() { printf 'MC_FABRIC_26_1_2'; }
