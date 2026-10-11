#!/usr/bin/env python3
"""Render Takaro map tiles for a VEIN server from the game's own in-game map texture.

The dedicated server cannot draw its map: the server cook strips texture pixels. The client
cook keeps them. This tool takes that texture (exported from your own client install) and writes
the tile directory the plugin serves for Takaro's `getMapInfo` and `getMapTile`:

  <out>/map.json           the numbers the plugin answers in getMapInfo
  <out>/<z>/<x>/<y>.png    128 px tiles; x grows east (world +Y), y grows north (world +X)
  <out>/blank.png          a transparent tile for the area around the rendered map

Copy <out> to the plugin's map directory, by default `<plugin data dir>/map` (for example
`Vein/Binaries/Linux/takaro/map`), or point TAKARO_MAP_DIR at it. No restart is needed.

Getting the texture (ChamplainValley, VEIN 0.025): the level's IngameMapTexture is
`/Game/Vein/UI/Map/T_ChamplainValley_Oct30`, stored as an uncompressed PF_B8G8R8A8 4096 x 4096
mip in the client's `Vein/Content/Paks/pakchunk200-Windows.pak`. Extract both halves with
repak (https://github.com/trumank/repak):

  repak get pakchunk200-Windows.pak Vein/Content/Vein/UI/Map/T_ChamplainValley_Oct30_2026.uexp > map.uexp
  repak get pakchunk200-Windows.pak Vein/Content/Vein/UI/Map/T_ChamplainValley_Oct30_2026.uasset > map.uasset
  python3 build-map-tiles.py --texture map.uexp --out takaro-map

A PNG export of the same texture (for example from FModel) works too.

World placement: the game's map widget (UZoomUserWidget::ConvertMapLocationToWorldSpace) maps a
texture position (u, v) in 0..1 to world X = centerX - W/2 * (2v - 1) and world Y = centerY +
W/2 * (2u - 1), with W the OrthoWidth of the level's MapMakerMarker actor. ChamplainValley has
W = 1200000 cm and no root component on that actor, so the center is the world origin. Those
are the defaults; pass --ortho-width/--center-x/--center-y for another level.

The texture belongs to the game's publisher. Render tiles from your own client copy; do not
redistribute the texture or the tiles.

Needs Pillow (`pip install pillow`, or `uv run --with pillow python3 build-map-tiles.py ...`).
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import sys
from pathlib import Path

TILE = 128
PACKAGE_FILE_TAG = 0x9E2A83C1
DEFAULT_ORTHO_WIDTH = 1_200_000.0
# Vertical extent reported as mapSizeZ. Z is up in VEIN; the smallest extent tells Takaro
# viewers which axis is vertical (7 Days to Die reports Y=256 < Z for its Y-up world).
DEFAULT_HEIGHT = 100_000.0
FORMAT = "takaro-vein-map/1"


class TextureError(ValueError):
    pass


def decode_uexp(data: bytes, uasset: bytes | None = None) -> tuple[int, int, bytes]:
    """Return (width, height, BGRA bytes) of the single inline mip of a cooked Texture2D.

    The cooked .uexp ends with the mip's SizeX, SizeY, SizeZ, 12 bytes and the package tag;
    the pixel bytes sit directly before that footer.
    """
    if uasset is not None and b"PF_B8G8R8A8" not in uasset:
        raise TextureError("texture is not PF_B8G8R8A8 (export a PNG instead)")
    if len(data) < 28 + 4:
        raise TextureError("file too small for a cooked texture")
    (tag,) = struct.unpack_from("<I", data, len(data) - 4)
    if tag != PACKAGE_FILE_TAG:
        raise TextureError("missing package tag; not a cooked .uexp")
    width, height, depth = struct.unpack_from("<iii", data, len(data) - 28)
    if width <= 0 or height <= 0 or depth != 1 or width > 16384 or height > 16384:
        raise TextureError(f"implausible mip size {width}x{height}x{depth}")
    size = width * height * 4
    start = len(data) - 28 - size
    if start < 0:
        raise TextureError("mip pixels missing (server cooks strip them; use the client's pak)")
    return width, height, data[start:start + size]


def load_texture(path: Path):
    from PIL import Image  # noqa: PLC0415 - optional dependency, only needed to render

    if path.suffix.lower() == ".uexp":
        uasset_path = path.with_suffix(".uasset")
        uasset = uasset_path.read_bytes() if uasset_path.exists() else None
        width, height, pixels = decode_uexp(path.read_bytes(), uasset)
        return Image.frombuffer("RGBA", (width, height), pixels, "raw", "BGRA", 0, 1)
    return Image.open(path).convert("RGBA")


def layout(texture_size: int, ortho_width: float) -> dict:
    """Tile pyramid parameters: the deepest zoom shows one texture pixel per screen pixel."""
    if texture_size < TILE:
        raise ValueError("texture smaller than one tile")
    block = TILE * ortho_width / texture_size  # world units one tile covers at maxZoom
    max_zoom = max(0, math.ceil(math.log2(texture_size / (2 * TILE))))
    return {"mapBlockSize": block, "maxZoom": max_zoom}


def world_bounds(ortho_width: float, center_x: float, center_y: float) -> dict:
    half = ortho_width / 2
    return {"east": (center_y - half, center_y + half), "north": (center_x - half, center_x + half)}


def tile_range(zoom: int, max_zoom: int, block: float, bounds: dict) -> tuple[range, range]:
    unit = block * 2 ** (max_zoom - zoom)
    east0, east1 = bounds["east"]
    north0, north1 = bounds["north"]
    xs = range(math.floor(east0 / unit), math.ceil(east1 / unit))
    ys = range(math.floor(north0 / unit), math.ceil(north1 / unit))
    return xs, ys


def tile_box(x: int, y: int, unit: float, bounds: dict, scaled_size: int, ortho_width: float):
    """Crop box (left, top, right, bottom) in the scaled texture for tile (x, y)."""
    px_per_unit = scaled_size / ortho_width
    east_min = bounds["east"][0]
    north_max = bounds["north"][1]
    left = (x * unit - east_min) * px_per_unit
    right = ((x + 1) * unit - east_min) * px_per_unit
    top = (north_max - (y + 1) * unit) * px_per_unit
    bottom = (north_max - y * unit) * px_per_unit
    return left, top, right, bottom


def render(image, out: Path, ortho_width: float, center_x: float, center_y: float,
           height: float, source: str) -> dict:
    from PIL import Image  # noqa: PLC0415

    if image.width != image.height:
        raise TextureError("the in-game map texture is square; got %dx%d" % image.size)
    size = image.width
    params = layout(size, ortho_width)
    max_zoom, block = params["maxZoom"], params["mapBlockSize"]
    bounds = world_bounds(ortho_width, center_x, center_y)
    if out.exists():
        for child in out.iterdir():
            if child.is_dir() and child.name.isdigit():
                shutil.rmtree(child)
    out.mkdir(parents=True, exist_ok=True)
    counts = {}
    for zoom in range(max_zoom + 1):
        unit = block * 2 ** (max_zoom - zoom)
        scaled_size = max(1, round(size / 2 ** (max_zoom - zoom)))
        scaled = image if scaled_size == size else image.resize((scaled_size, scaled_size), Image.LANCZOS)
        xs, ys = tile_range(zoom, max_zoom, block, bounds)
        n = 0
        for x in xs:
            column = out / str(zoom) / str(x)
            column.mkdir(parents=True, exist_ok=True)
            for y in ys:
                box = tile_box(x, y, unit, bounds, scaled_size, ortho_width)
                tile = scaled.transform((TILE, TILE), Image.EXTENT, box, Image.BICUBIC, fillcolor=(0, 0, 0, 0))
                tile.save(column / f"{y}.png", optimize=True)
                n += 1
        counts[zoom] = n
    Image.new("RGBA", (TILE, TILE), (0, 0, 0, 0)).save(out / "blank.png")
    info = {
        "format": FORMAT,
        "source": source,
        "textureSize": size,
        "orthoWidth": ortho_width,
        "center": {"x": center_x, "y": center_y},
        "axes": {"east": "y", "north": "x", "up": "z"},
        "tileSize": TILE,
        "mapBlockSize": block,
        "maxZoom": max_zoom,
        "mapSizeX": ortho_width,
        "mapSizeY": ortho_width,
        "mapSizeZ": height,
        "tiles": counts,
    }
    (out / "map.json").write_text(json.dumps(info, indent=2) + "\n")
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--texture", required=True, type=Path, help="the map texture: cooked .uexp or a PNG")
    ap.add_argument("--out", required=True, type=Path, help="tile directory to write")
    ap.add_argument("--ortho-width", type=float, default=DEFAULT_ORTHO_WIDTH,
                    help="world width the texture covers, in cm (MapMakerMarker OrthoWidth)")
    ap.add_argument("--center-x", type=float, default=0.0, help="world X at the texture center, cm")
    ap.add_argument("--center-y", type=float, default=0.0, help="world Y at the texture center, cm")
    ap.add_argument("--height", type=float, default=DEFAULT_HEIGHT, help="vertical extent reported as mapSizeZ, cm")
    args = ap.parse_args(argv)
    try:
        image = load_texture(args.texture)
        info = render(image, args.out, args.ortho_width, args.center_x, args.center_y, args.height,
                      args.texture.name)
    except (TextureError, OSError, ValueError) as exc:
        print(f"build-map-tiles: {exc}", file=sys.stderr)
        return 1
    total = sum(info["tiles"].values())
    print(f"wrote {total} tiles, maxZoom {info['maxZoom']}, mapBlockSize {info['mapBlockSize']:g} to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
