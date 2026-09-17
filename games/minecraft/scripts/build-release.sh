#!/usr/bin/env bash
# Builds the Minecraft connector at <version> into <out-dir>.
# Thin wrapper: every catalog target goes through takaro-maint, and the modules that
# have not moved to the catalog yet go through build-legacy.sh.
set -euo pipefail

VERSION="${1:?usage: build-release.sh <version> <out-dir>}"
OUT_DIR="${2:?usage: build-release.sh <version> <out-dir>}"

# Resolve the output directory before changing directory: a caller that passes a relative
# path means it relative to where it stands, not to this script.
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
cd "$(dirname "$0")/.."
REPO_ROOT=$(cd ../.. && pwd)

echo "Building Minecraft connector v${VERSION}..."

"${REPO_ROOT}/maintenance/bin/takaro-maint" build \
  --game minecraft --all-targets --version "$VERSION" --out "$OUT_DIR"

scripts/build-legacy.sh "$VERSION" "$OUT_DIR"
