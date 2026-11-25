# LarkBridge ESP32 port — agent handoff

**Written:** 2026-11-24 23:20 America/New_York (EST, UTC-5)  
**For:** a brand-new AI agent with no prior context  
**Repo HEAD at handoff:** `0654a429a421b11454a8162559ab5e0f244f7261` on branch `claude/esp32-wireless-port`  
**Working tree at handoff:** clean (all prior firmware/test work was already committed). This document is the only new commit. Nothing has been pushed, and nothing should be.

This is the current source of truth for bench state. Several older notes in the tree are stale (see [Stale documents](#stale-documents)); prefer this file and `firmware/qual/bench-report-2026-11-24-night.md` over `resume-here.md`, `firmware/qual/gates.toml`, `firmware/qual/human-queue.md`, and `firmware/qual/remaining-gates-2026-11-24.md`.

---

## 1. Project goal

Port **LarkBridge** from the Raspberry Pi product implementation in `pi/` to a dual-radio ESP32 bridge:

- **S31** (`firmware/s31/`): LARK USB capture, Pixel 7a Bluetooth (HFP/A2DP/AVRCP), and AEC.
- **DevKitC-32UE** (`firmware/output/`): car-facing A2DP source and AVRCP. One saved peer at a time (currently the bench speaker, not a car).
- The boards share **48 kHz stereo PCM over I2S** and control over **UART** (six-wire harness). See `firmware/docs/wiring.md`, `firmware/docs/protocol-v1.md`, `ESP32_PORT_PLAN.md` / `ESP32_WIRELESS_PORT_PLAN.md`, and `docs/architecture/decisions/ADR-0010-esp32-dual-radio-bridge.md`.

The Pi implementation remains the behavioral reference and the rollback while this port is qualified against gates H0–H17.

---

## 2. Where everything lives

| What | Path / identity |
|---|---|
| Hardware host | **BigBoy** (Windows + WSL). Work here, not on other machines. |
| Checkout (this branch) | `/mnt/c/Users/user1/Desktop/Projects/lark_bridge-esp32-wireless` (Windows: `C:\Users\user1\Desktop\Projects\lark_bridge-esp32-wireless`) |
| Branch | `claude/esp32-wireless-port` |
| Evidence (not a git repo) | `../lark_bridge-evidence/` — do not try to commit it. Large logs and binaries live here. |
| Active Claude HIL thread | `t3thread` id **`cf73a322`**, title "LarkBridge: HIL Power". Status at handoff: **ready** (idle) since 2026-11-24 22:51 ET. Last result is in `t3thread show cf73a322`. |
| Overnight soak timer | systemd user timer `lark-overnight-soak.timer`, armed for **2026-11-25 00:30 America/New_York**. Service is not running yet. |
| Sibling worktrees (do not mix them up) | `lark_bridge` (main), `lark_bridge-astra-review`, `lark_bridge-codex-review`, `lark_bridge-h2h5`, plus some `.claude/worktrees`. Commit on **this** checkout. |

Useful commands on BigBoy, from WSL:

```sh
t3thread show cf73a322
t3thread list
t3thread usage
```

`t3thread new` titles are capped at 32 characters. Instance examples used on this host: `--instance claudeAgent` / `--instance codex`.

---

## 3. Current bench state (as of 23:20 ET)

- **No test is running.** The Claude thread is idle. Do not interrupt a later soak or a live `t3thread` run.
- After the last H3 run, the **-50 dBFS keep-alive** was restored (32UE `a2dp_out` RMS 74). Pixel media volume was still **4/25**; AVRCP volume also read 4.
- **Images on the boards now (under test for the overnight soak):**
  - S31 (COM3): **`6dbdb64`** — UAC fix plus Astra cases 4 and 5.
  - 32UE (COM4): **`36202f1`** — Pixel harness media session that logs AVRCP keys, plus the Astra 4/5 work.
- **`soak-good` restore set** (used if the soak hits a reset loop, exit 4): S31 **`09f0f2a`** and 32UE **`42be26f`**, in `../lark_bridge-evidence/soak-good/{s31,output}.bin`. That older pair also passed D4 5/5, H8, and H11 3/3. Do not confuse the restore set with what is currently flashed.
- A **transient systemd user timer** will start the overnight media soak at 00:30 ET (about 11 Shelly cold boots across 7.5 hours). Each cold boot cuts power to the **whole USB hub**. If someone is still wiring the Pi rig or Pico at that time, delay the timer first (see [Overnight soak](#8-overnight-soak)).

---

## 4. Hardware

| Item | Details |
|---|---|
| S31 | ESP32-S31 Function CoreBoard on **COM3**, USB Serial/JTAG Type-C. LARK USB-C receiver on the S31 USB-A host port. Chip identity from boot logs: esp32s31 rev v0.0 bootloader. |
| Output | ESP32-DevKitC-**32UE** on **COM4**, Micro-B. Module is WROOM-32UE-class (ESP32-D0WD-V3 rev v3.1). U.FL pigtail through an SMA bulkhead to **one** external antenna; seated, no visible damage in Brian's photo. |
| Pixel 7a | USB serial **`3C081JEHN14930`**, ADB over USB. Bonded to the S31 over A2DP and HFP as `LarkBridge`. Media volume **4/25**. Voice-call volume is **5/15** and can only be lowered during a call. Harness app `dev.larkbridge.harness` is installed on this phone only. |
| Pixel 9 Pro | Not part of this bench. Do not use it as the speaker/phone under test. |
| Speaker | **MP43247**, address **`c9:5c:fd:6e:28:46`**, bonded to the 32UE. It runs at **maximum volume**. After a source-side disconnect it can go to standby and need a **physical power-button press** before it will accept pages. |
| Shelly Gen4 plug | S4PL-00116US, plug AP/IP **`192.168.33.1`**, no auth. Powers the USB hub. Helper: `firmware/rig/shelly_power.ps1`. |
| USB hub | On the Shelly. Typically carries the S31, 32UE, a **BT600** adapter, and the Pixel 7a USB. After a cut, COM ports re-enumerate in about 15 s. Hub draw is about 7.5 W. |
| Six-wire harness | Fitted (five jumpers visible in the H0 photo). UART and I2S are in use. There is **no logic analyzer**; Brian waived it on 2026-11-24. |
| Pi fake-car rig | **Not built yet.** Needs its **own rig SD card**. Brian had not flashed a Pi or connected a Pico as of 2026-11-24 22:27 ET. |
| Pico fake Lark | **Not built yet.** Wiring is in `firmware/docs/hil-rig.md` §3.4. Never feed the Pico from the Pi's 5 V or 3.3 V; series 1 kΩ on every signal line. |
| Product Pi | Separate from the rig. **Never modify the product SD card.** Runtime-only changes are OK and are lost on reboot (overlayroot). A runtime stop of the product Pi's BT500 paging (`hci0`, A0:AD:9F:73:6C:24) was applied earlier so it would stop poking the Pixel; a Pi reboot undoes that. |

### Shelly / power quirk (important)

`firmware/rig/cold_boot.py` uses `shelly_power.ps1 cycle` for a **1 s** cut; the plug turns itself back on via `toggle_after`.

**The S31 does not actually lose power through a hub cut.** COM3 disappears (USB data drops) but the board stays up, so it is powered from somewhere other than the hub. The script therefore pulses S31 reset over USB-Serial-JTAG as power returns (`--s31-reset`). That is a near-simultaneous **warm** reset of the S31 and a real **brownout** cold boot of the 32UE.

A requested 2 s cut is shorter in practice: the 32UE app has been seen starting about 0.9 s before the requested end of the cut.

**Hard rule:** every Shelly power cut must be momentary, always with `toggle_after` of **1–2 s**. Never leave the plug off. Example:

```powershell
Invoke-WebRequest "http://192.168.33.1/rpc/Switch.Set?id=0&on=false&toggle_after=1"
```

`initial_state` on the plug is already set to `"on"`.

---

## 5. How to work on BigBoy

- The Windows shell is **PowerShell**. Run Linux scripts by writing them with Unix newlines, then invoking bash:

  ```powershell
  [IO.File]::WriteAllText("C:\Users\user1\grok-tmp\x.sh", $s.Replace("`r`n","`n"))
  wsl -e bash -l /mnt/c/Users/user1/grok-tmp/x.sh
  ```

- **Avoid pipes inside** `wsl -e bash -lc "..."`. PowerShell will parse them and break the command (grep has failed this way).
- Git commits from an agent shell must set identity:

  ```sh
  git -c user.name='Grok Bot' -c user.email='grokbot@localhost' commit ...
  ```

- **Commit as you go. NEVER push.**
- Windows `adb`: `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe` (WSL: `/mnt/c/Users/user1/AppData/Local/Android/Sdk/platform-tools/adb.exe`).
- Windows Python: `py -3`. Use it for flashing, `hil.py`, and the soak (the soak wrapper already calls `py.exe`).
- Prefer a written WSL script over a long one-liner.

---

## 6. Build, flash, and test

Authoritative build/flash notes: `firmware/docs/flashing.md` and `firmware/docs/arrival-runbook.md`.

Pinned toolchains (stop if the commit does not match):

| Target | ESP-IDF | Required commit |
|---|---|---|
| output (ESP32 / 32UE) | v6.0.2 | `7101770dc6db2667b3c477cc31365dd1acd6db4e` |
| S31 | v6.1-rc1 (prerelease; do not float) | `44f0c59f7c81a72a5868a52d5f6dfbbf88829704` |

From the repo root:

```powershell
py -3 firmware/tools/build_firmware.py host
py -3 -m unittest discover -s firmware/tools/tests -p "test_*.py"
py -3 firmware/tools/build_firmware.py output
py -3 firmware/tools/build_firmware.py s31
py -3 firmware/tools/arrival.py bundle --target output
py -3 firmware/tools/arrival.py bundle --target s31
py -3 firmware/tools/arrival.py ports
```

**Flash app-only at `0x10000` and verify.** Do not erase bonds. Do not do a full flash unless you are deliberately changing the bootloader or partition table (the 16 MB S31 layout is already in place; NVS was preserved; the Pixel bond survived).

**Never use eFuse, secure-boot, or flash-encryption commands.**

A scored hardware gate needs a clean manifest (`source_dirty: false`) unless you are explicitly debugging. Release images reject `tone` / `tap` / `fault` / `forget`; those exist only in `-test` images.

Typical gate runners (Windows Python, repo root, current ports):

```powershell
py -3 rig/esp/hil.py run H2 --s31-port COM3 --output-port COM4
py -3 rig/esp/hil.py run H3 --s31-port COM3 --output-port COM4 --allow-reset
py -3 rig/esp/hil.py run H5 --s31-port COM3 --output-port COM4
py -3 firmware/rig/astra45.py
```

Every “call” on this bench is the Pixel harness’s **self-managed** call. It dials no one. **No real phone calls.**

### Audio levels (hard rule)

The MP43247 is at maximum volume. Brian already found the test tones too loud.

- Rig test tones are capped at **-30 dBFS** (`49ff7b4`).
- H3 / routed fault-injection tones now use **-50 dBFS** (`ROUTED_TONE_DBFS` in `57bf8c3`). H3 only checks that audio is present (RMS > 0).
- Keep-alive is **`tone routed_i2s 1000 -50`** on the S31 (with `fault test_a2dp` as used by the soak).
- Pixel media volume stays **low, 4/25**.
- The overnight soak aborts a media segment and falls back to keep-alive if 32UE `a2dp_out` RMS exceeds **733** (that is -30 dBFS peak).

---

## 7. Gate status (authoritative, 2026-11-24 night)

Statuses below are from the night bench report, the last `cf73a322` result, and HEAD `0654a42`. They **do not all match** `firmware/qual/gates.toml`.

### PASS

| Gate / item | Notes and evidence |
|---|---|
| T0-host, T1-build, T2 smokes, stageB-exit | Pinned IDF builds and unattended T2 smokes. |
| BT-soak | 6 h 11 min Pixel↔S31 ACL, 124/124 polls, 0 drops, 0 S31 resets. |
| Harness (practical) | Fitted and used for H2–H5. Continuity meter record may still be thin. |
| pixel-harness | Installed and granted on the Pixel 7a. `36202f1` adds a media session that logs AVRCP keys. |
| **H0** | PASS from Brian's bench photo. `../lark_bridge-evidence/20261124-H0-photo/`. Antenna path confirmed; SKU/module markings were not legible, so chip identity is from H1 boot logs. |
| **H1** | Independent flash/boot on both boards. |
| **H2** | UART protocol PASS (`20261124T165735Z-H2` under `../lark_bridge-evidence/h2-h5-2026-11-24/`). `gates.toml` still says PENDING — that is stale. |
| **H4** | PASS, scored from PCM taps and FIFO telemetry. Logic analyzer waived. Wire clock, slot width, and edge timing were **not** measured. |
| **H5** 30-minute soak | PASS. FIFO drift ≤ 333 ppm, inside the 1,000 ppm sample-rate limit (and inside the 500 ppm H5 limit in `gates.toml`). Run with the MP43247 as sink, not a fake car. Commit `06acac1` recorded the 30-minute pass. Later H5 smokes after 32UE reboots and cold boots also passed with 0 heartbeat timeouts. |
| **D4** | 5/5 PASS on `09f0f2a`/`42be26f` and again on `6dbdb64`/`36202f1`. Root cause was the Pixel’s 5 s link supervision timeout; a boot-hold fix is in the images. |
| **H8** | Media relay PASS on both image pairs. |
| **H11** | 3/3 PASS on `09f0f2a`/`42be26f`. Skipped on the later fixed images because call audio would go to the speaker at call volume 5/15. The level check is now relative to pre-call media RMS (`TAP_ACTIVE_RMS` measures the LARK mic, so Pixel volume does not affect it). |
| **Astra 3 / UAC log storm** | Fixed in `09f0f2a`. 5/5 cold boots PASS, `uac.lost` 0. |
| **Astra 4** (stale output link) | Fixed in `6dbdb64`, verified: 32UE notices a silently dead speaker link within 3 s and reconnected in 24 s with no reboot. `../lark_bridge-evidence/20261124-astra45/fix-case4`. |
| **Astra 5** (stranded AVRCP release) | Fixed in `6dbdb64`, verified: a new key is never blocked or swallowed by an old unfinished release. Pixel logged exactly one press and one release per tap, in order. `.../fix-case5`. |
| **Shelly 2 s brownouts** | 5/5 PASS, 0 heartbeat timeouts (22:30–22:46 ET on the fixed images). This is **done**, not pending. Stream 10.3–10.7 s from requested power-on; Pixel 12.3–12.7 s. |

Heartbeat timeouts after a 32UE reboot **no longer reproduce** on the merged images (`42be26f` and later). Max liveness gap seen was 510–559 ms.

### PARTIAL

| Gate | Notes |
|---|---|
| **H3** independent reset | PARTIAL, bundle `docs/experiments/results/E21/20261125T034804Z-H3`, images `6dbdb64`/`36202f1`. With the speaker live, both UART link-mute tests muted audio and recovered; pairings were kept. The two reboot cases recovered **faster than the 1.4 s heartbeat timeout**, so no timeout-driven mute was observed (the session reset still forces silence). **Brian's call:** score the reboot mute from the session change, or accept the link-mute evidence. |
| **H6** LARK UAC1 | PARTIAL / read-only earlier run: no transmitter, **0/100 reconnects**. The detach-under-load case still needs a **physical LARK USB replug** (human-queue P9: 10 receiver unplug/replug cycles, then 5 transmitter off/on). 100/100 really wants the Pico fake Lark (P7), which is not built. |

### DEFERRED

| Item | Notes |
|---|---|
| **D5 AEC** | Brian deferred this to tomorrow (2026-11-25). Engine is **off by default** (`CONFIG_LARKBRIDGE_S31_AEC_ENGINE` is n). Soak/production images have AEC off. Three bugs were found on the `sdkconfig.aec-fd` profile; see [Known issues](#10-known-issues). Notes: `firmware/qual/aec-d5-research-2026-11-24.md` and the night report. Do not re-enable AEC on the overnight soak images. |

### PENDING / not done

| Item | Notes |
|---|---|
| **Overnight media soak** | Scheduled 00:30 ET; see below. Media only, no calls. |
| **H14** fault injection | Not completed. Earlier attempt stopped at “baseline profiles are down” while the speaker was offline. Baseline needs **active SCO and active A2DP**. Re-run with a harness call and media once the speaker is connected. Be careful with call volume (5/15). Evidence so far: `../lark_bridge-evidence/20261124-h14-night/`, `20261124-h14-boot-hold/`, `20261124-h14-bt-smoke/`. |
| **H7** fake-car / real-car / AVRCP coverage | Speaker interop works; fake-car needs the Pi rig SD (P3). |
| **H9, H10, H12, H13, H15, H16, H17** | Still open. H10 is blocked on a working AEC (D5). H12 needs a full AVRCP button sweep. H13 needs antenna-placement work with Brian. H17 is vehicle power. |
| **analyzer** gate | No logic analyzer on the bench. |
| **fake-car / Pi rig / Pico** | Not built. |
| **Media dropout characterization** | See known issues; the soak will count `output.mutes`. |

---

## 8. Overnight soak

The script is already armed. From the repo root, the exact command (also baked into the systemd unit) is:

```sh
bash firmware/rig/overnight_soak.sh ../lark_bridge-evidence/20261125-overnight ../lark_bridge-evidence/soak-good --hours 7.5 --segment-min 20 --boot-every 2
```

What it does:

- About 21 scored 20-minute **media** segments (Pixel harness 1 kHz WAV → S31 → I2S → 32UE → MP43247), plus the H5 reducer and heartbeat / UAC-lost / liveness-gap counters.
- A Shelly cold boot after every second segment (~11 cycles, ~1.5 per hour). **Leave the bench untouched while this runs.**
- Media only, no calls. Pixel media 4/25, about **-51 dBFS** peak at the speaker. Falls back to the -50 dBFS keep-alive if media is too loud or fails to start twice.
- Counts 32UE `READY_AUDIO` mutes per segment (`output.mutes`) to measure the open 120 ms dropout.
- Images under test: S31 `6dbdb64`, 32UE `36202f1`.
- On a **reset loop** (exit 4) it restores `soak-good` app-only: S31 `09f0f2a`, 32UE `42be26f`. The `images.sha256` written into the **output** directory is the restore set, not the images under test.
- Exit codes: 0 all pass, 2 some segment failed, 3 unsafe bench state (stopped), 4 reset loop (stopped and restore flashed).

Watch / control:

```sh
# after 00:30, or once the service is active
tail -f ../lark_bridge-evidence/20261125-overnight/run.log

# start immediately (only if you intend to skip the 00:30 delay)
systemctl --user start lark-overnight-soak.service

# cancel the scheduled start
systemctl --user stop lark-overnight-soak.timer

# stop a soak that is already running
systemctl --user stop lark-overnight-soak.service
```

The timer is a **transient** user unit (`/run/user/1000/systemd/transient/`). It will not survive a BigBoy reboot. A WSL systemd test already confirmed that `py.exe` and PowerShell interop work from this unit.

If you need to move the start time: stop the timer, then re-run `systemd-run` with a new `--on-calendar`. The plug always comes back on because every cycle uses `toggle_after`.

Morning scoring: `../lark_bridge-evidence/20261125-overnight/summary.json` and `segments.jsonl`.

---

## 9. Key commits (this branch)

Newest first, all 2026-11-24, America/New_York times:

| Commit | Time | What |
|---|---|---|
| `0654a42` | 22:51 | Night report: overnight soak scheduled at 00:30 as a systemd user timer. **HEAD at handoff.** |
| `57bf8c3` | 22:49 | Route H3 tones at -50 dBFS; record the 22:48 speaker complaint cause. |
| `a5f0025` | 22:48 | H3 with live speaker (PARTIAL); night short-run results. |
| `9fa27dd` | 22:47 | Count output READY_AUDIO mutes per soak segment. |
| `e59611e` | 22:31 | Record H0 from Brian's photo. |
| `b9f3258` | 22:26 | Record verified Astra 4/5 fixes; runner observes Pixel media keys. |
| `36202f1` | 22:19 | Pixel harness media session (image on the **32UE now**). |
| `6dbdb64` | 22:17 | Fix Astra 4 and 5 (image on the **S31 now**; 32UE also carries this fix plus the harness commit). |
| `c42bd59` | 21:51 | Night report: speaker-offline blocker, H14 baseline, overnight plan (later superseded in part). |
| `df8ffd1` / `39eb5e6` | 21:36–21:39 | Astra 4/5 test hooks and hardware runner; overnight soak script. |
| `49ff7b4` | 21:31 | Cap rig test tones at -30 dBFS. |
| `860155a` | 21:22 | Keep RTC fast memory out of the heap on the FD AEC profile. |
| `e43ac58` / `dd78bab` | 21:02–21:15 | AEC starvation timer-wrap fix and ring-fill logging. |
| `ae67344` | 21:02 | Score H4 from PCM taps (analyzer waived). |
| `09f0f2a` | 20:54 | UAC RX coalesce; **soak-good S31 restore image**. |
| `f339c31` / `3517676` | 20:31–20:47 | Shelly power helper and cold-boot rig test. |
| `42be26f` | 20:05 | Merge Astra `b82a6c5` and Codex `68d7ea6`; **soak-good 32UE restore image**. |
| `06acac1` | 19:19 | Record the 30-minute H5 soak pass and the (since resolved) reboot-order timeout pattern. |
| `7d22636` | 19:17 | Gate I2S readiness on complete writes (likely why evening heartbeat timeouts stopped reproducing). |

Earlier the same day: 16 MB S31 partition table with NVS preserved, D4 boot-hold, AEC engine default-off, Pixel path work, MP43247 pairing.

---

## 10. Known issues

1. **Intermittent 120 ms media mute.** In soak dry-run 2 (4 × 2 min of Pixel media), 3/4 segments passed. Segment 4 failed on one FIFO flush: the 32UE logged `readiness=0x27 muted` then READY 120 ms later. `READY_AUDIO` dropped because the 32UE I2S reader went more than 100 ms (`I2S_CLOCK_ACTIVE_MS`) without a read. The S31 side was clean (no I2S write errors, steady route). Cause not found. The overnight soak counts these as `output.mutes`.
2. **S31 power is not from the Shelly-powered hub.** USB data drops during a cut; the chip stays up. The “simultaneous cold boot” is a 32UE brownout plus an S31 warm reset. Root of the S31's alternate power is unknown.
3. **Requested Shelly off-time is longer than the real outage.** See hardware notes above.
4. **MP43247 standby.** After a source-side disconnect (e.g. Astra case 4 before the fix, or a long gap), the speaker may refuse pages (`BTA_AV_OPEN_EVT FAILED`) until someone presses its power button. This blocked the soak and H3 until Brian revived it earlier tonight.
5. **D5 AEC, open bug:** on `860155a` the task watchdog fires on `s31_aec` (CPU 1) every 5 s during a call. The worker never yields while both rings hold frames, so IDLE1 starves. Frames themselves processed (0 starvations, max 7.7 ms per 32 ms frame). Next step is `vTaskDelay(1)` or a blocking wait per frame, then publishing tests. Two earlier bugs are already fixed: unsigned timer wrap (`e43ac58` / `dd78bab`) and a Load access fault from RTC fast memory being in the heap (`860155a`). Linear-only without PSRAM still faulted earlier. Uplink publishing is still locked (digital silence), as intended until this is finished.
6. **H3 not closed** — waiting on Brian's scoring decision.
7. **`gates.toml` and several qual notes are stale** — do not treat PENDING there as current (H2, H5, H8, H11, H0, pixel-harness, and the 2 s brownouts are the main mismatches).
8. **Product Pi BT500** can page the Pixel every ~20 s if the earlier runtime stop is lost (reboot). That adds radio contention.
9. **SSH to the product Pi** (`ssh larkbridge`) was set up with a key in the overlay only; a Pi reboot removes it. **Never put the Pi password in this repo, in evidence notes, or in chat.**
10. **Commit-graph warning** (`unable to find all commit-graph files`) appears on this worktree; it is noisy but git still works.

The 22:48 “speaker got louder, crackled, audio cut in and out, then disconnected” report was **the H3 run**, not a volume bug. A -30 dBFS tone (since lowered to -50), deliberate link mutes, an S31 reboot, and a 32UE reboot caused it. No underruns or I2S errors. Evidence: night report plus bundle `docs/experiments/results/E21/20261125T034804Z-H3`.

---

## 11. Open questions (Brian)

- Close **H3** by scoring reboot mute from the new session, or accept the UART link-mute evidence?
- After the soak: is the 120 ms `READY_AUDIO` mute rare enough to live with, or does it need a 32UE I2S-readiness fix before more endurance work?
- **D5 tomorrow:** yield in `s31_aec`, then publishing tests? Also: update ESP-SR, report to Espressif, or choose another AEC if the P4-class SIMD / watchdog issues persist.
- Permanent decision on the **product Pi BT500** paging the Pixel (D2). The runtime stop is already applied until the Pi reboots.
- **D3 slow clock:** S31 uses the internal ~136 kHz RC. The BT-soak was clean; keep RC unless the overnight soak shows ACL drops.
- Why does the **S31 stay powered** when the hub is cut, and should that be rewired so a hub cut is a true S31 cold boot?

---

## 12. Stale documents

Use these as background, not as the latest verdict:

- `firmware/docs/resume-here.md` — dated 2026-11-22, still talks as if H0–H17 are all pending.
- `firmware/qual/gates.toml` — H0 and H4 are updated; H2, H3, H5, H8, H11, `harness`, and `pixel-harness` are not.
- `firmware/qual/human-queue.md` — P1 still says the harness is absent; P5 still says H0 is unrecorded; several “in progress” notes are finished.
- `firmware/qual/remaining-gates-2026-11-24.md` — snapshot from earlier in the day (H0/H3/H4/H5/H8/H11 listed open).
- The “Overnight plan” paragraph in the night report that says the boards run `soak-good` `09f0f2a`/`42be26f` is **superseded** by the later “scheduled for 00:30” section: the boards under test are `6dbdb64`/`36202f1`, and `soak-good` is the restore set only.

Updating `gates.toml` and the human queue to match this handoff is a reasonable follow-up commit, but was not done in this handoff pass.

---

## 13. Hard rules for the next agent

- **Commit as you go. NEVER push.**
- **Never** use eFuse, secure-boot, or flash-encryption commands.
- **Stop** on a reset loop or an unknown USB device. The soak will try to restore `soak-good` images on a reset loop; still stop and report.
- **Never modify the Pi product SD card.** Runtime-only changes are OK and will not survive reboot. The rig SD is a separate card, not yet built.
- Shelly power cuts must be **momentary only**, always with `toggle_after` of **1–2 s**.
- Test audio **≤ -30 dBFS**; keep-alive **-50 dBFS**; Pixel media volume **4/25**. Prefer -50 dBFS for any tone the speaker will actually play while a person is nearby.
- **No real phone calls.** Harness self-managed calls only.
- **No Discord posts.**
- **Never put the Pi password** in docs, scripts, evidence, commit messages, or chat.
- Do not restart, modify, or redeploy the live Boss bot (a separate project on BigBoy) without explicit approval.
- Flash **app-only** at 0x10000 unless you have a specific, recorded reason for a full flash. Do not erase bonds.
- Leave the bench alone while the overnight soak is running (hub power cuts).
- Keep the Pixel 7a (serial `3C081JEHN14930`) as the bench phone; do not confuse it with the Pixel 9 Pro.
- Message Brian for results, physical actions, or stuck jobs — not for routine progress.

---

## 14. Prioritized next steps

1. **Do not disturb the bench** before and during the 00:30 ET soak. If Brian is still wiring a Pi or Pico at that time, stop `lark-overnight-soak.timer` and reschedule. Otherwise let it start on its own.
2. **Morning: score the soak.** Read `../lark_bridge-evidence/20261125-overnight/summary.json`, `segments.jsonl`, and `run.log`. Check `output.mutes`, heartbeat timeouts, cold-boot verdicts, and whether `soak-good` images were restored. Commit a short morning report. Confirm which images are actually on the boards before further flashing.
3. **Close H3** once Brian decides how to score the reboot-mute case. Update `gates.toml`.
4. **H14** with a live speaker, a harness call, and media (watch call volume). Then H15 only after H14 is actually a pass.
5. **H6** when Brian is at the bench: P9 physical LARK replugs (and transmitter on/off). Do not ask anyone to do 100 manual reconnects; that is what the Pico is for.
6. **Integrate the Pi rig and Pico** when Brian finishes them (rig SD only; `hil-rig.md` §3.4 and `rig/esp/pi-image/`). Then H7 fake-car, scripted H6 100/100, and optional `--sink-hook` scoring on a soak.
7. **D5 AEC (tomorrow):** add a yield or blocking wait in `s31_aec`, re-run the FD/PSRAM profile, then publishing tests. Keep AEC off on the soak/default images until it is clean. H10 stays blocked until then.
8. **Refresh stale status files** (`gates.toml`, `human-queue.md`, maybe a short remaining-gates addendum) so the next person does not rediscover tonight's results the hard way.
9. Remaining qualification in roughly this order, as hardware allows: finish H7, H9 (self-managed call, no AEC required for a limited pass), H12, H13 (needs Brian), H16/H17 later. Do not invent a real cellular call.

---

## 15. Quick file map

| File | Why it matters |
|---|---|
| `firmware/qual/bench-report-2026-11-24-night.md` | Full night evidence narrative (read the last sections for the scheduled soak and image IDs). |
| `firmware/docs/flashing.md` | Build/flash/pairing-reset rules. |
| `firmware/docs/hil-rig.md` | Rig topology; Pico wiring is §3.4. |
| `firmware/docs/wiring.md` | Six-wire harness. |
| `firmware/docs/arrival-runbook.md` | Bench sequence and `arrival.py`. |
| `firmware/rig/overnight_soak.sh` / `overnight_soak.py` | Overnight soak. |
| `firmware/rig/shelly_power.ps1` / `cold_boot.py` | Hub power cycles. |
| `firmware/rig/astra45.py` | Astra 4/5 hardware runner. |
| `rig/esp/hil.py` | Gate runner / reducers. |
| `rig/esp/pixel-harness/` | Phone app; install only on the Pixel 7a. |
| `../lark_bridge-evidence/` | All raw run artifacts (not versioned). |
