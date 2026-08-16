#!/usr/bin/env python3
"""Reduce an A2DP capture-loop gain sweep (stdin) to rig constants (stdout)."""

from __future__ import annotations

import json
import sys


def main() -> int:
    d = json.load(sys.stdin)
    target = float(d["target_peak_dbfs"])
    noise = d["noise_raw"]["per_channel"][0]

    pts = []
    for s in d["sweep"]:
        ch = s["measured"]["per_channel"][0]
        pts.append({
            "gain": s["gain"],
            "peak_dbfs": ch["peak_dbfs"],
            "rms_dbfs": ch["rms_dbfs"],
            "tone_dbfs": ch.get("tone_dbfs"),
            "snr_db": ch.get("snr_db"),
            "clipped_pct": ch["clipped_pct"],
        })

    clean = [p for p in pts if p["clipped_pct"] <= 0.01]
    chosen = min(clean, key=lambda p: abs(p["peak_dbfs"] - target)) if clean else None

    # Headroom against the capture chain's own noise floor. This is what decides whether
    # a real dropout is distinguishable from silence in the A2DP path.
    snr_vs_floor = None
    if chosen is not None:
        snr_vs_floor = round(chosen["peak_dbfs"] - noise["rms_dbfs"], 2)

    json.dump({
        "sink": d.get("sink"),
        "capture_noise_floor_dbfs": noise["rms_dbfs"],
        "target_peak_dbfs": target,
        "chosen_gain": chosen["gain"] if chosen else None,
        "chosen_peak_dbfs": chosen["peak_dbfs"] if chosen else None,
        "snr_vs_noise_floor_db": snr_vs_floor,
        "tone_snr_db": chosen.get("snr_db") if chosen else None,
        "any_clipping": any(p["clipped_pct"] > 0.01 for p in pts),
        # If nothing clipped even at maximum capture gain, the receiver's line-out is
        # WEAK, not hot -- the opposite of the inline-attenuator concern, and worth
        # stating explicitly so that concern can be closed out.
        "attenuator_needed": any(p["clipped_pct"] > 0.01 for p in pts),
        "sweep": pts,
    }, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
