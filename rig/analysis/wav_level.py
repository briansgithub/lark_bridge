#!/usr/bin/env python3
"""Measure level, noise floor and tone SNR of a WAV file.

Runs ON THE PI during bring-up, before any virtualenv or numpy exists, so this is
strict stdlib. Speed is irrelevant here: these are seconds-long voice-band clips.

    wav_level.py capture.wav
    wav_level.py capture.wav --tone 1000        # also measure SNR at that frequency
    wav_level.py capture.wav --tone 1000 --json

Reports per channel:
  peak_dbfs     highest absolute sample. > -0.1 means clipping is likely.
  rms_dbfs      overall level
  dc_offset     a large DC offset usually means a bias-fed mic input
  clipped_pct   fraction of samples at full scale — the direct clipping indicator
  tone_dbfs     energy at the requested frequency (Goertzel)
  snr_db        tone energy vs everything else
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import wave

FULL = 32767.0


def goertzel(samples: list[float], rate: int, freq: float) -> float:
    """Energy at one frequency. Cheaper and clearer than a whole FFT for a single bin."""
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + (n * freq) / rate)
    w = (2.0 * math.pi * k) / n
    cw, sw = math.cos(w), math.sin(w)
    coeff = 2.0 * cw
    s0 = s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    real = s1 - s2 * cw
    imag = s2 * sw
    return math.sqrt(real * real + imag * imag) / (n / 2.0)


def dbfs(x: float) -> float:
    return -200.0 if x <= 0 else 20.0 * math.log10(x)


def analyse(path: str, tone: float | None, skip_start: float = 0.5) -> dict:
    with wave.open(path, "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)

    if width != 2:
        raise SystemExit(f"{path}: expected 16-bit, got {width * 8}-bit")

    total = len(raw) // 2
    allv = struct.unpack(f"<{total}h", raw[: total * 2])

    # Analyse only the STEADY portion. A capture that begins before playback has
    # started contains leading silence, which scales the Goertzel magnitude down and
    # makes the tone look ~3 dB quieter than its own peak — which then makes the
    # residual calculation subtract almost the entire signal and collapse SNR to ~0 dB.
    # Every statistic uses the same window so they stay mutually consistent.
    skip = int(rate * max(skip_start, 0.0)) * ch
    if skip < total:
        allv = allv[skip:]
    analysed = len(allv) // ch

    out: dict = {"file": path, "rate": rate, "channels": ch, "frames": n,
                 "duration_s": round(n / rate, 3) if rate else 0,
                 "skip_start_s": skip_start,
                 "analysed_s": round(analysed / rate, 3) if rate else 0,
                 "per_channel": []}

    for c in range(ch):
        v = allv[c::ch]
        if not v:
            continue
        norm = [s / FULL for s in v]
        peak = max(abs(s) for s in norm)
        mean = sum(norm) / len(norm)
        rms = math.sqrt(sum(s * s for s in norm) / len(norm))
        clipped = sum(1 for s in v if abs(s) >= 32767) / len(v)

        info = {
            "channel": c,
            "peak_dbfs": round(dbfs(peak), 2),
            "rms_dbfs": round(dbfs(rms), 2),
            "dc_offset": round(mean, 5),
            "clipped_pct": round(clipped * 100, 4),
        }

        if tone:
            # Use a whole number of tone cycles, up to 1 s: Goertzel is O(n) in pure
            # Python and this is a Pi 3. A whole-cycle window also avoids spectral
            # leakage inflating the residual.
            want = min(len(norm), rate)
            cycles = max(int(want * tone / rate), 1)
            seg_len = min(int(cycles * rate / tone), len(norm))
            seg = norm[:seg_len]

            amp = goertzel(seg, rate, tone)          # sine AMPLITUDE, not RMS
            tone_rms = amp / math.sqrt(2)

            # Total power over the SAME window, so the subtraction is meaningful.
            seg_rms = math.sqrt(sum(s * s for s in seg) / len(seg))
            resid = max(seg_rms * seg_rms - tone_rms * tone_rms, 1e-20)

            info["tone_hz"] = tone
            info["tone_dbfs"] = round(dbfs(amp), 2)
            info["snr_db"] = round(dbfs(tone_rms) - dbfs(math.sqrt(resid)), 2)

        out["per_channel"].append(info)

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav")
    ap.add_argument("--tone", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-start", type=float, default=0.5,
                    help="seconds to discard from the start (avoids pre-playback silence)")
    a = ap.parse_args()

    r = analyse(a.wav, a.tone, a.skip_start)

    if a.json:
        json.dump(r, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"{r['file']}  {r['rate']} Hz  {r['channels']}ch  {r['duration_s']}s")
    for c in r["per_channel"]:
        line = (f"  ch{c['channel']}  peak {c['peak_dbfs']:8.2f} dBFS   "
                f"rms {c['rms_dbfs']:8.2f} dBFS   dc {c['dc_offset']:+.5f}   "
                f"clipped {c['clipped_pct']:.3f}%")
        if "tone_dbfs" in c:
            line += f"\n        tone {c['tone_hz']:.0f} Hz: {c['tone_dbfs']:.2f} dBFS   SNR {c['snr_db']:.1f} dB"
        print(line)

    worst = max((c["clipped_pct"] for c in r["per_channel"]), default=0)
    if worst > 0.01:
        print(f"  !! CLIPPING: {worst:.3f}% of samples at full scale — attenuate the source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
