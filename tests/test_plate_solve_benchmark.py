import math
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import astrometry_solve
import plate_solve_benchmark as benchmark


class PlateSolveBenchmarkTests(unittest.TestCase):
    def test_trial_order_contains_every_condition_per_repeat(self):
        trials = benchmark.make_trials(("astrometry", "siril"), repeats=10, seed=1234)

        self.assertEqual(len(trials), 60)
        expected = {
            (solver, label)
            for solver in ("astrometry", "siril")
            for label, _factor in benchmark.SCALE_CASES
        }
        for repeat in range(1, 11):
            actual = {(trial.solver, trial.scale_label) for trial in trials if trial.repeat == repeat}
            self.assertEqual(actual, expected)
        self.assertEqual(trials, benchmark.make_trials(("astrometry", "siril"), 10, 1234))

    def test_single_scale_case_is_renumbered(self):
        all_trials = benchmark.make_trials(("astrometry", "siril"), repeats=2, seed=1234)

        selected = benchmark.select_scale_trials(all_trials, "correct")

        self.assertEqual([trial.order for trial in selected], [1, 2, 3, 4])
        self.assertTrue(all(trial.scale_label == "correct" for trial in selected))
        self.assertEqual({trial.repeat for trial in selected}, {1, 2})

    def test_siril_cache_mode_defaults_to_reuse(self):
        parsed = benchmark.parse_args(["frame.fit"])

        self.assertEqual(parsed.siril_cache_mode, "reuse")

    def test_siril_cache_mode_accepts_cold_each(self):
        parsed = benchmark.parse_args(["frame.fit", "--siril-cache-mode", "cold-each"])

        self.assertEqual(parsed.siril_cache_mode, "cold-each")

    def test_focal_length_represents_supplied_pixel_scale(self):
        pixel_size = 2.9
        correct = benchmark.focal_length_for_scale(pixel_size, 4.0)

        self.assertAlmostEqual(benchmark.focal_length_for_scale(pixel_size, 2.0), correct * 2.0)
        self.assertAlmostEqual(benchmark.focal_length_for_scale(pixel_size, 8.0), correct / 2.0)
        self.assertAlmostEqual(206.265 * pixel_size / correct, 4.0)

    def test_summary_separates_success_failure_and_timeout(self):
        def result(status, elapsed):
            return benchmark.Result(
                order=1,
                solver="siril",
                scale_label="correct",
                scale_factor=1.0,
                repeat=1,
                supplied_pixel_scale_arcsec=4.0,
                supplied_focal_length_mm=100.0,
                effective_pixel_size_um=2.0,
                status=status,
                elapsed_seconds=elapsed,
                return_code=0,
                solved_ra_deg=None,
                solved_dec_deg=None,
                solved_pixel_scale_arcsec=None,
                error="",
                log_path="",
                result_path="",
            )

        rows = benchmark.summarize([result("success", 2.0), result("success", 4.0), result("failure", 5.0), result("timeout", 9.0)])
        row = next(item for item in rows if item["solver"] == "siril" and item["scale_label"] == "correct")

        self.assertEqual(row["attempts"], 4)
        self.assertEqual(row["successes"], 2)
        self.assertEqual(row["failures"], 1)
        self.assertEqual(row["timeouts"], 1)
        self.assertEqual(row["mean_success_seconds"], 3.0)
        self.assertAlmostEqual(row["stdev_success_seconds"], math.sqrt(2.0))
        self.assertEqual(row["mean_all_seconds"], 5.0)

    def test_solver_comparison_uses_success_means(self):
        summary = [
            {"solver": "astrometry", "scale_label": "correct", "mean_success_seconds": 12.0},
            {"solver": "siril", "scale_label": "correct", "mean_success_seconds": 3.0},
        ]

        rows = benchmark.compare_solvers(summary)
        correct = next(row for row in rows if row["scale_label"] == "correct")
        half = next(row for row in rows if row["scale_label"] == "half")

        self.assertEqual(correct["astrometry_over_siril_ratio"], 4.0)
        self.assertIsNone(half["astrometry_over_siril_ratio"])

    def test_center_can_fall_back_to_wcs(self):
        self.assertEqual(
            benchmark.infer_center({"CRVAL1": 123.5, "CRVAL2": -22.25}, None, None),
            (123.5, -22.25),
        )

    def test_siril_script_handles_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            input_path = Path(temporary) / "input files" / "frame.fit"
            output_path = Path(temporary) / "output files" / "solved.fit"
            script = benchmark.build_siril_script(
                input_path,
                output_path,
                150.0,
                2.9,
                "gaia",
            )

            self.assertIn(f'load "{input_path.resolve().as_posix()}"', script)
            self.assertIn(f'save "{output_path.resolve().as_posix()}"', script)
            self.assertIn("platesolve -force -focal=150.0000000000", script)
            self.assertIn("-focal=150.0000000000 -pixelsize=2.9000000000", script)
            self.assertIn("-catalog=gaia", script)

    def test_astrometry_parser_accepts_center_overrides(self):
        parsed = astrometry_solve.parse_args(
            [
                "frame.fit",
                "result.json",
                "wcs.fit",
                "--pixel-scale-arcsec",
                "3.99",
                "--center-ra-deg",
                "120.25",
                "--center-dec-deg",
                "-20.5",
            ]
        )

        self.assertEqual(parsed[4:], (3.99, 120.25, -20.5, None))

    def test_astrometry_parser_accepts_multiplicative_scale_range(self):
        parsed = astrometry_solve.parse_args(
            ["frame.fit", "result.json", "wcs.fit", "--pixel-scale-arcsec", "4", "--scale-range-factor", "2.2"]
        )

        self.assertEqual(parsed[4], 4.0)
        self.assertEqual(parsed[7], 2.2)

    def test_astrometry_scale_range_contains_half_and_double_errors(self):
        true_scale = 0.957375960307298
        for supplied in (true_scale / 2.0, true_scale, true_scale * 2.0):
            hint = astrometry_solve.estimate_scale_hint({}, supplied, 2.2)
            self.assertLess(hint["lower"], true_scale)
            self.assertGreater(hint["upper"], true_scale)

    def test_astrometry_upload_prefers_center_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "frame.fit"
            source.write_bytes(b"FITS")
            with (
                patch.object(astrometry_solve, "read_fits_header", return_value={"RA": 1.0, "DEC": 2.0}),
                patch.object(astrometry_solve, "estimate_scale_hint", return_value=None),
                patch.object(astrometry_solve, "multipart_body", return_value=(b"body", "test/type")) as multipart,
                patch.object(
                    astrometry_solve,
                    "request_bytes",
                    return_value=b'{"status":"success","subid":1}',
                ),
            ):
                _uploaded, request, _header, _scale = astrometry_solve.upload_file(
                    "session", source, center_ra_deg=120.25, center_dec_deg=-20.5
                )

            self.assertEqual(request["center_ra"], 120.25)
            self.assertEqual(request["center_dec"], -20.5)
            encoded_request = multipart.call_args.args[0]["request-json"]
            self.assertIn('"center_ra": 120.25', encoded_request)

    def test_dry_run_needs_no_solver_or_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "minimal.fit"
            cards = [
                "SIMPLE  =                    T".ljust(80),
                "BITPIX  =                    8".ljust(80),
                "NAXIS   =                    0".ljust(80),
                "END".ljust(80),
            ]
            source.write_bytes("".join(cards).encode("ascii").ljust(2880, b" "))
            output = io.StringIO()

            with redirect_stdout(output):
                status = benchmark.main(
                    [
                        str(source),
                        "--pixel-scale-arcsec",
                        "4.0",
                        "--effective-pixel-size-um",
                        "2.9",
                        "--ra-deg",
                        "120.0",
                        "--dec-deg",
                        "-20.0",
                        "--dry-run",
                    ]
                )

            self.assertEqual(status, 0)
            self.assertIn('"astrometry_uploads": 30', output.getvalue())


if __name__ == "__main__":
    unittest.main()
