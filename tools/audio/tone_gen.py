#!/usr/bin/env python3
"""Generate a test tone WAV using only the standard library.

Deliberately stdlib-only: this runs on the Pi during provisioning and during spikes,
sometimes before the project's virtualenv exists, and a test tool that cannot run because
its dependencies are missing is worse than no test tool.

Modes:
  sine    continuous tone — audible dropouts appear as gaps or clicks (spike S3)
  chirp   repeating frequency sweep — makes the *position* of a dropout audible
  pips    1 kHz pips at 1 Hz — countable by ear and by tools/audio/glitch_detect.py
  multitone five fixed voice-band tones with deterministic phases
  speech  deterministic speech-shaped syllables with regular silence gaps

Examples:
    ./tone_gen.py --freq 1000 --seconds 30 --out tone.wav
    ./tone_gen.py --mode pips --seconds 60 --channels 1 --out pips.wav
"""

from __future__ import annotations

import argparse
import math
import struct
import wave


def _samples(mode: str, freq: float, rate: int, seconds: float, amplitude: float):
    total = int(rate * seconds)
    two_pi = 2.0 * math.pi

    if mode == "sine":
        for n in range(total):
            yield amplitude * math.sin(two_pi * freq * n / rate)

    elif mode == "chirp":
        # Sweep freq -> 4*freq once per second, then repeat. A dropout in a chirp is
        # locatable by ear: you hear which part of the sweep vanished.
        period = rate
        for n in range(total):
            t = (n % period) / rate
            f = freq * (1.0 + 3.0 * t)
            yield amplitude * math.sin(two_pi * f * t)

    elif mode == "pips":
        # 100 ms tone, 900 ms silence. Counting missing pips is far more reliable for a
        # human than judging whether a continuous tone "stuttered".
        on = int(rate * 0.1)
        period = rate
        for n in range(total):
            pos = n % period
            if pos < on:
                # Raised-cosine envelope so the pip edges do not click.
                env = 0.5 * (1.0 - math.cos(two_pi * pos / on))
                yield amplitude * env * math.sin(two_pi * freq * pos / rate)
            else:
                yield 0.0

    elif mode == "multitone":
        frequencies = (250.0, 500.0, 1000.0, 2000.0, 3500.0)
        phases = (0.0, 0.7, 1.9, 2.6, 4.1)
        scale = amplitude / len(frequencies)
        for n in range(total):
            yield scale * sum(
                math.sin(two_pi * tone * n / rate + phase)
                for tone, phase in zip(frequencies, phases, strict=True)
            )

    elif mode == "speech":
        # A deterministic, synthetic voice-band excitation. It is deliberately not
        # presented as intelligible speech or a speech-quality substitute; alternating
        # voiced/unvoiced syllables and silence exercise adaptation over a broader,
        # non-stationary spectrum than a sine without adding a runtime dependency.
        segment = max(rate // 4, 1)
        pattern = (120.0, 155.0, 0.0, None, 105.0, 180.0, 0.0, None)
        noise = 0x1234ABCD
        for n in range(total):
            position = n % segment
            kind = pattern[(n // segment) % len(pattern)]
            if kind is None:
                yield 0.0
                continue
            envelope = math.sin(math.pi * position / segment) ** 2
            if kind == 0.0:
                noise = (1664525 * noise + 1013904223) & 0xFFFFFFFF
                sample = ((noise / 0xFFFFFFFF) * 2.0 - 1.0) * 0.35
            else:
                t = n / rate
                sample = (
                    0.48 * math.sin(two_pi * kind * t)
                    + 0.24 * math.sin(two_pi * kind * 2 * t + 0.3)
                    + 0.14 * math.sin(two_pi * 700 * t + 0.8)
                    + 0.09 * math.sin(two_pi * 1700 * t + 1.4)
                )
            yield amplitude * envelope * sample
    else:
        raise ValueError(f"unknown mode: {mode}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", required=True, help="output .wav path")
    ap.add_argument(
        "--mode",
        default="sine",
        choices=("sine", "chirp", "pips", "multitone", "speech"),
    )
    ap.add_argument(
        "--freq", type=float, default=1000.0, help="tone frequency in Hz (default 1000)"
    )
    ap.add_argument(
        "--rate",
        type=int,
        default=48000,
        help="sample rate (default 48000, the graph rate)",
    )
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--channels", type=int, default=2, choices=(1, 2))
    ap.add_argument(
        "--dbfs",
        type=float,
        default=-12.0,
        help="peak level in dBFS (default -12, leaves headroom so codecs do not clip)",
    )
    args = ap.parse_args()

    amplitude = (10.0 ** (args.dbfs / 20.0)) * 32767.0

    with wave.open(args.out, "wb") as w:
        w.setnchannels(args.channels)
        w.setsampwidth(2)  # S16_LE — matches the graph's transport format
        w.setframerate(args.rate)

        chunk: list[bytes] = []
        for value in _samples(args.mode, args.freq, args.rate, args.seconds, amplitude):
            sample = max(-32768, min(32767, int(value)))
            frame = struct.pack("<h", sample) * args.channels
            chunk.append(frame)
            if len(chunk) >= 8192:
                w.writeframes(b"".join(chunk))
                chunk.clear()
        if chunk:
            w.writeframes(b"".join(chunk))

    print(
        f"wrote {args.out}: {args.mode} {args.freq:g} Hz, {args.seconds:g}s, "
        f"{args.rate} Hz, {args.channels}ch, {args.dbfs:g} dBFS"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
