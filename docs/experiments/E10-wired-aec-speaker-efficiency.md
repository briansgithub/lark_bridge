# E10 — Can wired WebRTC AEC run cleanly and efficiently on the Pi 3B?

- **Status:** SPEAKER BASELINE FAIL — optimization and thermal gates remain closed
- **Gates milestone:** wired AEC release
- **Owner / date:** Codex / 2026-08-20

## Question

With the Lark and Pi onboard analog output as explicit masters, can one mono 48 kHz WebRTC AEC
instance provide at least 10 dB far-end suppression without playback discontinuities, while
retaining the Pi 3 CPU, memory, and thermal margins in the wired-AEC plan?

## Fixture and method

- Pi 3B v1.2, Hollyland Lark lavalier, and Monoprice Harmony Boombox over AUX.
- The lavalier and speaker were fixed one metre apart, line-of-sight, with unchanged orientation.
- Physical speaker volume was maximum. PipeWire output volume remained at the measured-safe
  `0.85` setting (hardware PCM approximately -0.23 dB).
- The selected stimulus was -25 dBFS after 3 dB calibration steps from -40 dBFS.
- CPU-fan and outdoor condenser noise remained present. Per-signal repeatability and the recorded
  live reference distinguish that background from the deterministic stimulus.
- Recordings remain in ignored artifact directories. Only this compact result is committed.

The harness records the generated stimulus, the actual `bridge.aec.sink` monitor, raw Lark, and
cleaned AEC output. Each recorder has a unique name, `node.dont-reconnect=true`, an explicit target,
and an exact-link assertion. This fixed an earlier invalid measurement in which the file called
`reference.wav` was the generated stimulus and a first monitor attempt silently fell back to the
default Lark source.

## Installed capability result

The exact installed WebRTC SPA binary supports high-pass filter, noise suppression, gain control,
transient suppression, and voice detection. `webrtc.extended_filter` is absent and is excluded.
The installed echo-cancel module also exposes `buffer.play_delay`, `buffer.max_size`, and the
diagnostic-only `debug.aec.wav-path` control. The current report is
`artifacts/wired-aec-capabilities-20260820T010314Z`.

No optional WebRTC profile was promoted. Production defaults remain unchanged.

## Calibration

The accepted three-run calibration is `artifacts/wired-aec-speaker-cal-20260820T001134Z`:

- Median raw 1 kHz level: -35.95 dBFS.
- Run-to-run spread: 2.52 dB.
- No clipping, steady PipeWire errors, resynchronization, throttling, or undervoltage.
- Median AEC-owner CPU: 16.71% of one core.
- Maximum temperature: 53.69 C.

The median is 0.95 dB below the preferred -35 dBFS boundary, but it is about 16 dB above the
observed room-noise RMS and well above the -55 dBFS hard floor.

## Playback timing

| Variant | Evidence | Steady ERR | Resync | Disposition |
|---|---:|---:|---|---|
| Direct output, no AEC, 2048 frames | 6 s | 0 | no | clean control |
| AEC 480 frames | 6 s | 17 | yes | reject |
| ALSA headroom 256 | 6 s | 10 | yes | reject and roll back |
| ALSA IRQ scheduling | 6 s | 1090 | no | reject and roll back |
| AEC 512 frames | 4 s | 63 | yes | reject |
| AEC 960 frames | 6 s | 6 | yes | reject |
| AEC 1024 frames | 6 s | 35 | no | reject |
| AEC 1440 frames | single listening checks | 0 | no | sounded clean, preliminary |
| AEC 1440 frames | corrected ten-run baseline | 12 | no journal message | reject as intermittent |
| AEC 1920 frames | 15 s silent screen | 0 | no | stable candidate |
| AEC 1920 frames | corrected ten-run baseline | **0** | **no** | selected speaker-test timing |

The 1440-frame trial sounded clean to the listener, but repeated construction exposed intermittent
onboard-output errors. The 1920-frame setting is therefore the only timing profile that passed ten
corrected speaker trials. It is a test profile, not a production selection; real-call incremental
uplink latency remains deferred.

## Corrected ten-trial baseline

The final current-profile evidence is
`artifacts/wired-aec-speaker-baseline-20260820T005418Z`.

| Signal | Runs | Raw level median | Raw spread | Suppression median | Best run |
|---|---:|---:|---:|---:|---:|
| 1 kHz sine | 4 | -34.81 dBFS | 3.37 dB | 1.00 dB | 9.13 dB |
| Voice-band multitone | 3 | -49.95 dBFS per component | 0.30 dB | 3.05 dB | 4.25 dB |
| Deterministic speech-shaped | 3 | -44.98 dBFS broadband | 0.13 dB | 1.60 dB | 2.64 dB |
| **All trials** | **10** | — | — | **1.77 dB** | **9.13 dB** |

All ten trials had zero steady errors, no resynchronization, no clipping, and no stale AEC nodes.
The sine spread exceeded the fixture target by 0.37 dB, consistent with the noted environmental
noise, but the suppression failure is far too large to be explained by that marginal variance.
Offline one-second-window reanalysis found that none of the ten runs sustained 10 dB suppression
for two consecutive windows after the two-second convergence allowance.

## Reference-delay diagnosis

The module's aligned three-channel diagnostic WAV established an approximately 372.5 ms physical
AUX/speaker/Lark echo path. The zero-delay profile occasionally exceeded 15 dB suppression late in
an eight-second diagnostic, but repeatedly lost convergence. PipeWire's native
`buffer.play_delay` was tested as a bench-only control:

| Added reference delay | Representative suppression | Result |
|---:|---:|---|
| 0 ms | 4.86 to 5.96 dB | best of diagnostic profiles; still fails |
| 50 ms | 3.47 dB | reject |
| 352.5 ms | 4.31 dB | aligned internal reference to about 20 ms; reject |
| 450 ms | 1.34 dB | reject |

The exact-delay run began above 18 dB internally, then degraded to near 0 dB before partly
recovering. This indicates unstable WebRTC adaptation on the present acoustic/Lark path rather than
a simple fixed-delay error. All added-delay values remain absent from production configuration.

## Pi 3 resource result

The final 1920-frame baseline measured:

- Median AEC-owner CPU: 16.66% of one core; p95 16.85%.
- Per-trial average total CPU: approximately 13.3% to 16.7%.
- Maximum observed temperature: 56.92 C.
- Minimum available memory: 679,632 KiB.
- ARM clock remained 1.2 GHz and throttle flags remained `0x0`.
- All measured PipeWire B/Q values remained below the 0.70 deadline gate.

The Pi 3 has ample CPU, memory, and thermal margin for this single AEC instance. Quality and graph
timing—not compute capacity—are the current blockers.

## Gate result and next action

**FAIL for AEC quality; PASS for the 1920-frame short resource and graph-safety screen.**

The randomized optional-processing trials and 15-minute thermal screen are implemented but require
a passing baseline summary. Both commands were verified to refuse this failing summary, so neither
test was run. This preserves the agreed experimental order and prevents a speaker-only result from
being promoted to production.

Before optional DSP or efficiency tuning, investigate the unstable adaptation using the aligned
internal capture and verify the Lark's stereo-to-mono channel behavior. Near-end preservation,
double-talk, real-call latency, call cycling, fault injection, and the two-hour soak remain deferred
until the remaining call fixture is available. Do not run the thermal screen or recommend a
production AEC profile until absolute suppression reaches 10 dB across a valid baseline.
