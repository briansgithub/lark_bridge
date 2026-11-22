# Dual-USB Bluetooth Implementation Plan

## Summary and execution venue

Implement this starting from clean branch `codex/dual-usb-bluetooth` at `398f0d6`.


- ADR-0009, E17, and the E16 evidence are now durable in Git; the full historical conversation is unnecessary.
Read ADR-0009, E17, applicable repository instructions, and current Git state. Use serial model-routed subagents inside that conversation; this avoids requiring the user to switch models manually.


| Phase | Model | Effort | Model handoff |
|---|---|---:|---|
| 0. Baseline and readiness diagnosis | GPT-5.6 Luna | Medium | **PAUSE before Phase 1 if model routing is unavailable** |
| 1. BT600 support package and readiness fix | GPT-5.6 Sol | Max | Continue directly into Phase 2 |
| 2. Permanent-role backend and recovery refactor | GPT-5.6 Sol | Max | **PAUSE; switch to Terra for Phase 3** |
| 3. Autonomous closed-loop harness | GPT-5.6 Terra | High | **PAUSE; use a fresh Sol reviewer** |
| 4. Independent predeployment review | GPT-5.6 Sol | Extra high (`xhigh`) | **PAUSE before hardware mutation** |
| 5. Candidate-card deployment and support gate | GPT-5.6 Terra | High | **PAUSE; switch to Sol before pairing** |
| 6. Pixel migration and live-call qualification | GPT-5.6 Sol | Max | **PAUSE; switch to Luna only after all smoke gates pass** |
| 7. Repetition and soak campaign | GPT-5.6 Luna | Medium | Stop immediately on failure and return to Sol Max |
| 8. Onboard retirement, durability, and final audit | GPT-5.6 Sol | Extra high | Final phase |

Ultra is unnecessary. Use at most two narrow diagnose/fix/retest cycles for one failure before stopping and reporting.

## Interfaces and architectural contract

### Durable configuration

Extend the existing device tables without changing the Android protocol:

```toml
[devices.phone]
address = "<Pixel MAC>"
adapter = "B0:82:E2:25:18:36"
adapter_product = "ASUS USB-BT600"
adapter_bus = "USB"
adapter_usb_vendor_id = "0b05"
adapter_usb_product_id = "1d70"

[devices.output]
adapter = "A0:AD:9F:73:6C:24"
adapter_product = "ASUS USB-BT500"
adapter_bus = "USB"
adapter_usb_vendor_id = "0b05"
adapter_usb_product_id = "1bf6"
```

Rules:

- Permanent addresses must be uppercase, distinct, and resolve to exactly one matching controller.
- Runtime identity is permanent address + bus + VID:PID. Product is a diagnostic label, not trusted identity.
- `hciX`, USB port, topology, enumeration order, and BlueZ’s default adapter remain diagnostic-only.
- A transitional UART call specification is allowed only while the onboard rollback path is active. Final readiness requires both configured roles to be USB.
- Missing, duplicate, or mismatched controllers fail closed; never infer the call controller from whichever Pixel bond happens to exist.

### Runtime status

Preserve existing status fields and add:

```text
controllers.call
controllers.output
  configured_address
  expected_bus
  expected_usb_id
  observed_address
  observed_usb_id
  hci
  ready
  error
```

Android continues using existing `list`, `status`, `set`, `scan`, and `pair_select` messages. No new permissions, scan behavior, or Quick Settings behavior are required.

### Services and routing

- Re-establish Pixel HFP/eSCO and RFCOMM only through BT600.
- Run speaker discovery, pairing, A2DP, and recovery only through BT500.
- Bind Pi RFCOMM sockets and SDP lookup explicitly to BT600’s resolved local address.
- Accept HFP PipeWire nodes only while the Pixel is connected on BT600 and not simultaneously connected through another controller.
- Run independent `bridge-btwatchdog@call` and `bridge-btwatchdog@output` instances, each with its own counters, locks, backoff, and recovery state.
- Automatic recovery may use exact-device D-Bus operations, per-controller down/up, its rfkill index, and its exact USB interface unbind/rebind.
- Automatic recovery must never class-wide rfkill Bluetooth, restart global `bluetooth.service`, reset the other adapter, or restart WirePlumber. Whole-stack recovery remains an explicit administrative last resort.

## Implementation phases

### 0. Establish the implementation baseline

- Verify both repositories and the live Pi without mutation.
- Confirm the new branch begins at `398f0d6`, the old architecture remains frozen at `4dba442`, and unrelated Android work remains untouched.
- Capture current deployed hashes, controller identities, services, mounts, package versions, bonds, and readiness result.
- Record the current readiness blockers:
  - generated `rpi-zram-writeback.timer` fails because its transaction reaches masked `systemd-remount-fs.service`;
  - the current boot contains deliberate E16 restart records.
- Confirm the production Android protocol remains compatible and no Android implementation is presently required.

Exit: authoritative baseline recorded; no scan, pairing, restart, deployment, or route change.

### 1. Build exact BT600 support and fix restart readiness

Keep kernel `6.18.34+rpt-rpi-v8`. Do not upgrade to candidate 6.18.39; its published modules still lack USB-BT600/RTL8761CU support.

Create reproducible package `larkbridge-bt600-support`:

- Build patched `btrtl.ko` and `btusb.ko` against the exact deployed source, configuration, `Module.symvers`, GCC 14.2.0-19, and binutils 2.44.
- Pin:
  - Raspberry Pi source `linux_6.18.34-1+rpt1`;
  - Raspberry source commit `c8c7494100e99ee05b11aaa4f0588a223a63d1af`;
  - btrtl commit `ab8cafa8695184e66d0fc3940ac8248a3e83398b`;
  - BT600 commit `6f0624b4427e38c3bb63a951c536cf8adaee1238`;
  - firmware commit `35e542439e8aaca81fa1527fe3887afbb68a6779`.
- Port only the semantic USB entry `0b05:1d70` with `BTUSB_REALTEK | BTUSB_WIDEBAND_SPEECH`; do not import unrelated devices.
- Include only:
  - `rtl8761cu_fw.bin`, SHA-256 `525bfa5ab9a3ed74b788f9ac494467c522a436c1bc5290ebcb05c7f920533c09`;
  - `rtl8761cu_config.bin`, SHA-256 `3142c2732470e4edcdb68edf58cc505b581e38d56b3ec09c64eddcb1d5099507`;
  - matching license/WHENCE and provenance JSON.
- Install modules under `/lib/modules/6.18.34+rpt-rpi-v8/updates/larkbridge/` with exact-release depmod overrides.
- Depend exactly on `linux-image-6.18.34+rpt-rpi-v8 = 1:6.18.34-1+rpt1`.
- Package scripts may run `depmod` only. They must not unload modules, restart services, rebuild initramfs, pair, or reboot.
- Build in a pinned Docker environment twice and require identical artifacts or documented normalization.
- Verify vermagic, module alias, firmware names, module dependency resolution, and retained stock modules for uninstall rollback.

Fix the readiness blocker by configuring `rpi-swap` for pure zram:

```ini
[Main]
Mechanism=zram
```

This removes the pointless writeback timer instead of unmasking `systemd-remount-fs.service`. Do not weaken the restart-record readiness gate; require a later clean boot.

Commit the package builder, patches, provenance checks, zram configuration, and tests as separate coherent checkpoints.

### 2. Implement permanent controller roles and isolated recovery

- Introduce one shared strict configuration/parser layer used by supervisor, output service, watchdogs, readiness checks, and test tools.
- Extend adapter discovery to derive VID, PID, USB parent/interface, bound driver, product/manufacturer, and rfkill index from sysfs while resolving BlueZ paths by permanent address.
- Replace address-only “first match” behavior with typed missing, duplicate, and identity-mismatch failures.
- Keep the E16 BT500 discovery/pair/select transaction unchanged except that it uses the strict output-role resolver.
- Remove call-side bond inference, UART fallback, default-adapter SDP, and unbound RFCOMM connections.
- Publish the new controller status block and make CLI/readiness diagnostics consume it.
- Split watchdogs by role and implement only targeted recovery.
- Update deployment, boot verification, power-loss checks, and active E17 instrumentation to resolve roles. Historical E01/E07 tools may retain clearly labelled onboard literals.
- Preserve the Broadcom SCO overlay, onboard watchdog path, and onboard Pixel bond only as temporary rollback infrastructure.
- Add transitional configuration support so the reviewed code can deploy while calls still use onboard Bluetooth. Final readiness must reject UART after cutover.
- Do not redesign the Android app. Build its existing tests once to prove protocol compatibility.

Required host tests:

- hci renumbering and reversed enumeration order;
- missing, duplicated, wrong-bus, and wrong-VID:PID controllers;
- same permanent address assigned to both roles;
- BT600-bound RFCOMM/SDP and BT600-only HFP acceptance;
- no bond inference or onboard fallback;
- independent watchdog counters/backoff;
- no global rfkill, BlueZ restart, WirePlumber restart, or other-controller reset;
- preservation of every E16 scan/pair/select and rollback guarantee;
- two-controller BlueZ state snapshot/restore and readiness validation.

### 3. Build the autonomous E17 test harness

Create a resumable PC-side orchestrator that controls SSH, ADB, Pi audio tools, monitoring, and evidence collection. It must:

- resolve adapters from the durable role configuration;
- use timestamped, machine-readable result directories and resumable phase state;
- use lightweight monitoring during long runs; reserve short targeted Bluetooth captures for operations that require proof;
- collect controller identity, BlueZ, PipeWire, RFCOMM, USB/kernel, service, config-slot, and ADB audio-route evidence;
- use ADB intents and UI hierarchy inspection rather than Pixel screenshots;
- attempt Pixel UI actions autonomously and request one human tap only if Android blocks automation;
- drive PC test audio through Stereo Mix and collect objective audio results;
- run `speaker_preflight.py` before every speaker-dependent scenario;
- pause for a speaker wake only when the preflight cannot detect playback at the Lark;
- maintain continuous or periodic out-of-window playback so the Boombox does not sleep during a campaign;
- stop on the first red transport gate rather than interpreting silence as software failure.

Closed-loop gates must reuse established thresholds:

- speaker-to-Lark level and tone margins each at least 10 dB;
- no unsafe peak and no more than 0.01% clipping;
- AEC suppression at least 10 dB using the existing metric tool;
- no candidate bond/profile connection before Pixel selection;
- selected-output recovery within three seconds after scan/switch;
- no unmeasured subjective “sounds fine” claim when objective capture is available.

### 4. Independent predeployment audit

A fresh Sol reviewer receives only the repositories, ADR-0009, E17, and commits from Phases 1–3.

Audit:

- package provenance, exact ABI, source hashes, licenses, rollback, and absence of hidden initramfs/kernel upgrade;
- all role lookups and command construction;
- every RFCOMM, SDP, Pair, ConnectProfile, reset, rfkill, and USB-unbind target;
- absence of `hciX`, default adapter, or physical-port decisions in active production paths;
- transactional config and pairing-state ordering;
- independent watchdog behavior and shutdown/cancellation;
- closed-loop harness thresholds and instrumentation load;
- current Android compatibility.

Fix findings, rerun host tests/lint/build, and issue an explicit candidate-card deployment decision.

### 5. Prepare and deploy on the spare SD card

First deploy only the reviewed zram-readiness fix to the current appliance through its established reversible transaction. Reboot through SSH, observe a quiet window, and require the official readiness result to be green with no failed units or spontaneous restart jobs.

Then pause for one consolidated SD-card session:

1. Agent issues a software poweroff and confirms the Pi is down.
2. User inserts the current card into the PC reader.
3. Agent identifies the disk by capacity/serial/partition labels and asks for one destructive source/target confirmation.
4. Agent creates and hashes a full-card image.
5. User replaces it with the spare card.
6. Agent writes the image, reads it back, verifies partition/filesystem metadata, and runs the repository’s offline verifier.
7. User boots the spare in the Pi.
8. Agent proves the unchanged clone matches the original baseline. The original card remains untouched thereafter as physical rollback.

Install `larkbridge-bt600-support` only into the candidate card’s immutable lower root, run depmod against that root, reseal it, verify boot-partition hashes remain unchanged, and reboot through SSH.

Without pairing the Pixel, require:

- custom module paths and hashes;
- exact vermagic and `0b05:1d70` alias;
- Realtek RTL8761CU examination and successful firmware load/version reporting;
- no generic-only attachment, firmware error, HCI timeout, USB reset, or URB error;
- BT500 bonds and behavior unchanged;
- all three controllers identified by permanent identity.

Use the diagnostic power meter during this initial gate and record voltage/current.

Then pause once for the user to remove the meter and swap the two dongles’ physical ports. This simultaneously supplies the required BT600 hot-remove/reinsert event. Reboot and prove that permanent roles remain unchanged. All decisive testing from this point uses the BT600 directly, without the meter.

### 6. Deploy role-aware services and migrate the Pixel

- Deploy the reviewed application/service changes to the candidate card with the durable call role still set to onboard Bluetooth.
- Prove saved BT500 outputs and the current Android app still work.
- Generate a fully validated candidate configuration in tmpfs assigning BT600 to the call role; do not commit it yet.
- Make only BT600 pairable/discoverable and name it `LarkBridge`. Keep BT500 non-pairable and temporarily label onboard Bluetooth as legacy.
- Use ADB/UI hierarchy automation to pair the Pixel. Ask for a tap only if Android’s protected confirmation cannot be automated.
- Seal the complete BlueZ state immediately after pairing.
- Disconnect onboard Pixel HFP/SCO without deleting or untrusting its rollback bond.
- Launch the candidate services against the tmpfs configuration.

Require before any live-call acceptance:

- Pixel bond and connection on BT600 only;
- mSBC negotiation, eSCO link, and bidirectional SCO packets;
- expected 16 kHz PipeWire call endpoints;
- RFCOMM control connected through BT600;
- no call ACL/SCO traffic on onboard Bluetooth;
- no speaker operation on BT600.

Run a ten-minute call-only closed-loop test. If it fails, stop candidate services, disconnect and remove only the newly created BT600 Pixel bond, restore onboard service/config, and retain all prior speaker state.

After call-only success, run saved BT500 switching, observation-only scan during the call, Aux fallback, Boombox/A40/iWorld selection as available, crackling regression, app reconnect, and speaker sleep/recovery. Run the mandatory speaker/Lark preflight before each speaker scenario.

Only after all gates pass:

- commit the exact tested call configuration through the existing A/B durable-config workflow;
- restart the minimum role services;
- verify the durable slot, active mirror, runtime status, and route agree;
- retain the onboard Pixel bond for the final rollback phase.

### 7. Automated repetition, isolation, and soak

Use the fixed harness without model improvisation:

- ten establish/use/teardown repetitions;
- one 60-minute production-topology soak with Lark USB, BT600 mSBC/eSCO, and BT500 A2DP;
- continuous test audio to prevent Boombox sleep;
- automated per-role USB deauthorize/re-authorize and exact-interface unbind/rebind;
- BT500 loss must preserve the Pixel call and permit Aux fallback;
- BT600 loss must preserve BT500 playback;
- recovery must target only the failed permanent identity.

Run one consolidated physical-isolation session after the automated simulation:

- user removes/reinserts BT500 during a call;
- user removes/reinserts BT600 during speaker playback;
- agent performs all observation and verdict work.

Require zero:

- controller resets outside the injected target;
- HCI timeouts, USB resets/URB errors, or frame-reassembly failures;
- wrong-controller operations;
- unexplained call drops;
- speaker reconnect loops or crackling;
- unsealed durable-state changes.

Luna must stop and hand evidence back to Sol Max on any red gate; it must not diagnose or patch live failures itself.

### 8. Retire onboard Bluetooth and prove durability

After every earlier gate is green:

- preserve the original SD card, a fresh candidate recovery bundle, current config slot, and full BlueZ snapshot;
- remove/untrust the live onboard Pixel bond only after the snapshot is sealed;
- remove the obsolete Pixel entry through ADB where possible;
- retain only the BT600 endpoint name `LarkBridge`;
- transactionally remove the Broadcom SCO overlay, add onboard-Bluetooth disablement, disable `hciuart` and `bridge-btfw`, and update readiness expectations;
- reboot through the boot-trial workflow;
- prove only BT600/call and BT500/output remain and that no service references `B8:27:EB:43:8D:51`;
- prove automatic Pixel, RFCOMM, speaker, Aux fallback, and output-choice restoration.

Run a fresh quiet boot and the official power-loss readiness check. Do not weaken or waive any red gate.

When every gate is green, pause for explicit user authorization immediately before the final physical power pull. After return, automatically verify:

- immutable-root and LARKDATA integrity;
- exact module/firmware/deployed hashes;
- both permanent controller roles;
- BT600 Pixel bond and BT500 speaker bonds;
- durable output choice;
- all required services;
- automatic call/speaker recovery;
- mandatory speaker/Lark preflight.

Finish with a fresh independent audit and a documentation-only checkpoint. Do not merge branches, delete rollback history, discard the original SD card, or remove worktrees without separate authorization.

## Human-intervention budget and defaults

Expected human actions are limited to:

1. The consolidated SD-card imaging/swap session.
2. Removing the diagnostic power meter and swapping dongle ports once.
3. One Pixel pairing confirmation only if ADB cannot perform it.
4. Initial Discord call/Stereo Mix setup only if desktop automation cannot establish it.
5. Waking a speaker only after automated preflight proves it is asleep.
6. One consolidated physical adapter-removal session.
7. Explicit authorization and the final power pull.

Assumptions:

- Spare SD card and reader are available.
- The power meter is diagnostic-only.
- The original card remains the physical rollback throughout.
- Kernel stays at `6.18.34+rpt-rpi-v8`.
- Android output protocol/UI remains unchanged unless an actual compatibility defect is found.
- LE Audio/LC3, a role swap, powered hub, newer Pi, and wired phone audio remain separate ADR-level fallbacks, not silent scope expansion.
- No Pixel screenshots are taken by default.
