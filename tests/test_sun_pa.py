import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import moving_target_stack as stacker
from sun_pa import ObserverCenter, SunPosition, observer_center_from_ephemeris_csv, sun_pa_fits_header, sun_position_angle_deg


class SunPositionAngleTests(unittest.TestCase):
    def test_cardinal_position_angles(self):
        self.assertAlmostEqual(sun_position_angle_deg(0.0, 0.0, 0.0, 10.0), 0.0, places=8)
        self.assertAlmostEqual(sun_position_angle_deg(0.0, 0.0, 10.0, 0.0), 90.0, places=8)
        self.assertAlmostEqual(sun_position_angle_deg(0.0, 0.0, 0.0, -10.0), 180.0, places=8)
        self.assertAlmostEqual(sun_position_angle_deg(0.0, 0.0, 350.0, 0.0), 270.0, places=8)

    def test_header_values_include_anti_solar_direction(self):
        sun = SunPosition(10.0, 0.0, ObserverCenter("geocenter"))
        values = sun_pa_fits_header(0.0, 0.0, sun)
        self.assertAlmostEqual(float(values["SUN_PA"]), 90.0, places=8)
        self.assertAlmostEqual(float(values["ASUN_PA"]), 270.0, places=8)
        self.assertEqual(values["SUNCENTR"], "GEOCENTR")

    def test_observer_center_reads_generated_ephemeris_csv(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ephemeris.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["center", "site_long_deg", "site_lat_deg", "site_elevation_km"])
                writer.writeheader()
                writer.writerow({"center": "fits-site", "site_long_deg": "139.535", "site_lat_deg": "35.7147", "site_elevation_km": "0.12"})
            center = observer_center_from_ephemeris_csv(path)
            self.assertEqual(center, ObserverCenter("fits-site", 139.535, 35.7147, 0.12))

    def test_sun_pa_left_rotation_maps_sun_vector_to_left(self):
        # Identity CD matrix makes RA point right and Dec point down in this
        # compact synthetic WCS. Sun PA=90 is therefore initially right.
        wcs = stacker.WcsModel(header={"CD1_1": 1.0, "CD1_2": 0.0, "CD2_1": 0.0, "CD2_2": 1.0})
        rotation = stacker.sun_pa_left_rotation_degrees(wcs, 0.0, 90.0)
        self.assertAlmostEqual(abs(rotation), 180.0, places=8)


class FitsHeaderUpdateTests(unittest.TestCase):
    def test_update_preserves_image_data_and_replaces_cards(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stack.fit"
            source_header = {"OBJECT": "Test", "DATE-OBS": "2026-08-19T16:47:37"}
            stacker.write_fits_float32(path, np.arange(12, dtype=np.float32).reshape(3, 2, 2), source_header, {"MTREFRA": 45.0, "MTREFDEC": 9.0})
            _header, _cards, data_offset = stacker.read_fits_header(path)
            before_data = path.read_bytes()[data_offset:]
            first = {"SUN_PA": (123.0, "deg; Sun PA"), "SUNCENTR": ("GEOCENTR", "observer")}
            stacker.update_fits_header_cards(path, first)
            stacker.update_fits_header_cards(path, {"SUN_PA": (124.0, "deg; Sun PA updated")})
            header, cards, updated_offset = stacker.read_fits_header(path)
            self.assertEqual(updated_offset, data_offset)
            self.assertEqual(path.read_bytes()[updated_offset:], before_data)
            self.assertAlmostEqual(float(header["SUN_PA"]), 124.0)
            self.assertEqual(header["SUNCENTR"], "GEOCENTR")
            self.assertEqual(sum(card[:8].strip() == "SUN_PA" for card in cards), 1)


if __name__ == "__main__":
    unittest.main()
