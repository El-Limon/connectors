#!/usr/bin/env bash
# Builds the Minecraft modules that are not catalog targets yet.
# TODO(#151): becomes a no-op once paper/neoforge move to targets/.
set -euo pipefail

VERSION="${1:?usage: build-legacy.sh <version> <out-dir>}"
OUT_DIR="${2:?usage: build-legacy.sh <version> <out-dir>}"

# Resolve the output directory before changing directory: a caller that passes a relative
# path means it relative to where it stands, not to this script.
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
cd "$(dirname "$0")/.."

for module in paper neoforge; do
  if [ ! -f "mod/${module}/build.gradle.kts" ]; then
    echo "  skipping ${module}: migrated to a catalog target"
    continue
  fi
  echo "Building legacy module ${module} v${VERSION}..."
  (cd mod && ./gradlew ":${module}:build" -Pversion="${VERSION}" --no-daemon)
  # The exact name the module produces — never a glob, so a rename fails here.
  jar="mod/${module}/build/libs/takaro-${module}-${VERSION}.jar"
  [ -f "$jar" ] || { echo "Error: expected ${jar}" >&2; exit 1; }
  cp "$jar" "$OUT_DIR/"
  echo "  -> ${OUT_DIR}/$(basename "$jar")"
done
