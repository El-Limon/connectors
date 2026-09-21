#!/usr/bin/env bash
# Healthcheck for the Dune map server.
#
# Funcom ship NO readiness probe anywhere in their CRDs — readiness is operator-side
# polling of a ServerStats CR — so this is ours. Two conditions, cheapest first:
#   1. the game process is alive
#   2. it is listening on the game and IGW UDP ports
#
# Funcom's own run.sh discovers the ports with `lsof -Pan -p <pid> -i`. That does NOT work
# here: in an unprivileged container lsof cannot read the socket tables and silently
# reports nothing even while the ports are bound (verified 2026-09-21 — the host showed
# 7817/udp and 7928/udp bound while lsof listed zero sockets). /proc/net/udp is always
# readable, so match the listening ports there instead.
set -euo pipefail

pid="$(pgrep -f 'DuneSandboxServer-Linux-Shipping DuneSandbox' | head -1 || true)"
[ -n "$pid" ] || { echo "no DuneSandboxServer process"; exit 1; }

hex() { printf '%04X' "$1"; }
missing=""
for port in "${DUNE_GAME_PORT:-7777}" "${DUNE_IGW_PORT:-7888}"; do
    if ! awk -v p=":$(hex "$port")" '$2 ~ p {found=1} END {exit !found}' /proc/net/udp /proc/net/udp6 2>/dev/null; then
        missing="${missing} ${port}"
    fi
done
[ -z "$missing" ] || { echo "process ${pid} up but not listening on udp:${missing}"; exit 1; }

echo "ok pid=${pid} udp=${DUNE_GAME_PORT:-7777},${DUNE_IGW_PORT:-7888}"
