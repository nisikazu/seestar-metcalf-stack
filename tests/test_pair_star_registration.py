import math
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pair_star_registration as registration
import optimize_common_footprint as footprint
import trace_star_registration as trace


class StarDetectionTests(unittest.TestCase):
    def test_detects_isolated_gaussian_stars(self):
        yy, xx = np.mgrid[:160, :220]
        image = np.full((160, 220), 100.0, dtype=np.float32)
        expected = [(45.3, 63.7), (155.2, 111.5), (120.7, 35.4)]
        for x, y in expected:
            image += 500.0 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2.0 * 1.2**2))
        stars = registration.detect_stars(image, threshold_sigma=5.0, max_stars=10, minimum_separation=5.0)

        self.assertGreaterEqual(len(stars), len(expected))
        for x, y in expected:
            self.assertLess(min(math.hypot(star.x - x, star.y - y) for star in stars), 0.3)


class SimilarityTransformTests(unittest.TestCase):
    def test_recovers_similarity_transform_with_outliers(self):
        random = np.random.default_rng(20260731)
        source = random.uniform([20.0, 30.0], [500.0, 700.0], size=(20, 2))
        expected = registration.SimilarityTransform(1.003, 0.73, -122.5, 87.25)
        destination = expected.apply(source) + random.normal(0.0, 0.05, size=source.shape)
        source_with_outliers = np.vstack([source, [[700.0, 10.0], [20.0, 820.0], [850.0, 560.0]]])
        destination_with_outliers = np.vstack([destination, [[5.0, 5.0], [920.0, 880.0], [400.0, 30.0]]])

        result = registration.estimate_similarity_transform(
            source_with_outliers,
            destination_with_outliers,
            match_radius=0.5,
            min_inliers=10,
            scale_min=0.98,
            scale_max=1.02,
            maximum_points=25,
        )

        self.assertGreaterEqual(len(result.inlier_pairs), 20)
        self.assertLess(result.rms_px, 0.12)
        self.assertAlmostEqual(result.transform.scale, expected.scale, places=3)
        self.assertAlmostEqual(result.transform.rotation_deg, expected.rotation_deg, places=2)
        self.assertAlmostEqual(result.transform.tx, expected.tx, places=1)
        self.assertAlmostEqual(result.transform.ty, expected.ty, places=1)

    def test_requires_enough_stars(self):
        points = np.array([[0.0, 0.0], [10.0, 10.0]], dtype=np.float64)
        with self.assertRaises(RuntimeError):
            registration.estimate_similarity_transform(points, points, min_inliers=3)


class RegistrationTraceTests(unittest.TestCase):
    def test_composition_matches_direct_application(self):
        current_to_previous = registration.SimilarityTransform(1.002, 0.4, 3.0, -7.0)
        previous_to_first = registration.SimilarityTransform(0.998, -0.3, -14.0, 9.0)
        current_to_first = trace.compose_similarity(previous_to_first, current_to_previous)
        points = np.array([[12.5, 40.0], [180.2, 300.6]], dtype=np.float64)

        np.testing.assert_allclose(
            current_to_first.apply(points),
            previous_to_first.apply(current_to_previous.apply(points)),
            atol=1e-9,
        )

    def test_overlap_is_one_for_identity_and_reduced_after_shift(self):
        self.assertAlmostEqual(trace.clipped_overlap_fraction(registration.SimilarityTransform(1, 0, 0, 0), 100, 200), 1.0)
        self.assertAlmostEqual(trace.clipped_overlap_fraction(registration.SimilarityTransform(1, 0, 25, 0), 100, 200), 0.75, places=2)

    def test_gap_splits_sessions(self):
        dates = [
            (datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc), Path("a.fit")),
            (datetime(2026, 7, 31, 10, 10, tzinfo=timezone.utc), Path("b.fit")),
            (datetime(2026, 7, 31, 11, 20, tzinfo=timezone.utc), Path("c.fit")),
        ]
        sessions = trace.split_sessions(dates, 60.0)

        self.assertEqual([[path.name for _when, path in session] for session in sessions], [["a.fit", "b.fit"], ["c.fit"]])


class CommonFootprintTests(unittest.TestCase):
    def make_footprint(self, index, tx):
        transform = registration.SimilarityTransform(1.0, 0.0, tx, 0.0)
        return footprint.FrameFootprint(
            index=index,
            file=f"{index}.fit",
            date_obs="2026-01-01T00:00:00Z",
            transform=transform,
            polygon=footprint.transformed_frame_polygon(transform, 100, 200),
        )

    def test_convex_rectangle_intersection(self):
        left = footprint.transformed_frame_polygon(registration.SimilarityTransform(1, 0, 0, 0), 100, 200)
        right = footprint.transformed_frame_polygon(registration.SimilarityTransform(1, 0, 25, 0), 100, 200)
        intersection = footprint.intersect_convex_polygons(left, right)

        self.assertAlmostEqual(footprint.polygon_area(intersection) / footprint.polygon_area(left), 74.0 / 99.0)

    def test_exact_trim_removes_displaced_outlier(self):
        footprints = [self.make_footprint(1, 0.0), self.make_footprint(2, 1.0), self.make_footprint(3, 80.0)]
        result = footprint.exact_trimmed_footprint(footprints, trim_count=1)

        self.assertEqual(result.excluded_positions, (2,))
        self.assertAlmostEqual(result.area / footprint.polygon_area(footprints[0].polygon), 98.0 / 99.0)
