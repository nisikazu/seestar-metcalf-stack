import io
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
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
import horizons_ephemeris as horizons


class FitsPatternTests(unittest.TestCase):
    def test_pipeline_default_pattern_accepts_fit_and_fits(self):
        with patch.object(sys, "argv", ["seestar-metcalf-stack", "frames"]):
            args = pipeline.parse_args()

        self.assertEqual(args.pattern, "*.fit*")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fit = root / "frame.fit"
            fits = root / "frame.fits"
            invalid = root / "frame.fits.invalid"
            fit.touch()
            fits.touch()
            invalid.touch()
            self.assertTrue(pipeline.is_fits_frame(fits))
            self.assertTrue(pipeline.is_fits_frame(fit))
            self.assertFalse(pipeline.is_fits_frame(invalid))

    def test_pipeline_accepts_site_and_pixel_scale_overrides(self):
        with patch.object(
            sys,
            "argv",
            [
                "seestar-metcalf-stack",
                "frames",
                "--site-longitude",
                "139.6",
                "--site-latitude",
                "+35.9",
                "--pixel-scale-arcsec",
                "0.959",
            ],
        ):
            args = pipeline.parse_args()

        self.assertEqual(args.site_longitude, 139.6)
        self.assertEqual(args.site_latitude, 35.9)
        self.assertAlmostEqual(args.pixel_scale_arcsec, 0.959)


class HorizonsObjectResolutionTests(unittest.TestCase):
    def test_site_override_has_priority_over_fits_header(self):
        args = Namespace(site_longitude=139.6, site_latitude=35.9, elevation_km=None, center="fits-site")
        frame = horizons.FitsFrame(
            Path("frame.fits"),
            datetime(2026, 8, 3, tzinfo=timezone.utc),
            "332P",
            140.0,
            36.0,
            100.0,
            None,
            None,
        )

        center, longitude, latitude, elevation = horizons.resolve_site_coordinates(args, frame)

        self.assertEqual((center, longitude, latitude), ("fits-site", 139.6, 35.9))
        self.assertAlmostEqual(elevation, 0.1)

    def test_missing_site_coordinates_fall_back_to_geocenter(self):
        args = Namespace(site_longitude=None, site_latitude=None, elevation_km=None, center="fits-site")
        frame = horizons.FitsFrame(
            Path("frame.fits"),
            datetime(2026, 8, 3, tzinfo=timezone.utc),
            "332P",
            None,
            None,
            None,
            None,
            None,
        )

        with redirect_stderr(io.StringIO()):
            center, longitude, latitude, elevation = horizons.resolve_site_coordinates(args, frame)

        self.assertEqual((center, longitude, latitude, elevation), ("geocenter", None, None, None))

    def test_compact_periodic_comet_prefers_designation(self):
        candidates = horizons.generate_object_candidates("24PSchaumasse")

        self.assertEqual(candidates[0].command, "DES=24P;CAP;NOFRAG")
        self.assertEqual(candidates[0].source, "compact-periodic-comet")

    def test_named_comet_without_spaces_is_normalized(self):
        candidates = horizons.generate_object_candidates("C2025A6 (Lemmon)")

        self.assertEqual(candidates[0].command, "DES=C/2025 A6;CAP;NOFRAG")

    def test_numbered_asteroid_keeps_name_as_fallback(self):
        candidates = horizons.generate_object_candidates("98943 Torifune")
        commands = [candidate.command for candidate in candidates]

        self.assertEqual(commands[0], "98943;")
        self.assertIn("NAME=Torifune;", commands)

    def test_explicit_horizons_command_is_not_rewritten(self):
        candidates = horizons.generate_object_candidates("DES=C/2025 A6;CAP;NOFRAG")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].command, "DES=C/2025 A6;CAP;NOFRAG")

    def test_missing_ephemeris_markers_classifies_no_match(self):
        with self.assertRaises(horizons.HorizonsIdentificationError):
            horizons.parse_horizons_result(
                "Small-body Index Search Results\nNo matches found.",
                [datetime(2025, 1, 1, tzinfo=timezone.utc)],
            )


class SessionListTests(unittest.TestCase):
    def test_verbose_session_resolution_prints_all_sessions_and_selection(self):
        sessions = [
            [
                (datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc), Path("first.fit")),
                (datetime(2026, 7, 20, 1, 1, tzinfo=timezone.utc), Path("second.fit")),
            ],
            [(datetime(2026, 7, 20, 3, 0, tzinfo=timezone.utc), Path("third.fit"))],
        ]
        args = Namespace(
            source_dir=Path("frames"),
            session_gap_min=60.0,
            session_index=1,
            session_at=None,
            count=None,
            include_failed_frames=False,
            verbose=True,
        )
        output = io.StringIO()

        with patch.object(pipeline, "load_sessions", return_value=sessions), redirect_stdout(output):
            selected_index, files, session_info = pipeline.resolve_session(args)

        rendered = output.getvalue()
        self.assertIn("Index  Frames", rendered)
        self.assertIn("    1       2", rendered)
        self.assertIn("<- selected", rendered)
        self.assertIn("    2       1", rendered)
        self.assertEqual(selected_index, 1)
        self.assertEqual(files, [Path("first.fit"), Path("second.fit")])
        self.assertEqual(session_info["session_count"], 2)


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

    def test_median_can_include_exact_zero_samples_for_legacy_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "median.npy"
            accumulator = stacker.MedianAccumulator(
                path,
                3,
                (1, 2),
                exclude_zero_samples=False,
            )
            mask = np.ones((1, 2), dtype=bool)
            accumulator.add(np.array([[0, 0]], dtype=np.float32), mask)
            accumulator.add(np.array([[0, 4]], dtype=np.float32), mask)
            accumulator.add(np.array([[9, 8]], dtype=np.float32), mask)

            result = accumulator.finalize(row_chunk=1)
            accumulator.close(remove=True)

            np.testing.assert_allclose(result, np.array([[0, 4]], dtype=np.float64))

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


class SubtractiveCompositeTests(unittest.TestCase):
    def test_inverse_moving_target_shift_restores_original_position(self):
        source = np.zeros((9, 11), dtype=np.float64)
        source[3, 4] = 100.0
        shifted, forward_valid = stacker.shift_image(source, 2.0, -1.0, np.ones_like(source, dtype=bool))
        restored, inverse_valid = stacker.inverse_moving_target_shift(shifted, 2.0, -1.0, forward_valid)

        self.assertGreater(float(np.max(shifted)), 0.0)
        self.assertGreater(float(np.max(restored)), 0.0)
        self.assertEqual(restored.shape, source.shape)
        self.assertTrue(np.any(inverse_valid))
        np.testing.assert_allclose(restored[2:6, 2:7], source[2:6, 2:7], atol=1.0e-5)

    def test_subtraction_removes_comet_and_preserves_star_signal(self):
        frame = np.full((7, 7), 10.0, dtype=np.float64)
        frame[3, 3] += 100.0
        frame[1, 5] += 25.0
        model = np.zeros_like(frame)
        model[3, 3] = 100.0
        valid = np.ones_like(frame, dtype=bool)
        cometless, output_valid = stacker.subtract_shifted_comet_model(frame, model, valid, valid)

        self.assertAlmostEqual(float(cometless[3, 3]), 10.0)
        self.assertAlmostEqual(float(cometless[1, 5]), 35.0)
        np.testing.assert_array_equal(output_valid, valid)

    def test_invalid_model_pixels_keep_original_frame(self):
        frame = np.arange(25, dtype=np.float64).reshape(5, 5) + 100.0
        model = np.full_like(frame, 7.0)
        frame_valid = np.ones_like(frame, dtype=bool)
        model_valid = np.ones_like(frame, dtype=bool)
        model_valid[0, 0] = False
        cometless, output_valid = stacker.subtract_shifted_comet_model(frame, model, frame_valid, model_valid)

        self.assertAlmostEqual(float(cometless[0, 0]), float(frame[0, 0]))
        self.assertAlmostEqual(float(cometless[2, 2]), float(frame[2, 2] - 7.0))
        np.testing.assert_array_equal(output_valid, frame_valid)

    def test_subtractive_intermediate_preserves_negative_values(self):
        frame = np.full((3, 3), 2.0, dtype=np.float64)
        model = np.full((3, 3), 5.0, dtype=np.float64)
        valid = np.ones((3, 3), dtype=bool)
        cometless, _valid = stacker.subtract_shifted_comet_model(frame, model, valid, valid)

        self.assertLess(float(np.min(cometless)), 0.0)
        self.assertAlmostEqual(float(cometless[1, 1]), -3.0)

    def test_cometless_plus_reference_model_reconstructs_scene(self):
        stars = np.full((5, 5), 12.0, dtype=np.float64)
        comet = np.zeros((5, 5), dtype=np.float64)
        comet[2, 2] = 30.0
        scene = stars + comet
        valid = np.ones_like(stars, dtype=bool)
        cometless, _valid = stacker.subtract_shifted_comet_model(scene, comet, valid, valid)
        reconstructed = stacker.add_reference_comet_model(cometless, comet, valid)

        np.testing.assert_allclose(reconstructed, scene)
        self.assertAlmostEqual(float(np.median(cometless)), 12.0)

    def test_subtractive_background_is_not_added_twice(self):
        stars = np.full((5, 5), 100.0, dtype=np.float64)
        comet = np.full((5, 5), 20.0, dtype=np.float64)
        scene = stars + comet
        valid = np.ones_like(stars, dtype=bool)
        cometless, _valid = stacker.subtract_shifted_comet_model(scene, comet, valid, valid)
        reconstructed = stacker.add_reference_comet_model(cometless, comet, valid)

        self.assertAlmostEqual(float(np.median(cometless)), 100.0)
        self.assertAlmostEqual(float(np.median(reconstructed)), 120.0)

    def test_sigma_clipping_rejects_moving_star_outlier_and_keeps_comet_signal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sigma.npy"
            accumulator = stacker.MedianAccumulator(path, 5, (1, 2))
            mask = np.ones((1, 2), dtype=bool)
            samples = [
                [100.0, 150.0],
                [100.0, 150.0],
                [100.0, 150.0],
                [1000.0, 150.0],
                [100.0, 150.0],
            ]
            for sample in samples:
                accumulator.add(np.asarray([sample], dtype=np.float32), mask)

            result = accumulator.finalize_sigma(sigma_low=3.0, sigma_high=3.0, row_chunk=1)
            accumulator.close(remove=True)

            np.testing.assert_allclose(result, np.array([[100.0, 150.0]], dtype=np.float64))

    def test_sigma_clipped_median_uses_asymmetric_thresholds(self):
        values = np.array([98.0, 100.0, 100.0, 100.0, 104.0], dtype=np.float64)

        result = stacker.sigma_clipped_median(values, sigma_low=1.0, sigma_high=10.0)

        self.assertAlmostEqual(float(result), 100.0)

    def test_processing_method_token_records_rankfit_percentage(self):
        self.assertEqual(stacker.processing_method_token("mean", 50), "mean")
        self.assertEqual(stacker.processing_method_token("median", 50), "median")
        self.assertEqual(stacker.processing_method_token("rankfit", 37), "rankfit5_p37")

    def test_directional_geometry_uses_inverse_comet_motion_and_perpendicular_filter(self):
        geometry = stacker.directional_filter_geometry([(0.0, 0.0), (4.0, 0.0)])

        self.assertAlmostEqual(geometry["comet_motion_dx_px"], -4.0)
        self.assertAlmostEqual(geometry["comet_motion_dy_px"], 0.0)
        self.assertAlmostEqual(abs(geometry["comet_motion_angle_deg"]), 180.0)
        self.assertAlmostEqual(geometry["star_trail_angle_deg"], 0.0)
        self.assertAlmostEqual(geometry["directional_filter_angle_deg"], 90.0)

    def test_directional_filter_suppresses_horizontal_bright_line(self):
        image = np.ones((11, 13), dtype=np.float64)
        image[5, 2:11] = 100.0

        cleaned, diagnostics = stacker.apply_directional_comet_filter(image, 90.0, size_px=2)

        self.assertLess(float(np.max(cleaned[5, 3:10])), 2.0)
        self.assertGreater(diagnostics["suppressed_pixels"], 0)

    def test_directional_filter_suppresses_vertical_bright_line(self):
        image = np.ones((13, 11), dtype=np.float64)
        image[2:11, 5] = 100.0

        cleaned, _diagnostics = stacker.apply_directional_comet_filter(image, 0.0, size_px=2)

        self.assertLess(float(np.max(cleaned[3:10, 5])), 2.0)

    def test_directional_filter_does_not_raise_dark_pixels_or_constant_background(self):
        constant = np.full((9, 9), 17.0, dtype=np.float64)
        cleaned_constant, _diagnostics = stacker.apply_directional_comet_filter(constant, 37.0, size_px=2)
        np.testing.assert_allclose(cleaned_constant, constant)

        image = np.full((9, 9), 10.0, dtype=np.float64)
        image[4, 4] = 1.0
        cleaned, _diagnostics = stacker.apply_directional_comet_filter(image, 90.0, size_px=2)
        self.assertAlmostEqual(float(cleaned[4, 4]), 1.0)
        self.assertLessEqual(float(np.max(cleaned)), float(np.max(image)))

    def test_directional_filter_ignores_invalid_samples(self):
        image = np.full((9, 9), 10.0, dtype=np.float64)
        image[4, 4] = 100.0
        image[4, 2] = 1000.0
        valid = np.ones_like(image, dtype=bool)
        valid[4, 2] = False

        cleaned, _diagnostics = stacker.apply_directional_comet_filter(
            image,
            0.0,
            size_px=2,
            valid_mask=valid,
        )

        self.assertLess(float(cleaned[4, 4]), 20.0)

    def test_directional_filter_preserves_pixel_when_samples_are_insufficient(self):
        image = np.full((9, 9), 10.0, dtype=np.float64)
        image[4, 4] = 100.0
        valid = np.zeros_like(image, dtype=bool)
        valid[4, 4] = True

        cleaned, _diagnostics = stacker.apply_directional_comet_filter(
            image,
            90.0,
            size_px=2,
            valid_mask=valid,
            minimum_valid_samples=3,
        )

        self.assertAlmostEqual(float(cleaned[4, 4]), 100.0)

    def test_directional_filter_processes_rgb_channels_independently(self):
        image = np.ones((3, 11, 13), dtype=np.float64)
        image[0, 5, 2:11] = 100.0
        image[1, 5, 2:11] = 200.0
        image[2, 5, 2:11] = 300.0

        cleaned, _diagnostics = stacker.apply_directional_comet_filter(image, 90.0, size_px=2)

        self.assertLess(float(np.max(cleaned[0, 5, 3:10])), 2.0)
        self.assertLess(float(np.max(cleaned[1, 5, 3:10])), 2.0)
        self.assertLess(float(np.max(cleaned[2, 5, 3:10])), 2.0)

    def test_directional_filter_output_never_exceeds_finite_input(self):
        image = np.full((3, 9, 9), 10.0, dtype=np.float64)
        image[0, 4, 3:6] = 100.0
        image[1, 2:7, 4] = 80.0
        image[2, 4, 4] = 50.0

        cleaned, _diagnostics = stacker.apply_directional_comet_filter(image, 45.0, size_px=2)

        self.assertTrue(np.all(cleaned <= image + 1.0e-9))

    def test_directional_core_protection_restores_sigma_core(self):
        sigma = np.full((9, 9), 10.0, dtype=np.float64)
        sigma[4, 4] = 100.0
        directional = sigma.copy()
        directional[4, 4] = 40.0

        protected, diagnostics = stacker.protect_directional_core(
            sigma,
            directional,
            5.0,
            5.0,
            2.0,
        )

        self.assertAlmostEqual(float(protected[4, 4]), 100.0)
        self.assertGreater(diagnostics["protected_pixels"], 0)


class ValidPixelMeanTests(unittest.TestCase):
    def test_registered_padding_is_detected_only_when_all_channels_are_zero(self):
        image = np.array(
            [
                [[0, 0], [1, 0]],
                [[0, 2], [1, 0]],
                [[0, 0], [1, 0]],
            ],
            dtype=np.float32,
        )

        mask = stacker.registered_valid_mask(image)

        np.testing.assert_array_equal(mask, np.array([[False, True], [True, False]]))

    def test_shift_mask_rejects_a_pixel_when_any_bilinear_source_is_padding(self):
        image = np.arange(9, dtype=np.float32).reshape(3, 3)
        source_valid = np.ones((3, 3), dtype=bool)
        source_valid[0, 0] = False

        _shifted, mask = stacker.shift_plane(image, 0.5, 0.5, source_valid)

        self.assertFalse(bool(mask[1, 1]))
        self.assertTrue(bool(mask[2, 2]))

    def test_mean_uses_integer_per_pixel_contribution_counts(self):
        first = np.array([[0, 10], [0, 10]], dtype=np.float32)
        second = np.full((2, 2), 20, dtype=np.float32)
        total = None
        counts = None

        total, counts = stacker.add_to_average(total, counts, first, stacker.registered_valid_mask(first))
        total, counts = stacker.add_to_average(total, counts, second, stacker.registered_valid_mask(second))
        result = stacker.finalize_average(total, counts)

        self.assertEqual(counts.dtype, np.uint32)
        np.testing.assert_array_equal(counts, np.array([[1, 2], [1, 2]], dtype=np.uint32))
        np.testing.assert_allclose(result, np.array([[20, 15], [20, 15]], dtype=np.float64))


class DualAlignmentImageTests(unittest.TestCase):
    def test_circular_target_mask_excludes_moving_target_from_star_mean(self):
        first = np.full((5, 7), 100.0, dtype=np.float32)
        second = np.full((5, 7), 100.0, dtype=np.float32)
        first[2, 2] = 900.0
        second[2, 4] = 900.0

        total = None
        counts = None
        for image, x in ((first, 3.0), (second, 5.0)):
            target_mask = stacker.circular_target_mask(image.shape, x, 3.0, 0.75)
            valid = stacker.registered_valid_mask(image) & ~target_mask
            total, counts = stacker.add_to_average(total, counts, image, valid)

        result = stacker.finalize_average(total, counts)

        self.assertEqual(int(counts[2, 2]), 1)
        self.assertEqual(int(counts[2, 4]), 1)
        self.assertEqual(int(counts[2, 3]), 2)
        self.assertEqual(float(result[2, 2]), 100.0)
        self.assertEqual(float(result[2, 4]), 100.0)

    def test_target_shift_places_comet_at_reference_pixel(self):
        image = np.zeros((6, 8), dtype=np.float32)
        image[1, 2] = 50.0

        shifted, valid = stacker.shift_image(image, 2.0, 1.0, np.ones_like(image, dtype=bool))

        self.assertTrue(bool(valid[2, 4]))
        self.assertEqual(float(shifted[2, 4]), 50.0)
        self.assertEqual(float(np.max(shifted)), 50.0)

    def test_zero_contribution_pixel_is_comet_weight_one(self):
        support, diagnostics = stacker.build_reliability_support(
            np.array([[True, True, False]], dtype=bool),
            np.array([[0, 2, 0]], dtype=np.uint32),
            used_frames=4,
            minimum_star_fraction=0.5,
        )
        self.assertTrue(bool(support[0, 0]))
        self.assertFalse(bool(support[0, 1]))
        self.assertEqual(diagnostics["low_contribution_area_pixels"], 1)

    def test_low_contribution_region_is_added_only_inside_target_support(self):
        target = np.zeros((7, 7), dtype=bool)
        target[2:5, 2:5] = True
        counts = np.full((7, 7), 10, dtype=np.uint32)
        counts[3, 3] = 0
        counts[0, 0] = 0
        support, diagnostics = stacker.build_reliability_support(
            target,
            counts,
            used_frames=10,
            minimum_star_fraction=0.8,
            dilation_px=1.0,
        )
        self.assertTrue(bool(support[3, 3]))
        self.assertFalse(bool(support[0, 0]))
        self.assertLess(int(np.count_nonzero(support)), 49)
        self.assertGreater(diagnostics["dilated_low_contribution_area_pixels"], 1)

    def test_reliability_support_does_not_spread_over_the_whole_image(self):
        target = np.zeros((31, 31), dtype=bool)
        target[15, 15] = True
        counts = np.full((31, 31), 10, dtype=np.uint32)
        counts[15, 15] = 0
        support, _diagnostics = stacker.build_reliability_support(
            target,
            counts,
            used_frames=10,
            minimum_star_fraction=0.75,
            dilation_px=2.0,
        )
        self.assertLess(int(np.count_nonzero(support)), 50)

    def test_local_background_matching_recovers_known_offset(self):
        star = np.full((1, 9, 9), 1000.0, dtype=np.float64)
        comet = np.full((1, 9, 9), 950.0, dtype=np.float64)
        annulus = np.ones((9, 9), dtype=bool)
        annulus[3:6, 3:6] = False
        valid = np.ones((9, 9), dtype=bool)
        star[0, 0, 0] = 100000.0
        comet[0, 0, 0] = 0.0

        local_star, local_comet, counts = stacker.robust_local_background_match(
            star,
            comet,
            annulus,
            valid,
            valid,
        )

        self.assertEqual(int(counts[0]), 71)
        self.assertAlmostEqual(float(local_star[0]), 1000.0)
        self.assertAlmostEqual(float(local_comet[0]), 950.0)
        self.assertAlmostEqual(float(local_star[0] - local_comet[0]), 50.0)

    def test_circular_fallback_remains_core_only(self):
        shape = (21, 21)
        star = np.zeros(shape, dtype=np.float64)
        comet = np.zeros(shape, dtype=np.float64)
        valid = np.ones(shape, dtype=bool)
        mask, diagnostics = stacker.build_tail_composite_mask(
            shape,
            11.0,
            11.0,
            2.0,
            2.0,
            star,
            comet,
            valid,
            valid,
            np.array([0.0]),
            np.array([0.0]),
            tail_sigma=3.0,
            tail_smoothing_px=1.0,
            tail_length_px=10.0,
        )
        core = stacker.circular_target_mask(shape, 11.0, 11.0, 2.0)
        np.testing.assert_array_equal(mask > 0.0, core)
        self.assertEqual(diagnostics["fallback"], "no-threshold-crossing")

    def test_automatic_radius_uses_registration_fwhm(self):
        radius, source = stacker.resolve_comet_mask_radius(None, [2.0, 3.0, None])

        self.assertEqual(source, "auto-fwhm")
        self.assertAlmostEqual(radius, 7.5)

    def test_tail_mask_keeps_core_connected_structure_and_rejects_isolated_structure(self):
        shape = (31, 31)
        star = np.zeros(shape, dtype=np.float64)
        comet = np.zeros(shape, dtype=np.float64)
        comet[14:17, 15:26] = 20.0
        comet[5:8, 5:8] = 100.0
        valid = np.ones(shape, dtype=bool)

        mask, diagnostics = stacker.build_tail_composite_mask(
            shape,
            16.0,
            16.0,
            2.0,
            2.0,
            star,
            comet,
            valid,
            valid,
            np.array([0.0]),
            np.array([0.0]),
            tail_sigma=3.0,
            tail_smoothing_px=1.0,
            tail_length_px=20.0,
        )
        core = stacker.circular_target_mask(shape, 16.0, 16.0, 2.0)

        self.assertEqual(diagnostics["method"], "tail")
        self.assertGreater(int(np.count_nonzero(mask > 0.0)), int(np.count_nonzero(core)))
        self.assertEqual(float(mask[15, 25]), 1.0)
        self.assertEqual(float(mask[6, 6]), 0.0)

    def test_dual_fits_header_records_process_and_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "comet_stars.fit"
            stacker.write_fits_float32(
                path,
                np.ones((4, 4), dtype=np.float32),
                {"OBJECT": "10P"},
                {
                    "MTPROC": "DUALCOMP",
                    "MTCOMP": "STAR+COMET",
                    "MTREFUTC": "2026-08-06T15:00:00Z",
                    "MTMASKR": 8.0,
                    "MTFEATH": 8.0,
                },
                history=[
                    "Dual-alignment composite generated from the same source frames",
                    "Star-aligned master + comet-aligned master",
                    "This image is a composite and should not be used directly for photometry",
                ],
            )
            header, cards, _offset = stacker.read_fits_header(path)

        self.assertEqual(header["MTPROC"], "DUALCOMP")
        self.assertEqual(header["MTCOMP"], "STAR+COMET")
        self.assertEqual(header["MTMASKR"], 8.0)
        self.assertTrue(any("Dual-alignment composite" in card for card in cards))
        self.assertTrue(any("not be used directly for photometry" in card for card in cards))


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

    def test_shared_preview_limits_apply_the_same_stretch_to_all_images(self):
        stars = np.array([[10.0, 20.0]], dtype=np.float32)
        comet = np.array([[10.0, 40.0]], dtype=np.float32)
        composite = np.array([[10.0, 60.0]], dtype=np.float32)

        limits = stacker.preview_stretch_limits([stars, comet, composite], 0.0, 100.0)

        self.assertEqual(limits, (10.0, 60.0))
        with tempfile.TemporaryDirectory() as temporary:
            paths = [Path(temporary) / name for name in ("stars.png", "comet.png", "composite.png")]
            for path, data in zip(paths, (stars, comet, composite)):
                stacker.export_preview_png(path, data, low_percentile=0.0, high_percentile=100.0, value_limits=limits)
            images = [np.asarray(Image.open(path)) for path in paths]

        self.assertEqual(int(images[0][0, 0]), int(images[1][0, 0]))
        self.assertEqual(int(images[1][0, 0]), int(images[2][0, 0]))
        self.assertEqual(int(images[0][0, 1]), 51)
        self.assertEqual(int(images[1][0, 1]), 153)
        self.assertEqual(int(images[2][0, 1]), 255)

    def test_contribution_count_png_scales_zero_to_max(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stars_contribution_count.png"
            stacker.export_count_png(path, np.array([[0, 2], [4, 8]], dtype=np.uint32))
            preview = np.asarray(Image.open(path))

        np.testing.assert_array_equal(preview, np.array([[0, 64], [128, 255]], dtype=np.uint8))


class SaturationWarningTests(unittest.TestCase):
    def test_unsigned_seestar_fits_saturation_level_is_65535(self):
        level = stacker.fits_saturation_level(
            {
                "BITPIX": 16,
                "BZERO": 32768,
                "BSCALE": 1,
            }
        )

        self.assertEqual(level, 65535.0)

    def test_saturation_keyword_overrides_storage_full_scale(self):
        level = stacker.fits_saturation_level(
            {
                "BITPIX": 16,
                "BZERO": 32768,
                "BSCALE": 1,
                "SATURATE": 60000,
            }
        )

        self.assertEqual(level, 60000.0)

    def test_datamax_does_not_replace_detector_full_scale(self):
        level = stacker.fits_saturation_level(
            {
                "BITPIX": 16,
                "BZERO": 32768,
                "BSCALE": 1,
                "DATAMAX": 12000,
            }
        )

        self.assertEqual(level, 65535.0)

    def test_detection_uses_strictly_greater_than_threshold(self):
        data = np.array(
            [
                [[58981.5, 58982.0], [100.0, 200.0]],
                [[0.0, 10.0], [20.0, 30.0]],
                [[0.0, 10.0], [20.0, 30.0]],
            ],
            dtype=np.float32,
        )

        mask, level, threshold, maximum = stacker.detect_saturation(
            data,
            {"BITPIX": 16, "BZERO": 32768, "BSCALE": 1},
            90.0,
        )

        np.testing.assert_array_equal(mask, np.array([[False, True], [False, False]]))
        self.assertEqual(level, 65535.0)
        self.assertEqual(threshold, 58981.5)
        self.assertEqual(maximum, 58982.0)

    def test_shifted_warning_mask_marks_all_touched_pixels(self):
        mask = np.zeros((3, 3), dtype=bool)
        mask[1, 1] = True

        shifted = stacker.shift_boolean_mask(mask, 0.5, 0.5)

        expected = np.zeros((3, 3), dtype=bool)
        expected[1:3, 1:3] = True
        np.testing.assert_array_equal(shifted, expected)

    def test_warning_preview_uses_requested_rgb_color(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "warning.png"
            data = np.array([[10.0, 20.0], [30.0, 40.0]], dtype=np.float32)
            warning_mask = np.array([[False, True], [False, False]])

            stacker.export_preview_png(
                path,
                data,
                low_percentile=0.0,
                high_percentile=100.0,
                warning_mask=warning_mask,
                warning_color=stacker.saturation_rgb("12AB34"),
            )

            preview = np.asarray(Image.open(path))
            self.assertEqual(preview.shape, (2, 2, 3))
            np.testing.assert_array_equal(preview[0, 1], np.array([0x12, 0xAB, 0x34], dtype=np.uint8))

    def test_color_accepts_optional_hash_and_normalizes_case(self):
        self.assertEqual(stacker.normalize_saturation_color("#ff0000"), "FF0000")
        with self.assertRaises(ValueError):
            stacker.normalize_saturation_color("red")


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

    def test_explicit_filename_overrides_mode(self):
        files = [Path("a.fit"), Path("b.fit"), Path("c.fit")]

        self.assertEqual(stacker.select_reference_index(files, "first", "c.fit"), 3)
        self.assertEqual(stacker.select_reference_index(files, "middle", "a.fit"), 1)

    def test_explicit_filename_must_be_in_selected_frames(self):
        files = [Path("a.fit"), Path("b.fit"), Path("c.fit")]

        with self.assertRaisesRegex(ValueError, "was not found"):
            stacker.select_reference_index(files, "first", "missing.fit")

    def test_explicit_filename_accepts_a_path_with_spaces(self):
        files = [Path("a.fit"), Path("Light C2025 R2 (SWAN).fit"), Path("c.fit")]

        self.assertEqual(
            stacker.select_reference_index(
                files,
                "first",
                "C:/frames with spaces/Light C2025 R2 (SWAN).fit",
            ),
            2,
        )


class RegistrationValidationTests(unittest.TestCase):
    def test_seq_parser_reads_quality_and_registration_metrics(self):
        content = "\n".join(
            [
                "S 'frame_' 1 2 2 5 0 6 0 0 0",
                "I 1 1",
                "I 2 1",
                "R1 2.31961 2.41961 0.65534 0 0.193171 8 H 1 0 0 0 1 0 0 0 1",
                "R1 3.00499 4.50748 0.738075 0 0.192033 11 H 1 0 14.5 0 1 -1.2 0 0 1",
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            seq = Path(temporary) / "frame_.seq"
            seq.write_text(content, encoding="utf-8")
            registrations = stacker.parse_siril_registration(seq)

        self.assertEqual(registrations[1].detected_stars, 8)
        self.assertAlmostEqual(registrations[1].fwhm_px, 2.31961)
        self.assertAlmostEqual(registrations[1].weighted_fwhm_px, 2.41961)
        self.assertAlmostEqual(registrations[1].roundness, 0.65534)
        self.assertAlmostEqual(registrations[2].star_tx_px, 14.5)

    def test_siril_log_parser_separates_detected_stars_from_matched_pairs(self):
        output = "\n".join(
            [
                "log: Matching stars in image 19: done",
                "log: Initial pair matches: 7",
                "log: Pair matches after fitting: 6",
                "log: Inliers: 0.857",
                "log: Reading FITS: another frame",
                "log: Matching stars in image 139: done",
                "log: Initial pair matches: 8",
                "log: Pair matches after fitting: 7",
                "log: Inliers: 0.875",
            ]
        )

        diagnostics = stacker.parse_siril_match_diagnostics(output)

        self.assertEqual(diagnostics[19].initial_pairs, 7)
        self.assertEqual(diagnostics[19].fitted_pairs, 6)
        self.assertAlmostEqual(diagnostics[19].inlier_fraction, 0.857)
        self.assertEqual(diagnostics[139].fitted_pairs, 7)

    def test_failed_registration_findstar_parser_keeps_per_frame_quality(self):
        output = "\n".join(
            [
                "log: Reading FITS: file frame_00001.fit, 3 layer(s)",
                "log: Found 4 Gaussian profile stars in image, channel #1 (FWHM 2.360296)",
                "log: Reading FITS: file frame_00002.fit, 3 layer(s)",
                "log: Found 7 Gaussian profile stars in image, channel #1 (FWHM 2.904935)",
            ]
        )
        catalog = "\n".join(
            [
                "# star#\tlayer\tB\tA\tbeta\tX\tY\tFWHMx [px]\tFWHMy [px]",
                "1\t1\t0\t0\t-1\t10\t20\t2.40\t2.00",
                "2\t1\t0\t0\t-1\t30\t40\t2.20\t2.20",
                "3\t1\t0\t0\t-1\t50\t60\t2.50\t2.00",
                "4\t1\t0\t0\t-1\t70\t80\t2.10\t2.00",
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            work_dir = Path(temporary)
            (work_dir / "frame_stars_00001.tsv").write_text(catalog, encoding="utf-8")
            registrations = stacker.parse_siril_findstar_diagnostics(output, work_dir, "frame", 2)

        self.assertEqual(registrations[1].detected_stars, 4)
        self.assertAlmostEqual(registrations[1].fwhm_px, 2.360296)
        self.assertAlmostEqual(registrations[1].roundness, 0.892857, places=5)
        self.assertEqual(registrations[2].detected_stars, 7)
        self.assertAlmostEqual(registrations[2].fwhm_px, 2.904935)

    def test_findstar_diagnostic_script_processes_frames_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "diagnose.ssf"
            stacker.write_siril_findstar_script(script, "frame", 2)
            content = script.read_text(encoding="ascii")

        self.assertIn("load frame_00001.fit", content)
        self.assertIn("findstar -layer=1 -out=frame_stars_00001.tsv", content)
        self.assertLess(content.index("load frame_00001.fit"), content.index("load frame_00002.fit"))

    def test_diagnostic_rows_keep_excluded_frames_and_roundness(self):
        matrix = (1.0, 0.0, 12.5, 0.0, 1.0, -3.0, 0.0, 0.0, 1.0)
        registrations = {
            1: stacker.SirilRegistration(
                index=1,
                selected=True,
                detected_stars=12,
                fwhm_px=2.4,
                weighted_fwhm_px=2.6,
                roundness=0.82,
                matrix=matrix,
            ),
            2: stacker.SirilRegistration(index=2, selected=False, detected_stars=3),
        }
        matches = {1: stacker.SirilMatchDiagnostics(index=1, fitted_pairs=8, inlier_fraction=0.9)}

        rows = stacker.build_registration_diagnostic_rows(
            [Path("good.fit"), Path("poor.fit")],
            1,
            registrations,
            matches,
            {2: ["not selected by Siril"]},
        )

        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["is_reference"])
        self.assertEqual(rows[0]["roundness"], 0.82)
        self.assertEqual(rows[0]["fitted_matched_pairs"], 8)
        self.assertFalse(rows[1]["used"])
        self.assertEqual(rows[1]["reason"], "not selected by Siril")

    def test_validation_accepts_every_registered_frame_with_enough_detected_stars(self):
        files = [Path("first.fit"), Path("second.fit")]
        matrix = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        registrations = {
            1: stacker.SirilRegistration(index=1, selected=True, detected_stars=6, matrix=matrix),
            2: stacker.SirilRegistration(index=2, selected=True, detected_stars=8, matrix=matrix),
        }
        with tempfile.TemporaryDirectory() as temporary:
            registration_dir = Path(temporary)
            for index in (1, 2):
                (registration_dir / f"r_frame_{index:05d}.fit").write_bytes(b"registered")

            issues = stacker.registration_validation_issues(
                files, registration_dir, "frame", registrations, 6
            )

        self.assertEqual(issues, {})

    def test_validation_reports_per_frame_insufficient_stars_and_registered_frame(self):
        files = [Path("first.fit"), Path("second.fit")]
        matrix = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        registrations = {
            1: stacker.SirilRegistration(index=1, selected=True, detected_stars=5, matrix=matrix),
            2: stacker.SirilRegistration(index=2, selected=False),
        }
        with tempfile.TemporaryDirectory() as temporary:
            registration_dir = Path(temporary)
            (registration_dir / "r_frame_00001.fit").write_bytes(b"registered")

            issues = stacker.registration_validation_issues(
                files, registration_dir, "frame", registrations, 6
            )

        self.assertEqual(set(issues), {1, 2})
        self.assertEqual(issues[1], ["only 5 detected star(s); requires 6"])
        self.assertIn("registered FITS was not produced", issues[2])
        self.assertIn("not selected by Siril", issues[2])


class PlateSolveCacheTests(unittest.TestCase):
    def test_cache_paths_use_reference_stem_in_source_directory(self):
        args = type("Args", (), {"solve_dir": None, "solve_name": None})()
        with tempfile.TemporaryDirectory() as temporary:
            reference = Path(temporary) / "frames" / "Light_Comet_20.0s.fit"

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

    def test_scale_hint_accepts_command_line_pixel_scale(self):
        hint = astrometry_solve.estimate_scale_hint({"NAXIS1": 3856, "NAXIS2": 2180}, 0.959)

        self.assertEqual(hint["source"], "command-line")
        self.assertAlmostEqual(hint["arcsecPerPix"], 0.959)
        self.assertAlmostEqual(hint["fovDeg"]["width"], 1.027, places=2)

    def test_wcs_response_validation_rejects_html(self):
        self.assertFalse(astrometry_solve.is_valid_wcs_bytes(b"<?xml version=\"1.0\"?>"))
        simple = b"SIMPLE  =                    T".ljust(80)
        end = b"END".ljust(80)
        self.assertTrue(astrometry_solve.is_valid_wcs_bytes(simple + end))

    def test_multipart_body_contains_json_and_fits_parts(self):
        body, content_type = astrometry_solve.multipart_body(
            {"request-json": '{"session":"redacted"}'}, "frame.fit", b"SIMPLE  = T"
        )

        boundary = content_type.split("boundary=", 1)[1].encode("ascii")
        self.assertIn(b"request-json", body)
        self.assertIn(b"frame.fit", body)
        self.assertIn(b"SIMPLE  = T", body)
        self.assertIn(boundary, body)


class VerboseOutputTests(unittest.TestCase):
    def test_stack_summary_parser_ignores_braces_in_verbose_output(self):
        output = 'Siril message {not json}\n[stack:mean] frame 2/2\n{"used_frames": 2, "work_dir": "C:/work"}\n'

        summary = pipeline.parse_stack_summary(output)

        self.assertEqual(summary["used_frames"], 2)

    def test_siril_disk_space_failure_is_detected_even_with_zero_exit_status(self):
        output = "\n".join(
            [
                "log: Not enough free disk space to perform this operation: 9.3 GiB available for 11.3 GiB needed",
                "log: Registration aborted.",
                "log: Script execution failed.",
            ]
        )

        reason = stacker.siril_failure_reason(output)

        self.assertIsNotNone(reason)
        self.assertIn("Not enough free disk space", reason)
        self.assertIn("Registration aborted", reason)

    def test_siril_success_output_has_no_failure_reason(self):
        self.assertIsNone(stacker.siril_failure_reason("log: Registration finished.\nprogress: 100%"))

    def test_siril_reference_star_count_is_read_from_registration_output(self):
        output = "log: Found 37 stars in reference, channel #1\n"

        self.assertEqual(stacker.siril_reference_star_count(output), 37)

    def test_siril_reference_star_count_is_none_when_not_reported(self):
        self.assertIsNone(stacker.siril_reference_star_count("log: Registration aborted."))

    def test_no_registered_image_has_actionable_stack_error(self):
        output = "\n".join(
            [
                "log: No image was registered to the reference",
                "log: Registration aborted.",
            ]
        )

        message = pipeline.child_error_message(output)

        self.assertIn("could not align any frame", message)
        self.assertIn("--reference-frame-file", message)

    def test_explicit_child_error_is_forwarded_without_traceback(self):
        output = "progress: 100%\nERROR: Selected reference frame is unsuitable.\n"

        self.assertEqual(
            pipeline.child_error_message(output),
            "Selected reference frame is unsuitable.",
        )

    def test_siril_failure_message_names_reference_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            diagnostics = Path(temporary) / "registration_diagnostics.csv"
            diagnostics.write_text("index,source\n", encoding="utf-8")
            message = stacker.siril_registration_failure_message(
                "log: No image was registered to the reference",
                142,
                "Light 10P Tempel.fit",
                diagnostics,
            )

        self.assertIn("reference 142 (Light 10P Tempel.fit)", message)
        self.assertIn("--reference-frame-file", message)
        self.assertIn("registration_diagnostics.csv", message)


class CrossPlatformCliTests(unittest.TestCase):
    def test_pipeline_verbose_and_open_output_are_enabled_by_default(self):
        with patch.object(sys, "argv", ["seestar-metcalf-stack", "frames"]):
            args = pipeline.parse_args()

        self.assertTrue(args.verbose)
        self.assertTrue(args.open_output)
        self.assertEqual(args.saturation_warning, "disable")
        self.assertEqual(args.saturation_threshold_percent, 90.0)
        self.assertEqual(args.saturation_color, "FF0000")
        self.assertEqual(args.padding_policy, "valid")
        self.assertEqual(args.zero_sample_policy, "exclude")

    def test_pipeline_accepts_legacy_padding_and_zero_inclusion(self):
        with patch.object(
            sys,
            "argv",
            [
                "seestar-metcalf-stack",
                "frames",
                "--padding-policy",
                "legacy",
                "--zero-sample-policy",
                "include",
            ],
        ):
            args = pipeline.parse_args()

        self.assertEqual(args.padding_policy, "legacy")
        self.assertEqual(args.zero_sample_policy, "include")

    def test_pipeline_accepts_explicit_saturation_warning_options(self):
        with patch.object(
            sys,
            "argv",
            [
                "seestar-metcalf-stack",
                "frames",
                "--saturation-warning",
                "enable",
                "--saturation-threshold-percent",
                "87.5",
                "--saturation-color",
                "12ab34",
            ],
        ):
            args = pipeline.parse_args()

        self.assertEqual(args.saturation_warning, "enable")
        self.assertEqual(args.saturation_threshold_percent, 87.5)
        self.assertEqual(args.saturation_color, "12AB34")

    def test_pipeline_accepts_dual_stack_options(self):
        with patch.object(
            sys,
            "argv",
            [
                "seestar-metcalf-stack",
                "frames",
                "--dual-stack",
                "--comet-mask-radius-px",
                "12.5",
            ],
        ):
            args = pipeline.parse_args()

        self.assertTrue(args.dual_stack)
        self.assertEqual(args.comet_mask_radius_px, 12.5)

    def test_pipeline_dual_stack_uses_subtractive_composite(self):
        with patch.object(sys, "argv", ["seestar-metcalf-stack", "frames"]):
            default_args = pipeline.parse_args()
        self.assertFalse(hasattr(default_args, "dual_composite_method"))
        self.assertFalse(default_args.comet_directional_filter)
        self.assertEqual(default_args.comet_directional_size, 2)

        with patch.object(
            sys,
            "argv",
            ["seestar-metcalf-stack", "frames", "--dual-stack"],
        ):
            subtractive_args = pipeline.parse_args()
        self.assertTrue(subtractive_args.dual_stack)

        with patch.object(
            sys,
            "argv",
            [
                "seestar-metcalf-stack",
                "frames",
                "--dual-stack",
                "--comet-directional-filter",
                "--comet-directional-size",
                "3",
            ],
        ):
            directional_args = pipeline.parse_args()
        self.assertTrue(directional_args.comet_directional_filter)
        self.assertEqual(directional_args.comet_directional_size, 3)

    def test_pipeline_accepts_composite_reliability_option(self):
        with patch.object(
            sys,
            "argv",
            [
                "seestar-metcalf-stack",
                "frames",
                "--dual-stack",
                "--composite-min-star-fraction",
                "0.8",
            ],
        ):
            args = pipeline.parse_args()

        self.assertAlmostEqual(args.composite_min_star_fraction, 0.8)

    def test_pipeline_accepts_optional_dual_clean_and_tail_options(self):
        with patch.object(
            sys,
            "argv",
            [
                "seestar-metcalf-stack",
                "frames",
                "--dual-stack",
                "--comet-clean-method",
                "sigma",
                "--comet-sigma-low",
                "2.5",
                "--comet-sigma-high",
                "4.0",
                "--composite-mask-method",
                "tail",
                "--composite-tail-sigma",
                "3.5",
                "--composite-tail-smooth-px",
                "6",
                "--composite-tail-length-px",
                "400",
            ],
        ):
            args = pipeline.parse_args()

        self.assertEqual(args.comet_clean_method, "sigma")
        self.assertEqual(args.comet_sigma_low, 2.5)
        self.assertEqual(args.comet_sigma_high, 4.0)
        self.assertEqual(args.composite_mask_method, "tail")
        self.assertEqual(args.composite_tail_sigma, 3.5)
        self.assertEqual(args.composite_tail_smooth_px, 6.0)
        self.assertEqual(args.composite_tail_length_px, 400.0)

    def test_pipeline_accepts_explicit_reference_frame_file(self):
        with patch.object(
            sys,
            "argv",
            ["seestar-metcalf-stack", "frames with spaces", "--reference-frame-file", "Light C2025 R2 (SWAN).fit"],
        ):
            args = pipeline.parse_args()

        self.assertEqual(args.source_dir, Path("frames with spaces"))
        self.assertEqual(args.reference_frame_file, "Light C2025 R2 (SWAN).fit")
        self.assertFalse(hasattr(args, "reference_frame_index"))

    def test_pipeline_no_verbose_and_no_open_output_disable_defaults(self):
        with patch.object(
            sys,
            "argv",
            ["seestar-metcalf-stack", "frames", "--no-verbose", "--no-open-output"],
        ):
            args = pipeline.parse_args()

        self.assertFalse(args.verbose)
        self.assertFalse(args.open_output)

    def test_windows_cmd_siril_launcher_uses_cmd_exe(self):
        siril = Path("tool path") / "siril-cli.cmd"
        work_dir = Path("work directory")
        script = work_dir / "register script.ssf"
        with patch.object(stacker.os, "name", "nt"):
            command = stacker.build_siril_command(siril, work_dir, script)
            uses_shell = stacker.siril_requires_windows_shell(siril)

        self.assertEqual(command, [str(siril), "-d", str(work_dir), "-s", str(script)])
        self.assertTrue(uses_shell)

    def test_posix_siril_launcher_runs_executable_directly(self):
        with patch.object(stacker.os, "name", "posix"):
            command = stacker.build_siril_command(
                Path("/opt/homebrew/bin/siril-cli"),
                Path("/tmp/work"),
                Path("/tmp/work/register.ssf"),
            )

        self.assertEqual(
            command,
            [
                "/opt/homebrew/bin/siril-cli",
                "-d",
                "/tmp/work",
                "-s",
                "/tmp/work/register.ssf",
            ],
        )
        self.assertFalse(stacker.siril_requires_windows_shell(Path("/opt/homebrew/bin/siril-cli")))

    def test_explicit_siril_file_is_resolved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "siril-cli"
            executable.write_text("test", encoding="utf-8")

            resolved = stacker.resolve_siril_command(executable)

        self.assertEqual(resolved, executable.resolve())


if __name__ == "__main__":
    unittest.main()
