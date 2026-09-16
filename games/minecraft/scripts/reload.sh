#!/usr/bin/env bash
set -euo pipefail

PLATFORM="${1:-all}"
RCON_PASS="${RCON_PASSWORD:-takaro123}"
CONTAINER_PREFIX="${MINECRAFT_CONTAINER_PREFIX:-takaro-dev-minecraft}"

reload_paper() {
    echo "Reloading Paper server..."
    docker exec "${CONTAINER_PREFIX}-paper" rcon-cli --password "$RCON_PASS" "reload confirm" 2>/dev/null || \
        echo "  Warning: Could not connect to Paper RCON"
}

reload_neoforge() {
    echo "NeoForge does not support hot reload. Restart the rig server:"
    echo "  just dev-stop minecraft-neoforge && just dev-start minecraft-neoforge"
}

reload_fabric() {
    echo "Fabric does not support hot reload. Restart the rig server:"
    echo "  just dev-stop minecraft-fabric && just dev-start minecraft-fabric"
}

case "$PLATFORM" in
    paper)    reload_paper ;;
    neoforge) reload_neoforge ;;
    fabric)   reload_fabric ;;
    all)
        reload_paper
        reload_neoforge
        reload_fabric
        ;;
    *)
        echo "Usage: $0 {paper|neoforge|fabric|all}"
        exit 1
        ;;
esac
