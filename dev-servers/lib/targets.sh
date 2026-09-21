#!/usr/bin/env bash
# ── Catalog targets ──────────────────────────────────────────────────────────
# A game whose server build is pinned by catalog/<game>/targets/<id>.json is installed,
# built and deployed through the maintenance command, so the rig, CI and a release all
# resolve the same bytes. Games without a target keep their own install path.
#
# Sourced by dev-servers/lib/common.sh. Per-game overrides (a prefix that cannot start
# with a digit, a data dir that is not the game's own) live in lib/games/<game>.sh.

ds_maint() { "${REPO_ROOT}/maintenance/bin/takaro-maint" "$@"; }

# ds_target <rig-game-id> -> the catalog target id driving it, empty when there is none.
ds_target() {
    ds_maint targets list --rig-game "$1" --format json 2>/dev/null \
        | python3 -c 'import json,sys
try:
    targets = json.load(sys.stdin)["targets"]
except Exception:
    targets = []
print(targets[0]["id"] if targets else "")'
}

# The catalog game a rig game belongs to (minecraft-fabric -> minecraft).
ds_target_game() { ds_dispatch_or "${1%%-*}" ds_target_game "$1"; }

ds_target_env_file() { printf '%s/.targets/%s.env' "$DS_DATA" "$1"; }

# Env key prefix for a rig game: minecraft-fabric -> MC_FABRIC, 7d2d -> SEVEND2D.
ds_target_prefix() {
    ds_dispatch_or "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')" ds_target_prefix "$1"
}

# The directory that holds the installed target; the game's data dir unless it says otherwise.
ds_target_dest() { ds_dispatch_or "$(ds_data_dir "$1")" ds_target_dest "$1"; }

# Resolve the target into the env file compose reads.
ds_write_target_env() {
    local game="$1" target
    target="$(ds_target "$game")"
    [ -n "$target" ] || return 0
    mkdir -p "${DS_DATA}/.targets"
    ds_maint targets resolve \
        --game "$(ds_target_game "$game")" \
        --target "$target" \
        --format env \
        --prefix "$(ds_target_prefix "$game")" \
        --out "$(ds_target_env_file "$game")"
}

# Refuse to start a target-driven game whose data dir does not hold that target.
ds_preflight_target() {
    local game="$1" target
    target="$(ds_target "$game")"
    [ -n "$target" ] || return 0
    if [ ! -f "$(ds_target_env_file "$game")" ]; then
        ds_die "${game} is driven by catalog target ${target} but has no resolved environment.
  dev-servers/scripts/install.sh ${game}"
    fi
    if ! ds_maint ledger check \
        --game "$(ds_target_game "$game")" \
        --target "$target" \
        --dest "$(ds_target_dest "$game")" >/dev/null; then
        ds_die "${game} does not hold catalog target ${target} (or its files changed).
  dev-servers/scripts/install.sh ${game}"
    fi
}
