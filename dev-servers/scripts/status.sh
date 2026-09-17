#!/usr/bin/env bash
# Shows what is installed, what is running, and what it costs.
#
# Usage: status.sh
set -euo pipefail
# shellcheck source=../lib/common.sh
. "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)/common.sh"

ds_load_env

BUDGET="${DEV_SERVERS_RAM_BUDGET_GB:-40}"

# Published host ports for exactly this game's services, read from the resolved
# compose config so profiles are respected and host_ip is not mistaken for a port.
ds_ports() {
    local id="$1"
    # shellcheck disable=SC2046  # the service list is deliberately word-split into args
    # Placeholders so the port map is visible before .env is filled in: some
    # compose files guard required credentials with ${VAR:?...}.
    TAKARO_REGISTRATION_TOKEN="${TAKARO_REGISTRATION_TOKEN:-unset}" \
    RCON_PASSWORD="${RCON_PASSWORD:-unset}" \
    ADMIN_PASSWORD="${ADMIN_PASSWORD:-unset}" \
    VALHEIM_SERVER_PASSWORD="${VALHEIM_SERVER_PASSWORD:-unset}" \
    ds_compose "$id" config --format json 2>/dev/null | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(0)
wanted = set(sys.argv[1:])
out = []
for name, svc in (doc.get("services") or {}).items():
    if name not in wanted:
        continue
    for p in svc.get("ports") or []:
        pub, proto = p.get("published"), p.get("protocol", "tcp")
        if pub is None:
            continue
        out.append((str(pub), proto))
seen, parts = set(), []
for pub, proto in out:
    key = f"{pub}/{proto}"
    if key in seen:
        continue
    seen.add(key)
    parts.append(pub if proto == "tcp" else f"{pub}/{proto}")
print(",".join(parts))
' $(ds_services "$id")
}

printf '\033[1m%-20s %-10s %-9s %-7s %-8s %s\033[0m\n' \
    GAME INSTALLED STATE RAM~GB DISK PORTS

running_ram=0
running_count=0

for game in $(ds_game_ids); do
    if ds_is_installed "$game"; then installed="yes"; else installed="-"; fi

    if ds_is_running "$game"; then
        state="running"; colour='\033[32m'
        running_ram=$((running_ram + $(ds_ram_gb "$game")))
        running_count=$((running_count + 1))
    else
        state="stopped"; colour=''
    fi

    data_dir="$(ds_data_dir "$game")"
    if [ -d "$data_dir" ]; then
        disk="$(du -sh "$data_dir" 2>/dev/null | cut -f1)"
    else
        disk="-"
    fi

    # Pad the plain string first, then colour it, so ANSI codes never count
    # toward the column width.
    printf '%-20s %-10s %b%-9s%b %-7s %-8s %s\n' \
        "$game" "$installed" "$colour" "$state" "${colour:+\\033[0m}" \
        "$(ds_ram_gb "$game")" "${disk:--}" "$(ds_ports "$game")"
done

echo
printf 'running: %d game(s), ~%d GB of %d GB budget\n' "$running_count" "$running_ram" "$BUDGET"
printf 'host:    %s GB RAM free, %s GB disk free\n' \
    "$(free -g | awk '/^Mem:/ {print $7}')" "$(ds_free_disk_gb)"
printf 'data:    %s in dev-servers/_data\n' "$(du -sh "$DS_DATA" 2>/dev/null | cut -f1)"

if [ -s "$DS_FOCUS" ]; then
    printf 'focus:   %s (focus.sh status)\n' \
        "$(awk 'NF && $1 !~ /^#/ {printf "%s ", $1}' "$DS_FOCUS")"
else
    printf 'focus:   (not declared — focus.sh set <game>... to declare this box'"'"'s active set)\n'
fi
