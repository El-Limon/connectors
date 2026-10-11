#pragma once

// Takaro's Protocol Version 0 map actions (`getMapInfo`, `getMapTile`) served from a directory of
// pre-rendered tiles. VEIN's dedicated server has no map image of its own (the server cook strips
// the texture), so the tiles are rendered once from the game client's in-game map texture with
// scripts/build-map-tiles.py and copied next to the plugin. Runs on the action worker only: file
// reads never touch the game thread.
//
// Layout of the directory (TAKARO_MAP_DIR, default <PluginDataDir>/map):
//   map.json             MapInfoDTO numbers written by the tile builder
//   <z>/<x>/<y>.png      one 128 px tile; x grows east, y grows north (Takaro/7D2D web-map scheme)
//   blank.png            optional; answered for a tile inside the zoom range that holds no map
#include <string>

namespace NativeMap {

// TAKARO_MAP_DIR (env or plugin.json `mapDir`), else <PluginDataDir>/map.
std::string Directory();

// MapInfoDTO as JSON. Always the six fields with the right types; `enabled` is false (and the
// numbers are neutral) when the directory has no usable map.json or no tiles at its maxZoom.
std::string InfoJson(const std::string& dir);

// The base64 PNG for one tile as a JSON string literal. `argsJson` is Takaro's normalized
// {x, y, z}. Throws std::runtime_error with an operator-facing message when the map is not
// installed, the arguments are not integers, the zoom is out of range or the tile is missing.
std::string TileJson(const std::string& dir, const std::string& argsJson);

// Standard base64 (RFC 4648, with padding). Exposed for tests.
std::string Base64(const std::string& bytes);

}  // namespace NativeMap
