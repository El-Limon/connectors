#!/usr/bin/env bash
# The catalog target every Rust script builds against.
#
# Source it, do not run it. `rust_resolve_target [target-id]` exports the RUST_* keys
# `takaro-maint targets resolve` produces, so no script here hard-codes a game build, a Carbon release,
# an image digest, a dependency URL or an artifact name.
#
# The resolution itself is `scripts/lib/target.sh`, shared by every game; these are the
# Rust names for it, so nothing that sources this file has to change.

# shellcheck source=../../../scripts/lib/target.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/../../../scripts/lib/target.sh"

rust_repo_root() {
    takaro_repo_root
}

rust_resolve_target() {
    takaro_resolve_target rust RUST "${1:-}"
}

# Rust's own flags: the shared grammar plus `--force`, which its setup script takes.
rust_parse_target_flag() {
    TARGET=""
    export TARGET
    while [ $# -gt 0 ]; do
        case "$1" in
            --target) shift; TARGET="${1:?--target needs a catalog target id}" ;;
            --target=*) TARGET="${1#--target=}" ;;
            --force) FORCE=1 ;;
            *) echo "unknown argument '$1'" >&2; return 2 ;;
        esac
        shift
    done
    export FORCE="${FORCE:-}"
}
