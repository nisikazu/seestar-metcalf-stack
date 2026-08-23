import math
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import moving_target_stack as stacker


def legacy_shift_plane(data, dx, dy, source_valid=None):
    height, width = data.shape
    if abs(dx) < 1.0e-9 and abs(dy) < 1.0e-9:
        valid = np.ones((height, width), dtype=bool) if source_valid is None else source_valid.copy()
        return data.astype(np.float64, copy=True), valid
    yy, xx = np.indices((height, width), dtype=np.float32)
    src_x = xx - np.float32(dx)
    src_y = yy - np.float32(dy)
    x0 = np.floor(src_x).astype(np.int32)
    y0 = np.floor(src_y).astype(np.int32)
    x1 = x0 + 1
    y1 = y0 + 1
    valid = (x0 >= 0) & (y0 >= 0) & (x1 < width) & (y1 < height)
    if source_valid is not None and np.any(valid):
        positions = np.flatnonzero(valid)
        py = positions // width
        px = positions % width
        valid[py, px] = (
            source_valid[y0[py, px], x0[py, px]]
            & source_valid[y0[py, px], x1[py, px]]
            & source_valid[y1[py, px], x0[py, px]]
            & source_valid[y1[py, px], x1[py, px]]
        )
    output = np.zeros((height, width), dtype=np.float32)
    if np.any(valid):
        wx = src_x[valid] - x0[valid]
        wy = src_y[valid] - y0[valid]
        output[valid] = (
            (1.0 - wx) * (1.0 - wy) * data[y0[valid], x0[valid]]
            + wx * (1.0 - wy) * data[y0[valid], x1[valid]]
            + (1.0 - wx) * wy * data[y1[valid], x0[valid]]
            + wx * wy * data[y1[valid], x1[valid]]
        )
    return output, valid


def legacy_shift_image(data, dx, dy, source_valid=None):
    if data.ndim == 2:
        return legacy_shift_plane(data, dx, dy, source_valid)
    planes = []
    common = None
    for plane in data:
        shifted, mask = legacy_shift_plane(plane, dx, dy, source_valid)
        planes.append(shifted)
        common = mask if common is None else common & mask
    return np.stack(planes), common


def brute_canvas_shift(data, dx, dy, source_valid, canvas):
    source_height, source_width = data.shape
    output_height, output_width = canvas.shape
    yy, xx = np.indices(canvas.shape, dtype=np.float64)
    src_x = xx + canvas.origin_x - dx
    src_y = yy + canvas.origin_y - dy
    x0 = np.floor(src_x).astype(np.int32)
    y0 = np.floor(src_y).astype(np.int32)
    x1 = x0 + 1
    y1 = y0 + 1
    valid = (x0 >= 0) & (y0 >= 0) & (x1 < source_width) & (y1 < source_height)
    if source_valid is not None and np.any(valid):
        positions = np.flatnonzero(valid)
        py = positions // output_width
        px = positions % output_width
        valid[py, px] = (
            source_valid[y0[py, px], x0[py, px]]
            & source_valid[y0[py, px], x1[py, px]]
            & source_valid[y1[py, px], x0[py, px]]
            & source_valid[y1[py, px], x1[py, px]]
        )
    output = np.zeros(canvas.shape, dtype=np.float32)
    if np.any(valid):
        wx = src_x[valid] - x0[valid]
        wy = src_y[valid] - y0[valid]
        output[valid] = (
            (1.0 - wx) * (1.0 - wy) * data[y0[valid], x0[valid]]
            + wx * (1.0 - wy) * data[y0[valid], x1[valid]]
            + (1.0 - wx) * wy * data[y1[valid], x0[valid]]
            + wx * wy * data[y1[valid], x1[valid]]
        )
    return output, valid


class SliceTranslationRegressionTests(unittest.TestCase):
    def test_canvas_rebases_output_pixels_and_wcs_origin(self) -> None:
        canvas = stacker.StackCanvas(shape=(15, 19), origin_x=-3.5, origin_y=2.25)

        self.assertEqual(canvas.registration_to_output_pixel(10.0, 20.0), (13.5, 17.75))
        rebased = canvas.rebase_wcs_header({"CRPIX1": 5.0, "CRPIX2": 7.0, "CRVAL1": 42.0})
        self.assertEqual(rebased["CRPIX1"], 8.5)
        self.assertEqual(rebased["CRPIX2"], 4.75)
        self.assertEqual(rebased["CRVAL1"], 42.0)

    def setUp(self):
        y, x = np.indices((9, 11), dtype=np.float32)
        self.mono = 10.0 * y + x + 0.03125 * x * y
        self.rgb = np.stack((self.mono, self.mono * 1.7 + 3.0, self.mono * 0.3 - 2.0))
        self.valid = np.ones(self.mono.shape, dtype=bool)
        self.valid[2, 3] = False
        self.valid[6, 8] = False

    def assert_matches_legacy(self, data, dx, dy, valid):
        expected, expected_mask = legacy_shift_image(data, dx, dy, valid)
        actual, actual_mask = stacker.shift_image(data, dx, dy, valid)
        np.testing.assert_array_equal(actual_mask, expected_mask)
        np.testing.assert_allclose(actual, expected, rtol=0.0, atol=2.0e-5, equal_nan=True)
        np.testing.assert_array_equal(actual[..., ~actual_mask], expected[..., ~expected_mask])

    def test_positive_negative_integer_fractional_and_zero_axis_shifts(self):
        for dx, dy in (
            (0.0, 0.0),
            (2.0, 1.0),
            (-2.0, -1.0),
            (1.25, -2.75),
            (-1.5, 2.125),
            (0.0, 1.375),
            (-2.625, 0.0),
        ):
            with self.subTest(dx=dx, dy=dy):
                self.assert_matches_legacy(self.mono, dx, dy, self.valid)
                self.assert_matches_legacy(self.rgb, dx, dy, self.valid)

    def test_unmasked_nan_and_zero_padding_match_legacy_everywhere(self):
        data = self.mono.copy()
        data[1, 2] = np.nan
        data[3:5, 6:8] = 0.0
        self.assert_matches_legacy(data, 0.75, -1.25, None)

    def test_registered_valid_mask_with_nan_and_zero_padding_matches_legacy(self):
        data = self.rgb.copy()
        data[:, 0, :] = 0.0
        data[:, 4, 5] = np.nan
        valid = stacker.registered_valid_mask(data)
        self.assert_matches_legacy(data, -0.5, 1.75, valid)

    def test_saturation_mask_matches_legacy_mask_and_pixels(self):
        saturation = np.zeros(self.mono.shape, dtype=bool)
        saturation[1, 1] = True
        saturation[4:6, 7] = True
        expected_shifted, expected_valid = legacy_shift_plane(saturation.astype(np.float32), 1.4, -0.6)
        expected = expected_valid & (expected_shifted > 0.0)
        actual = stacker.shift_boolean_mask(saturation, 1.4, -0.6)
        np.testing.assert_array_equal(actual, expected)

    def test_comet_and_reference_star_apertures_match_legacy(self):
        expected, expected_mask = legacy_shift_image(self.rgb, 1.35, -0.65, self.valid)
        actual, actual_mask = stacker.shift_image(self.rgb, 1.35, -0.65, self.valid)
        yy, xx = np.indices(self.mono.shape)
        for label, centre, radius in (
            ("comet", (4.0, 5.0), 2.25),
            ("reference-star", (6.0, 3.0), 1.75),
        ):
            aperture = (
                ((yy - centre[0]) ** 2 + (xx - centre[1]) ** 2 <= radius * radius)
                & expected_mask
                & actual_mask
            )
            with self.subTest(aperture=label):
                self.assertGreater(int(np.count_nonzero(aperture)), 0)
                expected_flux = np.sum(expected[:, aperture], axis=1, dtype=np.float64)
                actual_flux = np.sum(actual[:, aperture], axis=1, dtype=np.float64)
                np.testing.assert_allclose(actual_flux, expected_flux, rtol=0.0, atol=1.0e-4)

    def test_output_canvas_is_independent_from_source_shape_and_origin(self):
        canvas = stacker.StackCanvas(shape=(13, 16), origin_x=-3.0, origin_y=-2.0)
        expected, expected_mask = brute_canvas_shift(self.mono, 1.25, -0.75, self.valid, canvas)
        actual, actual_mask = stacker.shift_plane(self.mono, 1.25, -0.75, self.valid, canvas)
        self.assertEqual(actual.shape, canvas.shape)
        np.testing.assert_array_equal(actual_mask, expected_mask)
        np.testing.assert_allclose(actual, expected, rtol=0.0, atol=2.0e-5)

    def test_empty_canvas_intersection_returns_zero_and_invalid(self):
        canvas = stacker.StackCanvas(shape=(4, 5), origin_x=100.0, origin_y=100.0)
        actual, valid = stacker.shift_plane(self.mono, 0.0, 0.0, self.valid, canvas)
        self.assertFalse(np.any(valid))
        self.assertFalse(np.any(actual))


class SeparableBackgroundRegressionTests(unittest.TestCase):
    def test_plane_and_quadratic_match_full_grid_for_mono_and_rgb(self):
        rng = np.random.default_rng(20260824)
        mono = rng.normal(500.0, 25.0, size=(17, 23)).astype(np.float32)
        rgb = np.stack((mono, mono * 1.2 + 10.0, mono * 0.8 - 5.0))
        valid = np.ones(mono.shape, dtype=bool)
        valid[0, :] = False
        valid[8, 11] = False
        for data in (mono, rgb):
            channel_count = 1 if data.ndim == 2 else data.shape[0]
            for mode in ("plane", "quadratic"):
                term_count = stacker.background_term_count(mode)
                coefficients = rng.normal(0.0, 3.0, size=(channel_count, term_count))
                planes = data[np.newaxis] if data.ndim == 2 else data
                correction = np.tensordot(
                    coefficients,
                    stacker.background_grid(*valid.shape, mode),
                    axes=(1, 0),
                )
                expected = planes.astype(np.float64, copy=True)
                expected[:, valid] += correction[:, valid]
                if data.ndim == 2:
                    expected = expected[0]
                with self.subTest(shape=data.shape, mode=mode):
                    actual = stacker.apply_background_model(data, valid, coefficients, mode)
                    np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1.0e-12)
                    np.testing.assert_array_equal(actual[..., ~valid], data[..., ~valid])


class BoundedFrameProcessingTests(unittest.TestCase):
    def test_ordered_bounded_map_preserves_order_and_worker_limit(self):
        lock = threading.Lock()
        active = 0
        maximum_active = 0

        def worker(value):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.005 * (4 - value % 4))
            with lock:
                active -= 1
            return value * value

        actual = list(stacker.ordered_bounded_map(worker, range(8), 2))
        self.assertEqual(actual, [value * value for value in range(8)])
        self.assertLessEqual(maximum_active, 2)
        self.assertGreaterEqual(maximum_active, 2)

    def test_one_and_two_workers_produce_identical_frame_products_and_stacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_header = {
                "BITPIX": -32,
                "DATE-OBS": "2026-08-24T00:00:00Z",
                "SATURATE": 1000.0,
            }
            tasks = []
            for index, (dx, dy) in enumerate(((0.0, 0.0), (1.25, -0.75), (-0.5, 1.5)), start=1):
                yy, xx = np.indices((160, 200), dtype=np.float32)
                mono = 300.0 + 0.25 * xx + 0.5 * yy + index
                data = np.stack((mono, mono * 1.1, mono * 0.9)).astype(np.float32)
                data[0, 10 + index, 12] = 950.0
                path = root / f"registered_{index:02d}.fit"
                stacker.write_fits_float32(path, data, source_header, {})
                tasks.append(
                    stacker.StackFrameTask(
                        index=index,
                        source_name=path.name,
                        registered=path,
                        source_header=source_header,
                        frame_time=datetime(2026, 8, 24, 0, index, tzinfo=timezone.utc),
                        target=stacker.TargetPoint(
                            datetime(2026, 8, 24, 0, index, tzinfo=timezone.utc),
                            10.0,
                            20.0,
                        ),
                        target_x=100.0,
                        target_y=80.0,
                        dx=dx,
                        dy=dy,
                    )
                )

            canvas = stacker.StackCanvas.reference_footprint((160, 200))

            def run(workers):
                function = lambda task: stacker.process_stack_frame(
                    task,
                    canvas,
                    "valid",
                    "offset",
                    True,
                    90.0,
                )
                results = list(stacker.ordered_bounded_map(function, tasks, workers))
                star_sum = star_count = metcalf_sum = metcalf_count = None
                for result in results:
                    star_sum, star_count = stacker.add_to_average(
                        star_sum, star_count, result.star_data, result.star_mask
                    )
                    metcalf_sum, metcalf_count = stacker.add_to_average(
                        metcalf_sum, metcalf_count, result.metcalf_data, result.metcalf_mask
                    )
                return results, stacker.finalize_average(star_sum, star_count), stacker.finalize_average(
                    metcalf_sum, metcalf_count
                )

            serial_results, serial_star, serial_metcalf = run(1)
            parallel_results, parallel_star, parallel_metcalf = run(2)
            np.testing.assert_array_equal(parallel_star, serial_star)
            np.testing.assert_array_equal(parallel_metcalf, serial_metcalf)
            for serial, parallel in zip(serial_results, parallel_results):
                self.assertEqual(serial.task.index, parallel.task.index)
                np.testing.assert_array_equal(parallel.star_mask, serial.star_mask)
                np.testing.assert_array_equal(parallel.metcalf_mask, serial.metcalf_mask)
                np.testing.assert_array_equal(parallel.star_data, serial.star_data)
                np.testing.assert_array_equal(parallel.metcalf_data, serial.metcalf_data)
            self.assertEqual(serial_results[0].timings["star_resample"], 0.0)


class ProgressiveCleanupTests(unittest.TestCase):
    def test_preprocessing_cleanup_preserves_only_final_sequence_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            registration_dir = Path(temporary)
            processed = [registration_dir / "pp_frame_00001.fit", registration_dir / "pp_frame_00002.fit"]
            obsolete = [
                registration_dir / "frame_src_00001.fit",
                registration_dir / "frame_00001.fit",
                registration_dir / "cc_frame_00001.fit",
            ]
            metadata = registration_dir / "preprocess.ssf"
            calibration_dir = registration_dir / "calibration"
            calibration_dir.mkdir()
            calibration = calibration_dir / "master_dark.fit"
            for path in [*processed, *obsolete, metadata, calibration]:
                path.write_bytes(b"test")

            removed = stacker.cleanup_after_preprocessing(registration_dir, processed)

            self.assertEqual(
                {Path(path).name for path in removed},
                {path.name for path in [*obsolete, calibration]},
            )
            self.assertTrue(all(path.is_file() for path in processed))
            self.assertTrue(metadata.is_file())
            self.assertFalse(calibration_dir.exists())


if __name__ == "__main__":
    unittest.main()
