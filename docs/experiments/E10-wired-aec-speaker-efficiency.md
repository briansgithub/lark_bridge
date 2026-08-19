# E10 — Can wired WebRTC AEC run cleanly and efficiently on the Pi 3B?

- **Status:** IN PROGRESS — speaker-only phase
- **Gates milestone:** wired AEC release
- **Owner / date:** Codex / 2026-08-19

## Question

With the Lark and Pi onboard analog output as explicit masters, can one mono 48 kHz WebRTC AEC
instance provide at least 10 dB far-end suppression without playback discontinuities, while
retaining the Pi 3 CPU, memory, and thermal margins in the wired-AEC plan?

## Method

- Harness: `rig wired-aec bench`, `speaker-cal`, `speaker-baseline`, and `capabilities`.
- Hardware present: Pi 3B v1.2, Lark lavalier, Monoprice Harmony Boombox over AUX.
- Physical speaker volume was raised to maximum after the first inaudible attempt.
- The first measurable runs placed the lavalier approximately six inches from the speaker. This
  geometry is not accepted for the repeatable baseline; the pending fixture is one metre,
  line-of-sight, at the same height and with both positions marked.
- Recordings remain in ignored artifact directories. Only compact results belong in Git.
- Every timing experiment changed one variable and used an effectively silent -120 dBFS stream,
  so it could isolate graph scheduling without exposing the speaker or listener to repeated tones.

## Installed capability result

The installed WebRTC SPA library supports explicit high-pass filter, noise suppression, gain
control, transient suppression, and voice-detection properties. `webrtc.extended_filter` is not
present in the installed binary and is therefore excluded from the experiment matrix. The
installed-version report is `artifacts/wired-aec-capabilities-20260819T233759Z`.

## Fixture observations

| Run | Stimulus | Raw correlated level | Clean correlated level | Suppression | Result |
|---|---:|---:|---:|---:|---|
| `232351Z` | 1 kHz, -40 dBFS | -44.86 dBFS | -44.93 dBFS | 0.07 dB | measurable; fails AEC gate |
| `232431Z` | 1 kHz, -37 dBFS | -38.44 dBFS | -39.41 dBFS | 0.97 dB | measurable; fails AEC gate; crackle heard |

Neither run clipped. These values show that the acoustic path is measurable but do not establish
an AEC baseline because the fixture geometry was not the specified one metre and playback was
not timing-clean.

The crackle report exposed two independent issues:

1. The WirePlumber sink volume had returned to `1.00`, which maps to the +4 dB nonlinear hardware
   PCM setting documented in E09. It was restored to the measured-safe `0.85` (hardware PCM
   -0.23 dB) and the bench now refuses to play if the sink exceeds `0.86`.
2. PipeWire logged repeated `spa.alsa ... resync` events while the onboard output followed the
   Lark-driven AEC graph. This persisted at the safe output level and with an effectively silent
   stream, proving that digital overload was not the timing fault.

## Playback-timing isolation

| Variant | Duration | ERR delta | Location | Journal resync | Disposition |
|---|---:|---:|---|---|---|
| Direct onboard playback, no AEC | 6 s | 0 | none | no | clean control |
| Default AEC latency, 480 frames | 6 s | 17 | 16 onboard output | yes | reject |
| ALSA headroom = 256 | 6 s | 10 | 7 onboard output | yes | reject; follower delay worsened |
| ALSA IRQ scheduling | 6 s | 1090 | 1087 onboard output | no | reject and rolled back |
| AEC latency = 1024 | 6 s | 35 | AEC source/playback | no | reject; moved the fault |
| AEC latency = 960 | 6 s | 6 | onboard output | yes | reject |
| AEC latency = 1920 | 6 s | 1 | onboard output | no | preliminary only |
| AEC latency = 1920 | 15 s | **0** | none | **no** | candidate for audible validation |

The direct output runs cleanly at a 2048-frame quantum. In the default AEC graph, PipeWire selects
the Lark source (`priority.driver=2009`) over the onboard output (`priority.driver=1000`), and the
output becomes a follower at the WebRTC graph's 480-frame latency. The evidence therefore locates
the crackle in the combined cross-clock AEC graph, not in the speaker or standalone analog output.

1920 frames is four exact WebRTC blocks. It adds 30 ms relative to the current 480-frame AEC
request, so it remains only a candidate until measured end-to-end incremental latency is no more
than 50 ms and near-end/double-talk non-inferiority can be tested.

## Pi 3 resource result so far

The clean 15-second 1920-frame silent run measured:

- AEC owner CPU: 13.9% median and 14.91% p95 of one core.
- Total CPU: 8.12% median and 27.66% p95.
- AEC resident memory: 13,816 KiB maximum, 108 KiB increase.
- AEC sink B/Q: 0.27 p99; all measured nodes remained below the 0.70 deadline gate.
- Temperature: 39.7 C maximum; ARM clock stayed at 1.2 GHz; throttle flags remained `0x0`.
- Available memory remained about 690 MiB.

This is ample early resource margin, but it is not a thermal soak and says nothing yet about
near-end preservation or double-talk quality.

## Current verdict

**INCONCLUSIVE for AEC quality; timing defect reproduced and a viable timing candidate found.**

Do not run the ten-trial acoustic baseline or the 15-minute thermal screen until an audible
1920-frame run confirms no crackle at the fixed one-metre geometry. Do not promote 1920 frames to
production based on speaker-only testing.

## Next steps

1. Place and mark the lavalier one metre from the speaker, line-of-sight and at the same height.
2. Run one -40 dBFS, 1 kHz audible check with the 1920-frame candidate and confirm subjectively
   that playback is clean while enforcing zero sustained errors or journal resynchronizations.
3. If clean, run three calibration trials, then the ten-trial tone, multitone, and deterministic
   speech baseline.
4. Only after the baseline passes, run paired optional-WebRTC-processing trials and the 15-minute
   thermal screen.
5. Defer production selection, real-call latency, near-end speech, and double-talk decisions until
   the remaining call fixture is available.
