#!/usr/bin/env bash
# Project Zomboid: a javaagent inside the server JVM.
#
# Sourced by dev-servers/lib/common.sh. It registers the game in the shared registry and
# defines the steps install.sh, deploy-connector.sh and verify-connectors.sh dispatch to.

ds_register 100 'zomboid|zomboid.yml|-|zomboid|8|16|connector|Project Zomboid B42 + Takaro javaagent|zomboid'

install_zomboid() {
    ds_info "Pulling the Project Zomboid server image..."
    ds_compose zomboid pull
    mkdir -p "${DATA}/config/Takaro" "${DATA}/server"
    chmod -R 777 "$DATA" 2>/dev/null || true

    ds_info "First boot: downloading Project Zomboid B42 server files (SteamCMD 380870)..."
    ds_compose zomboid up -d
    # The agent compiles against the game jar; wait for SteamCMD to place it.
    ds_wait_for_condition zomboid \
        "[ -r '${DATA}/server/java/projectzomboid.jar' ]" \
        3600 "Project Zomboid server jar" \
        || ds_die "Zomboid server files never appeared; check 'dev-servers/scripts/logs.sh zomboid'"
    cleanup_stop
    ds_fix_ownership "$DATA"

    # The javaagent reads its config from compose env vars (TAKARO_*); there is
    # no file to render (an optional Zomboid/Takaro/TakaroConfig.txt can still
    # override, env wins).
    "${DS_DIR}/scripts/deploy-connector.sh" zomboid
}

deploy_zomboid() {
    local dest jar
    # The agent module compiles against the game classes (compileOnly); stage
    # the jar from the bind mount / running container first.
    ds_info "Staging the Project Zomboid server jar (games/zomboid/scripts/setup-environment.sh)..."
    ( cd "${REPO_ROOT}/games/zomboid" && ./scripts/setup-environment.sh )

    # PZ B42 is class-file v69 (Java 25); a JDK 21 javac cannot read the game
    # jar, so the agent builds on a JDK 25 toolchain.
    ds_info "Building Zomboid connector (:agent:shadowJar)..."
    if ds_have java && java -version 2>&1 | grep -qE '"(2[5-9]|[3-9][0-9])'; then
        ( cd "${REPO_ROOT}/games/zomboid/mod" && ./gradlew :agent:shadowJar --no-daemon )
    else
        ds_info "No host JDK 25+ — building in eclipse-temurin:25-jdk"
        ds_toolchain_run eclipse-temurin:25-jdk "${REPO_ROOT}/games/zomboid/mod" \
            ./gradlew :agent:shadowJar --no-daemon
    fi

    jar="$(find "${REPO_ROOT}/games/zomboid/mod/agent/build/libs" \
        -name 'TakaroConnector-*.jar' -not -name '*-sources*' 2>/dev/null | head -1)"
    [ -n "$jar" ] || ds_die "no agent jar built for zomboid"

    # The agent lives in the persistent cache-dir mount (Zomboid/Takaro); the
    # install dir is reverted by SteamCMD validate on every start.
    dest="$(ds_data_dir zomboid)/config/Takaro"
    mkdir -p "$dest"
    cp "$jar" "${dest}/TakaroConnector.jar"
    ds_ok "${dest}/TakaroConnector.jar"
}

ds_source_paths_zomboid() { echo "games/zomboid/mod/core games/zomboid/mod/agent games/zomboid/mod/gradle games/zomboid/mod/build.gradle.kts games/zomboid/mod/settings.gradle.kts games/zomboid/version.txt"; }
