#!/usr/bin/env bash
# Stops games. With no arguments, stops everything — the safe default on a box
# that cannot run all of these at once.
#
# Usage: stop.sh [game...] [--down]
#   --down  also remove the containers (keeps _data; use to pick up compose edits)
set -euo pipefail
# shellcheck source=../lib/common.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)/common.sh"

DOWN=0
GAMES=()
for arg in "$@"; do
    case "$arg" in
        --down) DOWN=1 ;;
        --all)  ;;  # accepted for symmetry with start.sh; stopping all is the default
        -*)     ds_die "unknown option ${arg}" ;;
        *)      ds_validate_game "$arg"; GAMES+=("$arg") ;;
    esac
done
[ ${#GAMES[@]} -gt 0 ] || mapfile -t GAMES < <(ds_game_ids)

ds_load_env

for game in "${GAMES[@]}"; do
    if ! ds_is_running "$game" && [ "$DOWN" -eq 0 ]; then
        continue
    fi
    if [ "$DOWN" -eq 1 ] && ds_owns_file "$game"; then
        # The game owns its whole compose file, so `down` is safe here.
        ds_info "Removing ${game} containers"
        ds_compose "$game" down --remove-orphans >/dev/null 2>&1 || true
    else
        ds_info "Stopping ${game}"
        # shellcheck disable=SC2046  # service list is intentionally word-split
        ds_compose "$game" stop $(ds_services "$game") >/dev/null 2>&1 || true
        if [ "$DOWN" -eq 1 ]; then
            # Shared compose file (minecraft): only remove this game's services
            # so the other platforms in the same project survive.
            # shellcheck disable=SC2046
            ds_compose "$game" rm -f $(ds_services "$game") >/dev/null 2>&1 || true
        fi
    fi
    ds_ok "${game} stopped"
done

ds_info "Persistent data is untouched in dev-servers/_data/"
