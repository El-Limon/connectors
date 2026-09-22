#!/usr/bin/env bash
# The catalog target every Enshrouded script builds against.
#
# Source it, do not run it. `enshrouded_resolve_target [target-id]` exports the ENSHROUDED_* keys
# `takaro-maint targets resolve` produces, so no script here hard-codes a game build, a
# an image digest, a dependency URL or an artifact name.
#
# The resolution itself is `scripts/lib/target.sh`, shared by every game; these are the
# Enshrouded names for it, so nothing that sources this file has to change.

# shellcheck source=../../../scripts/lib/target.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/../../../scripts/lib/target.sh"

enshrouded_repo_root() {
    takaro_repo_root
}

enshrouded_resolve_target() {
    takaro_resolve_target enshrouded ENSHROUDED "${1:-}"
}

# The one flag every Enshrouded script takes. Exports TARGET (empty = the game's default target).
enshrouded_parse_target_flag() {
    takaro_parse_target_flag "$@"
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
