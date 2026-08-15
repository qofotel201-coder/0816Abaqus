"""Unit tests for the standard-library resistance comparison helpers."""

import math
import os
import re
import sys
import unittest


TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TEST_DIR)
AUTOMATION_DIR = os.path.join(PROJECT_DIR, "automation")
if AUTOMATION_DIR not in sys.path:
    sys.path.insert(0, AUTOMATION_DIR)

import compare_cases as comparison


class PlanConstantsTests(unittest.TestCase):
    def test_plan_thresholds_and_hammer_period_are_frozen(self):
        self.assertEqual(comparison.PLAUSIBILITY_REFERENCE, 0.20)
        self.assertEqual(comparison.CUSTOMER_LIMIT, 0.40)
        self.assertEqual(comparison.CLOSURE_LIMIT, 0.10)
        self.assertEqual(comparison.HAMMER_PERIOD, 0.025)

    def test_module_does_not_depend_on_numpy(self):
        with open(comparison.__file__, "r", encoding="utf-8") as stream:
            source = stream.read()
        self.assertIsNone(
            re.search(r"(?m)^\s*(?:from\s+numpy\b|import\s+numpy\b)", source)
        )


class InterpolationAndMetricTests(unittest.TestCase):
    def test_linear_interpolation_at_same_depth(self):
        actual = comparison.linear_interpolate(
            [0.0, 1.0, 2.0], [0.0, 10.0, 20.0], [0.5, 1.5]
        )
        self.assertEqual(actual, [5.0, 15.0])

    def test_common_depth_uses_union_grid_only_inside_overlap(self):
        depth, baseline, case = comparison.align_on_common_depth(
            [0.0, 1.0, 2.0],
            [0.0, 10.0, 20.0],
            [0.5, 1.5, 2.5],
            [5.0, 15.0, 25.0],
        )
        self.assertEqual(depth, [0.5, 1.0, 1.5, 2.0])
        self.assertEqual(baseline, [5.0, 10.0, 15.0, 20.0])
        self.assertEqual(case, [5.0, 10.0, 15.0, 20.0])

    def test_trapezoidal_integral(self):
        self.assertAlmostEqual(
            comparison.trapezoidal_integral(
                [0.0, 1.0, 2.0], [0.0, 1.0, 0.0]
            ),
            1.0,
        )

    def test_twenty_percent_plan_reference_values(self):
        result = comparison.compare_cases(
            [0.0, 10.0], [100.0, 100.0], [0.0, 4.0, 10.0], [80.0, 80.0, 80.0]
        )
        self.assertAlmostEqual(result["DR"], 0.20)
        self.assertAlmostEqual(result["DW"], 0.20)
        self.assertAlmostEqual(result["RW"], 0.20)
        self.assertTrue(result["passes_40_percent"])

    def test_forty_percent_customer_boundary_is_inclusive(self):
        result = comparison.compare_cases(
            [0.0, 10.0], [100.0, 100.0], [0.0, 10.0], [60.0, 60.0]
        )
        self.assertAlmostEqual(result["DR"], 0.40)
        self.assertAlmostEqual(result["DW"], 0.40)
        self.assertAlmostEqual(result["RW"], 0.40)
        self.assertTrue(result["passes_40_percent"])

    def test_absolute_difference_splits_linear_zero_crossing(self):
        result = comparison.compare_cases(
            [0.0, 1.0], [10.0, 10.0], [0.0, 1.0], [9.0, 11.0]
        )
        self.assertAlmostEqual(result["absolute_difference_integral"], 0.5)
        self.assertAlmostEqual(result["DR"], 0.05)
        self.assertAlmostEqual(result["DW"], 0.0)
        self.assertAlmostEqual(result["RW"], 0.0)

    def test_rejects_non_monotonic_no_overlap_zero_baseline_and_nan(self):
        with self.assertRaises(comparison.ComparisonError):
            comparison.compare_cases(
                [0.0, 1.0, 1.0],
                [1.0, 1.0, 1.0],
                [0.0, 1.0],
                [1.0, 1.0],
            )
        with self.assertRaises(comparison.ComparisonError):
            comparison.compare_cases(
                [0.0, 1.0], [1.0, 1.0], [2.0, 3.0], [1.0, 1.0]
            )
        with self.assertRaises(comparison.ComparisonError):
            comparison.compare_cases(
                [0.0, 1.0], [0.0, 0.0], [0.0, 1.0], [1.0, 1.0]
            )
        with self.assertRaises(comparison.ComparisonError):
            comparison.compare_cases(
                [0.0, 1.0], [1.0, math.nan], [0.0, 1.0], [1.0, 1.0]
            )


class HammerEnvelopeTests(unittest.TestCase):
    def test_selects_physical_positive_peak_per_plan_period(self):
        rows = comparison.hammer_peak_envelope(
            [0.0, 0.005, 0.010, 0.0249, 0.025, 0.030, 0.0499, 0.050],
            [0.0, 0.1, 0.2, 0.3, 1.0, 1.1, 1.2, 2.0],
            [0.0, 5.0, 2.0, 1.0, 0.0, 7.0, 3.0, 4.0],
        )
        self.assertEqual([row["cycle_index"] for row in rows], [0, 1, 2])
        self.assertEqual([row["hammer"] for row in rows], [1, 2, 3])
        self.assertEqual([row["force"] for row in rows], [5.0, 7.0, 4.0])
        self.assertEqual([row["depth"] for row in rows], [0.1, 1.1, 2.0])

    def test_negative_direction_requires_explicit_calibration(self):
        rows = comparison.hammer_peak_envelope(
            [0.0, 0.010], [0.0, 0.1], [-2.0, -5.0], direction=-1.0
        )
        self.assertEqual(rows[0]["force"], -5.0)
        self.assertEqual(rows[0]["resistance"], 5.0)

    def test_rejects_mismatched_empty_series_and_invalid_direction(self):
        with self.assertRaises(comparison.ComparisonError):
            comparison.hammer_peak_envelope([], [0.0], [])
        with self.assertRaises(comparison.ComparisonError):
            comparison.hammer_peak_envelope([0.0], [0.0], [1.0], direction=0.0)


class ComponentClosureTests(unittest.TestCase):
    def test_exact_three_component_closure(self):
        result = comparison.component_closure(100.0, 40.0, 20.0, 40.0)
        self.assertEqual(result["component_total"], 100.0)
        self.assertEqual(result["normalized_error"], 0.0)
        self.assertTrue(result["passed"])

    def test_plan_ten_percent_force_balance_threshold(self):
        result = comparison.component_closure(130.0, 40.0, 20.0, 40.0)
        self.assertAlmostEqual(result["normalization_scale"], 230.0)
        self.assertAlmostEqual(result["normalized_error"], 30.0 / 230.0)
        self.assertFalse(result["passed"])

    def test_series_reports_worst_closure_row(self):
        result = comparison.evaluate_component_closure(
            [100.0, 130.0], [40.0, 40.0], [20.0, 20.0], [40.0, 40.0]
        )
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["worst_index"], 1)
        self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
