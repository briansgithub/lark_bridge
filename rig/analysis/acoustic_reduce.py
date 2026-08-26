#!/usr/bin/env python3
"""Reduce an acoustic-path volume sweep (stdin) to rig constants (stdout).

Input:  {"target_peak_dbfs":N, "noise_raw":<wav_level>, "sweep":[{"volume":V,"measured":<wav_level>},...]}

Picks the speaker-volume setting landing closest to the target peak without clipping, and
reports the acoustic SNR there. A real file, not an inline heredoc — see the note in
calibration_reduce.py.
"""

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
            "volume": s["volume"],
            "peak_dbfs": ch["peak_dbfs"],
            "rms_dbfs": ch["rms_dbfs"],
            "tone_dbfs": ch.get("tone_dbfs"),
            "snr_db": ch.get("snr_db"),
            "clipped_pct": ch["clipped_pct"],
        })

    clean = [p for p in pts if p["clipped_pct"] <= 0.01]
    chosen = min(clean, key=lambda p: abs(p["peak_dbfs"] - target)) if clean else None

    # Acoustic SNR measured against the microphone's own idle noise floor, which includes room
    # noise. This is the figure that decides whether the acoustic injection path is a
    # usable stimulus, and it is what U15 gates on.
    acoustic_snr = None
    if chosen is not None:
        acoustic_snr = round(chosen["peak_dbfs"] - noise["rms_dbfs"], 2)

    microphone_noise_floor = {
        "rms_dbfs": noise["rms_dbfs"],
        "peak_dbfs": noise["peak_dbfs"],
    }
    out = {
        "microphone_id": d.get("microphone_id"),
        "microphone_card": d.get("microphone_card"),
        "format": d.get("format"),
        "microphone_noise_floor": microphone_noise_floor,
        # Compatibility alias for existing E17 result readers.
        "lark_noise_floor": microphone_noise_floor,
        "target_peak_dbfs": target,
        "chosen_volume": chosen["volume"] if chosen else None,
        "chosen_peak_dbfs": chosen["peak_dbfs"] if chosen else None,
        "acoustic_snr_db": acoustic_snr,
        "tone_snr_db": chosen.get("snr_db") if chosen else None,
        "any_clipping": any(p["clipped_pct"] > 0.01 for p in pts),
        "sweep": pts,
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
