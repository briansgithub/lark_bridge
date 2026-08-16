# ADR-0007 — Fixed 48 kHz internal graph

- **Status:** Accepted
- **Date:** 2026-08-15
- **Relates to:** `PLAN.md` §5.1, §5.2

## Context

The system spans five rates: 8 kHz (CVSD HFP), 16 kHz (mSBC HFP), 44.1 kHz (A2DP), 48 kHz (A2DP),
48 kHz (USB headset). The brief asks for a clean internal architecture rather than repeated
resampling, and for an explanation of where resampling is unavoidable.

PipeWire can run the graph at a negotiated rate and switch it via `default.clock.allowed-rates`.
Running the graph at 16 kHz when only HFP is active would eliminate a resample — but in Mode 1, HFP
(16 kHz) and A2DP (48 kHz) endpoints are live **simultaneously**, so any dynamic switch optimises one
endpoint by penalising the other, and rate switches cause visible glitching on both.

## Decision

Fix the graph at **48 kHz**. `default.clock.rate = 48000`,
`default.clock.allowed-rates = [ 48000 ]` — deliberately a single-element list.

Quantum starts at 1024 (21.3 ms) for stability on a Pi 3, tuned down toward 512/256 during M8 with
XRUN counts as the gate. `min-quantum = 256`, `max-quantum = 2048`.

## Consequences

- **Exactly two resamples in Mode 1, both at the HFP boundary, both unavoidable:** 48→16 kHz on the
  uplink and 16→48 kHz on the downlink. HFP is a 16 kHz transport; no architecture avoids this.
  If the Pixel negotiates CVSD instead of mSBC these become 48↔8 kHz.
- **Zero resamples in the nominal Mode 2 path.** Lark, graph, I2S link and USB are all 48 kHz. The
  Pico's ±1-sample adjustment is rate *steering*, not rate conversion.
- A third resample appears only if the A2DP sink refuses 48 kHz and forces 44.1. Mitigated by
  `bluez5.default.rate = 48000`; if the headphones insist, we accept it and log a WARN rather than
  reshaping the graph around one device.
- Because the negotiated HFP codec silently determines the uplink quality ceiling,
  `bridgectl status` must always print it. A silent fall back to CVSD is the most likely cause of
  "why does this sound like 1995", and it must never be invisible.
- Cost: one resampler stage always running on the HFP legs even when nothing else is active. On a
  mono voice path this is negligible on a Pi 3.

## Alternatives considered

- **Dynamic graph rate via `allowed-rates = [ 16000 48000 ]`.** Rejected: in Mode 1 both rates are
  needed at once, so switching helps one endpoint and hurts the other, plus rate switches glitch.
- **Run the graph at 16 kHz and upsample only for A2DP.** Rejected: destroys the Lark's bandwidth for
  every non-HFP consumer (recording, monitoring, Mode 2) to save one resampler.
- **Per-mode graph rates (16 kHz in Mode 1, 48 kHz in Mode 2).** Rejected: mode switching would
  require a full graph restart, breaking ADR-0002's "mode change is a metadata write" property, which
  is what makes mode switching safe mid-session.
