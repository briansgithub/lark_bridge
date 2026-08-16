#!/usr/bin/env python3
"""Reduce a raw loopback sweep (stdin) to the rig's calibration constants (stdout).

Input:  {"noise_floor_raw": <wav_level json>, "sweep": [{"out_dbfs":N,"measured":<wav_level json>}, ...]}
Output: the constants every later measurement is reported against.

A real file rather than an inline heredoc on purpose: `python3 - <<'PY'` inside a pipeline
makes Python read its PROGRAM from stdin, so the piped data never reaches json.load().
That bug has now been hit twice in this repo; keeping reducers as files removes the
possibility entirely.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    d = json.load(sys.stdin)
    noise = d["noise_floor_raw"]["per_channel"][0]

    pts = []
    for s in d["sweep"]:
        ch = s["measured"]["per_channel"][0]
        pts.append({
            "out_dbfs": s["out_dbfs"],
            "peak_dbfs": ch["peak_dbfs"],
            "rms_dbfs": ch["rms_dbfs"],
            "tone_dbfs": ch.get("tone_dbfs"),
            "snr_db": ch.get("snr_db"),
            "clipped_pct": ch["clipped_pct"],
        })

    # Fit gain on mid-range points only: the quietest sits close enough to the noise
    # floor that including it would bias the result.
    mid = [p for p in pts if -35 <= p["out_dbfs"] <= 0]
    gains = [p["peak_dbfs"] - p["out_dbfs"] for p in mid]
    gain = sum(gains) / len(gains)

    # Worst deviation from constant gain. AGC or a compressor shows up here as several
    # dB of error — which is precisely why this is measured rather than assumed.
    lin_err = max(abs(g - gain) for g in gains)

    clip = next((p["out_dbfs"] for p in pts if p["clipped_pct"] > 0.01), None)
    nominal = min(pts, key=lambda p: abs(p["out_dbfs"] + 12))
    loudest = max(p["peak_dbfs"] for p in pts if p["clipped_pct"] <= 0.01)

    json.dump({
        "noise_floor": {
            "rms_dbfs": noise["rms_dbfs"],
            "peak_dbfs": noise["peak_dbfs"],
            "dc_offset": noise["dc_offset"],
        },
        "loopback_gain_db": round(gain, 2),
        "linearity_max_error_db": round(lin_err, 2),
        "clipping_onset_dbfs": clip if clip is not None else "none",
        "snr_db": nominal.get("snr_db"),
        "dynamic_range_db": round(loudest - noise["rms_dbfs"], 2),
        "sweep": pts,
    }, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
