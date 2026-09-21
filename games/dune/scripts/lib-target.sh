#!/usr/bin/env bash
# The catalog target every Dune: Awakening script builds against.
#
# Source it, do not run it. `dune_resolve_target [target-id]` exports the DUNE_* keys
# `takaro-maint targets resolve` produces, so no script here hard-codes a server build, an
# image digest, a dependency URL or an artifact name.

dune_repo_root() {
    (cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
}

dune_resolve_target() {
    local target="${1:-}" repo_root env_file key value
    repo_root="$(dune_repo_root)"
    TAKARO_MAINT="${TAKARO_MAINT:-${repo_root}/maintenance/bin/takaro-maint}"
    env_file="$(mktemp)"
    local -a args=(targets resolve --game dune --format env --prefix DUNE --out "$env_file")
    # An empty target resolves the game's default, which is what the frozen justfile recipe
    # (`build-release-dune <version> [out]`, no --target) depends on.
    [ -n "$target" ] && args+=(--target "$target")
    if ! "$TAKARO_MAINT" "${args[@]}" >/dev/null; then
        rm -f "$env_file"
        echo "could not resolve the Dune catalog target ${target:-(the default)}" >&2
        return 2
    fi
    # Parsed, never sourced: nothing generated is executed.
    while IFS='=' read -r key value; do
        case "$key" in
            DUNE_*) export "${key}=${value}" ;;
        esac
    done < "$env_file"
    rm -f "$env_file"
    export TAKARO_MAINT
}

# The one flag every Dune script takes. Exports TARGET (empty = the default).
dune_parse_target_flag() {
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
