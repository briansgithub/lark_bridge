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
- **4. Output Bluetooth event loss: reproduced (21:45).** Hooks are in
  `df8ffd1` (output `39eb5e6`), and the runner is `firmware/rig/astra45.py`.
  `fault bt_drop_disc` drops the next DISCONNECTED event, and
  `fault bt_disconnect` then drops the speaker link. For 60 s afterwards
  the output reported `connected: true, streaming: true` with `audio`
  false and FIFO 0. It never reconnected and raised no fault beyond the
  dropped event. A 32UE reboot then paged the saved peer every 5 s, but
  the MP43247 refused (`BTA_AV_OPEN_EVT FAILED`, timeouts). It had most
  likely gone to standby after the source-side disconnect. The speaker
  needs a power-button press. The dropped DISCOVERY_STOPPED hook
  (`bt_drop_dstop`) is built but not run: discovery only runs without a
  saved peer, and forgetting the peer would mean re-pairing by hand.
- **5. Stranded AVRCP release: reproduced (21:44).**
  `fault avrcp_relfail` fails the next 5 release submissions. Then:
  - five STOP taps each returned `ESP_FAIL` with the release still
    pending;
  - a PAUSE tap returned `ESP_ERR_INVALID_STATE`: a different key is
    blocked;
  - the next STOP tap returned `ESP_OK` but only completed the old
    release, so no new STOP was sent;
  - PAUSE then worked.

  Pixel logcat showed no AVRCP or media-key lines at default log levels.
  The phone-side key state still needs a harness MediaSession. Evidence:
  `20261124-astra45/run1`.

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

## Blocked at 21:55: speaker offline

Since the case 4 run, the MP43247 has refused every page from the 32UE.
It needs its power button pressed. Waiting on it:

- the overnight media soak;
- H3 with a live sink;
- the Shelly 2 s-off brownouts, which score the stream.

H14 adapter cycles stopped at "baseline profiles are down". Its baseline
needs active SCO audio and active A2DP audio (`session.hfp` and
`session.a2dp`), so run it with a harness call and media playing, once
the speaker is back.

## Overnight plan (launch about 00:30)

Boards run `soak-good`: S31 `09f0f2a` (AEC off, UAC fix) and 32UE
`42be26f`, both verified in today's runs. Pixel media is at 4/25, which
the level guard checks. The routed keep-alive stays at -50 dBFS, and
there are no calls.

```sh
bash firmware/rig/overnight_soak.sh ../lark_bridge-evidence/20261125-overnight \
    ../lark_bridge-evidence/soak-good --hours 7.5 --segment-min 20 --boot-every 2
```

- That gives about 21 scored 20-minute media segments (Pixel → S31 →
  I2S → 32UE → MP43247), with the H5 reducer and heartbeat, UAC-lost and
  liveness-gap counters per segment.
- A Shelly cold boot follows every second segment: 1 s off, S31 reset
  pulse, roughly 1.5 per hour and about 11 cycles in total, each scored.
- Exit codes: 0 pass, 2 a segment failed, 3 unsafe bench state (stops),
  4 reset loop (stops and restores the `soak-good` images).
- Pi fake car: `--sink-hook module:function` runs per segment to score
  the car sink alongside.
- Reflash the `soak-good` images first if the boards hold the hook
  images (`df8ffd1` / `39eb5e6`).

## Astra 4 and 5 fixed and verified (22:20–22:25)

Images: S31 `6dbdb64`, output `36202f1` (fixes in `6dbdb64`). Host suite:
9 CTest targets, with `bridge_tap_tests` new.

- **Case 5** (`bridge_tap`, with origin = output session and sequence).
  The run was relfail, then five STOP retries (`ESP_FAIL`, pending), a
  new PAUSE tap, a new STOP tap and a new PAUSE tap.
  - The three new taps all returned `ESP_OK`, with nothing left pending.
  - The new harness media session (`36202f1`) logged on the Pixel, in
    order: `STOP down` (the tap whose releases failed), `STOP up` (the
    stale release, sent before the new key), `PAUSE down/up`,
    `STOP down/up` and `PAUSE down/up`. That is exactly one press and one
    release per tap. Nothing was blocked or swallowed.
  - Evidence: `20261124-astra45/fix-case5`.
- **Case 4** (A2DP pull watchdog, 3 s).
  - 74.5 s: the DISCONNECTED event was dropped.
  - 77.3 s: "A2DP source stopped pulling audio; link presumed lost";
    connected and streaming went false.
  - 92.4 s: the disconnect deadline released the stale peer.
  - 98.6 s: the speaker reconnected and audio resumed (FIFO 4,005).
  - Recovery took 24 s from the drop, with no reboot. Before the fix the
    output stayed "connected" for as long as it was watched (60 s).
    Evidence: `20261124-astra45/fix-case4`.
- The H11 media level check is now relative: at least half the
  pre-call a2dp_out RMS, and 20 or more. `hil.TAP_ACTIVE_RMS` scores the
  LARK microphone tap, not the speaker path, so the Pixel volume does not
  affect it and it is unchanged.

## H0 from Brian's photo (22:30)

`../lark_bridge-evidence/20261124-H0-photo/` (photo, SHA-256, record).
The photo shows:

- the S31 CoreBoard, with the LARK receiver on USB-HS;
- the DevKitC with a WROOM-32UE-class module, whose U.FL pigtail runs
  through an SMA bulkhead to the external antenna. That is one antenna
  path, seated, with no visible damage;
- five jumpers between the boards.

Module markings are not legible in the photo. Chip identity comes from
the boot logs: ESP32-D0WD-V3 rev v3.1 on the output and an esp32s31 rev
v0.0 bootloader. `gates.toml` H0 is now PASS on that basis.

## H3 with the live speaker (22:48), images `6dbdb64` and `36202f1`

Bundle: `docs/experiments/results/E21/20261125T034804Z-H3`. Verdict
PARTIAL.

- Both UART-mute faults muted the output and recovered, with one new
  session each and bonds kept.
- Every phase had live audio before and after (the speaker was the sink).
  That was the evening's missing piece.
- The two remaining reasons are "reboot recovered before the heartbeat
  timeout". Each board's reboot started a new session sooner than the
  1.4 s timeout, so no timeout-driven mute was observed on reboot. The
  session reset forces silence on that path.
- To close H3, either score the reboot mute from the session change, or
  accept the uart_mute evidence. That is Brian's call.

## Other short runs on the fixed images (22:30–22:46)

- D4: 5/5 resets PASS. H8 media relay: PASS. H11 was skipped because the
  Pixel call volume is 5/15 and the call audio would go to the speaker.
- Shelly brownouts with a 2 s cut: 5/5 PASS with 0 heartbeat timeouts.
  Times from the requested power-on: stream 10.3–10.7 s, Pixel
  12.3–12.7 s. The 32UE app started about 0.9 s *before* the requested
  end of the cut, so the real outage is shorter than 2 s.
- Soak dry run 2 (4 × 2 min of Pixel media): 3 of 4 segments PASS, with
  0 heartbeat timeouts and `uac.lost` 0.
  - Segment 4 failed on one FIFO flush. The output logged
    `readiness=0x27 muted` and then READY 120 ms later: READY_AUDIO
    dropped because the 32UE's I2S reader went more than 100 ms
    (`I2S_CLOCK_ACTIVE_MS`) without a read.
  - The S31 side was clean: no I2S write errors and a steady route.
  - It is open. The overnight soak counts these per segment
    (`output.mutes`) to measure the rate.

## Speaker complaint at 22:48: caused by the H3 run

Brian heard the speaker get louder, then crackle, cut in and out,
disconnect and reconnect. All of it was H3 fault injection (bundle
`20261125T034804Z-H3`, `artifacts/bench.jsonl`). Times are from the
22:48:04 start:

- 2.5 s: `tone routed_i2s 1000 -30`, 20 dB above the -50 dBFS keep-alive.
- 5.5–8.7 s: the S31 UART was muted, so the output muted and recovered.
- 14.5–15.1 s: S31 reboot, a mute then READY.
- 23.6 s: a brief mute; 25.0 s: the output UART was muted.
- 34.1–38.6 s: 32UE reboot. The speaker disconnected and reconnected
  (`downstream A2DP peer connected`).

Each mute and unmute is a hard step on a 1 kHz tone, which is the
crackle. Nothing raised the volume: Pixel media is 4/25 and the Pixel
AVRCP volume manager reports 4. After the run: no underruns or I2S
errors, and the keep-alive was restored at -50 dBFS (a2dp_out RMS 74).

Change: routed rig tones (H3) now use -50 dBFS (`ROUTED_TONE_DBFS`).
H3 only checks that audio is present (RMS > 0).
