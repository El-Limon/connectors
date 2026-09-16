#!/usr/bin/env bash
# Deploy the Rust plugin into the shared dev rig (dev-servers/).
# Thin wrapper around dev-servers/scripts/deploy-connector.sh rust, which copies
# mod/TakaroConnector.cs into dev-servers/_data/rust/plugins (mounted at
# /rust/carbon/plugins in the rig's rust container).
set -euo pipefail
REPO_ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
exec "${REPO_ROOT}/dev-servers/scripts/deploy-connector.sh" rust
