#!/usr/bin/env bash
# The catalog target every Project Zomboid script builds against.
#
# Source it, do not run it. `zomboid_resolve_target [target-id]` exports the ZOMBOID_*
# keys `takaro-maint targets resolve` produces, so no script here hard-codes a game build,
# an image digest, a dependency URL, a jar hash or an artifact name.

zomboid_repo_root() {
    (cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
}

zomboid_resolve_target() {
    local target="${1:-}" repo_root env_file key value
    repo_root="$(zomboid_repo_root)"
    TAKARO_MAINT="${TAKARO_MAINT:-${repo_root}/maintenance/bin/takaro-maint}"
    env_file="$(mktemp)"
    local -a args=(targets resolve --game zomboid --format env --prefix ZOMBOID --out "$env_file")
    [ -n "$target" ] && args+=(--target "$target")
    if ! "$TAKARO_MAINT" "${args[@]}" >/dev/null; then
        rm -f "$env_file"
        echo "could not resolve the Zomboid catalog target ${target:-(the default)}" >&2
        return 2
    fi
    # Parsed, never sourced: nothing generated is executed.
    while IFS='=' read -r key value; do
        case "$key" in
            ZOMBOID_*) export "${key}=${value}" ;;
        esac
    done < "$env_file"
    rm -f "$env_file"
    export TAKARO_MAINT
}

# The flags every Zomboid script takes. Exports TARGET (empty = the game's default target)
# and EXTRA_GRADLE_ARGS (everything after `--`, for the release workflow's --rerun-tasks).
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
