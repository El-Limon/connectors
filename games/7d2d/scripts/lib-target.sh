#!/usr/bin/env bash
# The catalog target every 7D2D script builds against.
#
# Source it, do not run it. `sevend2d_resolve_target [target-id]` exports the SEVEND2D_*
# keys `takaro-maint targets resolve` produces, so no script here hard-codes a game build,
# an image digest, a dependency URL or an artifact name.

sevend2d_repo_root() {
    (cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
}

sevend2d_resolve_target() {
    local target="${1:-}" repo_root env_file key value
    repo_root="$(sevend2d_repo_root)"
    TAKARO_MAINT="${TAKARO_MAINT:-${repo_root}/maintenance/bin/takaro-maint}"
    env_file="$(mktemp)"
    local -a args=(targets resolve --game 7d2d --format env --prefix SEVEND2D --out "$env_file")
    [ -n "$target" ] && args+=(--target "$target")
    if ! "$TAKARO_MAINT" "${args[@]}" >/dev/null; then
        rm -f "$env_file"
        echo "could not resolve the 7D2D catalog target ${target:-(the default)}" >&2
        return 2
    fi
    # Parsed, never sourced: nothing generated is executed.
    while IFS='=' read -r key value; do
        case "$key" in
            SEVEND2D_*) export "${key}=${value}" ;;
        esac
    done < "$env_file"
    rm -f "$env_file"
    export TAKARO_MAINT
}

# The one flag every 7D2D script takes. Exports TARGET (empty = the game's default target).
sevend2d_parse_target_flag() {
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
