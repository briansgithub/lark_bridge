#!/usr/bin/env python3
"""Reduce randomized paired speaker-AEC profile trials."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any

PROFILE = re.compile(r"^pair-(\d+)-(baseline|candidate)$")
T_CRITICAL_95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}


def nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def clipping(run: dict[str, Any]) -> float:
    return max(
        float(nested(run, "metrics", "raw", "clipped_pct") or 0),
        float(nested(run, "metrics", "clean", "clipped_pct") or 0),
    )


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def summary_stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "n": 0,
            "median": None,
            "mean": None,
            "p95": None,
            "sample_stddev": None,
            "mean_ci95_lower": None,
            "mean_ci95_upper": None,
        }
    mean = statistics.fmean(values)
    deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    critical = T_CRITICAL_95.get(len(values) - 1, 1.96)
    margin = critical * deviation / math.sqrt(len(values))
    return {
        "n": len(values),
        "median": round(statistics.median(values), 3),
        "mean": round(mean, 3),
        "p95": round(float(percentile(values, 0.95)), 3),
        "sample_stddev": round(deviation, 3),
        "mean_ci95_lower": round(mean - margin, 3),
        "mean_ci95_upper": round(mean + margin, 3),
    }


def relative_reduction(
    baseline: list[float], candidate: list[float], statistic: str
) -> float | None:
    if not baseline or not candidate:
        return None
    if statistic == "median":
        baseline_value = statistics.median(baseline)
        candidate_value = statistics.median(candidate)
    elif statistic == "p95":
        baseline_value = percentile(baseline, 0.95)
        candidate_value = percentile(candidate, 0.95)
    else:
        raise ValueError(f"unsupported statistic: {statistic}")
    if baseline_value in {None, 0} or candidate_value is None:
        return None
    return round(
        100
        * (float(baseline_value) - float(candidate_value))
        / float(baseline_value),
        3,
    )


def absolute_failures(label: str, run: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    suppression = nested(run, "metrics", "suppression_db")
    raw = nested(run, "metrics", "raw_correlated_dbfs")
    steady_errors = nested(run, "runtime", "pw_top", "steady_error_delta_total")
    if steady_errors is None:
        steady_errors = nested(run, "runtime", "pw_top", "error_delta_total")
    if not isinstance(suppression, (int, float)) or suppression < 10:
        failures.append(f"{label}: suppression is below 10 dB")
    if not isinstance(raw, (int, float)) or raw < -55:
        failures.append(f"{label}: raw correlated signal is below -55 dBFS")
    if isinstance(steady_errors, int) and steady_errors > 0:
        failures.append(f"{label}: steady-state PipeWire errors increased")
    if clipping(run) > 0.01:
        failures.append(f"{label}: capture clipped")
    for failure in nested(run, "runtime", "gate_failures") or []:
        failures.append(f"{label}: {failure}")
    if any("resync" in str(item).lower() for item in nested(run, "metrics", "failures") or []):
        failures.append(f"{label}: playback resynchronized")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bench", nargs="+", type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--min-pairs", type=int, default=10)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()

    failures: list[str] = []
    grouped: dict[int, dict[str, dict[str, Any]]] = {}
    for path in args.bench:
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"{path}: {exc}")
            continue
        match = PROFILE.match(str(run.get("profile_name")))
        if match is None:
            failures.append(f"{path}: invalid paired profile name")
            continue
        pair = int(match.group(1))
        profile = match.group(2)
        if profile in grouped.setdefault(pair, {}):
            failures.append(f"pair {pair}: duplicate {profile} run")
        grouped[pair][profile] = run

    complete = {
        pair: profiles
        for pair, profiles in grouped.items()
        if set(profiles) == {"baseline", "candidate"}
    }
    if len(complete) < args.min_pairs:
        failures.append(f"found {len(complete)} complete pairs; need {args.min_pairs}")

    rows: list[dict[str, Any]] = []
    for pair, profiles in sorted(complete.items()):
        baseline = profiles["baseline"]
        candidate = profiles["candidate"]
        failures.extend(absolute_failures(f"pair {pair} baseline", baseline))
        failures.extend(absolute_failures(f"pair {pair} candidate", candidate))
        row = {"pair": pair}
        for metric, keys in {
            "suppression_db": ("metrics", "suppression_db"),
            "aec_cpu_median": ("runtime", "aec_cpu_percent_one_core_median"),
            "aec_cpu_p95": ("runtime", "aec_cpu_percent_one_core_p95"),
            "total_cpu_mean": ("runtime", "total_cpu_percent_mean"),
            "temperature_c": ("runtime", "temperature_c_max"),
            "module_startup_ms": ("module_startup_ms",),
        }.items():
            baseline_value = nested(baseline, *keys)
            candidate_value = nested(candidate, *keys)
            row[metric] = {
                "baseline": baseline_value,
                "candidate": candidate_value,
                "delta": (
                    round(float(candidate_value) - float(baseline_value), 3)
                    if isinstance(baseline_value, (int, float))
                    and isinstance(candidate_value, (int, float))
                    else None
                ),
            }
        rows.append(row)

    def deltas(metric: str) -> list[float]:
        return [
            float(value)
            for row in rows
            if (value := nested(row, metric, "delta")) is not None
        ]

    suppression_delta = deltas("suppression_db")
    aec_cpu_delta = deltas("aec_cpu_median")
    suppression_stats = summary_stats(suppression_delta)
    lower_bound = suppression_stats["mean_ci95_lower"]
    if isinstance(lower_bound, (int, float)) and lower_bound < -1:
        failures.append(
            "candidate suppression lower 95% confidence bound regressed by more than 1 dB"
        )
    if aec_cpu_delta and statistics.median(aec_cpu_delta) > 2:
        failures.append("candidate median AEC CPU rose by more than 2 percentage points")

    def profile_values(metric: str, profile: str) -> list[float]:
        return [
            float(value)
            for row in rows
            if (value := nested(row, metric, profile)) is not None
        ]

    cpu_baseline = profile_values("aec_cpu_median", "baseline")
    cpu_candidate = profile_values("aec_cpu_median", "candidate")
    cpu_p95_baseline = profile_values("aec_cpu_p95", "baseline")
    cpu_p95_candidate = profile_values("aec_cpu_p95", "candidate")
    temperature_baseline = profile_values("temperature_c", "baseline")
    temperature_candidate = profile_values("temperature_c", "candidate")
    startup_baseline = profile_values("module_startup_ms", "baseline")
    startup_candidate = profile_values("module_startup_ms", "candidate")
    efficiency = {
        "median_aec_cpu_reduction_pct": relative_reduction(
            cpu_baseline, cpu_candidate, "median"
        ),
        "p95_aec_cpu_reduction_pct": relative_reduction(
            cpu_p95_baseline, cpu_p95_candidate, "p95"
        ),
        "median_temperature_reduction_c": (
            round(
                statistics.median(temperature_baseline)
                - statistics.median(temperature_candidate),
                3,
            )
            if temperature_baseline and temperature_candidate
            else None
        ),
        "median_startup_reduction_pct": relative_reduction(
            startup_baseline, startup_candidate, "median"
        ),
    }
    no_cpu_regression = (
        efficiency["median_aec_cpu_reduction_pct"] is not None
        and efficiency["median_aec_cpu_reduction_pct"] >= 0
    )
    meaningful_efficiency = any(
        (
            isinstance(efficiency["median_aec_cpu_reduction_pct"], (int, float))
            and efficiency["median_aec_cpu_reduction_pct"] >= 10,
            isinstance(efficiency["p95_aec_cpu_reduction_pct"], (int, float))
            and efficiency["p95_aec_cpu_reduction_pct"] >= 10,
            isinstance(efficiency["median_temperature_reduction_c"], (int, float))
            and efficiency["median_temperature_reduction_c"] >= 1
            and no_cpu_regression,
            isinstance(efficiency["median_startup_reduction_pct"], (int, float))
            and efficiency["median_startup_reduction_pct"] >= 10
            and no_cpu_regression,
        )
    )
    if rows and not meaningful_efficiency:
        failures.append("candidate did not meet an efficiency-improvement gate")

    result = {
        "verdict": "PASS" if not failures else "FAIL",
        "candidate": args.candidate,
        "seed": args.seed,
        "pairs": len(complete),
        "preliminary_only": True,
        "production_promotion_allowed": False,
        "paired_statistics": {
            metric: summary_stats(deltas(metric))
            for metric in (
                "suppression_db",
                "aec_cpu_median",
                "aec_cpu_p95",
                "total_cpu_mean",
                "temperature_c",
                "module_startup_ms",
            )
        },
        "efficiency_improvement": efficiency,
        "meaningful_efficiency_gate_passed": meaningful_efficiency,
        "runs": rows,
        "failures": failures,
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
