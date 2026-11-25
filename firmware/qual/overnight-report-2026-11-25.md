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

### Bug 1 (FIXED & VERIFIED 100/100 in `9ccbd0bd53c9`): S31 `uac_lark.c` Panic on USB Disconnect Race
- **Root cause**: During USB unplug teardown, `espressif/usb_host_uac 1.5.0` moves `iface->state` out of `ACTIVE` before posting `UAC_HOST_DRIVER_EVENT_DISCONNECTED`. A racing `uac_host_device_read()` returned `ESP_ERR_INVALID_STATE`; `uac_lark.c` closed `s_lark_handle` prematurely and armed `schedule_reopen()`, which 1 s later called `uac_host_device_open()` on the gone USB address (`ESP_ERR_NOT_FOUND`), hitting an upstream NULL dereference at `uac_host.c:2171` (`if (uac_iface->ringbuf)` with `uac_iface == NULL`).
- **Fix (`9ccbd0b` + `8a13cb9`)**: Treat `ESP_ERR_INVALID_STATE` on `uac_host_device_read()` as disconnect teardown; unconditionally clear `s_reopen_pending` on `DISCONNECTED`; guard `open_lark()` and `schedule_reopen()` with `usb_host_device_addr_list_fill()`; use hardware `pico-ctl reset` in `p7_replug_hook.py`.
- **Re-test result (`../lark_bridge-evidence/20261125-uac-fix-retest/p7_100/`)**:
  - **P7 Automated Pico 100/100 + 30-min Soak: PASS (`rc=0`)** — `100/100` disconnects/reopens, `capture_seconds=1800.1`, `141,547` RX packets, `0` read/xfer errors, `0` failure lines, `0` RX gap intervals, `0` reboots, `tap_rms=2317` (`s16le_stereo_48k`).
  - **H6 overall status**: `PARTIAL` (P7 100/100 PASS; real Lark A1 P9 10/10 physical replug still required with Brian to close H6).

### Bug 2 (H6 / P9 Real Lark A1 Physical Replug): `ENUM: CHECK_SHORT_DEV_DESC FAILED`
- **Evidence**: `../lark_bridge-evidence/20261125-h6-p9/s31_console.log`
- **Status**: USB host debounce/recovery margins (`a0d7599`) + Bug 1 fix (`9ccbd0b`) are both flashed on S31 (`9ccbd0bd53c9-test`). Ready for Brian's 4-minute P9 real-Lark re-test (`py -3 firmware/rig/p9_manual_h6.py`).

### Bug 3 (FIXED & VERIFIED 20/20 in `c73c95f`): H14 / D4 Pixel BT Adapter Cycle Bluedroid Reconnect Hang
- **Root cause**: `bt_gateway.c:reconnect_worker` fired HFP and A2DP reconnect requests simultaneously 620 ms after disconnect, started backoff before the 5.12 s BT page timeout finished, and reset `backoff_ms` back to `1000 ms` inside `cancel_stalled_request()` — keeping S31 paging at ~83% duty cycle until an outgoing `Create_Connection` collided with the Pixel's incoming ACL (`0x0b Conn Exists` / `L2CAP got conn_comp in bad state`), stranding an orphaned ACL handle in the BT controller.
- **Fix (`c73c95f`)**:
  - Added 6 s post-disconnect quiet hold (`S31_BT_RECONNECT_DISCONNECT_HOLD_MS = 6000`), serialized single-profile outbound connects (`s31_reconnect_choose_profile`), and scheduled exponential backoff (`6 s -> 12 s -> 24 s -> 30 s`) *after* page completion/cancellation without resetting on stalls.
  - Added GAP `ESP_BT_GAP_ACL_CONN_CMPL_STAT_EVT` (`0x0b`) tracking and a bounded self-healing Bluedroid-only recycle (`recycle_bt_stack()`, which triggers `HCI_Reset` on re-enable without calling `esp_bt_controller_disable()` — live silicon probing caught that `esp_bt_controller_disable()` leaves ESP32-S31 rev0 `libbtbb` in `OLC: resched failed:1`), plus `-test`-only `fault bt_recycle` and expanded `s31_reconnect_timer_tests` (9/9 host C tests PASS).
- **Hardware verification (`../lark_bridge-evidence/20261125-d4-fix-clean/` & `20261125-d4-fix/`, clean AEC-off S31 build of `c73c95f`)**:
  - **H14 Pixel BT Adapter Cycles**: **PASS 10/10** (`2.06–7.03 s` recovery; plus pre-commit **10/10** in `1.41–3.48 s` = **20/20** consecutive cycles, `0` stalls, `0` `0x0b` collisions, `0` reboots).
  - **Live `fault bt_recycle` Fallback**: **PASS** — full HFP+A2DP+AVRCP recovery in `11.08 s`, `boots: {s31: 0, output: 0}`, `heap.free: 75748 -> 75700 B`, UAC uninterrupted (`rx 825 -> 1752`, `0` errors).
  - **10-Cycle Pico UAC Hot-Plug Sanity (clean AEC-off S31 build)**: **PASS 10/10** (`disc=10`, `opens=11`, `read_err=0`, `xfer_err=0`, `lost=0`, `boots.s31=0`, `tap lark_in` RMS `2317`).

### Minor Harness / Rig Notes (ALL FIXED & VERIFIED in `c73c95f`)
1. **`cold_boot.py` & `overnight_soak.py`**: Parameterized `--expected-peer` (defaulting to MP43247 or `rig/inventory.toml` `rig_bt_addr`). **Verified live** in `../lark_bridge-evidence/20261125-d4-fix-clean/coldboot/cycle1/result.json`: Shelly 1 s cold boot + S31 reset recovered `uart_link` (`11.27 s`), `pixel` (`14.76 s`), `speaker_connected` (`b8:27:eb:43:8d:51` `LarkRig Fake Car`, `34.50 s`), `stream` (`35.85 s`), and **60.31 s H5 PASS** (`fifo=3229..4758`, `max_abs_ppm=88`, `under=0`, `over=0`, `i2s_err=0`, `timeouts_window=0`).
2. **`fake-car.sh`, `bt-pin.sh`, and `50-larkbridge-fake-car.pipewire.conf`**: Re-asserted `btmgmt connectable on` (using the BlueZ 5.82 `(sleep 0.5) | timeout 5` pipe-EOF workaround after catching a systemd stdin hang) in `fake-car pair-accept` and `bt-pin.sh`, and set `priority.session = 2000` on `larkbridge_fake_car`. Verified **47/47** Pi-image unit tests PASS, deployed to `larkbridge-rig`, `larkbridge-rig-bt-pin.service` restarted cleanly, and `rig-selftest` **13/13 PASS**.
3. **S31 `sdkconfig` hygiene**: Removed scratch `firmware/s31/build-aec-fd/` and regenerated clean AEC-off `sdkconfig` from `sdkconfig.defaults;sdkconfig.test` via `build_firmware.py --no-pull --image test s31` (verified `aec_publish: 0` in boot JSON; note the incremental `idf.py reconfigure build` after committing `c73c95f` kept cached `-DLARKBRIDGE_VERSION=9982370cb44f-dirty-test` in `CMakeCache.txt` while compiling exact `c73c95f` sources).

---

## Current Bench State (Safe & Idle)
- **Shelly plug (`192.168.33.1`)**: ON (`output: true`, `7.8 W`).
- **Task Scheduler (`LarkBridgeOvernightWatchdog`)**: Deleted cleanly.
- **S31 (`COM3`)**: Clean AEC-off test image from `c73c95f` (Pixel 7a bond intact, Pico UAC streaming cleanly).
- **32UE (`COM4`)**: `aa745ab743fa-test` (bonded to `LarkRig Fake Car` `b8:27:eb:43:8d:51`; MP43247 unpaired).
- **Local git commits (unpushed)**: `1245c3f`..`c73c95f` (+ qual doc update) on `claude/esp32-wireless-port`.
