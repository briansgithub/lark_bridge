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


def speech_burst_samples(rate: int = 16000, seconds: float = 10.0, peak_dbfs: float = -24.0) -> list[int]:
    """Deterministic speech-band (300..3400 Hz) pulsed bursts for H10 AEC calibration.

    Kept at -24 dBFS peak (~-30 dBFS active RMS) so playback stays at a comfortable room volume
    even on fixed-gain speakers like MP43247. Alternating 400 ms speech-band bursts and 250 ms
    silent gaps give unambiguous envelope cross-correlation for acoustic delay calibration and
    broadband spectral excitation for ESP-SR AEC-FD convergence.
    """
    n = int(round(rate * seconds))
    burst_n = int(round(rate * 0.40))
    period_n = int(round(rate * 0.65))
    fade_n = int(round(rate * 0.015))
    # Deterministic LCG filtered through a simple 1-pole high-pass + 2-pole speech bandpass (~1 kHz).
    state = 0x13579BDF
    hp_x = 0.0
    hp_y = 0.0
    lp1 = 0.0
    lp2 = 0.0
    raw = []
    # Mix deterministic speech-formant tones + filtered noise for rich broadband content.
    formants = (340.0, 520.0, 780.0, 1180.0, 1690.0, 2340.0, 3050.0)
    for i in range(n):
        pos = i % period_n
        if pos >= burst_n:
            raw.append(0.0)
            continue
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        noise = ((state >> 8) / 8388608.0) - 1.0
        # 300 Hz HPF + ~2.8 kHz LPF on noise
        hp_y = 0.89 * (hp_y + noise - hp_x)
        hp_x = noise
        lp1 = lp1 + 0.55 * (hp_y - lp1)
        lp2 = lp2 + 0.55 * (lp1 - lp2)
        t = i / float(rate)
        # Slowly vary formant weights per burst so bursts are distinct and speech-like.
        burst_idx = i // period_n
        tones = 0.0
        for k, f in enumerate(formants):
            mod = 0.5 + 0.5 * math.sin(2.0 * math.pi * (0.37 * (k + 1)) * t + burst_idx * 0.9)
            tones += mod * math.sin(2.0 * math.pi * (f * (1.0 + 0.03 * math.sin(2.0 * math.pi * 2.3 * t))) * t)
        val = 0.70 * lp2 + 0.08 * tones
        g = 1.0
        if pos < fade_n:
            g = 0.5 - 0.5 * math.cos(math.pi * pos / fade_n)
        elif pos >= burst_n - fade_n:
            g = 0.5 - 0.5 * math.cos(math.pi * (burst_n - 1 - pos) / fade_n)
        raw.append(val * g)
    peak_raw = max(abs(x) for x in raw) or 1.0
    scale = (32767.0 * 10.0 ** (peak_dbfs / 20.0)) / peak_raw
    return [max(-32767, min(32767, int(round(x * scale)))) for x in raw]


def write_wav(path: Path, samples: list[int], rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the harness app's bundled test stimuli.")
    ap.add_argument("out_dir", nargs="?", default="build/assets/stimulus", type=Path)
    args = ap.parse_args(argv)
    for name, (freq, rate, secs, peak, fade) in STIMULI.items():
        write_wav(args.out_dir / name, tone_samples(freq, rate, secs, peak, fade), rate)
        print(f"wrote {args.out_dir / name}")
    write_wav(args.out_dir / "h10_speech_16k_10s.wav", speech_burst_samples(16000, 10.0, -24.0), 16000)
    print(f"wrote {args.out_dir / 'h10_speech_16k_10s.wav'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
