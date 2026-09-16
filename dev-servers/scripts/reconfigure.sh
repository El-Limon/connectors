#!/usr/bin/env bash
# Re-renders the Takaro runtime config for installed games. Use this after
# adding TAKARO_REGISTRATION_TOKEN to dev-servers/.env, or after changing any
# identity token or server name — no reinstall and no re-download needed.
#
# Usage: reconfigure.sh [game...]
#   with no arguments, reconfigures every installed game
set -euo pipefail
DS_LIB="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)"
# shellcheck source=../lib/common.sh
. "${DS_LIB}/common.sh"
# shellcheck source=../lib/render.sh
. "${DS_LIB}/render.sh"

GAMES=()
for arg in "$@"; do
    case "$arg" in
        -*) ds_die "unknown option ${arg}" ;;
        *)  ds_validate_game "$arg"; GAMES+=("$arg") ;;
    esac
done

ds_load_env
ds_require_token

if [ ${#GAMES[@]} -eq 0 ]; then
    while read -r g; do
        ds_is_installed "$g" && GAMES+=("$g")
    done < <(ds_game_ids)
fi
[ ${#GAMES[@]} -gt 0 ] || ds_die "no installed games to reconfigure (run install-all.sh first)"

ds_info "Reconfiguring: ${GAMES[*]}"
for game in "${GAMES[@]}"; do
    if ds_is_running "$game"; then
        ds_warn "${game} is running — restart it for the new config to take effect"
    fi
    ds_render_config "$game"
done

rm -f "${DS_DATA}/.needs-reconfigure"
echo
ds_ok "Done. Restart any running game so it picks up the new config."
