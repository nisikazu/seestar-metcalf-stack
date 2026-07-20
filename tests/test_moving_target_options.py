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


class CrossPlatformCliTests(unittest.TestCase):
    def test_pipeline_verbose_and_open_output_are_enabled_by_default(self):
        with patch.object(sys, "argv", ["seestar-metcalf-stack", "frames"]):
            args = pipeline.parse_args()

        self.assertTrue(args.verbose)
        self.assertTrue(args.open_output)

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
        siril = Path("tool") / "siril-cli.cmd"
        work_dir = Path("work")
        script = work_dir / "register.ssf"
        with patch.object(stacker.os, "name", "nt"):
            command = stacker.build_siril_command(siril, work_dir, script)

        self.assertEqual(command[:3], ["cmd.exe", "/c", str(siril)])
        self.assertEqual(command[-4:], ["-d", str(work_dir), "-s", str(script)])

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

    def test_explicit_siril_file_is_resolved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "siril-cli"
            executable.write_text("test", encoding="utf-8")

            resolved = stacker.resolve_siril_command(executable)

        self.assertEqual(resolved, executable.resolve())


if __name__ == "__main__":
    unittest.main()
