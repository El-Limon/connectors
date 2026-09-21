#!/usr/bin/env bash
# The catalog target every Enshrouded script builds against.
#
# Source it, do not run it. `enshrouded_resolve_target [target-id]` exports the ENSHROUDED_*
# keys `takaro-maint targets resolve` produces, so no script here hard-codes a game build,
# an image digest, a dependency URL or an artifact name.

enshrouded_repo_root() {
    (cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
}

enshrouded_resolve_target() {
    local target="${1:-}" repo_root env_file key value
    repo_root="$(enshrouded_repo_root)"
    TAKARO_MAINT="${TAKARO_MAINT:-${repo_root}/maintenance/bin/takaro-maint}"
    env_file="$(mktemp)"
    local -a args=(targets resolve --game enshrouded --format env --prefix ENSHROUDED --out "$env_file")
    [ -n "$target" ] && args+=(--target "$target")
    if ! "$TAKARO_MAINT" "${args[@]}" >/dev/null; then
        rm -f "$env_file"
        echo "could not resolve the Enshrouded catalog target ${target:-(the default)}" >&2
        return 2
    fi
    # Parsed, never sourced: nothing generated is executed.
    while IFS='=' read -r key value; do
        case "$key" in
            ENSHROUDED_*) export "${key}=${value}" ;;
        esac
    done < "$env_file"
    rm -f "$env_file"
    export TAKARO_MAINT
}

# The builder image: zig by hashed tarball on the pinned Node base, both from the catalog.
# Tagged with the target fingerprint, so a re-pinned target builds its own image.
enshrouded_builder_image() {
    local project_root
    project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
    BUILDER_IMAGE="takaro-enshrouded-builder:${ENSHROUDED_FP16:?resolve the target first}"
    docker build -q \
        -f "${project_root}/Dockerfile.builder" \
        --build-arg "ZIG_URL=${ENSHROUDED_DEP_ZIG_URL:?}" \
        --build-arg "ZIG_SHA256=${ENSHROUDED_DEP_ZIG_SHA256:?}" \
        -t "${BUILDER_IMAGE}" \
        "${project_root}" >/dev/null || return 1
    export BUILDER_IMAGE
}

# The one flag every Enshrouded script takes. Exports TARGET (empty = the game's default).
enshrouded_parse_target_flag() {
    TARGET=""
    export TARGET
    while [ $# -gt 0 ]; do
        case "$1" in
            --target) shift; TARGET="${1:?--target needs a catalog target id}" ;;
            --target=*) TARGET="${1#--target=}" ;;
            *) echo "unknown argument '$1'" >&2; return 2 ;;
        esac
        shift
    done
}
