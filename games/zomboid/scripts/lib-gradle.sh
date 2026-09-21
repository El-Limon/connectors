#!/usr/bin/env bash
# Running Gradle for one catalog target, always inside that target's pinned JDK image.
#
# The agent compiles against class-file v69 game classes, so a host JDK is not an option
# we can promise; `zomboid_gradle` therefore has no host branch. Source it after
# lib-target.sh, with the ZOMBOID_* keys already exported.

# zomboid_gradle <repo-root> <gradle args...>
zomboid_gradle() {
    local repo_root="$1"; shift
    local cache="${TAKARO_MAINT_CACHE:-${HOME}/.cache/takaro-maint}"
    mkdir -p "${cache}/gradle" "${cache}/home"
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        -v "${repo_root}:${repo_root}" \
        -v "${cache}:${cache}" \
        -w "${repo_root}/games/zomboid/mod" \
        -e GRADLE_USER_HOME="${cache}/gradle" \
        -e HOME="${cache}/home" \
        -e SOURCE_DATE_EPOCH \
        "${ZOMBOID_TOOLCHAIN}" \
        ./gradlew --no-daemon -Dorg.gradle.workers.max=2 "$@"
}

# The -P properties that carry the target's identity into the build.
zomboid_gradle_target_properties() {
    local repo_root="$1" version="$2"
    printf '%s\n' \
        "-PtakaroTarget=${ZOMBOID_TARGET}" \
        "-PtakaroFingerprint=${ZOMBOID_FINGERPRINT}" \
        "-PtakaroGameJar=${repo_root}/games/zomboid/_data/references/${ZOMBOID_FP16}/projectzomboid.jar" \
        "-PtakaroGameJarSha256=${ZOMBOID_GAME_JAR_SHA256}" \
        "-PtakaroGameVersion=${ZOMBOID_REVISION}" \
        "-PtakaroJavaRelease=${ZOMBOID_JAVA}" \
        "-PtakaroDepSha256_byte_buddy=${ZOMBOID_DEP_BYTE_BUDDY_SHA256}" \
        "-PtakaroDepSha256_java_websocket=${ZOMBOID_DEP_JAVA_WEBSOCKET_SHA256}" \
        "-PtakaroDepSha256_gson=${ZOMBOID_DEP_GSON_SHA256}" \
        ${version:+"-PtakaroArtifactName=${ZOMBOID_ARTIFACT/\{version\}/${version}}"}
}
