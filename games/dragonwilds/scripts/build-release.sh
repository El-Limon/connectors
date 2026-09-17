#!/usr/bin/env bash
# Builds the Takaro Dragonwilds release artifacts into <out-dir>:
#   takaro-dragonwilds-plugin.tar.gz    - libtakaro-dragonwilds.so + install notes
#   takaro-dragonwilds-sidecar.tar.gz   - compiled sidecar (dist/, package.json, Dockerfile, ...)
#   SHA256SUMS                          - checksums of both archives
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

echo "==> building the Dragonwilds plugin v${VERSION}"
"$PLUGIN_DIR/build.sh"

PKG="$STAGE/TakaroDragonwilds"
mkdir -p "$PKG"
cp "$PLUGIN_DIR/dist/libtakaro-dragonwilds.so" "$PKG/"

cat > "$PKG/README.txt" <<TXT
Takaro RuneScape: Dragonwilds plugin ${VERSION}

Server-side only. Players install nothing.
The plugin is loaded into the Linux dedicated server with LD_PRELOAD and serves a
loopback HTTP API on 127.0.0.1:18890 for the sidecar.

Install:
1. Stop the dedicated server.
2. Put libtakaro-dragonwilds.so OUTSIDE the Steam/game tree - a SteamCMD
   "app_update ... validate" deletes files it does not know about. With the example
   compose file that is data/dragonwilds-plugin/libtakaro-dragonwilds.so, bind-mounted
   read-only to /opt/takaro/libtakaro-dragonwilds.so.
3. Start the GAME BINARY ONLY with
     LD_PRELOAD=/opt/takaro/libtakaro-dragonwilds.so
   Never set it for SteamCMD (32-bit; it fails with a 64-bit preload).
4. Set TAKARO_PLUGIN_TOKEN on the game server process to a long random shared secret
   and give the sidecar the same value, or write it to <serverdir>/takaro/plugin.json.
   Without a token every request is rejected with 401.
5. Start the server and confirm <serverdir>/takaro/plugin.log contains
   "takaro dragonwilds plugin ${VERSION} starting".

You also need takaro-dragonwilds-sidecar.tar.gz - the plugin alone does not talk to Takaro.
TXT

tar -czf "$OUT_DIR/takaro-dragonwilds-plugin.tar.gz" -C "$STAGE" TakaroDragonwilds
echo "  -> $OUT_DIR/takaro-dragonwilds-plugin.tar.gz"

echo "==> building the sidecar v${VERSION}"
(
  cd "$ROOT/sidecar"
  npm ci
  npm run typecheck
  npm test
  npm run build
)

SPKG="$STAGE/TakaroDragonwildsSidecar"
mkdir -p "$SPKG"
cp -R "$ROOT/sidecar/dist" "$ROOT/sidecar/package.json" "$ROOT/sidecar/package-lock.json" \
      "$ROOT/sidecar/Dockerfile" "$ROOT/sidecar/.dockerignore" "$ROOT/sidecar/.env.example" "$SPKG/"
rm -rf "$SPKG/dist/__tests__" "$SPKG/dist/testing"

cat > "$SPKG/README.release.txt" <<TXT
Takaro RuneScape: Dragonwilds sidecar ${VERSION}

The sidecar is the part that talks to Takaro. It reads the plugin's loopback API
(http://127.0.0.1:18890) and the server log, so it must share the game server's network
namespace (compose: network_mode: "service:dragonwilds") or run on the same host.

Option A - Docker (what docker-compose.example.yml does):
1. Unpack this folder next to docker-compose.example.yml and rename it to "sidecar"
   so the compose service's "build: ./sidecar" finds it.
2. docker compose -f docker-compose.example.yml --env-file .env up -d --build

Option B - plain Node.js 22 on the host:
1. npm ci --omit=dev
2. Set at least TAKARO_REGISTRATION_TOKEN, TAKARO_IDENTITY_TOKEN, TAKARO_PLUGIN_URL,
   TAKARO_PLUGIN_TOKEN and DRAGONWILDS_LOG_FILE (see .env.example).
3. node dist/index.js

Verify: the sidecar log prints "Identified with Takaro (gameServerId=...)", the server
shows as online in Takaro, and curl http://127.0.0.1:18891/health returns status "ok".

Never commit or share live registration tokens or plugin tokens.
TXT

tar -czf "$OUT_DIR/takaro-dragonwilds-sidecar.tar.gz" -C "$STAGE" TakaroDragonwildsSidecar
echo "  -> $OUT_DIR/takaro-dragonwilds-sidecar.tar.gz"

( cd "$OUT_DIR" && sha256sum takaro-dragonwilds-plugin.tar.gz takaro-dragonwilds-sidecar.tar.gz > SHA256SUMS )
echo "==> $OUT_DIR/SHA256SUMS"
cat "$OUT_DIR/SHA256SUMS"
