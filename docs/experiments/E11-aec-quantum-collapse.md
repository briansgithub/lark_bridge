# E11 — Why does the onboard jack crackle while the far end is speaking?

- **Status:** ROOT CAUSE CONFIRMED — fix implemented and measured
- **Gates milestone:** wired AEC release
- **Owner / date:** Claude / 2026-08-21

## Question

The deployed unit crackles on the 3.5 mm output while the far-end party speaks. Is the
AEC responsible, and if so by what mechanism?

## What was actually deployed

The Pi was running `codex/aec-efficiency` HEAD exactly: the deployed
`pi/bridged/bridge_supervisor.py` hashes to `d6da1dd7c2a8dfcb…` (830 lines), matching that
branch's blob byte for byte. No local drift.

## Finding

**Loading the WebRTC AEC collapses the onboard sink's quantum from 2048 to 256.**

The module processes in 10 ms blocks and, given no `node.latency`, requests a 480-frame
quantum. 480 is below the configured `default.clock.quantum = 2048`, so PipeWire
renegotiates the whole graph downward and the bcm2835 sink lands on
`min-quantum = 256`. E10 had already rejected 256 on this device
("ALSA headroom 256 → 10 ERR, reject and roll back"), but nothing prevented the AEC from
imposing it.

`node.latency` and `buffer.play_delay` existed in `NativeAecHost` but were reachable only
from `rig/pi/measure/aec_bench.py` and `aec_thermal.py`. `begin_build()` passed neither, so
E10's ten-trial-clean 1920-frame timing was never what production ran.

Measured, sink quantum over 25 consecutive `pw-top` snapshots:

| Configuration | AEC nodes | Onboard sink |
|---|---:|---:|
| No AEC (control) | — | **2048** |
| AEC, no `node.latency` (production) | 480 | **256** |
| AEC, explicit `node.latency = 480` | 480 | **256** |
| AEC, explicit `node.latency = 1920` | 1920 | **1024** |

Production was indistinguishable from an explicit 480 — confirming the module's own
default was in force.

## Reproduction

No phone or call fixture is needed. The far-end path is
`SCO → bridge.callout → bridge.aec.sink → WebRTC APM → echo-cancel-playback → onboard
jack`; everything downstream of Bluetooth is exercised by playing a stimulus straight into
`bridge.aec.sink`. `rig/pi/measure/crackle_probe.py` does this using the real supervisor
module and the real deployed config, substituting a null sink for the Lark on the capture
side.

A 1 kHz sine at -12 dBFS was recorded at the **output sink's monitor** — what the DAC
actually receives — so a digital defect is distinguishable from PWM DAC behaviour.
`rig/analysis/glitch_detect.py` counts discontinuities.

**On an idle Pi the defect does not appear.** A 256-frame buffer survives when nothing
else runs. Under four-core CPU load, standing in for the real call workload (BlueZ SCO,
mSBC, USB capture, two loopbacks, the APM), it does not:

| Run | Sink quantum | `echo-cancel-playback` ERR | Glitches in 20 s |
|---|---:|---:|---:|
| Production default | 256 | 417 | **417 (19.5/s)** |
| `node.latency = 1920` | 1024 | 5 | 14 (0.65/s) |
| Production default (repeat) | 256 | 281 | 220 (10.1/s) |
| After fix, no flags | 1024 | 21 | 18 (0.84/s) |

The glitch count and the playback node's ERR count matched exactly in the first pair
(417/417): every underrun on the node feeding the DAC is one audible discontinuity.

This is why only the far end crackles — near-end audio never crosses the onboard DAC.

## Fix

`AecSettings` gained `node_latency_frames` (default **1920**) and `play_delay_frames`
(default unset); `load_settings()` parses and validates both; `begin_build()` passes them.
The status file now reports the timing in use.

## Caveats

- The load fixture is a blunt four-core busy loop, not a real call. It drove the Pi to
  83 °C and tripped `throttled=0x20002`; the real workload is lighter and cooler. It
  establishes the mechanism and the fix's direction, not an absolute error rate.
- Reproduction used a 1 kHz sine with a null sink on the AEC capture side. Real speech and
  a live Lark were not exercised.
- **This fixes the crackle, not AEC quality.** E10's suppression gate still FAILS
  (1.77 dB median against a 10 dB gate). `enabled = false` remains a valid remedy if echo
  cancellation is not yet worth its cost.
- A real-call confirmation remains outstanding.

## Next action

Confirm on a live call, then revisit E10's suppression failure separately.
