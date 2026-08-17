# E03 — Can one Broadcom radio carry HFP/eSCO and A2DP simultaneously?

- **Status:** Not started
- **Resolves risk:** **R1 — the highest-scoring risk in the project** (probability 4, impact 4, score 16)
- **Gates milestone:** M6, and decides whether Mode 1 or Mode 1W is the shipped default
- **Scripts:** `tests/stage-e-concurrent/s3-coexistence-smoke.sh` (smoke), then matrix rows E1–E6

## Question

With the Pi holding an HFP/eSCO link to the Pixel **and** an A2DP stream to headphones on
the same BCM43438 controller, how many audible dropouts per minute occur — and how does
that change with 2.4 GHz Wi-Fi disabled, with distance, and with RF congestion?

The threshold that matters: **<1 dropout/minute over 60 minutes with zero SCO
disconnections** is the acceptance bar in `PLAN.md` §14.4.

## Why it cannot be answered by reading

This is a controller scheduling question and no vendor publishes the answer.

What *is* documented: eSCO reserves periodic slots on the link; A2DP's ACL traffic must fit
around those reservations; and on the Pi 3 the Bluetooth and Wi-Fi radios share a die,
adding a coexistence arbiter whose behaviour is also unpublished. Rough load: two eSCO
directions at ~64 kbit/s each plus an SBC stream at ~200–350 kbit/s, all multiplexed over a
3 Mbit/s HCI UART with SCO-over-HCI framing.

Every term in that is knowable; their interaction under a real controller's scheduler is
not. Hence measurement.

**Explicitly out of scope: adding a second Bluetooth adapter.** The brief rules it out, and
if the answer is negative the deliverable is a measured limitation report with `btmon`
evidence — not a design that quietly routes around the finding.

## Method

Stage 1 — smoke test (10 minutes, tells you which world you are in):

```bash
./tests/stage-e-concurrent/s3-coexistence-smoke.sh --duration 600
./tests/stage-e-concurrent/s3-coexistence-smoke.sh --duration 600 --wifi-off
```

Stage 2 — acceptance runs, matrix rows E1–E6 from `PLAN.md` §9. **Row E6 is the control
group**: identical test in Mode 1W (wired output). Any degradation present in E5 but absent
in E6 is attributable to radio contention rather than to the audio graph. Do not skip it —
without the control, a negative E5 result is uninterpretable.

The smoke test combines objective counters readable on the Pi (PipeWire XRUNs, A2DP
transport state changes, `btmon` SCO/ACL traffic) with a **human dropout count**, because
the thing that matters — audible gaps at the headphones — happens after the radio and
cannot be observed from the Pi side. Use `--mode pips` tones if counting a continuous tone's
stutters proves unreliable; missing pips are easier to count than glitches.

## Coexistence variables to isolate

Change one at a time, per `PLAN.md` §6.7:

| Variable | Values |
|---|---|
| 2.4 GHz Wi-Fi | on / `dtoverlay=disable-wifi` |
| A2DP codec config | SBC default / `enable-sbc-xq = false` + bitpool cap |
| A2DP buffering | default / increased, to ride through SCO reservation windows |
| HFP codec | mSBC (16 kHz) / CVSD (8 kHz) |
| `ControllerMode` | `bredr` / `dual` |
| Distance | 1 m / 5 m + wall |
| RF environment | quiet / congested |

## Runs

| # | Date | Mode | Wi-Fi | Distance | Duration | Dropouts/min | SCO drops | XRUN Δ | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| 1 | | 1 | on | 1 m | 600 s | | | | |
| 2 | | 1 | off | 1 m | 600 s | | | | |
| 3 | | 1W | off | 1 m | 600 s | | | | (control) |

## Raw data

`docs/experiments/results/E03/`. **The `.btsnoop` captures are the deliverable** if the
verdict is negative — they are what turns "it didn't work" into a defensible measured
limitation.

## Result — 2026-08-16, stepwise isolation

Earlier attempts changed several variables at once and produced uninterpretable results. The
protocol was redone one variable at a time, with the operator confirming audible 1 kHz pips
(one per second — a *missing* pip is unambiguous where a stutter in a continuous tone is not)
before each transition.

| Step | Change | Objective | Subjective (operator) |
|---|---|---|---|
| **A** | A2DP alone | 1 ACL, transport active, controller alive | **pips clean** |
| **B** | + HFP connected, **no call** | 2 ACL, transport active, 6 reassembly errors | **pips clean, no stutter** |
| **C** | + call routed, **SCO live** | 2 ACL + eSCO, SCO rx/tx 804 per 6 s (nominal), controller alive | **degraded the instant SCO started**, recovered, ran ~20 s, then stopped |
| **C′** | sustained streaming + SCO | SCO still nominal; **A2DP link torn down** | silence, then disconnect |

### Two ACL links are NOT the problem

Step B is the important negative control: **A2DP and HFP coexist perfectly with no call
active.** Pips were clean by ear and the transport stayed active. Whatever fails is
specifically about **SCO**, not about holding two links.

### What actually tears the A2DP link down

Not the peer. The HCI trace is explicit:

```
< HCI Command: Disconnect (0x01|0x0006)     Reason: Remote User Terminated (0x13)
> Disconnect Complete                        Reason: Connection Terminated By Local Host (0x16)
@ MGMT Device Disconnected 98:47:44:CD:73:DE Reason: Connection terminated by local host (0x02)
```

`<` means the **host sent** the Disconnect. **Our own stack tore the link down.** In the
earlier iWorld run, bluetoothd logged `avdtp.c:cancel_request() Suspend…` then `Abort…`
immediately before doing the same thing.

**Working mechanism:** active SCO starves the ACL/AVDTP path → AVDTP signalling times out →
bluetoothd aborts and disconnects the A2DP device. The radio is not refusing; a *timeout in
our own stack* is giving up.

### Reproduced across two devices

The iWorld was suspected of being flaky (two `br-connection-page-timeout` failures, an
unprompted drop, and a self-reset into pairing mode). It was replaced with a **Soundcore
Space A40** as a control. **The A40 failed the same way**, which removes the peer as the
explanation.

### Controller wedge is a SEPARATE failure

In this run the controller **stayed alive** through the A2DP teardown (SCO continued at
nominal rate). The wedges seen earlier are therefore not the same event as the A2DP drop,
and should not be conflated. See E07.

## Verdict

**FAIL for sustained simultaneous operation — but the failure is in software, not proven to
be in the radio.**

Mode 1 (call audio to a Bluetooth car stereo while HFP carries the call) is **not currently
reliable**. The A2DP link survives seconds to tens of seconds once SCO is active, then our
stack disconnects it.

Confidence: **high that the behaviour is real** (reproduced, two devices, clean stepwise
isolation, explicit HCI evidence of a local teardown). **Low that it is irreducible** — the
proximate cause is an AVDTP timeout, which is a tunable, not a physical limit.

## Before declaring this a hardware limitation

The brief requires documenting a *measured* limitation rather than designing around it. That
bar is not met yet, because these are untried:

1. **AVDTP/AVRCP timeout tuning** in BlueZ — the teardown is a timeout, so lengthening it may
   simply let A2DP ride through SCO reservation windows.
2. **Larger A2DP buffering** (`session.suspend-timeout-seconds`, node latency) to survive
   starvation periods.
3. **Lower SBC bitpool / smaller ACL duty cycle** — `bluez5.enable-sbc-xq=false` is already
   set, but bitpool has not been capped.
4. **Wi-Fi disabled** (`dtoverlay=disable-wifi`) — never tested; the Pi's 2.4 GHz radio shares
   a die with Bluetooth and is currently enabled.
5. **`ControllerMode = bredr`** is configured but was never verified as applied.

Until at least 1, 3 and 4 are tried, "the Pi 3 cannot do HFP + A2DP" is unproven.

**Mode 1W (wired output) is unaffected** — it never asks the radio to do two things, and is
already proven end to end.

## Consequences for the plan

| Verdict | What happens |
|---|---|
| PASS | Mode 1 stays primary. R1 drops to probability 1. `config/bridge.toml.example` default changes to `mode = "bluetooth"`. |
| PARTIAL | Mode 1W is the shipped default (already is); Mode 1 stays supported and documented as degraded. Record which coexistence variables helped and by how much. |
| FAIL | Mode 1 is documented as a **measured controller limitation** with btmon evidence. `PLAN.md` §14.4 is formally replaced by the limitation report, per the escape clause already written into the acceptance criteria. Mode 1W becomes the product. Escalate to §15 Q1 for the user's call on which compromise they want. |

## Follow-up questions this raised

_(fill in — e.g. does A2DP recover on its own after an SCO teardown, or does it need a reconnect?)_
