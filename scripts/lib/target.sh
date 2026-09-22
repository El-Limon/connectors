#!/usr/bin/env bash
# The catalog target a game's scripts build against, resolved once, for every game.
#
# Source it, do not run it. `takaro_resolve_target <game-id> <PREFIX> [target-id]` exports
# the `<PREFIX>_*` keys `takaro-maint targets resolve` produces, so no script anywhere
# hard-codes a game build, a framework version, an image digest, a dependency URL or an
# artifact name.
#
# This used to be seven byte-identical copies under `games/<g>/scripts/lib-target.sh`,
# which meant a fix to the resolution -- the temporary env file that outlived a failure,
# say -- landed in whichever copy the author happened to be in. Each of those files is now
# a shim over this one, keeping its `<g>_*` names so no consumer changes.

takaro_repo_root() {
    (cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
}

# takaro_resolve_target <game-id> <PREFIX> [target-id]
takaro_resolve_target() {
    local game="${1:?takaro_resolve_target <game-id> <PREFIX> [target-id]}" prefix="${2:?}" target="${3:-}"
    local repo_root env_file key value
    # Already resolved: a build that re-execs itself inside a pinned toolchain image hands
    # the resolved keys over as environment, because that image has no takaro-maint to run.
    local target_key="${prefix}_TARGET" fingerprint_key="${prefix}_FINGERPRINT"
    local resolved="${!target_key:-}" fingerprint="${!fingerprint_key:-}"
    if [ -n "$resolved" ] && [ -n "$fingerprint" ] && { [ -z "$target" ] || [ "$target" = "$resolved" ]; }; then
        return 0
    fi
    repo_root="$(takaro_repo_root)"
    TAKARO_MAINT="${TAKARO_MAINT:-${repo_root}/maintenance/bin/takaro-maint}"
    env_file="$(mktemp)"
    local -a args=(targets resolve --game "$game" --format env --prefix "$prefix" --out "$env_file")
    [ -n "$target" ] && args+=(--target "$target")
    if ! "$TAKARO_MAINT" "${args[@]}" >/dev/null; then
        rm -f "$env_file"
        echo "could not resolve the ${game} catalog target ${target:-(the default)}" >&2
        return 2
    fi
    # Parsed, never sourced: nothing generated is executed. A value may hold `=` of its
    # own -- a URL query, a dependency coordinate -- so only the first one splits.
    while IFS='=' read -r key value; do
        case "$key" in
            "${prefix}"_*) export "${key}=${value}" ;;
        esac
    done < "$env_file"
    rm -f "$env_file"
    export TAKARO_MAINT
}

# The one flag every game script takes. Exports TARGET (empty = the game's default target).
takaro_parse_target_flag() {
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
