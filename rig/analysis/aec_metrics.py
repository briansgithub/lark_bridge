#!/usr/bin/env python3
"""Measure far-end tone suppression between raw and AEC-cleaned microphone WAVs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from wav_level import analyse


def first_channel(path: Path, tone: float, skip: float) -> dict:
    result = analyse(str(path), tone=tone, skip_start=skip, search_hz=2.0)
    channels = result.get("per_channel") or []
    if not channels:
        raise ValueError(f"{path} contains no audio channels")
    return channels[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--clean", type=Path, required=True)
    parser.add_argument("--tone", type=float, default=1000.0)
    parser.add_argument("--skip", type=float, default=2.0)
    parser.add_argument("--min-suppression-db", type=float, default=10.0)
    parser.add_argument("--min-raw-tone-dbfs", type=float, default=-55.0)
    args = parser.parse_args()

    failures: list[str] = []
    try:
        raw = first_channel(args.raw, args.tone, args.skip)
        clean = first_channel(args.clean, args.tone, args.skip)
        raw_tone = float(raw["tone_dbfs"])
        clean_tone = float(clean["tone_dbfs"])
        suppression = round(raw_tone - clean_tone, 2)
        if raw_tone < args.min_raw_tone_dbfs:
            failures.append(
                f"raw acoustic tone {raw_tone:.2f} dBFS is below the measurable floor"
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
        "tone_hz": args.tone,
        "skip_start_s": args.skip,
        "suppression_db": suppression,
        "raw": raw,
        "clean": clean,
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
