#!/usr/bin/env bash
# Builds and verifies the mod's third-party dependencies INSIDE the pinned Mono image.
#
# Runs as the `deps` compose service. Every download is checked against the sha256 the
# catalog records before it is unpacked or built, and the result is recorded next to the
# reference assemblies so a second run is a no-op.
#
# Reads (from `takaro-maint targets resolve`): SEVEND2D_FINGERPRINT and, per dependency,
# SEVEND2D_DEP_<NAME>_URL and SEVEND2D_DEP_<NAME>_SHA256.
set -euo pipefail

BINARIES="${BINARIES:-/binaries}"
LIB="${LIB:-/app/lib}"
LEDGER="${BINARIES}/.takaro/deps.json"
DLLS="websocket-sharp.dll LiteDB.dll System.Buffers.dll"

fetch() {
    # fetch <url> <sha256> <destination>
    local url="$1" sha="$2" dest="$3"
    curl -fsSL "$url" -o "$dest"
    printf '%s  %s\n' "$sha" "$dest" | sha256sum --check --status \
        || { echo "error: ${url} does not hash to ${sha}" >&2; exit 5; }
}

up_to_date() {
    [ -f "$LEDGER" ] || return 1
    grep -q "\"fingerprint\": \"${SEVEND2D_FINGERPRINT}\"" "$LEDGER" || return 1
    local dll
    for dll in $DLLS; do
        [ -f "${BINARIES}/${dll}" ] || return 1
        grep -q "$(sha256sum "${BINARIES}/${dll}" | cut -d' ' -f1)" "$LEDGER" || return 1
    done
    return 0
}

if up_to_date; then
    echo "dependencies already built for ${SEVEND2D_FINGERPRINT:0:16}; nothing to do"
    exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Building websocket-sharp from the pinned commit..."
fetch "${SEVEND2D_DEP_WEBSOCKET_SHARP_URL}" "${SEVEND2D_DEP_WEBSOCKET_SHARP_SHA256}" "${work}/websocket-sharp.tar.gz"
mkdir -p "${work}/websocket-sharp"
tar -xzf "${work}/websocket-sharp.tar.gz" -C "${work}/websocket-sharp" --strip-components=1
( cd "${work}/websocket-sharp" && msbuild websocket-sharp.sln /p:Configuration=Release /p:Deterministic=true )
ws_dll="$(find "${work}/websocket-sharp" -name websocket-sharp.dll -path '*Release*' | head -1)"
[ -n "$ws_dll" ] || { echo "error: websocket-sharp.dll was not built" >&2; exit 6; }

echo "Unpacking the pinned NuGet packages..."
fetch "${SEVEND2D_DEP_LITEDB_URL}" "${SEVEND2D_DEP_LITEDB_SHA256}" "${work}/litedb.nupkg"
unzip -o -q "${work}/litedb.nupkg" 'lib/net45/LiteDB.dll' -d "${work}/litedb"
fetch "${SEVEND2D_DEP_SYSTEM_BUFFERS_URL}" "${SEVEND2D_DEP_SYSTEM_BUFFERS_SHA256}" "${work}/buffers.nupkg"
unzip -o -q "${work}/buffers.nupkg" 'lib/net461/System.Buffers.dll' -d "${work}/buffers"

mkdir -p "${BINARIES}" "${LIB}" "$(dirname "$LEDGER")"
install -m 644 "$ws_dll" "${BINARIES}/websocket-sharp.dll"
install -m 644 "${work}/litedb/lib/net45/LiteDB.dll" "${BINARIES}/LiteDB.dll"
install -m 644 "${work}/buffers/lib/net461/System.Buffers.dll" "${BINARIES}/System.Buffers.dll"
for dll in $DLLS; do
    install -m 644 "${BINARIES}/${dll}" "${LIB}/${dll}"
done

sha_of() { sha256sum "$1" | cut -d' ' -f1; }
cat > "$LEDGER" <<JSON
{
  "schemaVersion": 1,
  "fingerprint": "${SEVEND2D_FINGERPRINT}",
  "websocket-sharp": {
    "tarballSha256": "${SEVEND2D_DEP_WEBSOCKET_SHARP_SHA256}",
    "dllSha256": "$(sha_of "${BINARIES}/websocket-sharp.dll")"
  },
  "LiteDB": {
    "packageSha256": "${SEVEND2D_DEP_LITEDB_SHA256}",
    "dllSha256": "$(sha_of "${BINARIES}/LiteDB.dll")"
  },
  "System.Buffers": {
    "packageSha256": "${SEVEND2D_DEP_SYSTEM_BUFFERS_SHA256}",
    "dllSha256": "$(sha_of "${BINARIES}/System.Buffers.dll")"
  }
}
JSON
echo "dependencies ready in ${BINARIES}"
