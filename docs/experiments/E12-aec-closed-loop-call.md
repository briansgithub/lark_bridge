# E12 — Does the AEC crackle fix hold on a real call, and is the output loud enough?

- **Status:** CRACKLE FIX CONFIRMED on a live call. Gain documented. Echo suppression not retested.
- **Gates milestone:** wired AEC release
- **Owner / date:** Claude / 2026-08-22

## Question

E11 fixed the far-end crackle synthetically and pinned `audio.aec.node_latency_frames = 1920`.
Three things were unproven: whether the fix holds under real call load, whether the far-end audio
is loud enough to drive a speaker, and what the 1920-frame buffer cost in latency.

## Fixture

Pixel 7a in a Discord call with a Windows PC (far end = the operator speaking into a USB mic),
routed to the Pi over HFP/SCO. Lark A1 on the Pi's USB. Pi 3.5 mm output to a speaker roughly
1 m from the Lark. Supervisor content is `codex/aec-crackle-diagnosis`
(`7526870c9f8d1611…`), graph quantum 2048, sink volume 85 %.

Recorded at **two taps at once**, because Discord, Opus and the mSBC/SCO leg all inject artifacts
of their own and a single tap cannot separate those from the defect:

- `pre` — `bridge.aec.sink` monitor, far-end audio **before** the WebRTC APM
- `post` — onboard sink monitor, what the DAC actually receives **after** playback

Every recorder is named, pinned with `node.dont-reconnect`, and asserted against its explicit
target before the run is trusted (`rig/pi/measure/call_capture.py`). E10 lost a baseline to a
recorder silently falling back to the default source.

## Method correction, recorded because it invalidated a first attempt

**Commenting `node_latency_frames` out of `bridge.toml` does not reproduce pre-fix behaviour.**
The key is optional and its *code* default is now 1920, so an absent key still yields 1920. TOML
has no null, so there is no config-only way to express "send no `node.latency` at all". A first
A/B ran all four conditions at 1920 and produced a clean, entirely meaningless result.

The control is therefore **480**, which E11 measured as indistinguishable from unset: both request
a 10 ms quantum and both drag the graph to `min-quantum` 256.

## Result — the fix holds, and the defect is real on a live call

Twenty seconds per condition, continuous far-end speech, one call, one supervisor restart between:

| Metric | `1920` (fixed) | `480` (control) |
|---|---:|---:|
| Graph driver quantum | 1024 | **256** |
| Onboard sink ERR delta | **3** | **103** |
| `echo-cancel-playback` ERR | 5 | 12 |
| `pre`→`post` correlation | **0.999 @ 0 ms** | 0.886 @ **405 ms** |
| Per-second `pre` vs `post` level | identical to 0.1 dB | diverges up to 23 dB |

At 1920 the two taps are identical at every one-second window — clean passthrough. At 480 they
correlate only after a 405 ms shift and their levels wander unpredictably.

**Operator, listening live:** the 480 stretch "did sound worse" and crackled; the 1920 stretch did
not. They placed the crackling mostly in the first seconds after the changeover, which the
recording window does not cover — capture begins ~5 s after the restart, deliberately, to exclude
graph-rebuild settling.

### Mechanism, refined from E11

E11 concluded the AEC drags *the onboard sink* to `min-quantum` 256. On a real call the graph
driver is not the sink at all — it is the **Lark USB input**, and the sink is a follower. The
driver's quantum still tracks the AEC setting exactly (1024 at 1920 frames, 256 at 480), so the
conclusion generalises rather than breaks: an unconstrained AEC drags *whatever drives the graph*
down to `min-quantum`, and the node feeding the DAC pays for it. E11's framing was specific to its
synthetic fixture, where the sink was the driver.

### Latency: the concern was inverted

1920 was pinned with an explicit worry that a 40 ms buffer would cost conversational latency, which
E10 had deferred. It does not: at 1920 the post tap tracks the pre tap at **zero lag**, so the AEC
stage adds no measurable envelope-scale delay. At 480 the playback path runs **405 ms behind** as
the pipeline backs up under a quantum it cannot sustain.

The setting suspected of costing latency is the one without a latency problem.

## Gain staging, as measured

Recorded at the operator's request, since the current setting is adequate but may change.

| Quantity | Value |
|---|---|
| Sink volume | 85 % = **-4.24 dB** |
| Base volume (hardware unity, 0 dB) | 86 % = -4.00 dB |
| Maximum | 100 % = 0.00 dB, i.e. **+4 dB above unity** |
| Far-end speech at the DAC | peak -11 to -13 dBFS, RMS -31 to -38 dBFS |
| Digital headroom before clipping | **~11 dB**, 0.000 % clipped |

**Why it is 85 %:** E10 selected 0.85 as the measured-safe setting, and it sits a hair *below*
hardware unity. The control is a **digital attenuator in the PWM path**, so anything below unity
costs effective bit depth on a 16-bit PWM output that has little to spare.

**Operator judgement:** loud enough as-is through the speaker at 1 m.

**If more level is wanted later:**

- Raising the Pi sink to 100 % buys **only ~4 dB**, and every dB of it is gain *above* hardware
  unity on a PWM DAC — the region where distortion is most likely. It is the entire remaining
  hardware range, and it is small.
- Lowering the Pi sink is the worst option: it discards bit depth for nothing.
- The digital signal already has ~11 dB of headroom, so the correct lever is **upstream** — the
  phone's in-call volume, or a more sensitive/amplified speaker. Neither costs bit depth and both
  have far more range than 4 dB.
- Do not reach for `webrtc.gain_control`: WebRTC's AGC acts on the capture path, not the render
  path, so it will not make the far end louder.

## Incident: E08 controller wedge reproduced, plus a recovery gap

The first (invalid) A/B ran **four supervisor restarts in ~2.5 minutes during active SCO**, and the
Bluetooth controller wedged. BlueZ continued to report `Connected: yes` with zero HFP audio nodes —
the half-open link E08 describes as actively blocking reconnection — and the Pixel could not
reconnect until the controller was reset.

- `bridge-btwatchdog` detected it, backed off, escalated to `bt-reset.sh`, rfkill-cycled the
  adapter, and **recovered the controller unaided**. The RX counters reset, confirming a firmware
  reload. That is the designed behaviour working.
- **`bridge-btfw.service` failed during the wedge** ("controller never became readable for SCO
  verification after 30 attempts") and **did not re-run after recovery**. Since a firmware reload
  resets SCO routing to PCM, that left routing unverified until it was restarted by hand. **A
  verification service that fails during a wedge and never retries afterwards is a gap**, and it
  belongs to the fault-injection campaign.
- E08's open questions name loopback churn as a candidate trigger. Repeated supervisor restarts
  during active SCO is now a concrete, if unproven, candidate. n=1.

The second A/B used a single restart and did not wedge.

## Caveats

- **n=1 per condition**, one call, 20 s each. Enough to separate a 34× effect; not a soak.
- **An earlier dropout-event count of 141 was retracted.** With `post` shifted 405 ms, per-sample
  ratio comparison is not a reliable event counter. The level-identity comparison and the ERR
  counters are alignment-independent and carry the result instead.
- The operator heard crackling mostly right after the changeover; the recording deliberately
  excludes the first ~5 s. Both observations are recorded; they are not reconciled.
- **The analog measurement leg was dropped.** The aux now drives the speaker, so there is no
  electrical end-to-end confirmation of the DAC and cable. The digital tap (~66 dB SNR against the
  analog leg's ~27 dB) carried all quantitative evidence.
- **Echo suppression was not retested.** E10's failure (1.77 dB against a 10 dB gate) stands
  unchanged. The speaker/Lark fixture is now in place, so this is newly possible.

## Verdict

**`codex/aec-crackle-diagnosis` is confirmed on hardware and ready to merge.** The fix is load-
bearing: without it, a live call drives the graph to `min-quantum` 256, produces 34× the sink
underruns, and puts the playback path 405 ms behind.

**On `audio.aec.enabled`:** the crackle argument for disabling it is gone. The open question is
whether the AEC earns its CPU when its suppression still fails E10's gate. Recommend keeping it
enabled and settling that with a suppression measurement on the now-available speaker fixture,
rather than disabling a stage that is no longer causing harm.

## Next action

Measure echo suppression on the live call with the speaker fixture, and hand the `bridge-btfw`
recovery gap plus the restart-churn trigger hypothesis to the fault-injection campaign.
