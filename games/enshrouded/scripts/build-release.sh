#!/usr/bin/env bash
# Builds and packages the Takaro Enshrouded plugin (dbghelp.dll proxy) into <out-dir>.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${1:?usage: build-release.sh <version> <out-dir>}"
OUT_DIR="${2:?usage: build-release.sh <version> <out-dir>}"

mkdir -p "$OUT_DIR"
echo "Building Enshrouded plugin v${VERSION}..."

./mod/build.sh

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

PLUGIN_DIR="$STAGE/TakaroEnshrouded"
mkdir -p "$PLUGIN_DIR"
cp mod/build/dbghelp.dll "$PLUGIN_DIR/"

cat > "$PLUGIN_DIR/README.txt" << EOF
Takaro Enshrouded Plugin ${VERSION}

This is a server-side plugin only. Players do not install anything.
It is a dbghelp.dll proxy: the game server loads it instead of the system dbghelp.

Install:
1. Stop the Enshrouded dedicated server (a running server holds dbghelp.dll open).
2. Put dbghelp.dll next to enshrouded_server.exe.
   With the example compose file that is data/enshrouded-plugin/dbghelp.dll, which is
   bind-mounted read-only to /opt/enshrouded/server/dbghelp.dll.
3. Make the server prefer this DLL over the system one. In the Linux/Wine container that
   is WINEDLLOVERRIDES="dbghelp=n,b"; a native Windows server loads the local file already.
4. Set TAKARO_PLUGIN_TOKEN on the game server process to a long random shared secret and
   give the sidecar the same value; without it the plugin rejects every request with 401.
5. Start the server and confirm data/enshrouded/server/takaro/plugin.log contains
   "takaro enshrouded plugin ${VERSION} starting (pid ...)".

The plugin's HTTP API listens on 127.0.0.1:18890 only and is never exposed to the host.
You also need the sidecar (takaro-enshrouded-sidecar.zip) - the plugin alone does not
talk to Takaro.
EOF

(cd "$STAGE" && zip -qr takaro-enshrouded-plugin.zip TakaroEnshrouded)
cp "$STAGE/takaro-enshrouded-plugin.zip" "$OUT_DIR/"

echo "  -> $OUT_DIR/takaro-enshrouded-plugin.zip"
