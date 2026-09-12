"""Offline image fixtures: alpha reuse, isolated components, icon/model parity."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageOps

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import prepare_parts as pp


class PreparePartsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-prepare-parts-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def save(self, image, name="input.png", **kwargs):
        path = self.folder / name
        image.save(path, **kwargs)
        return path

    def test_existing_alpha_is_preserved_without_background_removal(self):
        pixels = np.zeros((16, 16, 4), dtype=np.uint8)
        pixels[:, :, :3] = (200, 40, 240)  # Hidden RGB is arbitrary, not a background.
        pixels[3:13, 3:13] = (20, 120, 220, 255)
        pixels[3, 3:13, 3] = np.arange(10) * 25
        path = self.save(Image.fromarray(pixels))
        with mock.patch.object(pp, "remove_bg_color", side_effect=AssertionError), \
                mock.patch.object(pp, "remove_bg_vision", side_effect=AssertionError):
            for requested_method in ("auto", "color", "vision"):
                rgb, alpha, method, bg, _ = pp.prepare_image(path, requested_method)
                self.assertEqual(method, "alpha")
                self.assertIsNone(bg)
                np.testing.assert_array_equal(np.asarray(alpha), pixels[:, :, 3])
                np.testing.assert_array_equal(np.asarray(rgb), pixels[:, :, :3])

    def test_palette_transparency_is_preserved(self):
        source = Image.new("P", (10, 10), 0)
        source.putpalette([250, 200, 100, 30, 40, 50] + [0] * 762)
        source.paste(1, (2, 2, 8, 8))
        path = self.save(source, transparency=0)
        _, alpha, method, _, _ = pp.prepare_image(path)
        self.assertEqual(method, "alpha")
        self.assertEqual(alpha.getbbox(), (2, 2, 8, 8))

    def test_exif_rotation_keeps_rgb_and_alpha_aligned(self):
        source = Image.new("RGBA", (8, 12), (255, 0, 100, 0))
        source.paste((10, 220, 80, 200), (1, 2, 4, 8))
        exif = source.getexif()
        exif[274] = 6
        path = self.save(source, exif=exif)
        with Image.open(path) as stored:
            expected = ImageOps.exif_transpose(stored).convert("RGBA")
        rgb, alpha, method, _, _ = pp.prepare_image(path)
        self.assertEqual(method, "alpha")
        self.assertEqual(rgb.size, (12, 8))
        result = rgb.convert("RGBA")
        result.putalpha(alpha)
        np.testing.assert_array_equal(np.asarray(result), np.asarray(expected))

    def test_vision_receives_oriented_pixels_in_private_temporary_directory(self):
        source = Image.new("RGB", (8, 12), "white")
        source.paste((10, 220, 80), (1, 2, 4, 8))
        exif = source.getexif()
        exif[274] = 6
        path = self.save(source, exif=exif)
        seen = []

        def fake_swift(command, **_):
            seen.extend(command[2:4])
            with Image.open(command[2]) as oriented:
                self.assertEqual(oriented.size, (12, 8))
                self.assertNotIn(274, oriented.getexif())
                Image.new("L", oriented.size, 255).save(command[3])

        with mock.patch.object(pp.sys, "platform", "darwin"), \
                mock.patch.object(pp.subprocess, "run", side_effect=fake_swift):
            rgb, alpha, method, _, _ = pp.prepare_image(path, "vision")
        self.assertEqual(method, "vision")
        self.assertEqual(alpha.size, rgb.size)
        self.assertTrue(all(not Path(name).exists() for name in seen))
        self.assertFalse((self.folder / "._vision_mask.png").exists())

    def test_opaque_image_still_uses_color_background_removal(self):
        source = Image.new("RGB", (64, 64), "white")
        source.paste((10, 50, 180), (18, 18, 46, 46))
        _, alpha, method, bg, _ = pp.prepare_image(self.save(source))
        self.assertEqual(method, "color")
        self.assertEqual(bg, [255, 255, 255])
        self.assertEqual(alpha.getpixel((0, 0)), 0)
        self.assertEqual(alpha.getpixel((32, 32)), 255)

    def test_overlapping_bboxes_do_not_leak_other_component_into_cutout(self):
        pixels = np.zeros((80, 80, 4), dtype=np.uint8)
        pixels[8:72, 8:72] = (220, 20, 10, 255)
        pixels[18:62, 18:62] = 0  # Red ring surrounds a disconnected blue part.
        pixels[30:50, 30:50] = (10, 20, 220, 255)
        source = Image.fromarray(pixels)
        parts, masks = pp.prepare_segments(source.getchannel("A"))
        self.assertEqual(len(parts), 2)
        self.assertEqual(masks[0].getpixel((40, 40)), 0)
        self.assertEqual(masks[1].getpixel((40, 40)), 255)
        self.assertFalse(np.any((np.asarray(masks[0]) > 0) & (np.asarray(masks[1]) > 0)))
        for index, (part, mask) in enumerate(zip(parts, masks)):
            model = self.folder / f"model{index}.png"
            icon = self.folder / f"icon{index}.png"
            pp.render_part(source.convert("RGB"), mask, part["bbox"], model,
                           size=64, padding=0, alpha_out_path=icon)
            with Image.open(icon) as icon_image:
                rgba = np.asarray(icon_image)
                visible = rgba[:, :, 3] > 16
                dominant_channel = 0 if index == 0 else 2
                other_channel = 2 if index == 0 else 0
                self.assertTrue(np.all(rgba[:, :, dominant_channel][visible]
                                       > rgba[:, :, other_channel][visible]))

    def test_soft_edges_remain_owned_by_only_one_component(self):
        alpha = np.zeros((30, 40), dtype=np.uint8)
        alpha[5:25, 4:17] = 70
        alpha[8:22, 6:14] = 255
        alpha[5:25, 17:30] = 70  # Faint fringes touch, opaque cores do not.
        alpha[8:22, 20:28] = 255
        parts, masks = pp.prepare_segments(Image.fromarray(alpha))
        self.assertEqual(len(parts), 2)
        first, second = (np.asarray(mask) for mask in masks)
        self.assertFalse(np.any((first > 0) & (second > 0)))
        np.testing.assert_array_equal(first.astype(np.uint16) + second, alpha)

    def test_no_split_keeps_detached_fragments_and_translucent_pixels(self):
        alpha = Image.new("L", (40, 30), 0)
        alpha.paste(255, (10, 10, 25, 25))
        alpha.putpixel((35, 5), 100)
        parts, masks = pp.prepare_segments(alpha, no_split=True)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["bbox"], (10, 5, 36, 25))
        np.testing.assert_array_equal(np.asarray(masks[0]), np.asarray(alpha))

    def test_translucent_prop_is_not_rejected_and_empty_input_is(self):
        alpha = Image.new("L", (20, 20), 0)
        alpha.paste(90, (5, 5, 15, 15))
        parts, masks = pp.prepare_segments(alpha)
        self.assertEqual(parts[0]["bbox"], (5, 5, 15, 15))
        self.assertEqual(masks[0].getpixel((8, 8)), 90)
        with self.assertRaisesRegex(ValueError, "无前景"):
            pp.prepare_segments(Image.new("L", (20, 20), 0))

    def test_native_resolution_component_reaches_non_divisible_image_edges(self):
        alpha = Image.new("L", (1025, 769), 0)
        alpha.paste(255, (1001, 700, 1025, 769))
        parts, masks = pp.prepare_segments(alpha, min_area=0)
        self.assertEqual(parts[0]["bbox"], (1001, 700, 1025, 769))
        self.assertEqual(masks[0].getpixel((1024, 768)), 255)

    def test_icon_preserves_straight_alpha_without_baked_white_and_matches_model(self):
        rgb = Image.new("RGB", (8, 8), (0, 0, 255))
        alpha = Image.new("L", (8, 8), 128)
        model, icon = self.folder / "model.png", self.folder / "icon.png"
        pp.render_part(rgb, alpha, (0, 0, 8, 8), model, size=8, padding=0,
                       alpha_out_path=icon)
        with Image.open(icon) as cutout, Image.open(model) as generated_input:
            self.assertEqual(cutout.getpixel((4, 4)), (0, 0, 255, 128))
            expected = Image.alpha_composite(Image.new("RGBA", cutout.size, "white"),
                                             cutout).convert("RGB")
            np.testing.assert_array_equal(np.asarray(generated_input), np.asarray(expected))

    def test_standalone_keep_cutout_outputs_aligned_part_icons_and_metadata(self):
        source = Image.new("RGBA", (24, 24), (0, 0, 0, 0))
        source.paste((20, 180, 80, 255), (4, 4, 20, 20))
        path = self.save(source)
        output = self.folder / "prepared"
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "prepare_parts.py"), "--input", str(path),
             "--out-dir", str(output), "--no-split", "--keep-cutout", "--size", "32"],
            capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(result.stdout)["method"], "alpha")
        metadata = json.loads((output / "parts.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["parts"][0]["cutout"], str(Path("cutouts") / "part_00.png"))
        for relative in ("mask.png", "cutout.png", "parts/part_00.png", "cutouts/part_00.png"):
            self.assertTrue((output / relative).is_file())


if __name__ == "__main__":
    unittest.main()
