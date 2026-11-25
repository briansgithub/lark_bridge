Heartbeat timeouts no longer reproduce. Cold boots: 5/5 PASS after a UAC queue fix (`09f0f2a`). D4 5/5, H8 PASS, H11 3/3 on that image. H4 is scored from taps. D5 stopped at Brian's request (deferred); three AEC bugs found, notes below. Test levels capped at -30 dBFS. The overnight soak is ready.

# Night bench report, 2026-11-24 (20:30–00:30 ET)

Boards: S31 on COM3, DevKitC-32UE on COM4, MP43247 speaker and Pixel 7a
(`3C081JEHN14930`, USB adb). The Shelly Gen4 plug powers the USB hub.
Every flash was app-only at 0x10000 and verified. There was no bond erase
and no eFuse, secure-boot or flash-encryption change. Every call was the
harness's self-managed call, which dials no one. Nothing was pushed.
Evidence is in `../lark_bridge-evidence/20261124-codex-heartbeat/`,
`20261124-coldboot/` and `20261124-d5-night/`.

## Heartbeat timeouts after a 32UE reboot

Merged images `42be26f` (Astra `b82a6c5` and Codex `68d7ea6`) produced
no timeouts in any of these runs:

- four 60 s H5 smokes, each following an output-only `fault reboot` while
  the S31 kept running, with S31 uptime 75–380 s;
- the flash order itself, S31 first then 32UE, which is the evening's
  failing order;
- eleven cold boots, including one where only the 32UE lost power.

H5 passed every time with 0 timeouts on both boards. The maximum liveness
gap was 510–559 ms at the S31 receiver and at the 32UE sender, with write
calls of 1–2 ms. Neither ranked trial was applied, because there is
nothing to fix in this state. The likely cause of the evening's late 32UE
sends is the I2S readiness change in Astra `7d22636`, merged since.
Evidence: `codex-heartbeat/base1..4/summary.json`.

## Cold boots (Shelly plug)

`firmware/rig/cold_boot.py` uses `shelly_power.ps1 cycle` for a 1 s cut,
and the plug turns itself back on.

- **The S31 does not lose power through the cut.** Its USB data drops
  (COM3 disappears), but it stays up, so its power comes from somewhere
  other than the hub. `--s31-reset` therefore pulses its reset over
  USB-Serial-JTAG as power returns. That is a warm reset, close to
  simultaneous. The 32UE is a real cold boot (reset reason `brownout`).
- Times from power-on, 10 cycles on `09f0f2a` and `42be26f`: 32UE app
  0.03–1.4 s, S31 app 5.6–7.2 s, speaker 6.7–8.4 s, UART link 10.5–12.0 s,
  stream 10.9–12.6 s, Pixel HFP+A2DP 12.7–15.4 s.
- Heartbeat timeouts after a simultaneous boot: **0** in every H5 window.

**Found and fixed: UAC log storm (`09f0f2a`, Astra finding 3).** Cycle 4
of the first 5-cycle run (`42be26f`) filled the 16-entry UAC queue at HFP
connect. Each dropped RX_DONE then ran `ESP_EARLY_LOGE` inside the USB
driver task: 1,051 lines in 4 s. That starved the console and the owner
task, and the S31 stopped answering. The fix:

- RX_DONE keeps at most one queued notification, so lifecycle events
  always have space.
- The owner drains the ring per wake. An empty ring's `ESP_ERR_TIMEOUT`
  counts as drained; before, it closed the LARK.
- Drops are counted in `uac.lost` and logged by the owner. A lost
  lifecycle event raises `UAC_LOST`.

Result on `09f0f2a`: 5/5 cold boots PASS, `uac.lost` 0, and D4 5/5, H8
PASS and H11 3/3 again.

## Short regression

| Image | D4 (5 resets) | H8 media relay | H11 (3 calls) |
|---|---|---|---|
| `42be26f` | 5/5 | PASS | 3/3 |
| `09f0f2a` | 5/5 | PASS | 3/3 |

The -50 dBFS keep-alive was restored after each run.

## H4

H4 is scored from PCM taps and FIFO telemetry. Brian waived the logic
analyzer on 2026-11-24. Evidence:

- the receiver PCM smoke: 1,000 Hz on both channels at the nominal clock,
  with distinct L/R RMS (2,317 and 1,158) matching the S31 exactly;
- 5.77 M silence frames whose blocks carry the zero-block CRC;
- today's H8 taps: S31 `i2s_out` and 32UE `i2s_in` both at RMS 667,
  1 kHz;
- the 30-minute H5 FIFO drift at 333 ppm or less, inside the 1,000 ppm
  sample-rate limit.

Not measured: wire clock, slot width and edge timing.

## Astra cases 3–5

- **3. USB lifecycle loss:** fixed as described above. It was found on the
  bench, not by fault injection. The H6 detach-under-load case still needs
  a physical LARK replug.
- **4. Output Bluetooth event loss:** not run. It needs a test hook that
  delays the worker until its queue fills, then a power cycle of the
  sink. The MP43247 is not on the plug.
- **5. Stranded AVRCP release:** not run. It needs an S31 test hook that
  injects release-submit failures. The Pixel also has no media session
  that would show the key state (`dumpsys media_session`: none), so the
  harness APK needs a MediaSession that logs down/up.

## D5 AEC (FD + PSRAM): stopped 21:30, deferred to tomorrow

The engine stays off by default (`CONFIG_LARKBRIDGE_S31_AEC_ENGINE` is n).
The S31 is back on `09f0f2a` (AEC off). Each trial was three self-managed
harness calls on the `sdkconfig.aec-fd` profile, with evidence in
`20261124-d5-night/`.

1. **"Input starved" was a timer wrap (`e43ac58`).** Every restart logged
   `idle=4294967295 ms`. Engine start and the initial mic-delay discard
   stamp `last_progress_ms` after the loop sampled `current_ms`. The
   unsigned difference wrapped, so the pipeline restarted before its
   first frame. Ring fills at the restart (mic 384–2112 of 6144 bytes,
   reference 4800–5280 of 1024) were normal start-up fills. `dd78bab`
   also counts progress from the end of a frame and logs ring fills.
2. **Then a Load access fault (`860155a`).** `aec_process` →
   `esp_aec3_hps16fft_process` → `dl_esp32p4_sr_memcpy` loaded from
   0x2e003320 in RTC fast memory, which the heap included. ESP-SR's
   P4-class SIMD cannot reach that region. One HFP call reboot-looped the
   S31 three times (`fd-crash/console.log`), and the AEC-off image was
   restored. The FD profile now sets
   `# CONFIG_ESP_SYSTEM_ALLOW_RTC_FAST_MEM_AS_HEAP is not set`.
3. **Open: the task watchdog fires on `s31_aec` (CPU 1).** On `860155a`,
   all 3 calls ran with no fault and no reboot. Frames processed (542 and
   1,085 by calls 2 and 3), 0 starvations, max 7.7 ms per 32 ms frame,
   alignment 142–340 ppm. But `task_wdt: CPU 1: s31_aec` repeats every
   5 s during each call. The worker never yields while both rings hold
   frames, so IDLE1 starves. The next step is a `vTaskDelay(1)` or a
   blocking wait per frame, then publishing tests. The Pixel uplink was
   digital silence (publishing locked), as intended.

## Speaker level (21:26)

Brian reported the speaker as too loud. It runs at maximum volume.

- `rig/esp/h2h5.py` tones changed from -20 to -30 dBFS (`49ff7b4`). The
  evidence-dir smoke scripts changed from -20 to -30 dBFS (end-to-end
  window).
- Pixel media volume on the A2DP device set from 10 to 4/25 with
  volume-down key presses. `cmd media_session volume --set` was ignored.
- Voice-call volume is 5/15 and can only be lowered in a call. Call
  scripts press volume-down after routing to Bluetooth.
- The harness WAV is -6 dBFS peak. At media volume 8 it reached the
  speaker near -31 dBFS peak (a2dp_out RMS 657). At 4/25 it is lower, so
  RMS thresholds such as H11's 200 need re-measuring.
- The overnight soak stops media louder than -30 dBFS peak (a2dp_out
  RMS above 733) and falls back to the -50 dBFS keep-alive.
