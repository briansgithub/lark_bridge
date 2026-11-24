# Human queue

Steps that need Brian at the bench, or a decision only Brian can make. Agents append here and
carry on with other gates. Newest items go at the top of each section. Mark an item done with the
date and the evidence.

## Physical steps (bench)

| # | Added | For gate | Step | Notes |
|---|---|---|---|---|
| P11 | 2026-11-24 | H3, H5, H7, H8 | Wake **MP43247**, explicitly put it into pairing mode, and confirm the Bluetooth name it advertises. | BigBoy removed the bond after the 12:43 ET approval. Two 32UE inquiries for `MP43247` found no exact-name match, so no A2DP connection was attempted. See `evidence/20261124-mp43247-32ue/`. |
| ~~P10~~ | 2026-11-24 | H8 ingress | The Pixel reconnected to its existing **LarkBridge** bond during H1. | Foreground H8 ingress smoke passed with 180 streaming samples and no underruns. Downstream relay still needs P11. |
| P9 | 2026-11-24 | H6 | When prompted during the H6 reconnect smoke, unplug and replug the **Lark receiver's USB** at the S31 10 times, about 20 s apart. Then switch the Lark **transmitter** off and on 5 times, about 20 s apart. | The S31 counts each USB replug (`uac.disc`/`opens`), and the reducer scores each one. 100/100 still needs the Pico fake Lark (P7). |
| ~~P8~~ | 2026-11-24 | H6 acoustic source | Use BigBoy audio or the Pixel 7a speaker as the Lark source. | Brian reassigned MP43247 to the 32UE at 12:43 ET. P11 tracks its pairing blocker. |
| P1 (in progress 11:00 ET) | 2026-11-24 | H2, H3, H4, H5 | With both boards **unpowered**, fit the six-wire I2S + UART harness exactly as in `firmware/docs/wiring.md`. Run its unpowered continuity and adjacent-short checks, then power each board from its own USB only (no 5 V header link). Mark `harness` PASS in `firmware/qual/gates.toml` with the meter readings. | Tonight the harness is absent, so the S31 logs `route ACK timed out` every ~1.3 s. That is expected. |
| P2 | 2026-11-24 | H4, H5 | Clip the logic analyzer onto the harness (`hil-rig.md` §3.2) and plug it into the hub. Confirm `sigrok-cli --scan` sees it. | Needs P1. Not bought yet if it is not on the bench. |
| P3 | 2026-11-24 | H5, H7, H8 | Prepare the **rig** SD card (not the product card): flash Raspberry Pi OS Lite 64-bit, boot it, and run `rig/esp/pi-image/provision.sh` as its README says. Install an SSH key for the agent. | The product SD (overlayroot) stays untouched as the rollback. |
| ~~P4~~ | 2026-11-24 | Pi reads | Give the agent non-interactive SSH to the current Pi (`ssh-copy-id admin@larkbridge.local` from WSL), **or** run the read-only checklist in `rig/esp/pi-image/README.md` yourself and paste the output into an issue or note. | Tonight the Pi refused key auth (password only), so every Pi read was skipped. **Done 2026-11-24 ~10:40 ET** by Brian: key login works (`ssh larkbridge`). The key is only in the overlay, so a Pi reboot removes it. |
| P5 | 2026-11-24 | H0 | Do the H0 photos and the antenna/U.FL check, then record it with `arrival.py new-run --gate H0`. | There's no H0 record yet; H1 was recorded without one. |
| P6 | 2026-11-24 | H6 | Switch on the Lark A1 **transmitter** near the receiver for the next H6 run, so the 30 min capture carries real audio rather than an idle link. | Tonight's H6 was partial: receiver only, no transmitter. |
| P7 | 2026-11-24 | H6, Stage E | Buy or assemble the Pico fake-Lark cable (USB-A male to Micro-B) and the 1 kΩ control-line resistors (`hil-rig.md` §2, §3.4). H6's 100/100 reconnects need either that or a person replugging 100 times. | |

## Decisions

| # | Added | Question | Recommendation |
|---|---|---|---|
| ~~D1~~ | 2026-11-24 | **S31 flash size.** `firmware/s31/sdkconfig` (from defaults plus the target default) says `CONFIG_ESPTOOLPY_FLASHSIZE_2MB`, and the image header says 2 MB. The chip reports 16 MB: the H1 boot log has `Detected size(16384k) larger than the size in the binary image header(2048k)`. The app works, but 14 MB is unused, the single-app-large partition is capped by the 2 MB layout, and ESP-SR models and a coredump partition would need the space. | Add `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y` to `firmware/s31/sdkconfig.defaults`, plus a custom `partitions.csv` (factory 4 MB, coredump, and NVS kept at the same offset so bonds survive). This changes the bootloader header, so it needs a **full** flash (bootloader + partition table + app) with NVS preserved. Check the NVS offset before flashing. Not done tonight, because a full flash before 06:00 was off-limits and the partition layout is your call. **Done 2026-11-24 10:44 ET:** 16 MB with a custom table (`bae9b74`) that keeps NVS and phy_init in place. The full flash didn't write NVS, and the Pixel bond survived. See `followup-2026-11-24.md`. |
| D2 (runtime fix applied) | 2026-11-24 | **Second Bluetooth device paging the Pixel.** The Pixel's ACL history shows `LarkBridge BT500` (address `…:6C:24`, the Pi's USB-BT500 on the product SD) connecting and disconnecting every ~20 s (`reason:19`). It is not bonded to the Pixel. This adds radio contention to the S31 soak and to any H8/H9 timing. | For clean S31 soaks, power off the Pi's product bridge or unplug the BT500 while the ESP rig is under test. Then decide whether the product Pi should stop paging the Pixel when it is not the active bridge. **Applied for testing 2026-11-24 10:47 ET, runtime only:** `sudo systemctl stop bridge-btwatchdog@call.service` then `sudo bluetoothctl power off` on the BT500 (`hci0`, A0:AD:9F:73:6C:24). Overlayroot stayed on and the service stayed enabled. **Undo:** `ssh larkbridge 'sudo bluetoothctl power on && sudo systemctl start bridge-btwatchdog@call.service'`, or reboot the Pi. The permanent decision is still yours. |
| D3 | 2026-11-24 | **Slow clock.** The S31 uses `CONFIG_RTC_CLK_SRC_INT_RC` (the ~136 kHz RC). The IDF warning says Classic BT "may not maintain ACL" in light sleep. Tonight's soak result is in `firmware/qual/overnight-2026-11-24.md`. | If the soak stays clean, keep RC for now and add the result to the plan. If there are drops, fit or enable the 32 kHz crystal option or disable BT modem sleep. That is a firmware/hardware decision, so it's yours. |

| D4 | 2026-11-24 | **S31 reconnect can hang after boot.** After one of four resets today, the S31's first HFP/A2DP connect requests returned `ESP_OK` but produced no connection event at all. It then retried every ~32 s for 7 min without reconnecting, and a Pixel Bluetooth toggle didn't help either. The next reset reconnected in 2.6 s. Evidence: `lark_bridge-evidence/20261124-s31-16mb/reconnect-watch-1.log`, `pixel-bt-toggle-1.log`, `reset-reconnect-2.log`. **12:18 ET H14:** A Pixel adapter off/on also left all S31 profiles down. S31 logged ACL collision and L2CAP retries. Another S31 reset restored HFP, A2DP and AVRCP on the existing bond. See `lark_bridge-evidence/20261124-h14-bt-smoke/`. | Add a firmware guard (agent task). If no connection event arrives within ~10 s of a request, cancel it (`esp_a2d_sink_disconnect` / `esp_hf_client_disconnect`) before retrying, and log the stall. Then re-test with 20 scripted resets. |

## Hardware gates that wait on the above

- **H2–H5** need P1 (harness), and H4/H5 also need P2 (analyzer) and P3 (fake car). They can't run
  unattended until those are in place.
- **H7 (fake car part)** needs P3.
- ~~**Stage B exit (`stageB-exit`)**~~ Done 2026-11-24 06:05 ET: both T2 smokes passed unattended
  (`docs/experiments/results/E21/20261124T1101*`, `…T1104*`). Both boards now run the `209f25a` test image.
- **Pixel harness install** (`pixel-harness` gate, then H9) needs Brian's OK to install an app on the Pixel.

## Codex review pass

- **Docker access for T1-build.** Resolved on 2026-11-24. With Docker group access, both targets passed their pinned release and test builds. See [the review report](review-2026-11-24.md#verification).
