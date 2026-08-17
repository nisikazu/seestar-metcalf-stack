import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import siril_scale_tolerance as tolerance


class SirilScaleToleranceTests(unittest.TestCase):
    def test_parse_factors_sorts_and_deduplicates(self):
        self.assertEqual(tolerance.parse_factors("1.1, 0.9,1.1"), (0.9, 1.1))

    def test_contiguous_success_range_ignores_isolated_success(self):
        rows = [
            {"scale_factor": 0.7, "status": "success"},
            {"scale_factor": 0.8, "status": "failure"},
            {"scale_factor": 0.9, "status": "success"},
            {"scale_factor": 1.0, "status": "success"},
            {"scale_factor": 1.1, "status": "success"},
            {"scale_factor": 1.2, "status": "failure"},
        ]

        self.assertEqual(tolerance.contiguous_success_range(rows), (0.9, 1.1))

    def test_all_repeats_must_succeed_at_a_factor(self):
        rows = [
            {"scale_factor": 0.9, "status": "success"},
            {"scale_factor": 1.0, "status": "success"},
            {"scale_factor": 1.0, "status": "failure"},
            {"scale_factor": 1.1, "status": "success"},
        ]

        self.assertEqual(tolerance.contiguous_success_range(rows), (None, None))


if __name__ == "__main__":
    unittest.main()
