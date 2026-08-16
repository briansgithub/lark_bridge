# ADR-0001 — Three operating modes, with Mode 1W as the default

- **Status:** Accepted
- **Date:** 2026-08-15
- **Relates to:** `PLAN.md` §1.4, §2.1, risk R1

## Context

The brief specifies two modes: a Bluetooth bridge (Mode 1) and a USB/Pico bridge (Mode 2). Mode 1
requires the Pi 3B's single Broadcom BCM43438 controller to carry an HFP/eSCO link to the Pixel and
an A2DP stream to headphones *simultaneously*. Nothing in the documentation establishes that this
works acceptably, and no vendor publishes the controller's slot-scheduling behaviour under that load.
It is the project's highest-scoring risk (R1) and it sits directly on the critical path to a working
product.

Meanwhile, the *only* thing A2DP contributes to Mode 1 is the last hop of the downlink. Call audio is
already capped at HFP quality (16 kHz mSBC at best), so A2DP's fidelity is irrelevant — only its
reliability and latency matter, and its latency is bad: 150–250 ms one-way on typical headphones.

## Decision

Define **three** modes, and make **Mode 1W** — Bluetooth HFP for the call, wired output for the
audio you hear — a first-class mode and the shipped default until spike S3 proves otherwise.

| Mode | Mic path | Output | Radio carries |
|---|---|---|---|
| 1 | Lark → Pi → HFP → Pixel | A2DP headphones/car | HFP **+** A2DP |
| 1W | Lark → Pi → HFP → Pixel | USB DAC or 3.5 mm jack | HFP only |
| 2 | Lark → Pi → Pico → Pixel | Pixel → Pico → Pi → any sink | nothing |

Mode 3 (diagnostics) is a policy state, not an audio topology.

## Consequences

- The riskiest element is off the MVP critical path. If S3 fails, there is still a product.
- Mode 1 vs Mode 1W differ by **one `target.object` string** on the `bridge.callout` loopback. This
  is only true because of ADR-0002; the two decisions are load-bearing for each other.
- Stage E gains a control group: test E6 runs Mode 1W alongside E5's Mode 1, so any measured
  degradation is attributable to radio contention rather than to the audio graph. Without this we
  could only observe that Mode 1 is bad, not *why*.
- Mode 1W is also the low-latency mode (~50–70 ms downlink vs ~200–300 ms), which may make it the
  permanent preference regardless of what S3 finds.
- Cost: one extra mode to document and test, and a ~$6 USB dongle in the bill of materials.

## Alternatives considered

- **Two modes as briefed, Mode 1 with A2DP only.** Rejected: makes the MVP hostage to R1, and
  discovering the failure late would invalidate weeks of integration work.
- **Treat wired output as an undocumented fallback.** Rejected: untested fallbacks do not work when
  needed. If it is the recovery path it must be in the test matrix.
- **Skip Bluetooth entirely, ship Mode 2 first.** Rejected: Mode 2 depends on Android routing calls
  to USB audio, which is *also* unverified (R3) and less within our control than the BT path.
