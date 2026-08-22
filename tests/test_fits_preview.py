import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fits_preview


class FitsPreviewTests(unittest.TestCase):
    def test_sigma_preview_writes_rgb_for_three_planes(self):
        data = np.stack(
            [
                np.array([[0.0, 10.0], [20.0, 30.0]]),
                np.array([[0.0, 20.0], [40.0, 60.0]]),
                np.array([[0.0, 30.0], [60.0, 90.0]]),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "preview.png"
            fits_preview.export_preview_png(output, data, stretch="sigma")
            with Image.open(output) as image:
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (2, 2))

    def test_warning_mask_shape_must_match_image(self):
        data = np.ones((2, 2), dtype=np.float32)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Warning mask shape"):
                fits_preview.export_preview_png(
                    Path(temporary) / "preview.png",
                    data,
                    warning_mask=np.zeros((3, 3), dtype=bool),
                )

    def test_rotate_preview_png_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            destination = root / "rotated.png"
            Image.fromarray(np.arange(12, dtype=np.uint8).reshape(3, 4), mode="L").save(source)
            fits_preview.rotate_preview_png(source, destination, 0.0)
            with Image.open(source) as source_image, Image.open(destination) as destination_image:
                self.assertEqual(source_image.size, (4, 3))
                self.assertEqual(destination_image.size, (4, 3))

    def test_annotation_writes_overlay_and_projects_north(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.png"
            output = root / "annotated.png"
            Image.fromarray(np.zeros((240, 320, 3), dtype=np.uint8), mode="RGB").save(source)
            fits_preview.annotate_preview_png(
                source,
                output,
                (1.0, 0.0, 0.0, 1.0),
                0.0,
                90.0,
                corner="UR",
            )
            with Image.open(output) as image:
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (320, 240))
                self.assertGreater(int(np.asarray(image).sum()), 0)

    def test_annotation_overlay_is_transparent_outside_marks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "overlay.png"
            fits_preview.write_annotation_overlay_png(
                output,
                (1.0, 0.0, 0.0, 1.0),
                0.0,
                90.0,
            )
            with Image.open(output) as image:
                rgba = np.asarray(image.convert("RGBA"))
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.size, (138, 138))
                self.assertEqual(int(rgba[0, 0, 3]), 0)
                self.assertGreater(int(np.count_nonzero(rgba[:, :, 3])), 0)


if __name__ == "__main__":
    unittest.main()
