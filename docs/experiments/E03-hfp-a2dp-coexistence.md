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

## Result

_(fill in)_

## Verdict

_(PASS < 1/min · PARTIAL 1–10/min · FAIL > 10/min or SCO disconnects)_

## Consequences for the plan

| Verdict | What happens |
|---|---|
| PASS | Mode 1 stays primary. R1 drops to probability 1. `config/bridge.toml.example` default changes to `mode = "bluetooth"`. |
| PARTIAL | Mode 1W is the shipped default (already is); Mode 1 stays supported and documented as degraded. Record which coexistence variables helped and by how much. |
| FAIL | Mode 1 is documented as a **measured controller limitation** with btmon evidence. `PLAN.md` §14.4 is formally replaced by the limitation report, per the escape clause already written into the acceptance criteria. Mode 1W becomes the product. Escalate to §15 Q1 for the user's call on which compromise they want. |

## Follow-up questions this raised

_(fill in — e.g. does A2DP recover on its own after an SCO teardown, or does it need a reconnect?)_
