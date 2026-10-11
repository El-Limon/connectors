// getMapInfo / getMapTile for the native connector: served from a pre-rendered tile pack on disk.
//
// The dedicated server has no map renderer and ships no map art, so the tiles are produced offline
// (mod/tools/map-tiles/) and installed next to the server:
//   <mapDir>/map.json            manifest (format, tileSize, minZoom, maxZoom, mapSize*, optional outsideTile)
//   <mapDir>/<z>/<x>/<y>.png     Takaro tile coordinates: at zoom z a tile covers tileSize * 2^(maxZoom - z)
//                                world metres; x grows east, y grows north (world z), both from the origin.
// Without a valid manifest getMapInfo answers enabled:false and getMapTile fails with the reason.
// Files are re-read on each call (small, and a pack can be installed while the server runs).
#pragma once
#include "common.h"

#include <string>

namespace native {

struct MapTilePack {
    bool ok = false;
    std::string reason;  // why the pack is unusable (when !ok)
    int tileSize = 128, mapBlockSize = 128, minZoom = 0, maxZoom = 4;
    long long mapSizeX = 0, mapSizeY = 0, mapSizeZ = 0;
    std::string outsideTile;  // file under mapDir answered for in-range tiles that are not in the pack
};

MapTilePack LoadMapTilePack(const std::string& mapDir);
// Takaro MapInfoDTO: {enabled, mapBlockSize, maxZoom, mapSizeX, mapSizeY, mapSizeZ}.
JsonValue MapInfoPayload(const std::string& mapDir);
// The tile PNG as base64 (the response payload). Throws NativeError(Action) with a reason otherwise.
std::string MapTileBase64(const std::string& mapDir, const JsonValue& args);

std::string Base64Encode(const std::string& bytes);

constexpr size_t kMaxMapTileBytes = 1u << 20;

}  // namespace native
