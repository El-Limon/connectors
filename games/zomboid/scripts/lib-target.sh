#!/usr/bin/env bash
# The catalog target every Zomboid script builds against.
#
# Source it, do not run it. `zomboid_resolve_target [target-id]` exports the ZOMBOID_* keys
# `takaro-maint targets resolve` produces, so no script here hard-codes a game build, a
# JDK, an image digest, a dependency URL or an artifact name.
#
# The resolution itself is `scripts/lib/target.sh`, shared by every game; these are the
# Zomboid names for it, so nothing that sources this file has to change.

# shellcheck source=../../../scripts/lib/target.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/../../../scripts/lib/target.sh"

zomboid_repo_root() {
    takaro_repo_root
}

zomboid_resolve_target() {
    takaro_resolve_target zomboid ZOMBOID "${1:-}"
}

# Zomboid's own flags: the shared grammar plus everything after `--`, which the release
# workflow passes to Gradle as --rerun-tasks.
zomboid_parse_target_flag() {
    TARGET=""
    EXTRA_GRADLE_ARGS=""
    export TARGET EXTRA_GRADLE_ARGS
    while [ $# -gt 0 ]; do
        case "$1" in
            --target) shift; TARGET="${1:?--target needs a catalog target id}" ;;
            --target=*) TARGET="${1#--target=}" ;;
            --) shift; EXTRA_GRADLE_ARGS="$*"; break ;;
            *) echo "unknown argument '$1'" >&2; return 2 ;;
        esac
        shift
    done
}
