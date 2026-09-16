#!/usr/bin/env bash
# Detects when a connector's source has changed since its artifact was deployed,
# and optionally rebuilds and redeploys it.
#
# Usage:
#   sync-connectors.sh --check          report stale connectors, exit 1 if any
#   sync-connectors.sh                  rebuild + redeploy stale connectors
#   sync-connectors.sh --restart        ...and restart any that were running
#   sync-connectors.sh --baseline       record current source as deployed, no rebuild
#                                       (use once, when artifacts already match source)
#   sync-connectors.sh [game...]        limit to specific games
#
# A connector is "stale" when the SHA-256 of its source tree differs from the
# fingerprint recorded when its artifact was last deployed.
set -euo pipefail
DS_LIB="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)"
# shellcheck source=../lib/common.sh
. "${DS_LIB}/common.sh"

CHECK_ONLY=0
RESTART=0
BASELINE=0
GAMES=()
for arg in "$@"; do
    case "$arg" in
        --check)    CHECK_ONLY=1 ;;
        --baseline) BASELINE=1 ;;
        --restart) RESTART=1 ;;
        -*)        ds_die "unknown option ${arg}" ;;
        *)         ds_validate_game "$arg"; GAMES+=("$arg") ;;
    esac
done
[ ${#GAMES[@]} -gt 0 ] || mapfile -t GAMES < <(ds_game_ids)

FP_DIR="${DS_DATA}/.fingerprints"
mkdir -p "$FP_DIR"

if [ "$BASELINE" -eq 1 ]; then
    ds_info "Recording current source as the deployed baseline (no rebuild)"
    for game in "${GAMES[@]}"; do
        ds_is_installed "$game" || continue
        ds_record_fingerprint "$game" && ds_ok "$game"
    done
    exit 0
fi

# Palworld's bridge is a pinned third-party release, so "new version" means a
# new upstream GitHub release rather than a local source change.
ds_palworld_upstream() {
    local pinned latest
    pinned="$(grep -oP 'ARG BRIDGE_VERSION=\K[0-9.]+' "${DS_DIR}/images/palworld-bridge/Dockerfile" 2>/dev/null || echo "?")"
    latest="$(curl -fsSL --max-time 15 \
        https://api.github.com/repos/mad-001/Palworld-Bridge/releases/latest 2>/dev/null \
        | grep -oP '"tag_name":\s*"v?\K[0-9.]+' | head -1 || true)"
    printf '%s|%s' "$pinned" "${latest:-unknown}"
}

STALE=()
printf '\033[1m%-20s %-10s %s\033[0m\n' CONNECTOR STATE DETAIL

for game in "${GAMES[@]}"; do
    if [ "$game" = "palworld" ]; then
        IFS='|' read -r pinned latest <<< "$(ds_palworld_upstream)"
        if [ "$latest" = "unknown" ]; then
            printf '%-20s %-10s %s\n' "$game" "unknown" "could not reach GitHub; pinned v${pinned}"
        elif [ "$pinned" != "$latest" ]; then
            printf '%-20s \033[33m%-10s\033[0m %s\n' "$game" "UPDATE" "third-party bridge v${pinned} -> v${latest} (edit images/palworld-bridge/Dockerfile)"
        else
            printf '%-20s %-10s %s\n' "$game" "current" "third-party bridge v${pinned}"
        fi
        continue
    fi

    if ! ds_is_installed "$game"; then
        printf '%-20s %-10s %s\n' "$game" "-" "not installed"
        continue
    fi

    current="$(ds_source_fingerprint "$game")"
    recorded="$(cat "${FP_DIR}/${game}" 2>/dev/null || echo "none")"

    if [ "$current" = "n/a" ]; then
        printf '%-20s %-10s %s\n' "$game" "n/a" "no source tree to track"
    elif [ "$recorded" = "none" ]; then
        printf '%-20s \033[33m%-10s\033[0m %s\n' "$game" "UNKNOWN" "never fingerprinted — will deploy to establish a baseline"
        STALE+=("$game")
    elif [ "$current" != "$recorded" ]; then
        printf '%-20s \033[33m%-10s\033[0m %s\n' "$game" "STALE" "source changed since last deploy (${recorded:0:8} -> ${current:0:8})"
        STALE+=("$game")
    else
        printf '%-20s \033[32m%-10s\033[0m %s\n' "$game" "current" "${current:0:8}"
    fi
done

echo
if [ ${#STALE[@]} -eq 0 ]; then
    ds_ok "All deployed connectors match their source."
    exit 0
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    ds_warn "${#STALE[@]} connector(s) need redeploying: ${STALE[*]}"
    echo "  run: just dev-sync            (rebuild + redeploy)"
    echo "       just dev-sync --restart  (and restart them)"
    exit 1
fi

ds_load_env
for game in "${STALE[@]}"; do
    was_running=0
    ds_is_running "$game" && was_running=1

    ds_info "Redeploying ${game}..."
    if "${DS_DIR}/scripts/deploy-connector.sh" "$game"; then
        ds_record_fingerprint "$game"
        ds_ok "${game} redeployed"
        if [ "$was_running" -eq 1 ]; then
            if [ "$RESTART" -eq 1 ]; then
                ds_info "Restarting ${game}"
                "${DS_DIR}/scripts/stop.sh" "$game" >/dev/null
                "${DS_DIR}/scripts/start.sh" "$game" >/dev/null
                ds_ok "${game} restarted"
            else
                ds_warn "${game} is running with the OLD artifact — restart it to pick up the new one"
            fi
        fi
    else
        ds_warn "${game} failed to rebuild — leaving its fingerprint unchanged"
    fi
done
