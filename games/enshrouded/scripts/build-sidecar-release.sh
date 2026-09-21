#!/usr/bin/env bash
# Kept for the name the docs and older runbooks use. The sidecar is no longer packaged on
# its own: both components come out of one build, in one pinned image, from one catalog
# target, so that a release can never ship a plugin and a sidecar built from different
# trees. This forwards to build-release.sh, which produces both.
#
# Usage: build-sidecar-release.sh <version> <out-dir> --target <catalog target id>
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
echo "build-sidecar-release.sh now builds BOTH components; forwarding to build-release.sh" >&2
exec "${SCRIPT_DIR}/build-release.sh" "$@"
