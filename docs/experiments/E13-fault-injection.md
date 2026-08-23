# E13 — Where does LarkBridge get stuck?

- **Status:** THREE DEFECTS FOUND. Two fixed and verified on hardware, one characterised and unfixed.
- **Gates milestone:** wired AEC release
- **Owner / date:** Claude / 2026-08-22

## Question

The supervisor is written to survive transient endpoints — that is why it exists rather than static
config. Nobody had ever tried to break it. Where does it get stuck?

## Fixture

Live Discord call, Pixel 7a over HFP/SCO, Lark A1 on the Pi's USB, Pi 3.5 mm output to a speaker
about 1 m from the Lark. Supervisor from `claude/aec-crackle-diagnosis` at
`node_latency_frames = 1920`, graph quantum 2048.

**A deterministic far-end source ran throughout**: a speech-shaped loop played into Discord via
Stereo Mix, with Discord's output volume at zero to break the feedback path. This is what makes
"did audio resume" a real assertion rather than a guess, and it is the direct lesson of E12, which
misread an unplugged microphone as a supervisor defect. Measured arriving at the Pi at -19 dBFS on
the downlink and -12 dBFS at the speaker.

**The campaign is closed-loop.** Lark removal uses the USB `authorized` flag rather than hands, which
makes it *precisely timeable* — the race windows below are unhittable by a human unplugging a cable.
Phone-side faults go through adb. The only manual step in the whole campaign was the one-time
Discord routing change.

## Instruments

`rig/pi/measure/invariants.py` answers "is the bridge healthy" as structured data over seven
invariants. `faultctl.py` injects one named fault and samples that **as a timeline**, because a
single before/after pair cannot distinguish "recovered in 4 s" from "recovered in 90 s" from
"never", and those are three different products.

Validated before use: **zero violations on a healthy idle system (16 samples) and on a healthy
active call (121 samples)**. A checker that fires on a healthy system is useless.

**Deliberately not an invariant: "audio is flowing."** E12 reported a defect on that basis and was
wrong. Such a check fires on a healthy unit every time the far end goes quiet. Whether audio
*resumes after a fault* is a per-fault assertion against a known injected signal, and it lives in
the harness that controls that signal.

## Finding 1 — FAILED stranded a live call (FIXED)

Five AEC host deaths in a burst walk `attempts` 0→5, the supervisor reaches `FAILED`, and it never
comes back:

| | |
|---|---|
| State | `FAILED`, attempts 5, status fresh (0.1 s — alive and polling) |
| Endpoints | call_up True, lark True, output True — nothing unplugged |
| Far end on downlink | **-12.03 dBFS** — arriving |
| Onboard speaker | **-200.00 dBFS** — and discarded |

Ninety seconds, no recovery, **no invariant violated** — cleanly dead rather than corrupt, which is
worse in practice. The call stays connected and the speaker is silent for good.

`tick()` returns immediately when `FAILED`, and `update_signature()` resets `attempts` only when
`(call_up, lark, output_up)` *changes*. Mid-call nothing changes, so nothing retries. Hanging up
rescues it; nobody mid-call knows that.

**Fix:** `FAILED` schedules a retry at `FAILED_RETRY_SECONDS` (60 s) and resumes with a clean
counter. The `MAX_BUILD_ATTEMPTS` hot-loop guard is preserved and tested.

**Verified on hardware:** same burst → `FAILED` → recovered unaided in **109.5 s**, speaker back to
-12.26 dBFS against a -12.14 healthy reference.

### Two harness traps, because the first run of this produced a confident, meaningless pass

- The status file reports the **stale `owner_pid`** briefly after a kill, so a naive loop kills dead
  pids and counts failures that never happened.
- **`tick()` sets `attempts = 0` on every successful verification.** Waiting out the backoff between
  kills lets the graph rebuild and resets the counter. Failures accumulate only if each host dies
  *before* it verifies — reaching `FAILED` needs a **burst, not patience**. A user with an occasional
  AEC death would never see this; one with a sustained problem would.

## Finding 2 — a PipeWire or WirePlumber restart permanently kills call audio (NOT FIXED)

| Fault | Outcome |
|---|---|
| `kill-aec` | recovered, audio flowing, no violations |
| `kill-loopback` | recovered, audio flowing, no violations |
| `restart-supervisor` | recovered 8.8 s, audio flowing — call survived, as its unit file promises |
| `restart-wireplumber` | **SCO dropped, never returned** |
| `restart-pipewire` | **SCO dropped, never returned** |

The Bluetooth ACL survives; the SCO voice link does not, and nothing re-establishes it. The damage
is phone-side: **Android falls back to its own earpiece and stickily stays there.** Minutes later,
with the Pi reconnected and ACL up, `Computed Preferred communication device` is still `earpiece`.
The call continues, bypassing the bridge entirely, so the user hears audio from the phone with no
reason to connect that to a restart on the Pi.

Recovery is half automatable: `bluetoothctl connect` from the Pi restores ACL (phone-side Bluetooth
toggling did **not** auto-reconnect within 60 s), but restoring SCO is not — Android has already
decided where the call belongs.

**Unfixed, and arguably not the supervisor's to fix.** Worth deciding deliberately: the product
fails, even if the proximate cause is Android's routing policy.

## Finding 3 — raw un-cancelled uplink when the Lark appears (FIXED, reduced not eliminated)

When the Lark appears while an HFP sink exists, the session manager links it **straight to the far
end**:

```
alsa_input...Hollyland...analog-stereo -> bluez_output.5C_33_7B_CB_BF_C5.1
```

Raw un-cancelled microphone audio to the far end while `aec_verified` is false, and with the
speaker a metre from the Lark, the acoustic loop the supervisor exists to prevent — its own
docstring names this scenario. Reproduced on all four race-window variants and the baseline. The
violation fires in the *same sample* the Lark returns.

`remove_dangerous_autolinks` ran only once routes were up and `ATTACH_GRACE_SECONDS` had elapsed,
long after the link exists. **Fix:** it now runs before any build logic, on every tick where a Lark
and HFP sink coexist, and does not return early — stalling the build would extend the exposure it
is meant to end.

| | Window | Polls |
|---|---:|---:|
| Pre-fix | **8.19 s** | 244 / 1055 |
| Post-fix | **0.89 s** | 30 / 1061 |

**9.2x reduction, not elimination.** ~0.9 s remains, bounded by the 2 s poll interval. The complete
fix is a WirePlumber policy rule marking these nodes non-auto-linkable so the link is never made;
that changes routing policy rather than supervisor behaviour and deserves its own testing.

### The instrument could not see what it was measuring

The status-file sampler reported 6.4 s before the fix and **zero** after. Not credible: the
supervisor polls every 2 s, so some window had to remain. The sampler is gated on the supervisor's
own view, and the tick that first reports `lark_present` is the same tick that now removes the link
— so the exposure hid behind the measurement. `linkprobe.py` polls the graph directly and found
0.89 s. The A/B above was then re-measured with **one instrument on both sides**.

## Negative result — no leaks, no ratchet

Ten build/teardown cycles: modules 57, loopback processes 2, AEC file descriptors 54 — all
constant. RSS 13768–13824 KiB, jitter only, no trend. **Graph quantum 1920 every cycle, no
ratchet.** Recovery 13.3–15.2 s with no degradation. I6 and I7 hold.

## Tier 4 — the churn hypothesis is refuted

E12 observed the E08 controller wedge after four supervisor restarts during a live call and
offered restart churn as a candidate trigger, since E08's own open questions name loopback churn
as a suspect. That hypothesis was tested directly and **it lost**.

| Run | Restarts | Gap | State at each check | Wedge |
|---|---:|---:|---|---|
| Gentle | 8 | 12 s | `ACTIVE` (rebuild completed between each) | **no** |
| Harsh | 14 | 4 s | **`BUILDING` every time** — each restart interrupted an in-flight rebuild | **no** |

The controller answered on every check, ACL and SCO stayed up throughout, no invariant was
violated, and the call survived both runs with audio flowing. The harsh run is strictly more
aggressive than what preceded the E12 wedge — 14 interrupted rebuilds against 4 spaced restarts —
and the first run's 12 s spacing was already *tighter* than E12's roughly 37 s, so spacing alone
was never a plausible mechanism.

**The E08 wedge therefore remains unexplained, at n=1.** It is recorded here as unreproduced rather
than attributed to the nearest available cause.

Two other observations point the same way. An identical AEC kill burst was followed by the phone
dropping its Bluetooth link on one occasion and not on a second, and both churn runs left Bluetooth
untouched.

A genuine positive result falls out of this: **the supervisor survives 14 interrupted rebuilds
cleanly**, with no invariant violations and full recovery, which is a stronger statement about the
teardown path than any single fault produced.

## Corrections to E12

Both were mine, and both mattered enough to fix in place:

- **"Silent-but-ACTIVE" was retracted.** The operator's far-end microphone was not connected. The Pi
  was faithfully carrying silence and `ACTIVE` was correct. The liveness invariant it proposed would
  have fired on a healthy unit every time the far end went quiet — it was nearly built.
- **The `bridge-btfw` "never retries" claim is withdrawn as unverified.** `bt-reset.sh` has a
  `reapply_sco_routing()` on its success path that restarts the service, so the mechanism exists; I
  asserted its absence without looking. The journal had rotated before E13 could check whether that
  path ran and failed again, or never ran. What is verifiable: btfw's retry budget is
  `30 x 0.10 s = 3 s` under `TimeoutStartSec=5`, which is short for a controller settling after a
  firmware reload, and is the most likely reason a reapply would fail twice. Untested.

## Caveats

- **n=1 per fault**, except the ten-cycle leak trend.
- **Deauthorizing USB is not electrically identical to unplugging.** The device stays powered and the
  hub port is untouched. It drives the same kernel/ALSA/PipeWire removal path — all the supervisor
  can see — but it is not a physical disconnect.
- **Findings apply to the deployed hybrid**: system layer from `codex/boot-optimization`, user layer
  from `claude/aec-crackle-diagnosis`. Master's SCO routing refactor is **not** deployed, so Bluetooth
  results describe the older `set-sco-routing.sh`.
- **The E08 wedge was not reproduced**, so the btfw retry-budget hypothesis remains untested: it
  needs a firmware reload to exercise. Forcing one costs the call.
- Residual ~0.9 s uplink exposure is measured, not eliminated.

## Next action

Test the btfw retry budget by forcing a firmware reload (bt-reset.sh rung 6) and timing how long
the controller takes to become readable, against btfw's 3 s allowance. Decide whether Finding 2
warrants a product-level response. Consider the WirePlumber policy rule that would close Finding 3's
residual window. The E08 wedge needs a different hypothesis.
