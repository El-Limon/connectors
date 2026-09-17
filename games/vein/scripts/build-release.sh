#!/usr/bin/env bash
# Builds the Takaro VEIN release artifacts into <out-dir>:
#   takaro-vein-plugin.tar.gz    - libtakaro-vein.so + install notes
#   takaro-vein-sidecar.tar.gz   - compiled sidecar (dist/, package.json, runtime Dockerfile,
#                                  .env.example, docker-compose.example.yml, ...)
#   SHA256SUMS                   - checksums of both archives
#
# Usage: build-release.sh [version] [out-dir]
#   version  defaults to the contents of version.txt
#   out-dir  defaults to <game root>/dist
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

VERSION="${1:-$(tr -d '[:space:]' < "$ROOT/version.txt")}"
OUT_DIR="${2:-$ROOT/dist}"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

PLUGIN_DIR="$ROOT/mod"
[ -d "$PLUGIN_DIR" ] || PLUGIN_DIR="$ROOT/plugin"   # source mirror uses plugin/, gettakaro uses mod/

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> building the VEIN plugin v${VERSION}"
"$PLUGIN_DIR/build.sh"

PKG="$STAGE/TakaroVein"
mkdir -p "$PKG"
cp "$PLUGIN_DIR/dist/libtakaro-vein.so" "$PKG/"

cat > "$PKG/README.txt" <<TXT
Takaro VEIN plugin ${VERSION}

Server-side only. Players install nothing.
The plugin is loaded into the Linux dedicated server (Steam app 2131400) with
LD_PRELOAD and serves a loopback HTTP API on 127.0.0.1:18890 for the sidecar.

Install:
1. Stop the dedicated server.
2. Put libtakaro-vein.so OUTSIDE the Steam/game tree - a SteamCMD
   "app_update ... validate" deletes files it does not know about. With the example
   compose file that is data/vein-plugin/libtakaro-vein.so, bind-mounted
   read-only to /opt/takaro/libtakaro-vein.so.
3. Start the GAME BINARY ONLY with
     LD_PRELOAD=/opt/takaro/libtakaro-vein.so ./Vein/Binaries/Linux/VeinServer-Linux-Test ...
   Never set it for SteamCMD (32-bit; it fails with a 64-bit preload).
4. Set TAKARO_PLUGIN_TOKEN on the game server process to a long random shared secret
   and give the sidecar the same value. Without a token every request is rejected
   with 401.
5. Start the server and confirm
   <serverdir>/Vein/Binaries/Linux/takaro/plugin.log contains
   "takaro vein plugin ${VERSION} starting".

You also need takaro-vein-sidecar.tar.gz - the plugin alone does not talk to Takaro.
TXT

tar -czf "$OUT_DIR/takaro-vein-plugin.tar.gz" -C "$STAGE" TakaroVein
echo "  -> $OUT_DIR/takaro-vein-plugin.tar.gz"

echo "==> building the sidecar v${VERSION}"
(
  cd "$ROOT/sidecar"
  npm ci
  npm run typecheck
  npm test
  npm run build
)

SPKG="$STAGE/TakaroVeinSidecar"
mkdir -p "$SPKG"
cp -R "$ROOT/sidecar/dist" "$ROOT/sidecar/package.json" "$ROOT/sidecar/package-lock.json" \
      "$ROOT/sidecar/Dockerfile" "$ROOT/sidecar/.dockerignore" "$SPKG/"
# Everything the README tells an operator to copy must be inside the archive.
cp "$ROOT/.env.example" "$ROOT/docker-compose.example.yml" "$SPKG/"
rm -rf "$SPKG/dist/__tests__" "$SPKG/dist/testing"

cat > "$SPKG/README.release.txt" <<TXT
Takaro VEIN sidecar ${VERSION}

The sidecar is the part that talks to Takaro. It reads the plugin's loopback API
(http://127.0.0.1:18890) and the server log, so it must share the game server's network
namespace (compose: network_mode: "service:vein") or run on the same host.

Option A - Docker (what docker-compose.example.yml does):
1. Move docker-compose.example.yml and .env.example out of this folder into the
   directory above it, then rename this folder to "sidecar" so the compose
   service's "build: ./sidecar" finds it.
2. cp .env.example .env and fill in the tokens.
3. docker compose -f docker-compose.example.yml --env-file .env up -d --build

Option B - plain Node.js 22 on the host:
1. npm ci --omit=dev
2. Set at least TAKARO_REGISTRATION_TOKEN, TAKARO_IDENTITY_TOKEN, TAKARO_PLUGIN_URL,
   TAKARO_PLUGIN_TOKEN and VEIN_LOG_FILE (see .env.example).
3. node dist/index.js

Mount a persistent volume at /data (the example compose uses ./data/vein-sidecar).
It holds the event cursor; without it events are replayed after every restart.

Verify: the sidecar log prints "Identified with Takaro (gameServerId=...)", the server
shows as online in Takaro, and curl http://127.0.0.1:18891/health reports "ok": true.

Never commit or share live registration tokens or plugin tokens.
TXT

tar -czf "$OUT_DIR/takaro-vein-sidecar.tar.gz" -C "$STAGE" TakaroVeinSidecar
echo "  -> $OUT_DIR/takaro-vein-sidecar.tar.gz"

( cd "$OUT_DIR" && sha256sum takaro-vein-plugin.tar.gz takaro-vein-sidecar.tar.gz > SHA256SUMS )
echo "==> $OUT_DIR/SHA256SUMS"
cat "$OUT_DIR/SHA256SUMS"
