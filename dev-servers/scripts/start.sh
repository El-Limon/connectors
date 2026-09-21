#!/usr/bin/env bash
# Starts one or more games, refusing to blow the RAM budget.
#
# Usage: start.sh <game>... [--force]
#        start.sh --all --force
#
# This box does not have the memory to run every game at once, so --all is
# refused unless you explicitly force it.
set -euo pipefail
# shellcheck source=../lib/common.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)/common.sh"

FORCE=0
ALL=0
GAMES=()
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --all)   ALL=1 ;;
        -*)      ds_die "unknown option ${arg}" ;;
        *)       ds_validate_game "$arg"; GAMES+=("$arg") ;;
    esac
done

ds_load_env
ds_require_token

if [ "$ALL" -eq 1 ]; then
    if [ "$FORCE" -ne 1 ]; then
        ds_die "--all would start every game at once and exhaust this box's RAM.
  Start the ones you need: start.sh minecraft-paper valheim
  Or override deliberately: start.sh --all --force"
    fi
    mapfile -t GAMES < <(ds_game_ids)
fi

[ ${#GAMES[@]} -gt 0 ] || ds_die "usage: start.sh <game>... [--force]
  Games: $(ds_game_ids | tr '\n' ' ')"

# ── RAM budget ───────────────────────────────────────────────────────────────
BUDGET="${DEV_SERVERS_RAM_BUDGET_GB:-40}"
running_ram=0
running_list=()
for g in $(ds_game_ids); do
    if ds_is_running "$g"; then
        running_ram=$((running_ram + $(ds_ram_gb "$g")))
        running_list+=("$g")
    fi
done

wanted_ram=0
for g in "${GAMES[@]}"; do
    ds_is_running "$g" && continue
    wanted_ram=$((wanted_ram + $(ds_ram_gb "$g")))
done

total=$((running_ram + wanted_ram))
if [ "$total" -gt "$BUDGET" ] && [ "$FORCE" -ne 1 ]; then
    ds_die "starting ${GAMES[*]} would need ~${total} GB RAM (already running: ~${running_ram} GB${running_list:+ — ${running_list[*]}}).
  That is over DEV_SERVERS_RAM_BUDGET_GB=${BUDGET}.
  Stop something first (stop.sh <game>), raise the budget in dev-servers/.env, or pass --force.
  Or declare what this box is working on and let focus do it:
    dev-servers/scripts/focus.sh add ${GAMES[*]} && dev-servers/scripts/focus.sh apply"
fi

# ── Start ────────────────────────────────────────────────────────────────────
for game in "${GAMES[@]}"; do
    if ! ds_is_installed "$game"; then
        ds_warn "${game} is not installed yet — run: dev-servers/scripts/install.sh ${game}"
        continue
    fi
    if ds_is_running "$game"; then
        ds_ok "${game} already running"
        continue
    fi
    # A game driven by a catalog target must actually hold that target: starting one
    # that does not would run a server the connector was never built for.
    ds_preflight_target "$game"
    ds_info "Starting ${game} (~$(ds_ram_gb "$game") GB) — $(ds_description "$game")"
    # shellcheck disable=SC2046  # service list is intentionally word-split
    ds_compose "$game" up -d $(ds_startable_services "$game")
    ds_ok "${game} started"
done

echo
ds_info "Estimated RAM in use: ~${total} GB of ${BUDGET} GB budget"
ds_info "Follow a game's logs with: dev-servers/scripts/logs.sh <game> -f"
