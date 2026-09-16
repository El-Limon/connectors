#!/usr/bin/env bash
# Builds dist/libtakaro-dragonwilds.so inside the debian:bookworm toolchain container.
# Usage: ./build.sh [--native] [--tests]
#   --native  build on the host (needs g++ >= 10); default is docker
#   --tests   also build and run the unit tests
set -euo pipefail
cd "$(dirname "$0")"

NATIVE=0
TESTS=0
for a in "$@"; do
  case "$a" in
    --native) NATIVE=1 ;;
    --tests) TESTS=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

IMAGE=takaro-dragonwilds-build
if [ "$NATIVE" = 0 ]; then
  docker build -q -t "$IMAGE" -f Dockerfile.build . >/dev/null
  args=(--native)
  [ "$TESTS" = 1 ] && args+=(--tests)
  exec docker run --rm -v "$PWD":/src -w /src -u "$(id -u):$(id -g)" "$IMAGE" \
      ./build.sh "${args[@]}"
fi

CXX=${CXX:-g++}
CXXFLAGS=(-std=c++17 -O2 -fPIC -fvisibility=hidden -Wall -Wextra -Isrc)
LDFLAGS=(-shared -pthread -ldl -static-libstdc++ -static-libgcc)
# DEBUG_CORRUPT_SIG=<name> produces a deliberately broken build for the degrade proof.
if [ -n "${DEBUG_CORRUPT_SIG:-}" ]; then
  CXXFLAGS+=("-DTAKARO_DEBUG_CORRUPT_SIG=\"$DEBUG_CORRUPT_SIG\"")
fi

rm -rf build dist
mkdir -p build dist
for f in src/*.cpp; do
  echo "  CXX $f"
  "$CXX" "${CXXFLAGS[@]}" -c "$f" -o "build/$(basename "${f%.cpp}").o"
done
echo "  LD  dist/libtakaro-dragonwilds.so"
"$CXX" build/*.o "${LDFLAGS[@]}" -o dist/libtakaro-dragonwilds.so
strip --strip-unneeded dist/libtakaro-dragonwilds.so 2>/dev/null || true
( cd dist && sha256sum libtakaro-dragonwilds.so > SHA256SUMS )
echo "built dist/libtakaro-dragonwilds.so ($(stat -c %s dist/libtakaro-dragonwilds.so) bytes)"
cat dist/SHA256SUMS

if [ "$TESTS" = 1 ]; then
  ./tests/run.sh --native
fi
