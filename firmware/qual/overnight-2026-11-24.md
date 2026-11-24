# Overnight HIL run, 2026-11-24

The run was unattended from 23:40 ET on 11-23 to 06:05 ET on 11-24, on branch `claude/esp32-wireless-port` from `1882967`. **Nothing was pushed.**

Before 06:00, nothing touched a board. That means no flash, no reset and no DTR/RTS toggle. Nothing was tapped on the Pixel, and the Pi was never touched.

No Claude agent ran from about 00:15 to 04:40 ET because of a usage limit. A Codex review pass ran in that window and produced [`review-2026-11-24.md`](review-2026-11-24.md) and 19 commits. The soak job ran through the gap.

After 06:00, the only board actions were the two app-only flashes for Stage B exit. No eFuse, secure-boot, encryption, erase-flash or erase-bonds command was run.

## Results

| Gate | Result | Evidence |
| --- | --- | --- |
| T0-host | **PASS** | 815 Python tests and 161 subtests passed, along with five firmware CTest suites. The 24 rig HIL and soak tests also passed ([review](review-2026-11-24.md#verification)). |
| T1-build | **PASS** | Pinned Docker builds ran on a clean `209f25a`. The output board (IDF v6.0.2) built release and test images with 50% of its app partition free. The S31 (IDF v6.1-rc1) built both with 12% free. The test manifests verified. |
| BT-soak (Pixel to S31, 136 kHz RC slow clock) | **PASS** | The link was up in **124/124** polls over **6 h 11 min**: 22,250 s covered, with an 18 s gap between the two parts. There were 0 ACL events, 0 reconnects and 0 S31 reset or crash markers. The longest observed connection was 5 h 45 min. Evidence: [`E21/…BT-soak`](../../docs/experiments/results/E21/20261124T105554Z-BT-soak), with raw logs and `combined-summary.json` in `../lark_bridge-evidence/20261124-bt-soak/`. |
| T2-smoke-s31 | **PASS** | The app-only flash wrote 1,349,616 B at `0x10000` and its hash verified. The boot JSON matches the manifest (`209f25ad4fc0-test`, v6.1-rc1). The capture had 19 stats records, 0 errors and 0 reboot markers. The BT MAC is unchanged and `erase_bonds` is 0. Evidence: [`E21/…T2-smoke-s31`](../../docs/experiments/results/E21/20261124T110145Z-T2-smoke-s31). |
| T2-smoke-output | **PASS** | The app-only flash wrote 1,059,024 B at `0x10000` and its hash verified. The boot JSON matches the manifest (`209f25ad4fc0-test`, v6.0.2). The capture had 18 stats records, 0 errors and no crash markers. Evidence: [`E21/…T2-smoke-output`](../../docs/experiments/results/E21/20261124T110400Z-T2-smoke-output). |
| **stageB-exit** | **PASS** | Both smokes passed with no person present (06:01–06:05 ET). |
| H6 (Lark UAC1) | **PARTIAL** | See [below](#partial-h6-read-only-receiver-only). |
| Pixel harness app | Built, not installed | `rig/esp/pixel-harness/build/larkbridge-harness-debug.apk` built with JDK 17 and SDK 37, and its signature and DEX checks pass. It wasn't installed because the Pixel was limited to read-only use tonight. |
| Rig Pi scripts (Track B1) | Written and host-tested | `rig/esp/pi-image/` holds provisioning with a product-card guard, the fake car, a read-only selftest, Pico control stubs and the lease. The Pi itself was not read, because it refuses SSH key auth (P4). |
| H0, H2–H5, H7–H17 | Not run | These need Brian or rig hardware (see below). |

### What the S31 flash did to the Pixel link

The flash dropped the ACL at 06:00:50 (reason 8, supervision timeout). The Pixel reconnected by itself at 06:01:29 with 16-byte encryption on the existing bond, and LarkBridge is the active device again. The bonds survived the app-only flash, as designed. This was read with `adb dumpsys` only.

### Partial H6 (read-only, receiver only)

I ran `hil.py reduce H6` over both soak serial logs and the H1 boot log. It found:

- **5 h 00 min** of S31 capture;
- one enumeration of the Lark receiver (`3547:0407`, 48 kHz / S16 / stereo);
- **0 UAC failure lines** and **0 reboot markers**.

These deviations from the H6 spec keep it PARTIAL:

- No transmitter was on, so the link carried no real audio.
- The capture came from the pre-Stage-B image, so there are no JSON stats. Packet flow is inferred from the absence of errors, not measured. The test image now on the S31 removes this gap for the next run.
- The 100/100 reconnect and negative-descriptor tests were not run. They need the Pico fake Lark or a person replugging.

### What failed or got in the way

- **Pi SSH:** the Pi accepts passwords only, so every Pi read was skipped (P4).
- **Docker access:** the first shell was not in the `docker` group, so the review pass reran the builds with `sg docker`. All four passed.
- **Route-ACK timeouts:** the S31 logged 17,115 of them overnight, about one every 1.3 s. That is expected while the inter-board harness is absent, and they aren't counted as faults.
- **Radio contention:** the product Pi's BT500 paged the Pixel about every 20 s all night (D2). The soak passed despite that, but it was not a clean-air test.
- **One rig test was coupled to live gate statuses.** It failed once Stage B really passed. It's fixed in `50c0770`: the test now resets the gates it exercises.

## Review of the Codex pass

I read all 19 Codex commits and agree with them, so nothing was reverted:

- **Firmware:** atomics on counters shared across tasks, and the telemetry tap tables compiled only into test images.
- **Fault injection:** `fault silence` now also stops the test tone.
- **`hil.py` flash path:** it now accepts only the manifest-verified app at `0x10000`, with whitelisted flash options and the expected reset modes. This replaced a blacklist.
- **`hil.py` checks:** the smoke reducer requires a test image and rejects reboots during telemetry and mixed firmware. The lease uses an atomic lock directory.
- **Soak and builds:** the soak reducer returns INCONCLUSIVE on missing evidence instead of PASS. `--no-pull` is now enforced for pinned Docker builds.
- **Pi provisioning:** it is guarded against the product hostname.

Before relying on the app-only flash, I also checked that both boards' current partition tables match the new builds' byte for byte.

## Human queue (open items, in order)

The full table is in [`human-queue.md`](human-queue.md).

1. **P4.** Run `ssh-copy-id admin@larkbridge.local` from WSL so agents can read the Pi.
2. **D1: S31 flash size.** sdkconfig says 2 MB but the chip has 16 MB. I recommend 16 MB with a custom partition table that keeps NVS at the same offset. That needs a full flash.
3. **D2: second Bluetooth device.** Unplug the BT500 or power off the product bridge while the ESP rig is under test.
4. **D3: slow clock.** Tonight's soak passed on the RC clock: 6 h 11 min with no drops. I recommend keeping RC for now and re-soaking with clean air (after D2) and with audio streaming. Bluetooth modem sleep under real traffic is the untested case.
5. **P1.** With both boards unpowered, fit the six-wire I2S and UART harness (`firmware/docs/wiring.md`), then run the continuity and short checks. This unblocks H2–H5.
6. **P2/P3.** Connect the logic analyzer to the harness, and provision the **rig** SD card with `rig/esp/pi-image/provision.sh`. The product card stays untouched.
7. **P5.** Take the H0 photos and do the U.FL check.
8. **P6.** Turn the Lark transmitter on for the next H6 run.
9. **P7.** Get the Pico fake-Lark cable and the 1 kΩ resistors.

## Recommended next steps

1. **Pixel harness smoke.** It needs Brian's OK to install an app on the Pixel. Install and grant per `rig/esp/pixel-harness/README.md`, then run its smoke. This closes the `pixel-harness` gate and unblocks H9.
2. **Full H6** with the transmitter on (P6), using the test image now on the S31 so the JSON stats measure packet flow.
3. **D1 flash-size change.** If Brian approves, make the change on a branch, rebuild, and do a full flash with NVS preserved.
4. **H2–H5** once P1–P3 are done. Each runs unattended through `hil.py`.
5. **A second soak with D2 done and audio streaming,** to settle D3.

## Commits since `1882967`

There are 43 commits: 24 from Claude and 19 from Codex. Claude's commits are authored as Brian Ellsworth. This list stops at `50c0770`; the commit that adds this report comes after it.

| Commit | By | Subject |
| --- | --- | --- |
| `127a1a3` | Claude | rig/esp: passive Pixel<->S31 Bluetooth soak logger |
| `abf8567` | Claude | rig/esp/pixel-harness: deterministic 1 kHz/16 kHz stimulus generator |
| `60ad9ba` | Claude | common: qualification console parser and bounded JSON-lines writer |
| `a9a1c2e` | Claude | rig/esp/pi-image: rig lease with flock, expiry and stale takeover |
| `f377991` | Claude | rig/esp/pixel-harness: self-managed ConnectionService harness app + SDK-only build |
| `41e0cbb` | Claude | rig/esp/pixel-harness: README with build, morning install/grant/smoke steps |
| `b6561c7` | Claude | rig/esp: hil.py v0 and firmware/qual/gates.toml registry |
| `57ef591` | Claude | qual: human queue for the overnight run |
| `4732649` | Claude | rig/esp/pi-image: idempotent rig Pi provisioning with product-card guard |
| `013b053` | Claude | rig/esp/pi-image: fake car (A2DP sink only, AVRCP CT) and fake-car.sh |
| `172915e` | Claude | rig/esp/pi-image: read-only selftest.sh with JSON output |
| `e7f2a28` | Claude | rig/esp/pi-image: pico-ctl.sh stubs for the Pico fake Lark (SWD, reset, UART) |
| `6288fca` | Claude | rig/esp/pi-image: tests for provision, conf edits, status and pico-ctl |
| `fcb3cfd` | Claude | firmware: JSON telemetry, qualification console and test images |
| `74947e1` | Claude | bt_soak: also flag S31 USB/UAC console lines as events |
| `4f9c9ef` | Claude | Merge Pixel harness app skeleton (rig/esp/pixel-harness) |
| `29e4005` | Claude | rig/esp/pi-image: README with flash/provision/verify and read-only product-Pi checklist |
| `2d69c7b` | Claude | hil: H6 capture time is the sum of per-log spans |
| `3815952` | Claude | Merge rig Pi image scripts (rig/esp/pi-image) |
| `660b10e` | Claude | docs: renumber the ESP series from E18 to E21 |
| `8c4e28f` | Codex | Synchronize counters and flags read by firmware telemetry |
| `23c6322` | Codex | Serialize HIL lease updates and reject overlapping runs |
| `1472a2d` | Codex | Require complete evidence for a Bluetooth soak pass |
| `614cb20` | Codex | Restrict HIL flash arguments to the verified application image |
| `5cbcc83` | Codex | Keep Pi provisioning test roots out of live setup |
| `069cb31` | Codex | Reject smoke evidence spanning a crash or mixed firmware |
| `cd08b6b` | Codex | Enforce the no-pull option for pinned Docker builds |
| `a211081` | Codex | Pass Windows Java source paths safely to javac |
| `486d9d3` | Codex | Preserve Pixel stimulus samples across short AudioTrack writes |
| `77db247` | Codex | Refuse to provision a card with the product hostname |
| `98015f9` | Codex | Read the full PipeWire dump when checking the capture sink |
| `622dd84` | Codex | Require an explicit port for Bluetooth soak serial capture |
| `4f1dfbb` | Codex | Turn off qualification tones when injecting silence |
| `d0de638` | Codex | Require test images for the Stage B smoke gates |
| `acca0ca` | Codex | Record the overnight ESP32 review and Docker access blocker |
| `209f25a` | Codex | Compile telemetry tap tables only in qualification images |
| `44e2a5a` | Codex | Record passing pinned release and test firmware builds |
| `b26e6cf` | Codex | Summarize Bluetooth soak evidence without accessing devices |
| `0a53e2b` | Codex | Record the interim Pixel to S31 Bluetooth soak |
| `f3fbc18` | Claude | Mark T1-build passed from the pinned build verification |
| `f9210ee` | Claude | Record the passing overnight Pixel to S31 Bluetooth soak |
| `a0e99f5` | Claude | Pass Stage B exit with unattended T2 smoke on both boards |
| `50c0770` | Claude | Keep the Stage B exit test independent of live gate statuses |
