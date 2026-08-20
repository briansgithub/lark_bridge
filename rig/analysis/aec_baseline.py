#!/usr/bin/env python3
"""Reduce repeated speaker AEC bench runs into calibration or baseline evidence."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)


def nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def summarize(values: list[float]) -> dict[str, float | None]:
    return {
        "median": round(statistics.median(values), 3) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "spread": round(max(values) - min(values), 3) if values else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bench", type=Path, nargs="+")
    parser.add_argument("--mode", choices=("calibration", "baseline"), required=True)
    parser.add_argument("--min-runs", type=int, required=True)
    args = parser.parse_args()

    failures: list[str] = []
    warnings: list[str] = []
    runs: list[dict[str, Any]] = []
    for path in args.bench:
        try:
            runs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"{path}: {exc}")
    if len(runs) < args.min_runs:
        failures.append(f"found {len(runs)} runs; need at least {args.min_runs}")

    raw_levels = [
        float(value)
        for run in runs
        if (value := nested(run, "metrics", "raw_correlated_dbfs")) is not None
    ]
    suppressions = [
        float(value)
        for run in runs
        if (value := nested(run, "metrics", "suppression_db")) is not None
    ]
    startups = [
        float(value)
        for run in runs
        if (value := run.get("module_startup_ms")) is not None
    ]
    aec_cpu = [
        float(value)
        for run in runs
        if (value := nested(run, "runtime", "aec_cpu_percent_one_core_median"))
        is not None
    ]
    temperatures = [
        float(value)
        for run in runs
        if (value := nested(run, "runtime", "temperature_c_max")) is not None
    ]
    memory = [
        int(value)
        for run in runs
        if (value := nested(run, "runtime", "mem_available_kib_min")) is not None
    ]
    error_deltas = [
        int(value)
        for run in runs
        if (
            value := (
                nested(run, "runtime", "pw_top", "steady_error_delta_total")
                if nested(run, "runtime", "pw_top", "steady_error_delta_total")
                is not None
                else nested(run, "runtime", "pw_top", "error_delta_total")
            )
        )
        is not None
    ]
    clipped = [
        max(
            float(nested(run, "metrics", "raw", "clipped_pct") or 0),
            float(nested(run, "metrics", "clean", "clipped_pct") or 0),
        )
        for run in runs
    ]

    by_signal: dict[str, dict[str, Any]] = {}
    for signal in sorted({str(run.get("signal")) for run in runs}):
        signal_runs = [run for run in runs if str(run.get("signal")) == signal]
        signal_raw = [
            float(value)
            for run in signal_runs
            if (value := nested(run, "metrics", "raw_correlated_dbfs")) is not None
        ]
        signal_suppression = [
            float(value)
            for run in signal_runs
            if (value := nested(run, "metrics", "suppression_db")) is not None
        ]
        by_signal[signal] = {
            "runs": len(signal_runs),
            "raw_correlated_dbfs": summarize(signal_raw),
            "suppression_db": {
                "median": (
                    round(statistics.median(signal_suppression), 3)
                    if signal_suppression
                    else None
                ),
                "p95": percentile(signal_suppression, 0.95),
            },
        }
        if len(signal_raw) != len(signal_runs):
            failures.append(f"{signal}: one or more runs lack raw correlated-level metrics")
        elif signal_raw:
            if min(signal_raw) < -55:
                failures.append(
                    f"{signal}: raw signal floor failed at {min(signal_raw):.2f} dBFS"
                )
            spread = max(signal_raw) - min(signal_raw)
            if spread > 3:
                failures.append(f"{signal}: fixture spread {spread:.2f} dB exceeds 3 dB")

    if len(raw_levels) != len(runs):
        failures.append("one or more runs lack raw correlated-level metrics")
    elif raw_levels:
        sine_levels = [
            float(value)
            for run in runs
            if str(run.get("signal")) == "sine"
            and (value := nested(run, "metrics", "raw_correlated_dbfs")) is not None
        ]
        preferred_levels = sine_levels or raw_levels
        median_raw = statistics.median(preferred_levels)
        if args.mode == "calibration" and not -35 <= median_raw <= -20:
            warnings.append(
                f"median raw signal {median_raw:.2f} dBFS is outside preferred -35 to -20 dBFS"
            )
    if clipped and max(clipped) > 0.01:
        failures.append(f"capture clipping reached {max(clipped):.4f}%")

    if args.mode == "baseline":
        if len(suppressions) != len(runs):
            failures.append("one or more runs lack suppression metrics")
        elif statistics.median(suppressions) < 10:
            failures.append(
                f"median suppression {statistics.median(suppressions):.2f} dB is below 10 dB"
            )
        if error_deltas and sum(error_deltas) > 0:
            failures.append(f"PipeWire ERR counters accumulated by {sum(error_deltas)}")
        if any(
            "resync" in " ".join(nested(run, "metrics", "failures") or []).lower()
            for run in runs
        ):
            failures.append("PipeWire playback resynchronization occurred")

    larks = {run.get("lark") for run in runs}
    outputs = {run.get("wired_output") for run in runs}
    if len(larks) != 1 or None in larks:
        failures.append(f"Lark target changed or was absent: {sorted(map(str, larks))}")
    if len(outputs) != 1 or None in outputs:
        failures.append(
            f"wired output changed or was absent: {sorted(map(str, outputs))}"
        )

    result = {
        "verdict": "PASS" if not failures else "FAIL",
        "mode": args.mode,
        "runs": len(runs),
        "signal": sorted({str(run.get("signal")) for run in runs}),
        "raw_correlated_dbfs": summarize(raw_levels),
        "by_signal": by_signal,
        "suppression_db": {
            "median": (
                round(statistics.median(suppressions), 3) if suppressions else None
            ),
            "p95": percentile(suppressions, 0.95),
        },
        "module_startup_ms": {
            "median": round(statistics.median(startups), 3) if startups else None,
            "p95": percentile(startups, 0.95),
        },
        "aec_cpu_percent_one_core": {
            "median": round(statistics.median(aec_cpu), 3) if aec_cpu else None,
            "p95": percentile(aec_cpu, 0.95),
        },
        "temperature_c_max": max(temperatures) if temperatures else None,
        "mem_available_kib_min": min(memory) if memory else None,
        "pipewire_error_delta_total": sum(error_deltas) if error_deltas else None,
        "failures": failures,
        "warnings": warnings,
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
