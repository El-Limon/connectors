#!/usr/bin/env bash
# Deploy the Minecraft connector into the shared dev rig (dev-servers/).
# Thin wrapper around dev-servers/scripts/deploy-connector.sh, which builds the
# module and copies the jar into dev-servers/_data/minecraft-<platform>/.
set -euo pipefail
REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
DEPLOY="${REPO_ROOT}/dev-servers/scripts/deploy-connector.sh"

PLATFORM="${1:-all}"
case "$PLATFORM" in
    paper|neoforge|fabric) "$DEPLOY" "minecraft-${PLATFORM}" ;;
    all)
        "$DEPLOY" minecraft-paper
        "$DEPLOY" minecraft-neoforge
        "$DEPLOY" minecraft-fabric
        ;;
    *)
        echo "Usage: $0 {paper|neoforge|fabric|all}" >&2
        exit 1
        ;;
esac
