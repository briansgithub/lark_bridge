# LarkBridge ESP32 Wireless Port — Overnight Campaign Report (2026-11-25)

## Executive Summary & Provenance

> [!IMPORTANT]
> **Full transparency on provenance, scope, and safety stops:**
> 1. **Earlier 7.5 h soak did not run:** It was cancelled at Brian's request earlier in the evening. Tonight's fresh soak (`soak_6h`) completed **2 × 20-min clean segments (40 min continuous streaming)** before the Task Scheduler watchdog falsely tripped its stall timer (it monitored `campaign.log` instead of `soak_6h/segments.jsonl`) and exited cleanly with Shelly verified ON.
> 2. **Firmware under test tonight:** Local commits **`830ee1f`**, **`a0d7599`**, and **`aa745ab`** on branch `claude/esp32-wireless-port` (NOT the earlier `6dbdb64`/`36202f1` baseline). None of these commits have been pushed.
> 3. **Output sink used tonight:** **`LarkRig Fake Car`** (`B8:27:EB:43:8D:51`, silent PipeWire tap on `larkbridge-rig`). Pairing the fake car cleared the **MP43247** (`c9:5c:fd:6e:28:46`) bond on 32UE. To restore MP43247 later, send `forget peer` on `COM4` and put MP43247 into pairing mode.
> 4. **H6 remains FAIL (two real S31 USB bugs caught):** Real Hollyland Lark A1 physical replug (P9) failed on cycle 1/10 at 03:16 (`ENUM: CHECK_SHORT_DEV_DESC FAILED` on `6dbdb64`), AND automated Pico fake-Lark replug (P7) caught a genuine **`Guru Meditation Error: Core 0 panic'ed (Load access fault)`** inside `uac_lark.c` → `usb_host_uac 1.5.0` on `aa745ab`. Testing stopped immediately on the panic per bench safety rules.

---

## Key Wins Tonight

### 1. Pi 3B Rig + Pico Fake-Lark Fully Operational (`1245c3f`, `16378dc`, `830ee1f`)
- **Pi 3B Rig (`larkbridge-rig`, `192.168.0.251`)**: Separate Trixie 64-bit SD card (product SD untouched). `rig-selftest` **13/13 PASS**, `fake-car status` `ok: true`, PipeWire default sink pinned to `larkbridge_fake_car`, non-root OpenOCD SWD verified at **1000 kHz** after peeling ribbon wires apart to eliminate `SWDIO → SWCLK` capacitive crosstalk (documented in [rig-bringup-2026-11-25.md](rig-bringup-2026-11-25.md) and [hil-rig.md](../docs/hil-rig.md)).
- **Pico Fake-Lark (`rig/esp/fake-lark/`)**: RP2040 TinyUSB UAC 1.0 Hollyland Lark A1 (`3547:0407`) emulator + UART control (`PING`, `STATUS`, `REPLUG`, `PLAY`, `SKEW`, `PROFILE`). Live streaming verified on S31: `tap lark_in` RMS **`2317`** (exact `-20 dBFS` 1 kHz stereo 48 kHz S16), `read_err=0`, `xfer_err=0`.
- **Negative USB Descriptor Profiles (5/5 PASS)**: S31 cleanly rejected all 5 invalid profiles (`wrong_vid`, `wrong_rate`, `mono_only`, `bit24_only`, `uac2_bad`) and cleanly restored `lark_a1` (`tap_rms=2317`).
- **Pico ±500 ppm Rate Skew (PASS)**: `uac.started=true`, `read_err=0`, `xfer_err=0` across `+500 ppm`, `-500 ppm`, and `0 ppm`.

### 2. D5 ESP-SR AEC-FD Watchdog Starvation — FIXED & VERIFIED (`aa745ab743fa-test-aecfd`)
- **Root cause**: `aec_task` (priority 10) ran PSRAM-resident FD AEC continuously without yielding to `IDLE1`. An initial `vTaskDelay(pdMS_TO_TICKS(1U))` attempt evaluated to `vTaskDelay(0)` because S31 runs at `CONFIG_FREERTOS_HZ=100` (10 ms/tick).
- **Fix (`aa745ab`)**: Yield 1 raw tick (`vTaskDelay(1)`) every 8 processed frames (~256 ms) and on resync paths in `firmware/s31/main/aec_gate.c`.
- **Hardware result (3 × 60 s Pixel self-managed calls with Pico `PLAY speech -20` + Pixel voice stimulus, `out_link=true`)**:
  - **PASS**: `calls_completed=3/3`, **`wdt_hits=0`**, **`resync=0`**, **`starve=0`**, **`output_drop=0`**
  - **3,398+ AEC frames processed**, **`max_us=7564` (7.56 ms / 32 ms frame = 23.6% CPU)**, `stack_min=7256 B`, PSRAM heap rock-steady (`16.66 MB` free, `min_heap=16.66 MB`).
  - Standard AEC-off `s31-test.bin` (`aa745ab743fa-test`) was cleanly restored to `COM3` afterward.

### 3. 32UE Intermittent 120 ms `READY_AUDIO` Mute — FIXED & MEASURED HONESTLY (`aa745ab743fa-test`)
- **Fix (`aa745ab`)**: Pinned `i2s_rx` task to Core 1 (isolated from Bluedroid/BT controller on Core 0), kept `I2S_CLOCK_ACTIVE_MS` at the **honest `100 ms` threshold** (did NOT mask it), and added `i2s_gap_max_ms` telemetry (`output_audio.c` / `output_telemetry.c` / `overnight_soak.py`).
- **Hardware result (`soak_6h` segments 3 & 4 — 40 min continuous streaming on `aa745ab743fa-test`)**:
  - Both 20-min segments **H5 PASS**: `fifo_min=3339..3375`, `fifo_max=4742..4782`, `max_abs_ppm=128..156`, `under=0`, `over=0`, `i2s_err=0`, `hb_timeouts=0`, **`output_mutes=0`**, and **`i2s_gap_max_ms=10 ms`** (1 single 480-frame DMA block — zero >10 ms jitter!).

### 4. Silent Fake-Car Gates Passed Tonight
| Gate | Image | Verdict | Evidence / Key Metrics |
|---|---|---|---|
| **H7** (Fake-car T3 media) | `a0d75996029d-test` | **PASS** | Live Pixel 7a A2DP audio recorded on Pi PipeWire (`rms=67.0`, `peak=100`, `0.0%` clip at safe vol `4/25`) |
| **H12** (Fake-car T3 AVRCP) | `a0d75996029d-test` | **PASS (T3)** | 4/4 buttons (`play`, `pause`, `next`, `prev`) delivered Pi Fake Car → 32UE → S31 → Pixel `MediaSession` |
| **H9** (10-min Pixel call, AEC-off) | `a0d75996029d-test` | **PASS** | 20/20 samples clean, `no_reset=true`, `hfp_seen=true`, `returned_clean=true`, `hb_timeout_delta=0`, mid-call fake-car WAV `rms=12658.0`, `0.0%` clip |
| **H11** (10-call audio-focus matrix) | `a0d75996029d-test` | **PASS (10/10)** | All 10 cycles transitioned `A2DP(1) → HFP(2, sco=true) → A2DP(1, sco=false)`, `no_reset=true` |
| **Negative USB Profiles** | `aa745ab743fa-test` | **PASS (5/5)** | `wrong_vid`, `wrong_rate`, `mono_only`, `bit24_only`, `uac2_bad` all rejected; `lark_a1` restored (`rms=2317`) |
| **D5 AEC-FD WDT trial** | `aa745ab743fa-test-aecfd` | **PASS** | 3 × 60 s calls, 3,398 frames, `wdt_hits=0`, `starve=0`, `resync=0`, `max_us=7564` |
| **H5 / Mute Fix (40 min)** | `aa745ab743fa-test` | **PASS (2/2 segs)** | 2 × 20-min segments: `under=0`, `over=0`, `mutes=0` (at 100 ms), **`i2s_gap_max_ms=10 ms`** |

---

## Failures & Open Bugs Found (Actionable Next Steps)

### Bug 1 (CRITICAL — Caught by P7): S31 `uac_lark.c` Panic on USB Disconnect Race
- **Evidence**: `../lark_bridge-evidence/20261125-overnight-v2/final_gates/p7_h6/bench.jsonl`
- **Trace**:
  ```
  E (105777) s31_uac: LARK capture read failed: ESP_ERR_INVALID_STATE
  W (106837) s31_uac: retrying LARK UAC capture (1/5)
  E (106837) USB HOST: usbh_devs_open error: ESP_ERR_NOT_FOUND
  E (106837) uac-host: uac_host_device_open(2124): Unable to open USB device
  Guru Meditation Error: Core  0 panic'ed (Load access fault). Exception was unhandled.
  ```
- **Root cause**: In `firmware/s31/main/uac_lark.c:288-294`, when a USB unplug races with `uac_host_device_read()`, `read()` returns `ESP_ERR_INVALID_STATE` before `UAC_HOST_DRIVER_EVENT_DISCONNECTED` arrives. `uac_lark.c` calls `close_lark(handle)` **and `schedule_reopen()`**, which 1 s later calls `uac_host_device_open()` on the stale/gone USB address while re-enumeration is in progress — triggering a Load access fault inside `espressif/usb_host_uac 1.5.0`.
- **Recommended fix (for review before flashing)**: On `ESP_ERR_INVALID_STATE` / `ESP_ERR_NOT_FOUND` in `uac_lark.c`, call `close_lark(handle)` and do **NOT** call `schedule_reopen()` — wait for the next `UAC_HOST_DRIVER_EVENT_RX_CONNECTED`.

### Bug 2 (H6 / P9 Real Lark A1 Physical Replug): `ENUM: CHECK_SHORT_DEV_DESC FAILED`
- **Evidence**: `../lark_bridge-evidence/20261125-h6-p9/s31_console.log`
- **Status**: Added `CONFIG_USB_HOST_DEBOUNCE_DELAY_MS=500`, `RESET_HOLD_MS=50`, `RESET_RECOVERY_MS=100`, `SET_ADDR_RECOVERY_MS=30` to `firmware/s31/sdkconfig.defaults` (`a0d7599`). Needs a 4-minute P9 real-Lark re-test with Brian after Bug 1 is fixed.

### Minor Harness / Rig Notes
1. **`cold_boot.py`**: Hardcodes MP43247 MAC (`c9:5c:fd:6e:28:46`), so cycle 1 rejected `b8:27:eb:43:8d:51` (`LarkRig Fake Car`). Parameterize `--expected-peer` before running overnight cold-boot soaks against the fake car.
2. **`fake-car` reconnect after 32UE reboot**: `fake-car pair-accept` leaves `hci0` `Connectable: no`; run `sudo btmgmt --index 0 connectable on` on `larkbridge-rig` so 32UE auto-reconnects after reboots.
3. **H14 (`h14_adapter_cycles.py`)**: Did not run its 10 cycles because S31 had just been flashed after D5 and Pixel profiles hadn't finished reconnecting within the 45 s pre-check; ready to re-run once Bug 1 is fixed.

---

## Current Bench State (Safe & Idle)
- **Shelly plug (`192.168.33.1`)**: ON (`output: true`, `5.5 W`).
- **Task Scheduler (`LarkBridgeOvernightWatchdog`)**: Deleted cleanly.
- **S31 (`COM3`)**: Standard AEC-off `aa745ab743fa-test` (Pixel 7a bond intact).
- **32UE (`COM4`)**: `aa745ab743fa-test` (bonded to `LarkRig Fake Car` `b8:27:eb:43:8d:51`; MP43247 unpaired).
- **Local git commits (unpushed)**: `1245c3f`, `16378dc`, `dd5074b`, `830ee1f`, `a0d7599`, `aa745ab` on `claude/esp32-wireless-port`.
