# Remaining ESP32 qualification gates, 2026-11-24

Full-gate status against the [qualification matrix](../docs/qualification-matrix.md).
This snapshot reconciles the local qualification reports, the H2–H5 bench
worktree record `1229daa`, and Claude's follow-up through `9b78756`. PASS
applies to the recorded images, peers, and topology. Open includes missing,
partial, inconclusive, and superseded smoke failures. The tool registry still
has older status entries for some completed work.

Commands below are for the hardware owner. Run them from the project root on
the hardware host, substituting the recorded console names and paired
addresses for `S31_PORT`, `OUTPUT_PORT`, `SINK_MAC`, and `FAKE_CAR_MAC`.
Use Windows Python for Windows console names. Evidence paths beginning
`../lark_bridge-evidence/` are outside the repository. A capture or
analysis helper listed for a manual gate covers only the step described.

## Hardware gates

| Gate | Status and latest evidence | P3 rig SD card | Human / real call | Script or procedure |
| --- | --- | --- | --- | --- |
| H0 · Identity and antenna | **Open.** P5 requests photos, SKU and physical antenna check. H1 logs supply boot identity only. [Human queue](human-queue.md) | No | Human for inspection; no call. | `python firmware/tools/arrival.py new-run --gate H0 --label inventory`. Inspect per [arrival runbook](../docs/arrival-runbook.md) and attach the photos and inventory with `arrival.py record`. |
| H1 · Independent flash and boot | **PASS.** Both boot bundles date from `057eb3c`. Both post-handoff smokes also passed. See `20261124T042154-H1-s31-boot-lark-attached`, `20261124T042953-H1-output-devkitc-boot`, and `20261124-h1-post-handoff`. [Follow-up](followup-2026-11-24.md) | No | One-time USB setup; unattended replay; no call. | Follow [app-only flashing](../docs/flashing.md) with pinned artifacts, then `python rig/esp/boot_capture.py --chip esp32s31 --port S31_PORT --out s31-boot.log` and `python rig/esp/boot_capture.py --chip esp32 --port OUTPUT_PORT --out output-boot.log`. |
| H2 · UART protocol | **PASS.** `26a2190`, bench record `20261124T165735Z-H2` under `../lark_bridge-evidence/h2-h5-2026-11-24/`. Clean window, corruption recovery, and duplicate suppression passed. [Follow-up](followup-2026-11-24.md) | No | P1 harness fit once; unattended afterward; no call. | `python rig/esp/hil.py run H2 --s31-port S31_PORT --output-port OUTPUT_PORT`. |
| H3 · Independent reset | **Open.** First 1,540 ms mute result exceeded 1,500 ms. Guard retry recorded 1,400–1,409 ms and recovery within 5.28 s. D4 connected-reset smoke recovered 10/10. Full playback mute and both-board bond retention still need one scored run with a live sink. [Follow-up](followup-2026-11-24.md) | No. The paired speaker can supply the live link. | Unattended once paired; no call. | `python rig/esp/hil.py run H3 --s31-port S31_PORT --output-port OUTPUT_PORT --allow-reset`. Keep a sink capture for audible mute and recovery. |
| H4 · I2S electrical and PCM | **Open.** Nominal 1 kHz, channel RMS, and 120 s of decoded silence passed on the receiver. Wire clock, slot width and edges remain unmeasured. [Follow-up](followup-2026-11-24.md) | No | Human to fit a supported analyzer and probes; no call. | `python rig/esp/hil.py run H4 --s31-port S31_PORT --output-port OUTPUT_PORT --analyzer fx2lafw` for a compatible FX2 analyzer. Without it, omit `--analyzer` for the PCM-only run. |
| H5 · FIFO and drift | **Open.** `5e173bd` 60 s speaker smoke passed at 3,239–4,789 FIFO frames and max 110 ppm. Full 30 min after warmup still due. [MP43247 report](mp43247-2026-11-24.md) | No for FIFO limits with the speaker. P3 supplies a synchronized sink WAV on the planned fake-car path. | Unattended once paired; no call. | `python rig/esp/hil.py run H5 --s31-port S31_PORT --output-port OUTPUT_PORT --fake-car-address SINK_MAC`. For extended drift use `python rig/esp/soaks/h5_long_drift.py --s31-port S31_PORT --output-port OUTPUT_PORT --fake-car-address SINK_MAC --out h5-long`. |
| H6 · LARK UAC1 | **Open.** Enumeration and steady capture smokes only. Overnight lacked transmitter and recorded 0/100 reconnects. The 100-cycle reducer and fixture path were verified with mocks. [Overnight](overnight-2026-11-24.md) · [Host review](codex-review-2026-11-24.md) | No with a host P7 hook. P3 is optional fixture hosting. | P6 transmitter and P7 fixture setup. P9 is attended without actuators; no call. | `python rig/esp/soaks/h6_reconnect.py --s31-port S31_PORT --output-port OUTPUT_PORT --p7-cycle-hook P7_HOOK --out h6-reconnect`. Use `--p7-setup-hook`, `--p9-usb-hook`, `--p9-tx-hook`, or `--boot-log` as needed. |
| H7 · Downstream Bluetooth | **Open.** MP43247 paired at 44.1 kHz and streamed again 5.753 s after one 32UE reboot. Fake-car, real car, and AVRCP coverage remain. [MP43247 report](mp43247-2026-11-24.md) | Yes for the planned fake-car phase. | Human for real-car pairing and listening; no call. | On the rig Pi after acquiring its lease, `fake-car pair-accept 120 --until-paired`, `fake-car record 300 h7.wav`, and `fake-car avrcp play --device OUTPUT_MAC`. Repeat pair/reconnect and control checks with the car. No full H7 runner. |
| H8 · Pixel media | **Open.** First ingress smoke failed. Foreground retry `237452d5` passed for 180 s and `d12de50` verified the two-board tone relay. Full stereo sink recording and AVRCP actions remain. [Follow-up](followup-2026-11-24.md) | Yes for planned fake-car WAV and AVRCP. Speaker stream can run without it. | One-time phone and sink setup; no real call. | `bash rig/esp/audio_soak.sh h8-ingress 180 S31_PORT`, then `python rig/esp/summarize_soak.py --pixel-timezone America/New_York h8-ingress`. For the downstream leg use `python rig/esp/soaks/s31_32ue_streaming.py --s31-port S31_PORT --output-port OUTPUT_PORT --sink-address SINK_MAC --adb ADB_EXE --pixel-serial PIXEL_SERIAL --out h8-stream`. Use `fake-car record 180 h8.wav` and the H12 commands for remaining checks. |
| H9 · Pixel call | **Open.** D5 crashed the original AEC-on call. `213dead` gave a 12 s AEC-locked downlink smoke with no reset. Ten minutes and silence on the SCO-routed uplink remain. [Follow-up](followup-2026-11-24.md) · [D5 research](aec-d5-research-2026-11-24.md) | No for harness capture. | Install and grant harness once; self-managed call is enough for this gate; real call at final sign-off. | Use the [H9 harness commands](../../rig/esp/pixel-harness/README.md#h9-uplink-measurement). Set `--ez record_uplink true` on `CALL_START`, play 210 loops of the 3 s voice WAV, poll `STATUS`, then `CALL_END`. Save both board logs and uplink JSON. No full H9 reducer. |
| H10 · LARK call and AEC | **Open.** Earlier constructor crash confirmed. FD/PSRAM and linear-only candidates compiled, neither scored on hardware. Three calibrated captures and fail-to-silence checks remain. [D5 research](aec-d5-research-2026-11-24.md) | No if a recording computer and far end supply the fixture. Optional for repeatable acoustic setup. | Human for capture setup and eligibility loss tests; live far-end call path per runbook. | Follow [AEC qualification](../docs/aec-qualification.md), including the delay sweep. Score each final capture with `python rig/analysis/aec_metrics.py --reference reference.wav --raw raw.wav --clean clean.wav --signal speech --min-suppression-db 10 --trailing-silence 5`. |
| H11 · Audio focus | **Open.** `9b78756` records 10/10 scoped preempt/restore after harness fix `1f7260e`. Still lacks sink recording evidence for stale-media leakage and a separate media app. [Claude follow-up at 9b78756](../../../lark_bridge-esp32-wireless/firmware/qual/followup-2026-11-24.md#h14-and-scoped-h11-on-the-new-s31-image-1805-et) | Yes for the planned fake-car recording. | Self-managed calls; human if setting up external media app. | Scoped replay uses `python ../lark_bridge-evidence/20261124-h11-scoped/h11_scoped.py`. Copy the archived script to a fresh evidence directory before executing since it writes alongside itself. Add `fake-car record SECONDS h11.wav` on the rig Pi. No full H11 reducer. |
| H12 · Steering controls | **Open.** No full exactly-once AVRCP button sweep. H8 relay and AVRCP link-up are prerequisites. [Matrix](../docs/qualification-matrix.md) | Yes for emulated controls. A real car can send its controls directly. | Human for real buttons and unsupported button checks; no call. | On the rig Pi run `fake-car avrcp play --device OUTPUT_MAC`, then `pause`, `next`, and `prev` in place of `play`. Confirm each Pixel action once. Repeat with car controls and log with `arrival.py new-run --gate H12 --label steering`. |
| H13 · RF coexistence | **Open.** No fixed spacing/orientation matrix or frozen enclosure baseline. [RF matrix](../docs/rf-power-matrix.md) | Optional for instrumented sink capture. | Human for each antenna placement. Active call per cell; harness for bench, real call for final sign-off. | Run `bash rig/esp/audio_soak.sh OUT_DIR 600 S31_PORT` and a 300 s harness call per [RF matrix](../docs/rf-power-matrix.md). Use `python firmware/tools/arrival.py new-run --gate H13 --label CELL` per layout. No complete matrix runner. |
| H14 · Fault injection | **Open.** Initial adapter loss failed to recover. `9b78756` boot-hold image recovered profiles in 2.4 s in an adapter smoke after clearing the test route. Full fault matrix and audible fail-safe remain. [Evidence](../../../lark_bridge-evidence/20261124-h14-boot-hold/) | No for adapter/UART/software resets. Rig Pi and switched hub needed for planned hard-power faults. | Human for UAC/power faults without fixtures. A harness call can provide active-call coverage. | `python rig/esp/soaks/h14_adapter_cycles.py --s31-port S31_PORT --output-port OUTPUT_PORT --adb ADB_EXE --pixel-serial PIXEL_SERIAL --out h14-adapter`. Run H2/H3 fixture commands for UART resets and corruption, H6 for USB, and record remaining faults per [HIL guide](../docs/hil-rig.md). |
| H15 · Repetition | **Open.** Ten D4 resets or H11 calls exercised only individual paths. No ten-cycle end-to-end result. [Follow-up](followup-2026-11-24.md) | Yes for the planned automated production-like fixture; required power and capture functions can be supplied separately. | Human unless all physical steps are actuated; harness call for bench repetition. | No end-to-end cycle runner. Repeat the H6, H8/H9 and H14 procedures ten times with cycle boundaries and recovery evidence, and use `python firmware/tools/arrival.py new-run --gate H15 --label ten-cycles` to record. |
| H16 · Active-call endurance | **Open.** The 6 h 11 min ACL soak and the media campaign exercise different paths. A full 3,600 s active call with AEC, heap and audio error evidence remains after H10/H15. [Overnight](overnight-2026-11-24.md) · [Matrix](../docs/qualification-matrix.md) | Yes for planned unattended production-topology capture and power control. | Live active call in production topology. Final car listening/cellular sign-off is attended. | `python rig/esp/bt_soak.py --adb ADB_EXE --port S31_PORT --seconds 3600 --out h16-capture` captures the S31 leg during an already active call. Also capture 32UE stats and far-end audio and score the matrix's heap and error limits. No full H16 runner. |
| H17 · Vehicle power | **Open.** Protected automotive supply and production topology still to be selected. [RF/power matrix](../docs/rf-power-matrix.md) | Optional logging/actuation; P3 does not supply the automotive power fixture. | Human with appropriate power fixture; real call only for the separate final listening sign-off. | No ESP vehicle-power runner. Follow the documented crank/cut/cycle matrix after supply selection. Use `python firmware/tools/arrival.py new-run --gate H17 --label vehicle-power` and attach rail/scope and boot logs. |

## Supporting gates

| Registry gate | Status and evidence | P3 | Human / real call | Entry point |
| --- | --- | --- | --- | --- |
| T0-host | **PASS.** Eight CTest targets and host Python suites in the Codex review. | No | No / no | `python firmware/tools/build_firmware.py host` |
| T1-build | **PASS.** Both release and test images on pinned IDF in the [Codex review](codex-review-2026-11-24.md). | No | No / no | `python firmware/tools/build_firmware.py all --no-pull`, then repeat with `--image test`. |
| T2-smoke-s31 / T2-smoke-output | **PASS.** Tracked E21 bundles `20261124T110145Z-T2-smoke-s31`, `20261124T110400Z-T2-smoke-output`; an S31 repeat passed at `20261124T154538Z-T2-smoke-s31`. | No | No / no | `python rig/esp/hil.py run T2-smoke-s31 --port S31_PORT`; `python rig/esp/hil.py run T2-smoke-output --port OUTPUT_PORT`. |
| stageB-exit | **PASS.** Both T2 smokes passed unattended. [Overnight](overnight-2026-11-24.md) | No | No / no | `python rig/esp/hil.py gates --json` for the registry and the two T2 results for evidence. |
| BT-soak | **PASS.** E21 `20261124T105554Z-BT-soak`, 124/124 polls up over 6 h 11 min, no S31 reset. | No | No / no | `python rig/esp/bt_soak.py --adb ADB_EXE --port S31_PORT --seconds 22250 --out bt-soak`, then `python rig/esp/hil.py reduce BT-soak --log bt-soak/summary.json`. |
| harness (P1) | **PASS for bench connectivity.** Both boards exchanged UART in H2 and passed decoded PCM in the H4 smoke. Keep physical continuity evidence with wiring inventory. | No | Human for wiring / no call | [Harness wiring checklist](harness-wiring.md), then H2 and H4 commands above. |
| analyzer | **Open.** P2 confirmed no logic analyzer. | No | Human to fit probes / no call | H4 with `--analyzer` above. |
| fake-car (P3) | **Open.** Rig SD provision/setup still queued. | Yes | Human to prepare/boot rig SD once / no call | On the rig card, follow [provisioning](../../rig/esp/pi-image/README.md), then run `rig-selftest` and `fake-car status`. |
| pixel-harness | **PASS.** Installed, permissions granted and used for `d12de50` smokes. The newer capture APK is host-built only. | No | Human approval for install / no real call | `bash rig/esp/pixel-harness/build.sh` and the [install, grant and STATUS commands](../../rig/esp/pixel-harness/README.md). |

## Unattended speaker campaign

The campaign runs H5 long drift, H6 with P7, ten H14 adapter cycles, and
four hours of S31-to-32UE media streaming in that order. It uses one
host rig lease and one console capture. It checks both USB port identities,
the test images, the fixture executables, the specified Pixel ADB transport,
and fresh downstream 1 kHz keep-alive PCM near -50 dBFS. Start with the
speaker bonded and connected, the 1 kHz keep-alive active, the Pixel
harness installed, and an unattended P7 fixture ready.

```sh
python rig/esp/soaks/end_of_project.py \
  --s31-port S31_PORT --output-port OUTPUT_PORT \
  --sink-address SINK_MAC --adb ADB_EXE --pixel-serial PIXEL_SERIAL \
  --p7-cycle-hook P7_HOOK --out end-of-project
```

Each result updates `summary.json` and `summary.md`. The final verdict
requires all stages to pass with their expected durations, cycles and
counters. A failure skips later stages. A reset, panic, console identity
change, or unrecognized UAC microphone stops the run and blocks subsequent
fixture commands. Check `bench.jsonl` and `hooks.log` before recovering the
rig. H5 replaces the initial keep-alive with its own stimulus and clears it
on exit. The campaign uses the speaker throughout. Fake-car recording,
vehicle checks, and H16 active-call endurance remain separate runs.
