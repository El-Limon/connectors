"""Checks for the map tile builder: texture decoding, pyramid math and axis orientation."""

import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("build_map_tiles", Path(__file__).with_name("build-map-tiles.py"))
assert SPEC and SPEC.loader
tiles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tiles)

try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def cooked(width, height, pixels, tag=tiles.PACKAGE_FILE_TAG):
    header = b"\x00" * 120
    footer = struct.pack("<iii", width, height, 1) + b"\x00" * 12 + struct.pack("<I", tag)
    return header + pixels + footer


class DecodeTest(unittest.TestCase):
    def test_reads_the_inline_mip_before_the_footer(self):
        pixels = bytes(range(16)) * 4  # 4x4 BGRA
        w, h, out = tiles.decode_uexp(cooked(4, 4, pixels), b"..PF_B8G8R8A8..")
        self.assertEqual((w, h, out), (4, 4, pixels))

    def test_rejects_a_server_cook_without_pixels(self):
        # The server .uexp keeps only the footer: 273 bytes for a 4096 x 4096 texture.
        stripped = b"\x00" * 245 + struct.pack("<iii", 4096, 4096, 1) + b"\x00" * 12 + struct.pack("<I", tiles.PACKAGE_FILE_TAG)
        with self.assertRaisesRegex(tiles.TextureError, "server cooks strip"):
            tiles.decode_uexp(stripped)

    def test_rejects_other_pixel_formats_and_files(self):
        with self.assertRaisesRegex(tiles.TextureError, "PF_B8G8R8A8"):
            tiles.decode_uexp(cooked(1, 1, b"\x00" * 4), b"PF_DXT1")
        with self.assertRaisesRegex(tiles.TextureError, "package tag"):
            tiles.decode_uexp(cooked(1, 1, b"\x00" * 4, tag=0))


class LayoutTest(unittest.TestCase):
    def test_champlain_valley_pyramid(self):
        # 4096 px over 12 km: 128 px tiles of 375 m at zoom 4, a 2 x 2 grid at zoom 0.
        params = tiles.layout(4096, 1_200_000)
        self.assertEqual(params, {"mapBlockSize": 37_500.0, "maxZoom": 4})
        bounds = tiles.world_bounds(1_200_000, 0, 0)
        for zoom, count in [(0, 2), (1, 4), (4, 32)]:
            xs, ys = tiles.tile_range(zoom, 4, 37_500.0, bounds)
            self.assertEqual((len(xs), len(ys)), (count, count))
            self.assertEqual((xs.start, ys.start), (-count // 2, -count // 2))

    def test_tile_boxes_follow_the_game_widget_transform(self):
        # Tile (x=-1, y=0) at zoom 0 is west of the origin and north of it: the top-left quadrant.
        bounds = tiles.world_bounds(1_200_000, 0, 0)
        self.assertEqual(tiles.tile_box(-1, 0, 600_000, bounds, 256, 1_200_000), (0.0, 0.0, 128.0, 128.0))
        self.assertEqual(tiles.tile_box(0, -1, 600_000, bounds, 256, 1_200_000), (128.0, 128.0, 256.0, 256.0))

    def test_world_point_lands_on_the_matching_texture_pixel(self):
        # Owls Head Post Office (world X 101680, Y -516978) sits at u=0.0692, v=0.4153 of the map.
        width, size = 1_200_000, 4096
        u = (-516_978 + width / 2) / width
        v = (width / 2 - 101_680) / width
        self.assertAlmostEqual(u * size, 283.4, places=1)
        self.assertAlmostEqual(v * size, 1700.9, places=1)


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class RenderTest(unittest.TestCase):
    def test_quadrants_keep_north_up_and_east_right(self):
        from PIL import Image

        size = 512
        img = Image.new("RGBA", (size, size))
        colours = {"nw": (255, 0, 0, 255), "ne": (0, 255, 0, 255), "sw": (0, 0, 255, 255), "se": (255, 255, 0, 255)}
        half = size // 2
        img.paste(colours["nw"], (0, 0, half, half))
        img.paste(colours["ne"], (half, 0, size, half))
        img.paste(colours["sw"], (0, half, half, size))
        img.paste(colours["se"], (half, half, size, size))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "map"
            info = tiles.render(img, out, 1_200_000, 0, 0, 100_000, "test")
            self.assertEqual(info["maxZoom"], 1)
            self.assertEqual(info["mapBlockSize"], 300_000.0)
            self.assertEqual(info["tiles"], {0: 4, 1: 16})
            saved = json.loads((out / "map.json").read_text())
            for key in ("mapBlockSize", "maxZoom", "mapSizeX", "mapSizeY", "mapSizeZ"):
                self.assertEqual(saved[key], info[key])
            self.assertLess(saved["mapSizeZ"], saved["mapSizeY"])

            def centre(z, x, y):
                return Image.open(out / str(z) / str(x) / f"{y}.png").convert("RGBA").getpixel((64, 64))

            # x grows east (world +Y), y grows north (world +X).
            self.assertEqual(centre(0, 0, 0), colours["ne"])
            self.assertEqual(centre(0, -1, 0), colours["nw"])
            self.assertEqual(centre(0, -1, -1), colours["sw"])
            self.assertEqual(centre(0, 0, -1), colours["se"])
            self.assertEqual(centre(1, 1, 1), colours["ne"])
            self.assertEqual(Image.open(out / "blank.png").getpixel((0, 0))[3], 0)

    def test_uexp_round_trip(self):
        from PIL import Image

        bgra = bytes([0, 0, 255, 255]) * (256 * 256)  # pure red in BGRA
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.uexp"
            path.write_bytes(cooked(256, 256, bgra))
            (Path(tmp) / "map.uasset").write_bytes(b"PF_B8G8R8A8")
            img = tiles.load_texture(path)
            self.assertEqual(img.size, (256, 256))
            self.assertEqual(img.getpixel((10, 10)), (255, 0, 0, 255))
            self.assertIsInstance(img, Image.Image)


if __name__ == "__main__":
    unittest.main()
