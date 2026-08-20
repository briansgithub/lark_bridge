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


def load_mono(path: Path, target_rate: int = 250) -> tuple[list[float], int]:
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


def load_energy_envelope(path: Path, envelope_rate: int = 100) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"{path} is not 16-bit PCM")
    values = struct.unpack(f"<{len(frames) // 2}h", frames)
    mono = [values[index] / 32767.0 for index in range(0, len(values), channels)]
    block = max(rate // envelope_rate, 1)
    envelope = [
        math.sqrt(statistics.fmean(value * value for value in mono[index : index + block]))
        for index in range(0, len(mono), block)
        if mono[index : index + block]
    ]
    return envelope, rate // block


def correlated_level(
    reference: list[float], capture: list[float], rate: int, max_lag_s: float = 2.0
) -> tuple[float, int, float]:
    if not reference or not capture:
        raise ValueError("empty reference or capture")
    reference = [value - statistics.fmean(reference) for value in reference]
    capture = [value - statistics.fmean(capture) for value in capture]
    best_level = 0.0
    best_lag = 0
    best_correlation = 0.0
    for lag in range(min(int(rate * max_lag_s), max(len(capture) - 1, 0)) + 1):
        count = min(len(reference), len(capture) - lag)
        if count < rate:
            continue
        ref = reference[:count]
        observed = capture[lag : lag + count]
        ref_power = sum(value * value for value in ref)
        observed_power = sum(value * value for value in observed)
        if ref_power <= 0 or observed_power <= 0:
            continue
        dot = sum(a * b for a, b in zip(ref, observed, strict=True))
        correlation = abs(dot) / math.sqrt(ref_power * observed_power)
        scale = dot / ref_power
        ref_rms = math.sqrt(ref_power / count)
        level = abs(scale) * ref_rms
        if correlation > best_correlation:
            best_level = level
            best_lag = lag
            best_correlation = correlation
    return best_level, best_lag, best_correlation


def dbfs(value: float) -> float:
    return -200.0 if value <= 0 else 20 * math.log10(value)


def tail_metrics(path: Path, seconds: float) -> dict[str, float]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"{path} is not 16-bit PCM")
    values = struct.unpack(f"<{len(frames) // 2}h", frames)
    mono = [values[index] / 32767.0 for index in range(0, len(values), channels)]
    count = min(max(int(rate * seconds), 1), len(mono))
    tail = mono[-count:]
    rms = math.sqrt(statistics.fmean(value * value for value in tail))
    peak = max(abs(value) for value in tail)
    clipped = sum(abs(value) >= 32767 / 32768 for value in tail)
    return {
        "rms_dbfs": round(dbfs(rms), 2),
        "peak_dbfs": round(dbfs(peak), 2),
        "clipped_pct": round(100 * clipped / len(tail), 6),
    }


def window_rms_dbfs(
    path: Path,
    *,
    skip: float,
    trailing_silence: float,
    window_seconds: float = 1.0,
) -> list[float]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"{path} is not 16-bit PCM")
    values = struct.unpack(f"<{len(frames) // 2}h", frames)
    mono = [values[index] / 32767.0 for index in range(0, len(values), channels)]
    start = min(int(skip * rate), len(mono))
    end = max(start, len(mono) - int(trailing_silence * rate))
    window = max(int(window_seconds * rate), 1)
    levels: list[float] = []
    for index in range(start, end - window + 1, window):
        segment = mono[index : index + window]
        rms = math.sqrt(statistics.fmean(value * value for value in segment))
        levels.append(round(dbfs(rms), 2))
    return levels


def convergence_metrics(
    raw_levels: list[float],
    clean_levels: list[float],
    *,
    required_db: float,
    allowance_seconds: float = 2.0,
    window_seconds: float = 1.0,
) -> dict[str, object]:
    suppressions = [
        round(raw - clean, 2)
        for raw, clean in zip(raw_levels, clean_levels, strict=False)
    ]
    converged_index = next(
        (
            index
            for index in range(max(len(suppressions) - 1, 0))
            if suppressions[index] >= required_db
            and suppressions[index + 1] >= required_db
        ),
        None,
    )
    return {
        "allowance_s": allowance_seconds,
        "window_s": window_seconds,
        "window_suppression_db": suppressions,
        "achieved": converged_index is not None,
        "convergence_time_s": (
            round(allowance_seconds + converged_index * window_seconds, 3)
            if converged_index is not None
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--clean", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    parser.add_argument(
        "--signal", choices=("sine", "multitone", "speech", "noise"), default="sine"
    )
    parser.add_argument("--tone", type=float, default=1000.0)
    parser.add_argument("--skip", type=float, default=2.0)
    parser.add_argument("--trailing-silence", type=float, default=0.0)
    parser.add_argument("--min-suppression-db", type=float, default=10.0)
    parser.add_argument("--min-raw-tone-dbfs", type=float, default=-55.0)
    parser.add_argument(
        "--max-incremental-latency-ms",
        type=float,
        help="optional real-call latency gate; speaker-only tests leave this unset",
    )
    args = parser.parse_args()

    failures: list[str] = []
    try:
        components = []
        reference_summary = (
            first_channel(args.reference, None, args.skip)
            if args.reference is not None
            else None
        )
        if args.signal in {"sine", "multitone"}:
            frequencies = (
                (args.tone,) if args.signal == "sine" else MULTITONE_FREQUENCIES
            )
            for frequency in frequencies:
                raw_component = first_channel(args.raw, frequency, args.skip)
                clean_component = first_channel(args.clean, frequency, args.skip)
                raw_level = float(raw_component["tone_dbfs"])
                clean_level = float(clean_component["tone_dbfs"])
                reference_level = (
                    float(first_channel(args.reference, frequency, args.skip)["tone_dbfs"])
                    if args.reference is not None
                    else None
                )
                components.append(
                    {
                        "frequency_hz": frequency,
                        "reference_dbfs": reference_level,
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
            reference_samples, ref_rate = load_energy_envelope(args.reference)
            raw_samples, raw_rate = load_energy_envelope(args.raw)
            clean_samples, clean_rate = load_energy_envelope(args.clean)
            if ref_rate != raw_rate or ref_rate != clean_rate:
                raise ValueError("reference/raw/clean analysis rates differ")
            _, raw_lag, raw_correlation = correlated_level(
                reference_samples, raw_samples, ref_rate
            )
            _, clean_lag, clean_correlation = correlated_level(
                reference_samples, clean_samples, ref_rate
            )
            raw = first_channel(args.raw, None, args.skip)
            clean = first_channel(args.clean, None, args.skip)
            raw_correlated = float(raw["rms_dbfs"])
            clean_correlated = float(clean["rms_dbfs"])
            suppression = round(raw_correlated - clean_correlated, 2)
            latency_reliable = raw_correlation >= 0.3 and clean_correlation >= 0.3
            components.append(
                {
                    "raw_lag_ms": round(1000 * raw_lag / ref_rate, 2),
                    "clean_lag_ms": round(1000 * clean_lag / ref_rate, 2),
                    "incremental_clean_latency_ms": (
                        round(1000 * (clean_lag - raw_lag) / ref_rate, 2)
                        if latency_reliable
                        else None
                    ),
                    "latency_reliable": latency_reliable,
                    "raw_correlation": round(raw_correlation, 4),
                    "clean_correlation": round(clean_correlation, 4),
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
        incremental_latency = (
            components[0].get("incremental_clean_latency_ms")
            if args.signal in {"speech", "noise"} and components
            else None
        )
        raw_windows = window_rms_dbfs(
            args.raw,
            skip=args.skip,
            trailing_silence=args.trailing_silence,
        )
        clean_windows = window_rms_dbfs(
            args.clean,
            skip=args.skip,
            trailing_silence=args.trailing_silence,
        )
        convergence = convergence_metrics(
            raw_windows,
            clean_windows,
            required_db=args.min_suppression_db,
        )
        if (
            args.max_incremental_latency_ms is not None
            and isinstance(incremental_latency, (int, float))
            and incremental_latency > args.max_incremental_latency_ms
        ):
            failures.append(
                f"incremental cleaned latency {incremental_latency:.2f} ms exceeds "
                f"{args.max_incremental_latency_ms:.2f} ms"
            )
        silence = None
        if args.trailing_silence > 0:
            silence = {
                "raw": tail_metrics(args.raw, args.trailing_silence),
                "clean": tail_metrics(args.clean, args.trailing_silence),
            }
            if silence["clean"]["clipped_pct"] > 0.01:
                failures.append("cleaned trailing silence clipped")
            if silence["clean"]["rms_dbfs"] > silence["raw"]["rms_dbfs"] + 6:
                failures.append("cleaned trailing-silence noise rose by more than 6 dB")
    except (OSError, ValueError, KeyError) as exc:
        raw = {}
        clean = {}
        suppression = None
        convergence = None
        failures.append(str(exc))

    result = {
        "verdict": "PASS" if not failures else "FAIL",
        "signal": args.signal,
        "tone_hz": args.tone,
        "skip_start_s": args.skip,
        "suppression_db": suppression,
        "raw": raw,
        "clean": clean,
        "reference": reference_summary if "reference_summary" in locals() else None,
        "raw_correlated_dbfs": raw_correlated if "raw_correlated" in locals() else None,
        "clean_correlated_dbfs": (
            clean_correlated if "clean_correlated" in locals() else None
        ),
        "components": components if "components" in locals() else [],
        "convergence": convergence if "convergence" in locals() else None,
        "trailing_silence": silence if "silence" in locals() else None,
        "thresholds": {
            "min_suppression_db": args.min_suppression_db,
            "min_raw_tone_dbfs": args.min_raw_tone_dbfs,
            "max_incremental_latency_ms": args.max_incremental_latency_ms,
        },
        "failures": failures,
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
