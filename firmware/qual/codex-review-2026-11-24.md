# LarkBridge host review, 2026-11-24

Reviewed `409c03d..213dead` on `codex/host-review-1124`, including the codec,
H5, D4, and AEC changes. All work stayed on the host. The branch was not pushed.

## Fixes

| Commit | Finding and change |
| --- | --- |
| `246fd07` | A short A2DP pull at a step below one input frame could request zero new input frames or start between the last two consumed frames. The unsigned position calculation could wrap, and the caller treated zero input as no output, losing phase. Keep two frames of history, support zero-input pulls, and bound the Q32 arithmetic and sample indexing. |
| `d11d0e4` | A concurrent flush could clear the FIFO between the prefill check and trimming. The resulting unsigned subtraction could corrupt the count and indices. Check, trim, and publish the primed state under the ring lock. An old multi-block pull also checks for a flush before consuming again. Calculate I2S clock age using a 64-bit timestamp sampled before the current time. The prior signed 32-bit age could classify a clock stopped for about 25 days as active. |
| `504b1ff`, `13f4a70` | The fallback excluded a sink minimum bitpool of exactly 35 from the 35-bitpool cap. A 44.1 kHz-only, single-mode sink could then get no distinct retry. Accept the boundary and run the production selection logic in host tests. |
| `031d50c` | A phone that connected both profiles during the boot hold could cause the worker to replace the hold deadline with its ordinary backoff deadline. If a profile dropped again, an outgoing page could go out before the full five seconds elapsed. Keep a separate one-shot elapsed-time gate. |
| `41a9433` | Added CTest coverage for SBC rates, mode preferences and mono fallback, invalid ranges, the bitpool boundary, FIFO convergence from 4096 frames at both signs of 180 ppm, and the boot hold across a 32-bit tick wrap. The common suite also checks the 48-to-44.1 kHz ramp and zero-input frame accounting. |
| `21b32c8` | Removed the H5 test build's unused `libm` link dependency, which would make MSVC look for `m.lib`. |
| `2aa4709`, `a1b04ae`, `13bc342` | Added four soak entry points using the existing console and lease infrastructure. H6 scores P7 independently of the optional P9 phase. H14 and streaming accept either built-in Pixel commands or fixture executables. |
| `e0979b6` | The H6 reducer treated the normal USB detach warning as a transfer failure. The unattended soak now requires exactly 100 detach warnings, disconnects, and reopens. Transfer and capture errors still fail the run. |

The 240-frame output blocks fit the resampler staging buffer at 44.1 and 48 kHz
through the full ±500 ppm range. The nominal frame accumulator fits in 32
bits, and the Q32 adjustment and interpolation fit in signed 64 bits. The
FIFO convergence test models average fill per 100 ms and includes integer
filter rounding. Burst timing and DMA scheduling need hardware evidence.

## Open hardware checks

- **Output negotiation.** Exercise 44.1 kHz, 48 kHz, and mono with a sink or
  emulator that advertises each case. Capture the successful preferred-codec
  result, the on-air configuration, and the audio after a fallback. The
  selector restricts negotiation to 16 blocks, eight subbands, and loudness
  allocation. The host tests cover selection, while the ESP32 encoder and
  sink determine what plays. The A2DP output tap scores stereo.
- **Failed negotiation and bonds.** Codec failure recycles the connection
  without erasing bonds. A permanently incompatible saved sink can therefore
  retry indefinitely at the configured interval, and failed qualification of a
  new sink can leave its stack bond without an application peer record. The
  rejection path for a peer that cannot be persisted requests bond deletion
  without checking completion. Check NVS and the stack bond list after failed
  negotiation and recovery. Decide when repeated failures warrant removing a
  peer before changing bond policy.
- **Route transitions.** The FIFO flush and prime operations now share a lock.
  The rest of a PCM callback can still overlap a route change after it has
  copied a block, and I2S DMA can already have PCM in flight. Use the existing
  hardware silence-boundary check across route changes.
- **H5 and D4.** Compare FIFO span, correction and audible behavior at both
  sink rates. Run the connected-reset D4 check again and exercise adapter
  off/on separately. A different phone may negotiate a different supervision
  timeout from the Pixel's observed five seconds.
- **AEC.** `91fa85b` tried to measure retained heap with a boot-time ESP-SR
  allocation. That probe itself faulted with roughly 232 KB free. `213dead`
  removed the probe and disabled the engine by default in both image kinds.
  Confirm a call completes without reset and the call uplink is silent.
  Investigate the ESP-SR FFT/NLP initialization failure before enabling the
  engine or attempting an AEC quality run.

## Soak scripts

Invoke the scripts in `rig/esp/soaks` with Python 3.11 or later on a host that
can open both board consoles. Each takes `--s31-port`, `--output-port`, and a
new `--out` directory. They require test images with bond erase disabled.
They use the shared rig lease and write `bench.jsonl` and `summary.json`.
Fixture commands also write `hooks.log`. Exit 0 means PASS and exit 2 means
FAIL, including partial evidence. The fixture hooks are local executables on
the runner's PATH or at an explicit path. They should return only after their
requested transition is complete and clean up their own child processes.

| Script | Run contract and evidence |
| --- | --- |
| `h5_long_drift.py` | Four hours by default. Supply the bonded fake-car address with `--fake-car-address`. The H5 reducer scores FIFO band, ppm, stream continuity, and error deltas after warmup. If there is a sink recording, supply `--recording` with a WAV aligned to the measured window. |
| `h6_reconnect.py` | Supply `--p7-cycle-hook` after wiring the Pico fixture. It receives the cycle number and must disconnect, reconnect, and restore compatible UAC data. Each of 100 cycles needs a new disconnect, a reopen, and renewed RX. Supply `--p9-usb-hook` and `--p9-tx-hook` if the real Lark exercises can be actuated unattended. Those receive the cycle number for ten USB and five transmitter cycles before P7. The TX hook must verify the actual actuator transition. The board evidence verifies USB remains up. Supply `--p7-setup-hook` for any fixture handover after P9. The scored P7 window begins after setup. Keep real-Lark manual P9 work out of the unattended run when it cannot be actuated. |
| `h14_adapter_cycles.py` | Ten cycles by default. Supply `--adb` and `--pixel-serial` for the built-in Pixel adapter commands, or `--adapter-hook` accepting `off` or `on`. The script checks profile loss, an effective silence route, revoked output audio, and recovery of HFP, A2DP and AVRCP on the same saved peer. Adapter cycles cover only this portion of H14. |
| `s31_32ue_streaming.py` | Four hours by default. Supply the 32UE address with `--sink-address`. Use `--adb` and `--pixel-serial` with the installed Pixel harness, or a `--media-hook` accepting `start SECONDS` and `stop` to play continuous nonzero media. Both-board telemetry, source PCM byte rate, and downstream nonzero tap counts provide the verdict. Capture the sink separately if acoustic continuity is required. |

Host checks used `--help` and mocks only. Mocks exercised all four scenarios,
including 100 H6 reconnects and ten H14 adapter cycles. They also checked
fixture command arguments, lease cleanup, and PASS and FAIL summaries.

## Host verification

Eight CTest targets passed in the standard host build and under UBSan. The
11 firmware tool tests and 35 rig Python tests also passed.

| Docker target | Pinned ESP-IDF | Release | Test |
| --- | --- | --- | --- |
| Output ESP32 | `v6.0.2` | PASS | PASS |
| S31 ESP32-S31 | `v6.1-rc1` | PASS | PASS |

All four builds used `firmware/tools/build_firmware.py --no-pull` and emitted
build manifests. The recorded commit differs across builds because host tests,
scripts, and the report were committed while the sequence ran. The compiled
firmware source is equivalent across those commits.

The only difference among the generated LarkBridge Kconfig settings for
each target was `CONFIG_LARKBRIDGE_TEST_IMAGE`. Both images keep bond erase
disabled. Both output images use an 8192-frame ring and a 4096-frame prefill
(about 85 ms at 48 kHz). Both S31 images leave the AEC engine and experimental
uplink disabled.
