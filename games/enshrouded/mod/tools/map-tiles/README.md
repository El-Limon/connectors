# Enshrouded map tile pack

The native connector answers `getMapInfo` / `getMapTile` from a tile pack in `<server>\takaro\map\`
(override with `TAKARO_MAP_DIR` or `mapDir` in `takaro\plugin.json`); see `src/native/maptiles.h`.
The dedicated server ships no map art, so the pack is rendered offline from the game client:

1. Export the raw map textures from a complete client install with the EML CLI (kfc-parser `emm`):
   copy `eml-mod/` to `<client>/mods/takaro_map_export/`, then
   `emm run -e -g <client dir> --export-directory <raw dir>`.
2. Render: `python3 render_tiles.py <raw dir> <out dir>` (numpy, pillow; about 10 minutes, 8525 tiles, ~110 MB).
3. Copy `<out dir>` to `<server>/takaro/map/` (contains `map.json`). No restart needed.

World frame (build 1024233 server / 1076226 client): origin (0,0), 10240 x 10240 m (x east, z north),
1 m per pixel at zoom 4, 128 px tiles; tile y grows north. This matches takaro-player-map's tiling.

The pack is derived from Keen Games' client assets (height data and colour ramp). Use it on servers
you run; do not publish or redistribute it without permission.
