import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import moving_target_stack as stacker
import moving_target_pipeline as pipeline
import astrometry_solve


class MedianAccumulatorTests(unittest.TestCase):
    def test_pixel_median_ignores_invalid_shift_borders(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "median.npy"
            accumulator = stacker.MedianAccumulator(path, 3, (2, 2))
            accumulator.add(np.array([[1, 10], [100, 5]], dtype=np.float32), np.ones((2, 2), dtype=bool))
            accumulator.add(
                np.array([[3, 20], [200, 7]], dtype=np.float32),
                np.array([[True, False], [True, True]]),
            )
            accumulator.add(np.array([[9, 30], [300, 11]], dtype=np.float32), np.ones((2, 2), dtype=bool))

            result = accumulator.finalize(row_chunk=1)
            accumulator.close(remove=True)

            np.testing.assert_allclose(result, np.array([[3, 20], [200, 7]], dtype=np.float64))
            self.assertFalse(path.exists())

    def test_median_excludes_exact_zero_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "median.npy"
            accumulator = stacker.MedianAccumulator(path, 3, (1, 2))
            mask = np.ones((1, 2), dtype=bool)
            accumulator.add(np.array([[0, 0]], dtype=np.float32), mask)
            accumulator.add(np.array([[0, 4]], dtype=np.float32), mask)
            accumulator.add(np.array([[9, 8]], dtype=np.float32), mask)

            result = accumulator.finalize(row_chunk=1)
            accumulator.close(remove=True)

            np.testing.assert_allclose(result, np.array([[9, 6]], dtype=np.float64))

    def test_rankfit_recovers_center_of_rank_polynomial(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "rankfit.npy"
            accumulator = stacker.MedianAccumulator(path, 13, (1, 1))
            rank = np.linspace(-1.0, 1.0, 13)
            values = 100.0 + 20.0 * rank + 5.0 * rank**2 - 2.0 * rank**5
            mask = np.ones((1, 1), dtype=bool)
            for value in values:
                accumulator.add(np.array([[value]], dtype=np.float32), mask)

            result = accumulator.finalize_rankfit(60, row_chunk=1)
            accumulator.close(remove=True)

            self.assertAlmostEqual(float(result[0, 0]), 100.0, places=4)

    def test_processing_method_token_records_rankfit_percentage(self):
        self.assertEqual(stacker.processing_method_token("mean", 50), "mean")
        self.assertEqual(stacker.processing_method_token("median", 50), "median")
        self.assertEqual(stacker.processing_method_token("rankfit", 37), "rankfit5_p37")


class PreviewTests(unittest.TestCase):
    def test_preview_percentiles_ignore_exact_zero_padding(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "preview.png"
            data = np.zeros((10, 10), dtype=np.float32)
            data[-1] = np.arange(10, 110, 10, dtype=np.float32)

            stacker.export_preview_png(path, data, low_percentile=0.0, high_percentile=100.0)

            preview = np.asarray(Image.open(path))
            self.assertEqual(int(preview[0, 0]), 0)
            self.assertEqual(int(preview[-1, 0]), 0)
            self.assertEqual(int(preview[-1, -1]), 255)
            self.assertGreater(int(preview[-1, 4]), 0)
            self.assertLess(int(preview[-1, 4]), 255)


class ReferenceSelectionTests(unittest.TestCase):
    def test_middle_selects_frame_nearest_temporal_midpoint(self):
        files = [Path("a.fit"), Path("b.fit"), Path("c.fit")]
        dates = {
            "a.fit": "2026-07-09T10:00:00Z",
            "b.fit": "2026-07-09T10:08:00Z",
            "c.fit": "2026-07-09T10:20:00Z",
        }

        def fake_header(path):
            return {"DATE-OBS": dates[path.name]}, [], 0

        with patch.object(stacker, "read_fits_header", side_effect=fake_header):
            self.assertEqual(stacker.select_reference_index(files, "first"), 1)
            self.assertEqual(stacker.select_reference_index(files, "middle"), 2)


class PlateSolveCacheTests(unittest.TestCase):
    def test_cache_paths_use_reference_stem_in_source_directory(self):
        args = type("Args", (), {"solve_dir": None, "solve_name": None})()
        reference = Path(r"C:\frames\Light_Comet_20.0s.fit")

        json_path, wcs_path = pipeline.solve_cache_paths(args, reference)

        self.assertEqual(json_path, reference.parent / "Light_Comet_20.0s_astrometry.json")
        self.assertEqual(wcs_path, reference.parent / "Light_Comet_20.0s_wcs.fits")

    def test_valid_cached_json_is_reused_without_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_dir = Path(temporary) / "frames"
            work_dir = Path(temporary) / "work"
            source_dir.mkdir()
            work_dir.mkdir()
            reference = source_dir / "Light_Target.fit"
            reference.write_bytes(b"not read when cache is valid")
            cache_json = source_dir / "Light_Target_astrometry.json"
            cache_json.write_text(
                '{"calibration":{"ra":1.0,"dec":2.0,"pixscale":3.0,"orientation":4.0}}',
                encoding="utf-8",
            )
            args = type(
                "Args",
                (),
                {
                    "wcs_fits": None,
                    "astrometry_json": None,
                    "skip_solve": False,
                    "solve_dir": None,
                    "solve_name": None,
                    "work_dir": work_dir,
                },
            )()

            with patch.object(pipeline, "run") as upload:
                wcs_path, json_path = pipeline.solve_first_frame(args, reference)

            self.assertIsNone(wcs_path)
            self.assertEqual(json_path, cache_json)
            upload.assert_not_called()

    def test_submission_checkpoint_can_resume_without_reupload(self):
        with tempfile.TemporaryDirectory() as temporary:
            result_path = Path(temporary) / "Light_Target_astrometry.json"
            checkpoint = Path(temporary) / "Light_Target_astrometry_submission.json"
            checkpoint.write_text('{"subid":15501234}', encoding="utf-8")

            self.assertEqual(pipeline.cached_submission_id(result_path), "15501234")


class AstrometryHelperTests(unittest.TestCase):
    def test_scale_hint_uses_fits_camera_metadata(self):
        hint = astrometry_solve.estimate_scale_hint(
            {
                "FOCALLEN": 250.0,
                "XPIXSZ": 2.9,
                "YPIXSZ": 2.9,
                "NAXIS1": 1920,
                "NAXIS2": 1080,
            }
        )

        self.assertIsNotNone(hint)
        self.assertAlmostEqual(hint["arcsecPerPix"], 2.391, places=2)
        self.assertAlmostEqual(hint["fovDeg"]["width"], 1.28, places=2)

    def test_multipart_body_contains_json_and_fits_parts(self):
        body, content_type = astrometry_solve.multipart_body(
            {"request-json": '{"session":"redacted"}'}, "frame.fit", b"SIMPLE  = T"
        )

        boundary = content_type.split("boundary=", 1)[1].encode("ascii")
        self.assertIn(b"request-json", body)
        self.assertIn(b"frame.fit", body)
        self.assertIn(b"SIMPLE  = T", body)
        self.assertIn(boundary, body)


if __name__ == "__main__":
    unittest.main()
