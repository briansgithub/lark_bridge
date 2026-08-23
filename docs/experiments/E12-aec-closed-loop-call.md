# E12 — Does the AEC crackle fix hold on a real call, and is the output loud enough?

- **Status:** CRACKLE FIX CONFIRMED on a live call. Gain documented. Echo suppression PASSES for the first time.
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

## Echo suppression: passes for the first time

Measured on the live call with the speaker roughly 1 m from the Lark.

**This was NOT single-talk, contrary to how it was first written up.** The operator is physically
in the same room as both microphones, so while they spoke into the far-end USB mic the Lark heard
*two* copies of that speech: their voice directly through the air, and the same speech returning
from the speaker as echo. An earlier draft of this section claimed "nobody speaks at the Lark",
which is wrong, and the operator caught it.

Three taps: `reference` = `bridge.aec.sink` monitor (the echo source), `raw` = the Lark,
`clean` = the **HFP sink monitor** -- what is actually handed to Bluetooth for the phone.

| Quantity | Value |
|---|---:|
| **Suppression** | **12.04 dB** (gate: 10 dB) -- **PASS**, no failures |
| `raw` correlation with reference | **0.9505** @ 20 ms lag |
| `clean` correlation with reference | **0.2893** |
| `raw` / `clean` correlated level | -41.05 / -53.09 dBFS |

Compare E10: **1.77 dB median, FAIL**.

**The correlation collapse is the load-bearing evidence, not the level drop.** A stage that merely
attenuated everything uniformly would leave correlation high while the level fell. Correlation
falling from 0.95 to 0.29 means the far-end component specifically was removed.

That distinction also survives the main confound. The `clean` tap is the 16 kHz HFP sink, so
everything above 8 kHz is discarded by the SCO leg regardless of what the AEC does, which could
flatter a level-only measurement. It cannot explain the correlation collapse: a band-limited copy
of the echo would still correlate strongly with the reference.

### Hypothesis for why this now passes when E10 failed

E10 measured an echo path of **~372.5 ms** and described "unstable WebRTC adaptation" -- a profile
that began above 18 dB internally, then degraded to near 0 dB before partly recovering. Here the
echo path measures **20 ms** and suppression is stable across the capture.

E12 also measured the unfixed pipeline running **405 ms behind** while the fixed one adds no
measurable delay. A plausible causal chain is that the crackle fix incidentally fixed convergence:
an APM given a short, stable delay can lock onto it, where one chasing a backlog of hundreds of
milliseconds cannot.

**This is a hypothesis, not a result.** n=1, and the fixtures differ -- E10 used a generated
stimulus at a calibrated level through a different speaker. It should be tested by re-running
E10's calibrated speaker protocol on the fixed build before anyone claims the AEC is repaired.

### Does the co-located talker invalidate the 12.04 dB?

Probably not the number, but definitely the framing.

The lag argument is what saves the measurement. `correlated_level` searches non-negative lags only,
and the two copies sit on opposite sides of zero relative to the `reference` tap:

- **Echo**: reference -> APM -> playback -> DAC -> speaker -> ~1 m of air -> Lark. Tens of
  milliseconds. The measured peak is **+20 ms at 0.95 correlation**, which fits.
- **Direct voice**: mouth -> Lark is ~3 ms, but that same speech only reaches the `reference` tap
  after PC -> Discord -> phone -> SCO -> Pi, on the order of 100-300 ms. Relative to the reference
  it therefore lands at a **negative** lag, which is never searched.

So the +20 ms peak is the echo, and the direct voice raises total RMS in both `raw` and `clean`
without contributing to either correlated level. Suppression is not obviously inflated by it.

What the confound does destroy is the claim that this was a clean best case:

- The APM saw **near-end speech essentially continuously**, so its double-talk logic was active
  throughout. This is closer to sustained double-talk than to single-talk -- arguably a *harder*
  condition, not an easier one, which if anything makes 12.04 dB more impressive and less
  representative at the same time.
- **Near-end preservation remains completely untested, and now matters more.** Nobody has checked
  whether the AEC is also chewing up the Lark wearer's voice, and this fixture cannot answer it
  because the near-end and far-end talkers were the same person saying the same words.

### What this does not establish

- **Near-end preservation and true double-talk.** Needs a near-end talker who is *not* the far-end
  talker, or a recorded far-end source so the two are uncorrelated.
- **n=1**, one 25 s capture, one speaker at one distance and level.
- `latency_reliable: false` in the report; the incremental-latency figure was correctly withheld.

## Incident: E08 controller wedge reproduced, plus a recovery gap

The first (invalid) A/B ran **four supervisor restarts in ~2.5 minutes during active SCO**, and the
Bluetooth controller wedged. BlueZ continued to report `Connected: yes` with zero HFP audio nodes —
the half-open link E08 describes as actively blocking reconnection — and the Pixel could not
reconnect until the controller was reset.

- `bridge-btwatchdog` detected it, backed off, escalated to `bt-reset.sh`, rfkill-cycled the
  adapter, and **recovered the controller unaided**. The RX counters reset, confirming a firmware
  reload. That is the designed behaviour working.
- **`bridge-btfw.service` was left in `failed` state after the wedge** ("controller never became
  readable for SCO verification after 30 attempts"), and SCO routing stayed unverified until it was
  restarted by hand. Since a firmware reload resets SCO routing to PCM, that matters.

  **This was originally written up as "it never retries after recovery", which E13 found to be
  wrong.** `bt-reset.sh` has a `reapply_sco_routing()` on its success path that does
  `systemctl restart bridge-btfw.service`, so the retry mechanism exists. The journal had rotated
  by the time E13 looked, so whether that path ran and btfw failed *again*, or never ran at all,
  is **unverified and should not be asserted either way**.

  What is verifiable from the code: btfw's retry budget is `MAX_ATTEMPTS=30` at `RETRY_DELAY=0.10`,
  i.e. **3 seconds**, under a `TimeoutStartSec=5`. That is a short window in which to wait for a
  controller to become readable immediately after a firmware reload, and it is the most likely
  reason a reapply would fail a second time. Worth testing deliberately rather than inferring.
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
- **Echo suppression passed at 12.04 dB**, but n=1 and with the far-end talker co-located with the
  near-end mic, so the Lark heard the same speech twice. Near-end preservation is untested and
  E10's calibrated protocol has not been re-run on the fixed build.
- **Two captures were lost to a disconnected far-end microphone**, originally and wrongly written
  up here as a supervisor liveness defect. See the retraction below.

## Verdict

**`codex/aec-crackle-diagnosis` is confirmed on hardware and ready to merge.** The fix is load-
bearing: without it, a live call drives the graph to `min-quantum` 256, produces 34× the sink
underruns, and puts the playback path 405 ms behind.

**On `audio.aec.enabled`: keep it enabled.** Both arguments for disabling it have gone. The crackle
it was blamed for is fixed, and its suppression passes its gate for the first time at 12.04 dB
against E10's 1.77 dB. It is doing useful work for ~16.7 % of one core.

That rests on an n=1 measurement taken with the far-end talker in the same room as the near-end
mic. Revisit after a proper near-end preservation and double-talk test with uncorrelated talkers.

## RETRACTED: "a healthy-looking graph can be carrying no audio"

This section previously reported a supervisor defect: twice the far end went silent while the
supervisor reported `ACTIVE` with `verified: true`, `missing: []` and `unexpected: []`, and the HFP
downlink read -84.29 dBFS. It concluded that "the supervisor validates link topology, not signal
presence" and proposed a liveness check.

**That was wrong, and the conclusion is withdrawn.** The operator's far-end microphone was not
connected properly during those two captures. Nothing was being sent, so nothing arrived. The Pi
was working correctly and faithfully carrying silence, and `ACTIVE` was the correct report.

A bridge carrying silence because nobody is talking is not broken, and the liveness check proposed
here would have been a false-positive machine -- flagging a healthy unit every time the far end went
quiet. It was nearly built.

The related claim elsewhere in this document that **phone-side SCO routing is unreliable, "three
occurrences in one session"**, rested on the same mistake and is also withdrawn. The operator's own
impression that routing is "usually, but flaky" stands as their observation; the evidence offered
here for it does not. The E08 controller wedge was a separate and real event, unaffected by this.

**What survives, reframed:** "after a fault clears, do the links come back without the audio?" is a
real question, but it is a per-fault assertion against a *known injected* far-end signal, not a
standing health invariant. It only means anything when the far end is under test control.

**Methodological lesson worth keeping:** with no known far-end signal, "working but quiet" and
"broken and silent" are indistinguishable at every tap. E13 addresses this by looping a
deterministic source into the call rather than relying on someone talking.

## Finding: instrumentation cannot tap `bridge.aec.source`

Correct behaviour rather than a defect, but it constrains every future harness.
`remove_dangerous_autolinks` enforces exclusive consumption of the AEC source and unlinks any
consumer that is not the `bridge.mic` loopback -- which is what stops the cleaned mic signal
leaking. A recorder aimed there does not survive. Instrumentation must use the HFP sink monitor
instead, which is also the truer measurement since it includes the loopback stage.

## Next action

Hand two items to the fault-injection campaign: the `bridge-btfw` recovery gap and the
restart-churn wedge trigger. (A third, the "silent-but-ACTIVE liveness gap", was retracted -- see
above.) Re-run E10's calibrated speaker protocol on the fixed build to test whether the crackle fix
really did repair convergence.
