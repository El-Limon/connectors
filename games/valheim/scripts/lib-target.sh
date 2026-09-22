#!/usr/bin/env bash
# The catalog target every Valheim script builds against.
#
# Source it, do not run it. `valheim_resolve_target [target-id]` exports the VALHEIM_* keys
# `takaro-maint targets resolve` produces, so no script here hard-codes a game build, a BepInEx version,
# an image digest, a dependency URL or an artifact name.
#
# The resolution itself is `scripts/lib/target.sh`, shared by every game; these are the
# Valheim names for it, so nothing that sources this file has to change.

# shellcheck source=../../../scripts/lib/target.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/../../../scripts/lib/target.sh"

valheim_repo_root() {
    takaro_repo_root
}

valheim_resolve_target() {
    takaro_resolve_target valheim VALHEIM "${1:-}"
}

# The one flag every Valheim script takes. Exports TARGET (empty = the game's default target).
valheim_parse_target_flag() {
    takaro_parse_target_flag "$@"
}

# The artifact file name for one role, with {version} substituted.
valheim_artifact_name() {
    local pattern="${1:?valheim_artifact_name <pattern> <version>}" version="${2:?}"
    printf '%s\n' "${pattern//\{version\}/$version}"
}
