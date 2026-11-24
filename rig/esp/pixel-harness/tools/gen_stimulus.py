#!/usr/bin/env python3
"""Generate the harness app's bundled test stimuli (deterministic, stdlib only).

Writes 16-bit PCM mono WAVs into ``<out_dir>`` (default ``build/assets/stimulus``):

* ``tone_1k_16k_3s.wav``: 1 kHz sine, 16 kHz, 3.0 s, peak -6 dBFS, with a 10 ms raised-cosine
  fade in and out so the edges do not click.

1 kHz at 16 kHz is exactly 16 samples per cycle, so the output is bit-identical on every run.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from pathlib import Path

STIMULI = {
    # name: (freq_hz, rate_hz, seconds, peak_dbfs, fade_ms)
    "tone_1k_16k_3s.wav": (1000.0, 16000, 3.0, -6.0, 10.0),
}


def tone_samples(freq: float, rate: int, seconds: float, peak_dbfs: float, fade_ms: float) -> list[int]:
    n = int(round(rate * seconds))
    amp = 32767.0 * 10.0 ** (peak_dbfs / 20.0)
    fade = int(round(rate * fade_ms / 1000.0))
    out = []
    for i in range(n):
        g = 1.0
        if fade > 0 and i < fade:
            g = 0.5 - 0.5 * math.cos(math.pi * i / fade)
        elif fade > 0 and i >= n - fade:
            g = 0.5 - 0.5 * math.cos(math.pi * (n - 1 - i) / fade)
        out.append(int(round(amp * g * math.sin(2.0 * math.pi * freq * i / rate))))
    return out


def write_wav(path: Path, samples: list[int], rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the harness app's test stimuli.")
    ap.add_argument("out_dir", nargs="?", default="build/assets/stimulus", type=Path)
    args = ap.parse_args(argv)
    for name, (freq, rate, secs, peak, fade) in STIMULI.items():
        write_wav(args.out_dir / name, tone_samples(freq, rate, secs, peak, fade), rate)
        print(f"wrote {args.out_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
