import importlib.util
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "developer-tools" / "wcs-comparison" / "compare_wcs.py"
SPEC = importlib.util.spec_from_file_location("compare_wcs", MODULE_PATH)
assert SPEC and SPEC.loader
compare_wcs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compare_wcs
SPEC.loader.exec_module(compare_wcs)


def one_arcsec_wcs(**updates):
    header = {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CRVAL1": 100.0,
        "CRVAL2": 20.0,
        "CRPIX1": 50.0,
        "CRPIX2": 50.0,
        "CD1_1": -1.0 / 3600.0,
        "CD1_2": 0.0,
        "CD2_1": 0.0,
        "CD2_2": 1.0 / 3600.0,
    }
    header.update(updates)
    return compare_wcs.TanSipWcs("test", Path("test.fit"), header)


class WcsComparisonTests(unittest.TestCase):
    def test_reference_pixel_maps_to_reference_sky(self):
        ra, dec = one_arcsec_wcs().pixel_to_world(50.0, 50.0)

        self.assertAlmostEqual(ra, 100.0, places=12)
        self.assertAlmostEqual(dec, 20.0, places=12)

    def test_local_geometry_has_expected_scale_and_orientation(self):
        geometry = one_arcsec_wcs().local_geometry(50.0, 50.0)

        self.assertAlmostEqual(geometry.scale_x_arcsec, 1.0, places=7)
        self.assertAlmostEqual(geometry.scale_y_arcsec, 1.0, places=7)
        self.assertAlmostEqual(geometry.area_scale_arcsec, 1.0, places=7)
        self.assertAlmostEqual(geometry.y_axis_pa_deg, 0.0, places=7)
        self.assertEqual(geometry.parity, "negative")

    def test_forward_sip_changes_pixel_solution_by_expected_amount(self):
        linear = one_arcsec_wcs()
        distorted = one_arcsec_wcs(A_ORDER=2, A_2_0=0.001)
        linear_sky = linear.pixel_to_world(60.0, 50.0)
        distorted_sky = distorted.pixel_to_world(60.0, 50.0)

        separation = compare_wcs.angular_separation_arcsec(*linear_sky, *distorted_sky)

        self.assertAlmostEqual(separation, 0.1, places=5)

    def test_angular_separation_retains_sub_arcsecond_precision(self):
        separation = compare_wcs.angular_separation_arcsec(100.0, 20.0, 100.0, 20.0 + 0.1 / 3600.0)

        self.assertTrue(math.isfinite(separation))
        self.assertAlmostEqual(separation, 0.1, places=7)


if __name__ == "__main__":
    unittest.main()
