import io
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
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


class HorizonsObjectResolutionTests(unittest.TestCase):
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

    def test_processing_method_token_records_rankfit_percentage(self):
        self.assertEqual(stacker.processing_method_token("mean", 50), "mean")
        self.assertEqual(stacker.processing_method_token("median", 50), "median")
        self.assertEqual(stacker.processing_method_token("rankfit", 37), "rankfit5_p37")


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
