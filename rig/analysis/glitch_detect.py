#!/usr/bin/env python3
"""Find playback discontinuities (crackle) in a recording of a steady tone.

The companion to wav_level.py: that tool answers "is the level right", this one
answers "did the stream break". Strict stdlib for the same reason wav_level.py is —
it has to run on the Pi, where there is no numpy and no virtualenv, against a
recording that never has to leave the machine. It is unhurried rather than fast:
a 20 s stereo clip takes seconds on a laptop and a couple of minutes on a Pi 3.

A steady sine is the ideal crackle probe. The stimulus occupies one bin, so a buffer
repeat, drop or resync shows up as a broadband transient trivially separable from it.
Detection is therefore "energy well above the fundamental, concentrated in time".

    glitch_detect.py capture.wav --tone 1000
    glitch_detect.py capture.wav --tone 1000 --json

Two independent detectors run, because a clean digital tap and a hissy analog capture
fail in different ways:

  hp_burst   High-pass above the harmonics of the tone, then find short bursts rising
             above the median out-of-band level of the recording itself. Robust on a
             noisy analog leg.
  step       Second difference of the waveform. A sine has a hard bound on this; a
             sample-domain discontinuity blows straight through it. Crisp on a clean
             digital tap, meaningless on a noisy analog one.

Exit status is 0 whether or not glitches are found. This measures; it does not gate.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import sys
import wave

FULL = 32768.0

# Section Qs for a 4th-order Butterworth built from two cascaded biquads.
BUTTERWORTH_Q = (0.54119610, 1.30656296)


def read_wav(path: str) -> tuple[list[list[float]], int]:
    """Return per-channel float samples in [-1, 1) and the sample rate."""
    with wave.open(path, "rb") as handle:
        if handle.getsampwidth() != 2:
            raise SystemExit(f"{path}: only 16-bit PCM is supported")
        rate = handle.getframerate()
        count = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())

    flat = array.array("h")
    flat.frombytes(raw)
    if sys.byteorder == "big":
        flat.byteswap()
    return [
        [flat[i] / FULL for i in range(channel, len(flat), count)] for channel in range(count)
    ], rate


def highpass(samples: list[float], rate: int, cutoff: float) -> list[float]:
    """Two cascaded RBJ biquads: a 4th-order Butterworth high-pass, in stdlib.

    scipy would make this one line, but this file has to run on the Pi.
    """
    w0 = 2.0 * math.pi * cutoff / rate
    cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
    out = samples
    for q in BUTTERWORTH_Q:
        alpha = sin_w0 / (2.0 * q)
        a0 = 1.0 + alpha
        b0 = ((1.0 + cos_w0) / 2.0) / a0
        b1 = (-(1.0 + cos_w0)) / a0
        b2 = b0
        a1 = (-2.0 * cos_w0) / a0
        a2 = (1.0 - alpha) / a0
        z1 = z2 = 0.0
        stage = [0.0] * len(out)
        for i, x in enumerate(out):
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            stage[i] = y
        out = stage
    return out


def envelope(samples: list[float], win: int) -> list[float]:
    """Sliding RMS via a running sum, so this stays O(n) rather than O(n*win)."""
    win = max(win, 1)
    env = [0.0] * len(samples)
    total = 0.0
    for i, x in enumerate(samples):
        total += x * x
        if i >= win:
            dropped = samples[i - win]
            total -= dropped * dropped
        env[i] = math.sqrt(max(total / min(i + 1, win), 0.0))
    return env


def merge_events(flagged: list[int], rate: int, merge_ms: float) -> list[tuple[int, int]]:
    """Collapse neighbouring flagged samples into discrete events."""
    if not flagged:
        return []
    gap = int(rate * merge_ms / 1000.0)
    events = []
    start = prev = flagged[0]
    for i in flagged[1:]:
        if i - prev > gap:
            events.append((start, prev))
            start = i
        prev = i
    events.append((start, prev))
    return events


def describe(events, rate: int, values: list[float], reference: float) -> list[dict]:
    described = []
    for a, b in events:
        peak = max(abs(values[i]) for i in range(a, b + 1))
        described.append(
            {
                "t_s": round(a / rate, 4),
                "dur_ms": round((b - a + 1) * 1000.0 / rate, 3),
                "excess_db": round(20.0 * math.log10(max(peak, 1e-12) / reference), 1),
            }
        )
    return described


def hp_burst(x: list[float], rate: int, tone: float, thresh_db: float, merge_ms: float):
    """Out-of-band transient detector. Survives a noisy analog capture."""
    cutoff = min(tone * 3.5, rate * 0.45)  # above the 3rd harmonic of the stimulus
    env = envelope(highpass(x, rate, cutoff), int(rate * 0.001))

    floor = sorted(env)[len(env) // 2] or 1e-12
    limit = floor * (10.0 ** (thresh_db / 20.0))
    # The high-pass rings at the analysis-window edge; that ring is an artifact of where
    # we started looking, not a discontinuity in the stream.
    settle = int(rate * 0.05)
    flagged = [i for i in range(settle, len(env) - settle) if env[i] > limit]
    events = describe(merge_events(flagged, rate, merge_ms), rate, env, floor)
    return events, 20.0 * math.log10(floor)


def step(x: list[float], rate: int, tone: float, merge_ms: float) -> list[dict]:
    """Sample-domain discontinuity detector for clean digital taps.

    For a sine of amplitude A the second difference is bounded by A*w^2 with
    w = 2*pi*f/fs. Anything far above that bound is not a sine any more.
    """
    d2 = [x[i] - 2.0 * x[i - 1] + x[i - 2] for i in range(2, len(x))]
    w = 2.0 * math.pi * tone / rate
    amplitude = sorted(abs(v) for v in x)[int(len(x) * 0.999)]
    bound = amplitude * w * w
    if bound <= 0:
        return []
    # 4x the theoretical bound absorbs dither and resampler ripple.
    flagged = [i for i, v in enumerate(d2) if abs(v) > bound * 4.0]
    return describe(merge_events(flagged, rate, merge_ms), rate, d2, bound)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("wav")
    ap.add_argument("--tone", type=float, default=1000.0, help="stimulus frequency in Hz")
    ap.add_argument(
        "--threshold-db",
        type=float,
        default=25.0,
        help="hp_burst trigger, dB above the median out-of-band level",
    )
    ap.add_argument(
        "--merge-ms",
        type=float,
        default=5.0,
        help="flagged samples closer together than this count as one event",
    )
    ap.add_argument(
        "--skip",
        type=float,
        default=1.5,
        help="seconds to drop from the start (lead silence and stream startup)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    channels, rate = read_wav(args.wav)
    start = int(rate * args.skip)
    report: dict = {"file": args.wav, "rate": rate, "skipped_s": args.skip, "channels": []}

    for number, channel in enumerate(channels):
        x = channel[start:]
        if len(x) < rate // 10:
            raise SystemExit(f"{args.wav}: too short after --skip")
        bursts, floor_db = hp_burst(x, rate, args.tone, args.threshold_db, args.merge_ms)
        steps = step(x, rate, args.tone, args.merge_ms)
        duration = len(x) / rate
        rms = math.sqrt(sum(v * v for v in x) / len(x))
        report["channels"].append(
            {
                "channel": number,
                "duration_s": round(duration, 2),
                "rms_dbfs": round(20.0 * math.log10(max(rms, 1e-12)), 2),
                "peak_dbfs": round(20.0 * math.log10(max(max(abs(v) for v in x), 1e-12)), 2),
                "oob_floor_dbfs": round(floor_db, 2),
                "hp_burst_count": len(bursts),
                "hp_burst_per_s": round(len(bursts) / duration, 2),
                "step_count": len(steps),
                "hp_bursts": bursts[:40],
                "steps": steps[:40],
            }
        )

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
        return 0

    print(f"{args.wav}  {rate} Hz  (first {args.skip}s skipped)")
    for c in report["channels"]:
        print(
            f"  ch{c['channel']}: rms {c['rms_dbfs']:7.2f} dBFS   peak {c['peak_dbfs']:7.2f} dBFS"
            f"   out-of-band floor {c['oob_floor_dbfs']:7.2f} dBFS"
        )
        print(
            f"         glitches: hp_burst {c['hp_burst_count']} "
            f"({c['hp_burst_per_s']}/s)   step {c['step_count']}"
        )
        for b in c["hp_bursts"][:10]:
            print(f"           t={b['t_s']:8.4f}s  {b['dur_ms']:7.3f} ms  +{b['excess_db']} dB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
