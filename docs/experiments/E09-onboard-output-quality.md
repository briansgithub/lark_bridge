# E09 — Is the Pi's onboard 3.5 mm output good enough for Mode 1W?

- **Status:** CONCLUDED 2026-08-16 — **usable, with a level ceiling**
- **Relates to:** E07 runs 11–12, which moved the product's output here
- **Instrument:** dongle B (borrowed, returned after this run) — **not repeatable once it is gone**

## Question

E07 established that every USB audio device on the Pi 3B shortens time-to-desync on the
Bluetooth HCI UART, so Mode 1W's output moved from dongle A to the Pi's own headphone jack.
That swapped in an output stage nobody had measured, feeding a car aux input.

The Pi 3B's analog out is PWM-based with a poor reputation: low level, noise that tracks CPU
activity, DC offset pops. Is it good enough for 16 kHz call audio in a car?

## Why it had to be measured now

Dongle B is the **only** capture device the rig has — dongle A is output-only (trap 6), the Lark
is capture-only, and the Pi has no analog input. It was borrowed and had to go back. After that,
output quality is a matter of opinion.

## Method

Pi 3.5 mm jack → dongle B pink (in). 1 kHz sine, 7 levels from −40 to 0 dBFS, plus a noise floor
captured **with a stream playing** (the bcm2835 driver powers the output stage down when idle,
which would report a floor the product never sees).

`rig/pi/measure/onboard-cal.sh`, reusing `tone_gen.py` and `wav_level.py`. AGC forced off — left
on, the dongle chases the tone and flattens the sweep into a straight line, which reads as a
beautifully linear output stage and is an artefact of the instrument.

**The control that makes this interpretable:** the whole sweep was run twice, at dongle B capture
gain 40 % and 15 %. If the dongle's mic preamp were the thing overloading, dropping its gain 9 dB
would move the compression knee.

## Results

| out dBFS | tone @40 % | offset | tone @15 % | offset |
|---|---|---|---|---|
| −40 | −49.69 | −9.69 | −58.71 | −18.71 |
| −30 | −39.66 | −9.66 | −48.68 | −18.68 |
| −20 | −29.63 | −9.63 | −38.70 | −18.70 |
| −12 | −21.64 | −9.64 | −30.66 | −18.66 |
| −6 | −15.68 | −9.68 | −24.65 | −18.65 |
| −3 | −13.28 | **−10.28** | −22.30 | **−19.30** |
| 0 | −12.54 | **−12.54** | −21.56 | **−21.56** |

**The knee does not move.** Capture gain shifted the whole chain by 9 dB
(`loopback_gain_db` −10.05 → −19.03) while compression began at the same **output** level in both
runs and reached an identical **2.9 dB** at full scale. The compression is the **Pi's output
stage**, not the instrument.

### Noise floor is instrument-limited

| | 40 % | 15 % |
|---|---|---|
| noise floor RMS | −87.05 dBFS | −87.45 dBFS |

A 9 dB capture-gain reduction moved it 0.4 dB. That floor is dongle B's own ADC, so **the Pi's
output noise is below what this rig can see** — we can bound it, not measure it. DC offset was
−1e-05, i.e. absent; no pop/thump risk on the aux input.

### Versus the instrument's own loopback (U13)

| metric | dongle B out→in (U13) | Pi onboard out |
|---|---|---|
| linearity max error | **0.21 dB** | **2.41 dB** |
| aggregate SNR | 48.06 dB | 19.54 dB |
| dynamic range | 71.49 dB | 74.59 dB |
| noise floor RMS | −89.17 dBFS | −87.05 dBFS |

**Linearity is the robust comparison** — it is gain-independent and measured identically both
times. The Pi is **~11× worse** on it.

The SNR column should be read as indicative only, per `docs/hardware/loopback-rig.md`: per-point
SNR is dominated by spectral leakage and is non-monotonic. But the *aggregate* figures were
produced by the same code on the same hardware, and 48 dB → 19.5 dB is far too large to be
leakage. The instrument demonstrably resolves 48 dB, so the Pi's ~19.5 dB is real distortion, not
a measurement limit. Dynamic range barely differs because it is peak-vs-noise-floor and blind to
distortion — which is exactly why it is the wrong figure to quote here.

## The fix, and the mistake in choosing it

The sweep above ran with the PCM mixer at its maximum, **+4.00 dB**. The compression is therefore
not a property of "the Pi's output" in the abstract — it is the output stage being driven past
its linear range by the mixer. Backing the mixer off fixes it.

First attempt set the mixer to **−6.64 dB**, which worked (linearity 2.41 → 0.25 dB) but gave
away 10.6 dB of level to solve a problem that needed about 6. That matters: this feeds a **car
aux input**, and a needlessly quiet source means turning the head unit up and raising its own
noise with it. Re-measured across settings:

| PCM mixer | linearity max error | compression at 0 dBFS | relative level |
|---|---|---|---|
| +4.00 dB | 2.41 dB | 2.90 dB | loudest |
| **0.00 dB** | **0.30 dB** | **0.06 dB** | −4 dB |
| −6.64 dB | 0.25 dB | −0.01 dB | −10.6 dB |

**Shipped setting: PCM = 0.00 dB**, persisted with `alsactl store`. Linear to 0.30 dB — against
an instrument floor of 0.21 dB, i.e. linear to the limit of what this rig can resolve — while
keeping 6.6 dB more level than the over-corrected setting.

> **Invalid run, recorded so it is not mistaken for data:** a sweep labelled `-2dB` produced
> offsets identical to the −6.64 dB run. `amixer sset PCM -2dB` does not parse — the argument is
> swallowed and the mixer is left unchanged, and the `|| true` guard hid the failure. Set levels
> as `0dB` / `90%`, and verify with `sget` rather than trusting the exit status.

## Verdict

**Usable for this product at PCM 0 dB.**

At that setting the response is linear across the full digital range. At the mixer's maximum it
compresses, reaching 2.9 dB by full scale, and distortion rises with level.

This is a genuinely poor DAC by hi-fi standards. It does not matter much here:

- the source is **16 kHz mSBC-compressed HFP call audio**, already the quality ceiling of the
  whole chain;
- it feeds a **car aux input** competing with road noise;
- the operator confirmed the far end was clearly intelligible on this path during run 12.

Distortion of this order is audible on music and largely irrelevant on band-limited speech. The
alternative — a USB dongle — costs a measured **17.2 s time-to-desync** (E07). That trade is not
close.

## Consequences

1. Keep the onboard PCM mixer at **0.00 dB** (persisted via `alsactl store`). Do not raise it to
   its +4 dB maximum — that is where the output stage compresses.
2. If Mode 1W audio is ever reported as distorted, check the level **before** suspecting the
   Bluetooth path — this stage compresses, and it is the only known nonlinearity in the chain.
3. **This measurement cannot be repeated.** Dongle B has been returned. Any future audio-quality
   question on the output path is subjective unless another capture device is obtained.

## What returning dongle B costs

Recorded so it is an informed trade, not a discovery later:

- **U22 (A2DP capture loop) is no longer runnable.** If Mode 1 is revisited, its dropout
  measurement becomes manual listening again — the thing the rig existed to eliminate.
- **U13 / U15** (rig error floor, acoustic path) cannot be re-derived if the rig changes.
- Objective verification of *anything the bridge outputs* is gone.

The uplink is unaffected: it is measured on the phone, not on the Pi.
