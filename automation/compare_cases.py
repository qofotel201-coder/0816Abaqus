"""Deterministic case comparison using only the Python 3 standard library.

The module aligns resistance histories on their common penetration-depth
interval, integrates the PLAN.md DR/DW/RW metrics, builds a basic per-hammer
peak envelope, and checks resistance-component closure.  Inputs are rejected
when they are non-finite, non-monotonic, inconsistent, or physically
insufficient; the functions never extrapolate beyond common depth.
"""

from __future__ import annotations

import bisect
import math
from typing import Iterable, List, Sequence, Tuple


PLAUSIBILITY_REFERENCE = 0.20
CUSTOMER_LIMIT = 0.40
CLOSURE_LIMIT = 0.10
HAMMER_PERIOD = 0.025
DEFAULT_ZERO_TOLERANCE = 1.0e-12


class ComparisonError(ValueError):
    """Raised when case data cannot support a defensible comparison."""


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ComparisonError(f"{label} must be numeric, not boolean")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ComparisonError(f"{label} must be numeric") from exc
    if not math.isfinite(number):
        raise ComparisonError(f"{label} must be finite")
    return number


def _numeric_sequence(values: Iterable[object], label: str) -> List[float]:
    try:
        materialized = list(values)
    except TypeError as exc:
        raise ComparisonError(f"{label} must be iterable") from exc
    return [
        _finite_float(value, f"{label}[{index}]")
        for index, value in enumerate(materialized)
    ]


def _validated_series(
    coordinates: Iterable[object],
    values: Iterable[object],
    label: str,
) -> Tuple[List[float], List[float]]:
    x_values = _numeric_sequence(coordinates, f"{label}.coordinates")
    y_values = _numeric_sequence(values, f"{label}.values")
    if len(x_values) != len(y_values):
        raise ComparisonError(f"{label} coordinates and values must have equal length")
    if len(x_values) < 2:
        raise ComparisonError(f"{label} must contain at least two points")
    for index in range(1, len(x_values)):
        if x_values[index] <= x_values[index - 1]:
            raise ComparisonError(
                f"{label} coordinates must be strictly increasing; "
                f"index {index - 1}->{index} is invalid"
            )
    return x_values, y_values


def linear_interpolate(
    coordinates: Iterable[object],
    values: Iterable[object],
    targets: Iterable[object],
) -> List[float]:
    """Linearly interpolate at exact in-range targets without extrapolation."""

    x_values, y_values = _validated_series(coordinates, values, "series")
    target_values = _numeric_sequence(targets, "targets")
    result: List[float] = []
    for target in target_values:
        if target < x_values[0] or target > x_values[-1]:
            raise ComparisonError(
                f"target {target!r} lies outside [{x_values[0]}, {x_values[-1]}]"
            )
        right = bisect.bisect_left(x_values, target)
        if right < len(x_values) and x_values[right] == target:
            result.append(y_values[right])
            continue
        if right == 0 or right == len(x_values):
            raise ComparisonError("interpolation bracket could not be established")
        left = right - 1
        fraction = (target - x_values[left]) / (
            x_values[right] - x_values[left]
        )
        result.append(
            y_values[left] + fraction * (y_values[right] - y_values[left])
        )
    return result


def align_on_common_depth(
    baseline_depth: Iterable[object],
    baseline_force: Iterable[object],
    case_depth: Iterable[object],
    case_force: Iterable[object],
) -> Tuple[List[float], List[float], List[float]]:
    """Return union-grid values over the strict common depth interval."""

    depth_00, force_00 = _validated_series(
        baseline_depth, baseline_force, "baseline"
    )
    depth_11, force_11 = _validated_series(case_depth, case_force, "case")
    common_start = max(depth_00[0], depth_11[0])
    common_end = min(depth_00[-1], depth_11[-1])
    if common_end <= common_start:
        raise ComparisonError("baseline and case have no positive common depth interval")

    grid = {common_start, common_end}
    grid.update(value for value in depth_00 if common_start <= value <= common_end)
    grid.update(value for value in depth_11 if common_start <= value <= common_end)
    common_depth = sorted(grid)
    if len(common_depth) < 2:
        raise ComparisonError("common depth grid contains fewer than two points")
    return (
        common_depth,
        linear_interpolate(depth_00, force_00, common_depth),
        linear_interpolate(depth_11, force_11, common_depth),
    )


def trapezoidal_integral(
    coordinates: Iterable[object], values: Iterable[object]
) -> float:
    """Integrate a finite series with the composite trapezoidal rule."""

    x_values, y_values = _validated_series(coordinates, values, "integral")
    total = 0.0
    for index in range(1, len(x_values)):
        width = x_values[index] - x_values[index - 1]
        total += width * (y_values[index - 1] + y_values[index]) * 0.5
    return total


def _insert_zero_crossings(
    coordinates: Sequence[float], values: Sequence[float]
) -> Tuple[List[float], List[float]]:
    """Split sign-changing linear segments so |y| is integrated exactly."""

    refined_x: List[float] = [coordinates[0]]
    refined_y: List[float] = [values[0]]
    for index in range(1, len(coordinates)):
        x_left = coordinates[index - 1]
        x_right = coordinates[index]
        y_left = values[index - 1]
        y_right = values[index]
        if y_left * y_right < 0.0:
            crossing = x_left + (x_right - x_left) * (-y_left) / (
                y_right - y_left
            )
            refined_x.append(crossing)
            refined_y.append(0.0)
        refined_x.append(x_right)
        refined_y.append(y_right)
    return refined_x, refined_y


def trapezoidal_absolute_integral(
    coordinates: Iterable[object], values: Iterable[object]
) -> float:
    """Integrate the absolute value of the piecewise-linear series."""

    x_values, y_values = _validated_series(coordinates, values, "absolute_integral")
    refined_x, refined_y = _insert_zero_crossings(x_values, y_values)
    return trapezoidal_integral(refined_x, [abs(value) for value in refined_y])


def compare_cases(
    baseline_depth: Iterable[object],
    baseline_force: Iterable[object],
    case_depth: Iterable[object],
    case_force: Iterable[object],
    *,
    zero_tolerance: float = DEFAULT_ZERO_TOLERANCE,
    customer_limit: float = CUSTOMER_LIMIT,
) -> dict:
    """Compute the PLAN.md DR, DW and signed RW metrics.

    Both curves are linearly interpolated onto the union of their coordinates
    over the common depth interval.  Absolute-value integrals are split at
    linear zero crossings before trapezoidal integration.
    """

    tolerance = _finite_float(zero_tolerance, "zero_tolerance")
    limit = _finite_float(customer_limit, "customer_limit")
    if tolerance <= 0.0:
        raise ComparisonError("zero_tolerance must be positive")
    if limit < 0.0:
        raise ComparisonError("customer_limit must be non-negative")

    depth, force_00, force_11 = align_on_common_depth(
        baseline_depth, baseline_force, case_depth, case_force
    )
    denominator = trapezoidal_absolute_integral(depth, force_00)
    if denominator <= tolerance:
        raise ComparisonError("baseline absolute-work denominator is zero or too small")

    differences = [
        force_11[index] - force_00[index] for index in range(len(depth))
    ]
    absolute_difference = trapezoidal_absolute_integral(depth, differences)
    baseline_work = trapezoidal_integral(depth, force_00)
    case_work = trapezoidal_integral(depth, force_11)
    dr = absolute_difference / denominator
    dw = abs(case_work - baseline_work) / denominator
    rw = (baseline_work - case_work) / denominator
    return {
        "depth_start": depth[0],
        "depth_end": depth[-1],
        "point_count": len(depth),
        "common_depth": depth,
        "baseline_force": force_00,
        "case_force": force_11,
        "baseline_work": baseline_work,
        "case_work": case_work,
        "baseline_absolute_work": denominator,
        "absolute_difference_integral": absolute_difference,
        "DR": dr,
        "DW": dw,
        "RW": rw,
        "customer_limit": limit,
        "passes_40_percent": dr <= limit and dw <= limit,
    }


def hammer_peak_envelope(
    times: Iterable[object],
    depths: Iterable[object],
    forces: Iterable[object],
    *,
    period: float = HAMMER_PERIOD,
    start_time: float = 0.0,
    direction: float = 1.0,
) -> List[dict]:
    """Select one signed-force maximum from each hammer period.

    ``direction`` must be +1 or -1 and is selected only after a separate sign
    calibration.  The returned ``force`` retains the original sign while
    ``resistance`` equals ``direction * force``.
    """

    time_values = _numeric_sequence(times, "times")
    depth_values = _numeric_sequence(depths, "depths")
    force_values = _numeric_sequence(forces, "forces")
    if not (len(time_values) == len(depth_values) == len(force_values)):
        raise ComparisonError("times, depths and forces must have equal length")
    for index in range(1, len(time_values)):
        if time_values[index] < time_values[index - 1]:
            raise ComparisonError("times must be non-decreasing")

    period_value = _finite_float(period, "period")
    start_value = _finite_float(start_time, "start_time")
    direction_value = _finite_float(direction, "direction")
    if period_value <= 0.0:
        raise ComparisonError("period must be positive")
    if direction_value not in (-1.0, 1.0):
        raise ComparisonError("direction must be +1 or -1")
    if not time_values:
        return []

    groups: dict[int, dict] = {}
    boundary_tolerance = 1.0e-12
    for index, time_value in enumerate(time_values):
        relative_time = time_value - start_value
        if relative_time < -boundary_tolerance:
            continue
        if relative_time < 0.0:
            relative_time = 0.0
        cycle_index = int(
            math.floor(relative_time / period_value + boundary_tolerance)
        )
        resistance = direction_value * force_values[index]
        candidate = {
            "cycle_index": cycle_index,
            "hammer": cycle_index + 1,
            "time": time_value,
            "depth": depth_values[index],
            "force": force_values[index],
            "resistance": resistance,
            "sample_index": index,
        }
        previous = groups.get(cycle_index)
        if previous is None or resistance > previous["resistance"]:
            groups[cycle_index] = candidate
    return [groups[index] for index in sorted(groups)]


def sum_components(outer: object, inner: object, toe: object) -> float:
    """Return Qouter + Qinner + Qtoe after finite-value validation."""

    return (
        _finite_float(outer, "outer")
        + _finite_float(inner, "inner")
        + _finite_float(toe, "toe")
    )


def component_closure(
    reported_total: object,
    outer: object,
    inner: object,
    toe: object,
    *,
    tolerance: float = CLOSURE_LIMIT,
    scale_floor: float = DEFAULT_ZERO_TOLERANCE,
) -> dict:
    """Check reported total against the three independently extracted parts."""

    total_value = _finite_float(reported_total, "reported_total")
    outer_value = _finite_float(outer, "outer")
    inner_value = _finite_float(inner, "inner")
    toe_value = _finite_float(toe, "toe")
    tolerance_value = _finite_float(tolerance, "tolerance")
    floor_value = _finite_float(scale_floor, "scale_floor")
    if tolerance_value < 0.0:
        raise ComparisonError("tolerance must be non-negative")
    if floor_value <= 0.0:
        raise ComparisonError("scale_floor must be positive")

    component_total = outer_value + inner_value + toe_value
    residual = total_value - component_total
    # PLAN.md freezes force-balance normalization as the sum of the absolute
    # magnitudes of every balance term, protected only by a non-zero floor.
    scale = max(
        abs(total_value)
        + abs(outer_value)
        + abs(inner_value)
        + abs(toe_value),
        floor_value,
    )
    normalized_error = abs(residual) / scale
    return {
        "reported_total": total_value,
        "component_total": component_total,
        "residual": residual,
        "normalization_scale": scale,
        "normalized_error": normalized_error,
        "tolerance": tolerance_value,
        "passed": normalized_error <= tolerance_value,
    }


def evaluate_component_closure(
    reported_totals: Iterable[object],
    outer_values: Iterable[object],
    inner_values: Iterable[object],
    toe_values: Iterable[object],
    *,
    tolerance: float = CLOSURE_LIMIT,
    scale_floor: float = DEFAULT_ZERO_TOLERANCE,
) -> dict:
    """Evaluate closure row-by-row and report the worst normalized residual."""

    totals = list(reported_totals)
    outer = list(outer_values)
    inner = list(inner_values)
    toe = list(toe_values)
    if not (len(totals) == len(outer) == len(inner) == len(toe)):
        raise ComparisonError("all component series must have equal length")
    if not totals:
        raise ComparisonError("component series must not be empty")
    rows = [
        component_closure(
            totals[index],
            outer[index],
            inner[index],
            toe[index],
            tolerance=tolerance,
            scale_floor=scale_floor,
        )
        for index in range(len(totals))
    ]
    worst_index = max(range(len(rows)), key=lambda index: rows[index]["normalized_error"])
    return {
        "row_count": len(rows),
        "rows": rows,
        "max_normalized_error": rows[worst_index]["normalized_error"],
        "worst_index": worst_index,
        "tolerance": rows[worst_index]["tolerance"],
        "passed": all(row["passed"] for row in rows),
    }


compute_metrics = compare_cases
