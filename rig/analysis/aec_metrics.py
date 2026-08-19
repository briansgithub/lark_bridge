#!/usr/bin/env python3
"""Measure far-end tone suppression between raw and AEC-cleaned microphone WAVs."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import struct
import sys
import wave
from pathlib import Path

try:
    from .wav_level import analyse
except ImportError:
    from wav_level import analyse


MULTITONE_FREQUENCIES = (250.0, 500.0, 1000.0, 2000.0, 3500.0)


def first_channel(path: Path, tone: float | None, skip: float) -> dict:
    result = analyse(str(path), tone=tone, skip_start=skip, search_hz=2.0)
    channels = result.get("per_channel") or []
    if not channels:
        raise ValueError(f"{path} contains no audio channels")
    return channels[0]


def load_mono(path: Path, target_rate: int = 500) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"{path} is not 16-bit PCM")
    values = struct.unpack(f"<{len(frames) // 2}h", frames)
    mono = [values[index] / 32767.0 for index in range(0, len(values), channels)]
    factor = max(rate // target_rate, 1)
    downsampled = [
        sum(mono[index : index + factor]) / len(mono[index : index + factor])
        for index in range(0, len(mono), factor)
        if mono[index : index + factor]
    ]
    return downsampled, rate // factor


def correlated_level(
    reference: list[float], capture: list[float], rate: int, max_lag_s: float = 2.0
) -> tuple[float, int]:
    if not reference or not capture:
        raise ValueError("empty reference or capture")
    reference = [value - statistics.fmean(reference) for value in reference]
    capture = [value - statistics.fmean(capture) for value in capture]
    best_level = 0.0
    best_lag = 0
    for lag in range(min(int(rate * max_lag_s), max(len(capture) - 1, 0)) + 1):
        count = min(len(reference), len(capture) - lag)
        if count < rate:
            continue
        ref = reference[:count]
        observed = capture[lag : lag + count]
        ref_power = sum(value * value for value in ref)
        if ref_power <= 0:
            continue
        scale = sum(a * b for a, b in zip(ref, observed, strict=True)) / ref_power
        ref_rms = math.sqrt(ref_power / count)
        level = abs(scale) * ref_rms
        if level > best_level:
            best_level = level
            best_lag = lag
    return best_level, best_lag


def dbfs(value: float) -> float:
    return -200.0 if value <= 0 else 20 * math.log10(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--clean", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument(
        "--signal", choices=("sine", "multitone", "speech"), default="sine"
    )
    parser.add_argument("--tone", type=float, default=1000.0)
    parser.add_argument("--skip", type=float, default=2.0)
    parser.add_argument("--min-suppression-db", type=float, default=10.0)
    parser.add_argument("--min-raw-tone-dbfs", type=float, default=-55.0)
    args = parser.parse_args()

    failures: list[str] = []
    try:
        components = []
        if args.signal in {"sine", "multitone"}:
            frequencies = (
                (args.tone,) if args.signal == "sine" else MULTITONE_FREQUENCIES
            )
            for frequency in frequencies:
                raw_component = first_channel(args.raw, frequency, args.skip)
                clean_component = first_channel(args.clean, frequency, args.skip)
                raw_level = float(raw_component["tone_dbfs"])
                clean_level = float(clean_component["tone_dbfs"])
                components.append(
                    {
                        "frequency_hz": frequency,
                        "raw_dbfs": raw_level,
                        "clean_dbfs": clean_level,
                        "suppression_db": round(raw_level - clean_level, 2),
                    }
                )
            raw = first_channel(args.raw, None, args.skip)
            clean = first_channel(args.clean, None, args.skip)
            raw_correlated = statistics.median(
                component["raw_dbfs"] for component in components
            )
            clean_correlated = statistics.median(
                component["clean_dbfs"] for component in components
            )
            suppression = round(
                statistics.median(
                    component["suppression_db"] for component in components
                ),
                2,
            )
        else:
            if args.reference is None:
                raise ValueError("--reference is required for speech metrics")
            reference, ref_rate = load_mono(args.reference)
            raw_samples, raw_rate = load_mono(args.raw)
            clean_samples, clean_rate = load_mono(args.clean)
            if ref_rate != raw_rate or ref_rate != clean_rate:
                raise ValueError("reference/raw/clean analysis rates differ")
            raw_level, raw_lag = correlated_level(reference, raw_samples, ref_rate)
            clean_level, clean_lag = correlated_level(
                reference, clean_samples, ref_rate
            )
            raw_correlated = round(dbfs(raw_level), 2)
            clean_correlated = round(dbfs(clean_level), 2)
            suppression = round(raw_correlated - clean_correlated, 2)
            raw = first_channel(args.raw, None, args.skip)
            clean = first_channel(args.clean, None, args.skip)
            components.append(
                {
                    "raw_lag_ms": round(1000 * raw_lag / ref_rate, 2),
                    "clean_lag_ms": round(1000 * clean_lag / ref_rate, 2),
                    "raw_correlated_dbfs": raw_correlated,
                    "clean_correlated_dbfs": clean_correlated,
                }
            )
        if raw_correlated < args.min_raw_tone_dbfs:
            failures.append(
                f"raw correlated signal {raw_correlated:.2f} dBFS is below the measurable floor"
            )
        if suppression < args.min_suppression_db:
            failures.append(
                f"suppression {suppression:.2f} dB is below {args.min_suppression_db:.2f} dB"
            )
        if float(raw.get("clipped_pct", 0)) > 0.01:
            failures.append("raw microphone capture clipped")
        if float(clean.get("clipped_pct", 0)) > 0.01:
            failures.append("cleaned microphone capture clipped")
    except (OSError, ValueError, KeyError) as exc:
        raw = {}
        clean = {}
        suppression = None
        failures.append(str(exc))

    result = {
        "verdict": "PASS" if not failures else "FAIL",
        "signal": args.signal,
        "tone_hz": args.tone,
        "skip_start_s": args.skip,
        "suppression_db": suppression,
        "raw": raw,
        "clean": clean,
        "raw_correlated_dbfs": raw_correlated if "raw_correlated" in locals() else None,
        "clean_correlated_dbfs": (
            clean_correlated if "clean_correlated" in locals() else None
        ),
        "components": components if "components" in locals() else [],
        "thresholds": {
            "min_suppression_db": args.min_suppression_db,
            "min_raw_tone_dbfs": args.min_raw_tone_dbfs,
        },
        "failures": failures,
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
