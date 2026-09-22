#!/usr/bin/env bash
# The catalog target every Terraria script builds against.
#
# Source it, do not run it. `terraria_resolve_target [target-id]` exports the TERRARIA_* keys
# `takaro-maint targets resolve` produces, so no script here hard-codes a game build, a
# an image digest, a dependency URL or an artifact name.
#
# The resolution itself is `scripts/lib/target.sh`, shared by every game; these are the
# Terraria names for it, so nothing that sources this file has to change.

# shellcheck source=../../../scripts/lib/target.sh
. "$(dirname -- "${BASH_SOURCE[0]}")/../../../scripts/lib/target.sh"

terraria_repo_root() {
    takaro_repo_root
}

terraria_resolve_target() {
    takaro_resolve_target terraria TERRARIA "${1:-}"
}

# The one flag every Terraria script takes. Exports TARGET (empty = the game's default target).
terraria_parse_target_flag() {
    takaro_parse_target_flag "$@"
}
