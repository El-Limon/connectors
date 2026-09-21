#!/usr/bin/env bash
# The catalog target every Valheim script builds against.
#
# Source it, do not run it. `valheim_resolve_target [target-id]` exports the VALHEIM_*
# keys `takaro-maint targets resolve` produces, so no script here hard-codes a game build,
# a BepInEx version, an image digest, a dependency URL or an artifact name.

valheim_repo_root() {
    (cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)
}

valheim_resolve_target() {
    local target="${1:-}" repo_root env_file key value
    # Already resolved: the build re-execs itself inside the pinned .NET SDK image and hands
    # the resolved keys over as environment, because that image has no takaro-maint to run.
    if [ -n "${VALHEIM_TARGET:-}" ] && [ -n "${VALHEIM_FINGERPRINT:-}" ] &&
        { [ -z "$target" ] || [ "$target" = "$VALHEIM_TARGET" ]; }; then
        return 0
    fi
    repo_root="$(valheim_repo_root)"
    TAKARO_MAINT="${TAKARO_MAINT:-${repo_root}/maintenance/bin/takaro-maint}"
    env_file="$(mktemp)"
    local -a args=(targets resolve --game valheim --format env --prefix VALHEIM --out "$env_file")
    [ -n "$target" ] && args+=(--target "$target")
    if ! "$TAKARO_MAINT" "${args[@]}" >/dev/null; then
        rm -f "$env_file"
        echo "could not resolve the Valheim catalog target ${target:-(the default)}" >&2
        return 2
    fi
    # Parsed, never sourced: nothing generated is executed.
    while IFS='=' read -r key value; do
        case "$key" in
            VALHEIM_*) export "${key}=${value}" ;;
        esac
    done < "$env_file"
    rm -f "$env_file"
    export TAKARO_MAINT
}

# The one flag every Valheim script takes. Exports TARGET (empty = the game's default target).
valheim_parse_target_flag() {
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

# The artifact file name for one role, with {version} substituted.
valheim_artifact_name() {
    local pattern="${1:?valheim_artifact_name <pattern> <version>}" version="${2:?}"
    printf '%s\n' "${pattern//\{version\}/$version}"
}
