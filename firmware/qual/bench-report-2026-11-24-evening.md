H5 30-min soak PASS (0 heartbeat timeouts). Open: heartbeat timeouts appear after a 32UE reboot while the S31 keeps running, and clear on S31 reboot. D4 fixed 10/10. D5 FD+PSRAM AEC constructs. H8/H9/H11/H14 scoped smokes pass.

# Evening bench report, 2026-11-24 (16:45–19:35 ET)

Boards: S31 on COM3, DevKitC-32UE on COM4, MP43247 speaker, Pixel 7a over
USB adb. Every flash was app-only at 0x10000 and verified, with no bond
erase and no eFuse, secure-boot or flash-encryption changes. USB identities
matched before every write. Nothing was pushed. Every call was the harness's
self-managed call, which dials no one.

## Fixed and verified

- **H5 FIFO/drift** (`fe7a7f8`, `5e173bd`). Three causes:
  - An I2S clock-age race dropped READY_AUDIO and flushed the FIFO.
  - The loop targeted 4,096 frames while priming at 1,440, which pinned
    correction at 500 ppm.
  - Per-block resampling put a phase jump in every block.

  60 s smoke: FIFO 3,239–4,789 frames, 110 ppm, no mutes. End-to-end at
  -20 dBFS: 1 kHz at the A2DP output to the speaker.
- **D4 reconnect hang** (`4c743fb`). A reset that cuts a live link leaves
  the Pixel holding the ACL for its 5 s supervision timeout. The S31 paged
  at 2 s and wedged Bluedroid. A 5 s boot hold took resets from 5/10 to
  10/10 from a connected state (5/5 again on the merge). Recovery takes
  7.4 s.
- **D5 AEC crash** (`213dead`). ESP-SR's full-duplex init allocates from
  PSRAM only, and PSRAM was disabled. The engine is off by default, so
  calls no longer reboot the S31.
  - Codex FD+PSRAM trial: the constructor succeeds on 3/3 calls.
  - Its pipeline restarts on "input starved" every 0.15–1.2 s, which is
    still open.
  - Linear-only without PSRAM still faults.
- **Harness.** The installed APK `06d68960` merges Codex's uplink recorder
  with `1f7260e` (media outlives a call). Uplink at the Pixel is digital
  silence (RMS < 1) while publishing is locked.

## Scoped gates (no fake car, so recordings and AVRCP are not covered)

| Gate | Result |
|---|---|
| H7 | PASS, 4.8–5.8 s, every run |
| H8 relay | PASS: Pixel media to speaker, 1 kHz at each tap |
| H9 call | PASS: SCO at 16 kHz, downlink at each tap, no reboot |
| H11 | PASS 10/10: back to A2DP in 1.0 s after each call |
| H14 | PASS: profiles back 2.4 s after adapter on |
| H3 | PARTIAL: mute 1.40–1.41 s, recovery 4.5–6.0 s (rig fixed in `e5829d1`, `f37b566`) |
| H4 | PARTIAL as before: wire timing needs a logic analyzer |

## Open: S31 heartbeat timeouts

After the Codex merge (`1b8bb3a`), H5 60 s failed three times. The S31
timed out the 32UE heartbeat (gap > 1,400 ms) and forced silence, up to 18
times per run. There were no UART decode errors, and the heartbeat receive
rate was normal.

- One run with S31 `213dead` passed with zero timeouts.
- Earlier runs on `213dead`/`4c743fb` also showed zero.
- The S31 link and telemetry sources and config in `3bb3419` are identical
  to `213dead`, but that image still timed out.

So this looks intermittent, not tied to a commit. My console-lock theory
(`98d5296`, reverted for S31 in `3bb3419`) did not hold. Next: log
per-heartbeat TX time on the 32UE and RX time on the S31, then compare.

**H5 30-minute soak: PASS** (S31 `3bb34194c48c-test`, 32UE
`1b8bb3a08417-dirty-test`, -50 dBFS). FIFO 3,225–5,214 frames, max |ppm|
333, no underrun, overrun or I2S errors, and no interruption over 1,800 s.
The S31 logged **zero** heartbeat timeouts over 31 minutes (max gap
1,000 ms).

The H5 smoke right before it, on the same images, had 18 timeouts across
S31 uptime 41–136 s. H3 then rebooted the S31, which cleared them. Pattern
across all runs: timeouts follow a 32UE flash or reboot while the S31 keeps
running, and stop after an S31 reboot. Runs where the S31 booted last, or
where the 32UE was untouched, were clean. The gap meter records real
arrival times, so the 32UE sends late in that state. Evidence:
`../lark_bridge-evidence/20261124-final/`.

Details: `followup-2026-11-24.md`, `mp43247-2026-11-24.md`,
`aec-d5-research-2026-11-24.md`, `human-queue.md` (D4, D5).
