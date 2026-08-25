# E17 — Dual-USB Bluetooth architecture and qualification report

- **Status:** Architecture accepted; implementation and hardware qualification not started
- **Date:** 2026-08-24
- **Decision:** ADR-0009
- **Previous campaign:** E16

## Executive conclusion

The onboard Bluetooth UART is not reliable enough to remain the call transport. The failure is not
an audio-volume or radio-range problem: E07 captured loss of byte alignment in the HCI H4 stream
between the Pi 3B and its onboard BCM43438 while mSBC SCO was active. E16 then reproduced H4
reassembly bursts under a quieter, hardened configuration. Once framing is lost, the host parses
audio bytes as bogus events, Bluetooth commands stop receiving replies, and recovery removes the
Lark call route for tens of seconds.

UART is needed only because the onboard Bluetooth controller is physically attached through it.
It carries controller commands and events, ACL data, and—because of E01's required SCO-over-HCI
setting—the live call audio packets. It is not required by the Lark microphone, Aux output, USB
sound devices, PipeWire audio graph, Android application, durable configuration, or Bluetooth as a
product feature. Moving both Bluetooth roles to USB controllers bypasses the defective path rather
than attempting to mask it.

The accepted target is:

- **ASUS USB-BT600, permanent address `B0:82:E2:25:18:36`: Pixel HFP/HF and mSBC/eSCO call
  controller**, with the RFCOMM control service re-established here after BT600 pairing.
- **ASUS USB-BT500 supplied with the project, permanent address `A0:AD:9F:73:6C:24`: A2DP
  speaker controller** for discovery, pairing, connection, selection, and recovery.
- **Onboard BCM43438, permanent address `B8:27:EB:43:8D:51`: retired after qualification.**

This assignment preserves the BT500's already-proven E15/E16 speaker behavior. The BT600 is the
stronger call candidate because its upstream Linux device entry explicitly enables Realtek handling
and wideband speech. Bluetooth 6.0 and LC3 marketing do not themselves prove HFP or mSBC: the
project must still qualify the classic BR/EDR eSCO path on its real workload.

There is one immediate blocker. The current Pi kernel, Bluetooth modules, and firmware package do
not contain complete RTL8761CU/USB-BT600 support. The dongle currently enumerates generically but
must not be paired or treated as production-capable until the support gate below passes.

## Scope and safety state

This report records a read-only hardware inventory and an architectural decision. It did not scan,
pair, migrate a bond, connect a profile, change a live route, disable onboard Bluetooth, deploy a
kernel, install firmware, reboot, or perform a power cut. The inline USB power meter remained only
an observed physical interposer.

The old implementation campaign is frozen at commit
`4dba442a95166a60630a7f68e0248f8cc4f488fa` on branch
`claude/mode1-bluetooth-output`. New work begins from that exact commit on
`codex/dual-usb-bluetooth`. This document and ADR-0009 are the first new-architecture checkpoint;
the branch boundary preserves the tested-but-rejected onboard-UART history without rewriting it.

The live Pi remains on the sealed E16 deployment. At the final E16 handoff the external call was
down, both wireless speakers were disconnected, Boombox was still the durable desired output, Aux
was the correct fallback, and A40 bond/trust state was intact. E16 explicitly marked abrupt-power
testing not ready.

## Incident report: what the corruption is

### Observed failure signature

Normal Bluetooth HCI UART traffic uses H4 framing: the first byte identifies the packet type and
the following header says how much payload follows. During the captured failures, valid 60-byte
mSBC SCO receive packets stopped abruptly and were replaced by implausible 244-byte vendor events.
Those records were not genuine controller events; they were call-audio bytes interpreted at the
wrong boundary. The kernel subsequently emitted `Frame reassembly failed (-84)`, commands timed
out, and the controller stopped responding.

The key distinction is:

- **Established by capture:** the onboard HCI H4 stream loses byte alignment during SCO and does
  not recover in place.
- **Strong measured inference, not isolated proof:** a short PL011 receive-service delay causes an
  overrun or dropped byte. The path is programmed I/O with no DMA, its 32-byte FIFO provides only
  about 347 microseconds of slack at 921600 baud, and it competes with high-priority Pi 3 USB host
  interrupt work.
- **Ruled out as the fundamental explanation:** sustained CPU throughput, an HCI/console collision,
  speaker volume, a simple RF disconnect, or a single required user action.

Once a byte is lost, H4 has no reliable host-side resynchronization marker. The error message can
therefore lag the actual last good SCO packet, and continuing host transmit counters do not prove
that received call audio is alive.

### Evidence across experiments

| Evidence | Result | Meaning |
| --- | --- | --- |
| E01 | BCM43438 PCM/I2S pins are unavailable on the Pi 3B; bidirectional mSBC worked only with SCO routed over HCI | Every call placed real-time SCO traffic on the onboard UART by design |
| E07 occurrence 4 | Last good SCO RX after 17.2 s; audio bytes then appeared as bogus vendor events; host kept transmitting into the dead stream | Direct H4 desynchronization evidence |
| E07 measured transport | PL011 PIO/no DMA, 32-byte FIFO, 921600 baud, heavy `dwc_otg` FIQ count | Interrupt-latency/FIFO-overrun mechanism is physically plausible |
| E07 run 11 | Three streaming USB devices: desync at 17.2 s; USB bus empty: no desync in 228 s and 30,354 SCO RX frames | USB-host load strongly changes failure rate |
| E07 run 12 | Lark plus onboard 3.5 mm: 84,102 SCO RX frames over 10 min 31 s, zero errors | Reduced load helped, but only the final 2–3 min included active Lark streaming, so this is not proof |
| E15 | Onboard HFP plus BT500 A2DP survived seven minutes once, with zero observed dropouts | Splitting roles helped, but USB-hosted HFP and dual USB were not tested |
| E16 quiet gate | Two bursts of eight H4 reassembly errors during a light five-minute window, plus seven immediately before it | Wi-Fi-off/performance/low-load hardening did not eliminate onboard framing corruption |
| E16 recovery consequence | An onboard-controller recovery left the LarkBridge SCO route absent for about 46.65 s | Watchdog recovery cannot satisfy call continuity |

The recurrence interval is variable—from seconds to many minutes in earlier campaigns—so a short
successful call cannot close this failure.

## Why the project used UART and what bypassing it changes

The Pi 3B's BCM43438 is not a USB peripheral. Its Bluetooth HCI is wired internally to the SoC's
PL011 UART. HCI commands/events and ACL traffic must use that path, and E01 selected transport
routing for SCO because the alternative PCM pins do not reach a usable connector. Consequently the
continuous two-way mSBC call stream also used UART.

External Bluetooth dongles present HCI over USB instead. Their synchronous audio uses USB
isochronous endpoints, so neither controller commands nor SCO packets traverse PL011. Disabling the
onboard controller after qualification eliminates the UART transport from the Bluetooth design;
the OS can still use UART for unrelated purposes if ever needed.

This is a bypass, not a proof that USB is automatically reliable. Both dongles share the Pi 3B's
single USB 2.0 host with the Lark and any other USB peripherals. Dual-radio is therefore accepted as
the next architecture to qualify, not declared complete on enumeration alone.

## Read-only adapter inventory

The `hci` numbers observed during inventory are deliberately omitted from the identity column. They
were transient runtime mappings and changed when the BT600 appeared.

| Physical identity | Permanent address | Observed USB identity / transport | Observed controller facts | Architectural disposition |
| --- | --- | --- | --- | --- |
| Pi 3B onboard Broadcom BCM43438 | `B8:27:EB:43:8D:51` | Internal PL011 HCI UART at 921600 baud | SCO-over-HCI required by E01; recurring H4 corruption in E07/E16 | Retire and disable only after USB call acceptance |
| ASUS USB-BT500 supplied with project | `A0:AD:9F:73:6C:24` | `0b05:1bf6`, Realtek, USB full speed; labelled product has a noncanonical live USB ID | HCI/LMP 5.4 (`0x0d`), subversion `0x8761`, SCO MTU `255:12`; bidirectional isochronous alternatives through 63-byte packets | Keep as speaker/A2DP controller |
| ASUS USB-BT600 | `B0:82:E2:25:18:36` | `0b05:1d70`, Realtek RTL8761CU/CUV, USB full speed | HCI/LMP `0x0e`, subversion `0x8761`, SCO MTU `255:12`; classic SCO, extended/transparent SCO, EDR eSCO features; bidirectional isochronous alternatives through 63-byte packets | Qualify, then use as Pixel HFP/eSCO controller |

Both USB descriptors declare 100 mA maximum. Both registered with a
`Failed to read codec capabilities (-22)` message, so that message does not distinguish their roles
and must not be waved away without live audio qualification.

The BT500 discrepancy matters. ASUS's commonly upstreamed USB-BT500 identity is `0b05:190e`, while
this supplied unit reports `0b05:1bf6`. Its permanent address and extensive project-specific A2DP
evidence are authoritative for this appliance. Code must verify the expected identity tuple and
fail closed; it must not rename a different device merely because it appears in the same port.

## Role decision and rationale

### BT500 remains the speaker controller

E15 discovered and bonded an arbitrary Boombox on this controller and sustained concurrent A2DP
with a call on the other controller. E16 then used permanent address `A0:AD:9F:73:6C:24` for the
phone-driven workflow: non-mutating BR/EDR discovery, user-selected A40 pairing, A2DP Sink
validation, connection,
PipeWire-node readiness, selection, durable remember, and wrong-controller duplicate untrust. That
is the most mature Bluetooth path in the project.

Keeping that assignment minimizes migration: the speaker bonds, output IDs, transaction contract,
and Android experience can remain. The full production-duration speaker soak is still owed; E16
also captured timeout/re-enumeration behavior under intrusive monitoring, so “proven” here means
functional role and protocol, not unlimited reliability.

### BT600 becomes the call-controller candidate

The upstream USB-BT600 device entry applies both `BTUSB_REALTEK` and
`BTUSB_WIDEBAND_SPEECH`, and the dongle exposes the USB isochronous transport and controller feature
bits required for classic SCO/eSCO. That makes it a better evidence-based HFP candidate than moving
the already established BT500 speaker side.

It is still only a candidate until real mSBC/eSCO testing passes. Upstream's device submission
reported A2DP and ASHA tests, not this project's Pixel HFP/HF role. LE Audio/LC3 support is unrelated
to the required classic HFP call path.

### Target data flow

```text
Pixel 7a
  ├─ HFP/HF + mSBC/eSCO ── ASUS USB-BT600 [B0:82:E2:25:18:36] ─┐
  └─ RFCOMM app control ─── ASUS USB-BT600 [B0:82:E2:25:18:36] ─┤
                                                                    Pi / BlueZ / PipeWire
Lark USB microphone ────────────────────────────────────────────────┤
Far-end playback ───────── ASUS USB-BT500 [A0:AD:9F:73:6C:24] ─────┤─ A2DP speaker
Far-end playback ───────── onboard or USB audio output ─────────────┘─ Aux fallback

Onboard BCM43438 [B8:27:EB:43:8D:51]: disabled after acceptance
```

The Android app does not pair speakers locally. It sends `scan` and `pair_select` over the
existing control channel; the Pi executes those operations only on the BT500.

## Immediate BT600 software blocker

The live Pi inventory found:

- kernel `6.18.34+rpt-rpi-v8`, Debian package `1:6.18.34-1+rpt1`, built 2026-06-09;
- installed `firmware-realtek` `1:20250410-2+rpt1`;
- no `0b05:1d70` device entry in the live `btusb` module;
- no RTL8761CU mapping or firmware names in the live `btrtl` module;
- no `rtl_bt/rtl8761cu_fw.bin` or `rtl_bt/rtl8761cu_config.bin` in the immutable image; and
- generic attachment logs without the expected Realtek firmware initialization sequence.

The available package metadata showed a newer firmware candidate, but package version alone is not
the gate: the exact files and matching kernel support must be present and verified.

The required upstream support chain is:

1. [`ab8cafa86951` — btrtl RTL8761CUV controller mapping](https://git.kernel.org/bluetooth/bluetooth-next/c/ab8cafa86951),
   which maps HCI revision `0x0e` / LMP subversion `0x8761` and requests the CU firmware and config.
2. [`6f0624b4427e38c3bb63a951c536cf8adaee1238` — ASUS USB-BT600 USB ID and wideband-speech quirk](https://kernel.googlesource.com/pub/scm/linux/kernel/git/netdev/net-next/+/6f0624b4427e38c3bb63a951c536cf8adaee1238),
   which adds `0b05:1d70` with `BTUSB_REALTEK | BTUSB_WIDEBAND_SPEECH` and explains that generic
   binding skips required firmware initialization.
3. [`35e542439e8aaca81fa1527fe3887afbb68a6779` — Realtek RTL8761CU firmware and config](https://kernel.googlesource.com/pub/scm/linux/kernel/git/firmware/linux-firmware/+/35e542439e8aaca81fa1527fe3887afbb68a6779).

The RTL8761CU controller mapping, the USB ID with Realtek/wideband-speech flags, and a successful
`rtl8761cu_fw.bin` load are mandatory. Upstream sets `.config_needed = false` for this controller,
so `rtl8761cu_config.bin` is not strictly required for initialization even though the matching
upstream firmware commit supplies it and the driver names it. This project will install and hash
both files as one controlled bundle, but the evidence must distinguish a mandatory firmware load
from an optional config load. Dropping files into an image whose driver never requests the firmware,
or adding only the USB ID without the controller mapping, is not an acceptable partial fix.

ASUS lists the [USB-BT500 as Bluetooth 5.0 + EDR/BLE](https://www.asus.com/ca-en/networking-iot-servers/wireless-adapters/all-series/usb-bt500/techspec/)
and the [USB-BT600 as Bluetooth 6.0 with BR/EDR and BLE](https://www.asus.com/ca-en/networking-iot-servers/wireless-adapters/all-series/usb-bt600/techspec/).
Those product specifications corroborate the physical products but do not replace the live identity
or HFP qualification.

## Required implementation changes

### Identity and configuration

1. Add explicit call-controller and output-controller identities to durable configuration. Each
   role must include permanent controller address and the expected physical/USB identity checks.
2. Require the two configured permanent addresses to be different and require each role to resolve
   to exactly one matching physical controller; ambiguity or duplication fails closed.
3. Enumerate BlueZ adapters, read their permanent addresses, and derive current object paths at
   runtime. Treat `hci` indices and USB paths as diagnostic output only.
4. Refuse startup or a role-scoped operation when two devices claim one role, a configured device
   is missing, or its observed identity conflicts. Do not silently fall back to BlueZ's default.
5. Record user-facing names as product labels, not port labels: “ASUS USB-BT600 (call)” and
   “ASUS USB-BT500 (speakers).” Normal Android UI need not expose addresses; diagnostic status must.
6. Audit `systemd-rfkill` state after port moves. E15 found that persisted state can be coupled to
   enumerated device paths even when application configuration is correct.

### BlueZ, audio, and services

1. Pair the Pixel on the BT600 object only. Require HFP/HF, mSBC negotiation, eSCO traffic, and
   the expected PipeWire 16 kHz call nodes before marking it ready.
2. Keep E16 `scan` and `pair_select` on the BT500 only. The call controller must never receive a
   speaker Pair, Connect, or ConnectProfile operation.
3. Apply any vendor SCO routing or firmware-reset behavior only if the USB controller actually
   requires it; do not carry BCM43438-specific setup forward by habit.
4. Replace onboard-address and `hci0` assumptions in supervisor, watchdog, recovery, health,
   deployment, diagnostics, and test paths. The committed E16 capture helper also hard-codes the
   previous `hci0`/`hci1` objects and must be made role-aware before reuse.
5. Disable the onboard controller only after the USB call gate passes, using a reversible boot and
   service change. Verify that no service still waits for or writes to `B8:27:EB:43:8D:51`.

### Recovery and concurrency

1. Give each USB controller its own health state, timeout budget, restart counter, and backoff.
2. Reattach or reset only the permanent address assigned to the failing role. Re-resolve its current
   `hci` mapping afterward.
3. Serialize destructive controller operations per role while allowing independent call and
   speaker monitoring. Coordinate any unavoidable global BlueZ restart so state is reported as a
   whole-appliance recovery, not hidden as a local success.
4. On BT500 loss, keep the call and fall back output to Aux when available. On BT600 loss, leave the
   current speaker route intact and restore only the call controller when it returns.

### Android UI/UX

The phone-driven output UI does not need a new architecture screen. The simplest experience remains
the E16 design: saved outputs appear immediately, “Add Bluetooth device” performs an explicit
BT500 scan, and a single result tap requests pair/connect/select/remember. The Pi owns adapter
selection, so the user never chooses “hci” devices or USB ports.

Useful streamlining opportunities are:

- replace duplicate LarkBridge entries on the Pixel by giving the BT600 call endpoint one durable,
  unambiguous product name and removing the obsolete onboard bond only after migration succeeds;
- surface one plain status such as “Phone link ready” rather than exposing call-controller detail;
- preserve “Needs setup on LarkBridge” for a speaker bonded to the wrong controller, with one repair
  action that invokes the transactional BT500 flow;
- keep scanning and pairing out of the Quick Settings tile; and
- show Aux immediately even if either radio is unavailable, so Bluetooth faults never make the
  output screen appear empty.

## Inline USB power meter

The meter does not alter logical identity and must never be used to decide a controller role. The
BT600 enumerated through it without a recorded USB attachment error, but an inline device can still
add voltage drop, connector resistance, or signal-integrity variability.

For every BT600 support, pairing, scan, and call test, record meter voltage/current and the Pi's
undervoltage/USB-reset indicators. If the meter will remain in the final appliance, qualify that
topology. If it is only a diagnostic tool, repeat the decisive production soak without it. A result
that changes with the meter is a power/signal finding to diagnose, not a reason to change permanent
addresses or swap roles.

## Staged implementation and acceptance gates

No phase may use success in a later-looking UI as a substitute for its lower transport gate.

### Gate 1 — immutable-image BT600 support

1. Add or backport the two kernel support changes and the exact upstream firmware/config assets
   through the
   repository's normal build/deployment workflow.
2. Reseal the immutable lower root and retain a recovery bundle before restarting Bluetooth.
3. Cold-attach and cold-boot with the BT600. Require logs that show Realtek device recognition,
   firmware/config request and successful load/version reporting.
4. Fail if initialization remains generic, the firmware does not load, either controlled bundle
   file is absent from the immutable image, any HCI command times out, or the wideband-speech quirk
   is absent. Absence of a config-load log is not by itself a failure because upstream marks that
   asset optional.
5. Do not pair the Pixel during this gate.

### Gate 2 — identity independent of port and order

1. Boot with both USB dongles and resolve roles by permanent address.
2. Move each dongle among available Pi ports, including reversing their enumeration order, then
   reboot. The role mapping must remain BT600/call and BT500/speakers.
3. Verify missing, duplicate, and identity-mismatch cases fail closed with an actionable status.
4. Verify rfkill and recovery state do not follow the old port-derived `hci` name.

### Gate 3 — BT600 call-only qualification

1. Pair the Pixel only on `B0:82:E2:25:18:36`; preserve the onboard bond and trust as rollback but
   keep the endpoint names distinguishable during migration.
2. Before every BT600 call trial, disconnect onboard Pixel HFP/SCO without deleting or untrusting
   its bond. Prove there is no call ACL/SCO traffic on `B8:27:EB:43:8D:51` and that the selected
   communication endpoint, HFP nodes, and all synchronous traffic resolve to the BT600.
3. Confirm HFP/HF, mSBC, an eSCO synchronous connection, two-way SCO packet flow, and PipeWire's
   expected 16 kHz nodes.
4. Run the mandatory speaker/Lark acoustic preflight before judging call audio.
5. Complete a ten-minute call-only shakedown, then a live Discord two-way speech test. Require no
   USB URB errors, HCI timeouts, controller reset, route loss, or unexplained silence.

### Gate 4 — concurrent BT500 speaker qualification

1. Repeat saved selection, 12-second non-mutating BR/EDR discovery, A40 `pair_select`, A2DP-only
   connection,
   durable remember, Boombox/iWorld selection where supported, and Aux fallback.
2. Prove from monitoring that every speaker Pair/ConnectProfile path targets
   `A0:AD:9F:73:6C:24` and no candidate is mutated before Pixel selection.
3. While a BT600 call is active, scan on BT500 and switch saved outputs. Require continuous call
   control and Lark uplink, with automatic A2DP recovery within the E16 three-second bar.
4. Re-test the prior crackling/reconnect condition using light, non-intrusive capture and the
   required speaker/Lark preflight.

### Gate 5 — isolation, repetition, and soak

1. Remove/reinsert BT500 during a call: the BT600 call must remain up, output may fall back to Aux,
   and recovery may target only the BT500 permanent address.
2. Remove/reinsert BT600 during speaker playback: the BT500 route must remain, and call recovery may
   target only the BT600 permanent address.
3. Complete at least ten representative establish/use/teardown repetitions and one 60-minute
   production-topology soak with Lark USB capture, BT600 mSBC/eSCO, and BT500 A2DP active.
4. Require zero controller resets, HCI command timeouts, USB resets/URB errors, wrong-controller
   operations, unexplained call drops, UART frame-reassembly errors, and audible reconnect churn.
5. Track speaker dropouts against E15's `<1/min` exploratory bar, but production acceptance is a
   clean call plus no user-disruptive speaker churn, not merely an average below that bar.

### Gate 6 — retire onboard Bluetooth and verify durability

1. Only after gates 1–5 pass, disable onboard Bluetooth reversibly and remove its active Pixel role.
2. Cold boot and prove no production service addresses `B8:27:EB:43:8D:51` or relies on an `hci`
   index/default adapter.
3. Verify automatic Pixel-call and selected-speaker restoration without shell intervention.
4. Run the official power-loss readiness check. Fix every red gate before asking for an abrupt cut.
5. With explicit user confirmation, execute only the repository's justified abrupt-power campaign,
   then verify immutable-root integrity, both controller identities/bonds, durable output choice,
   services, and audio preflight after return.

## Failure criteria and rollback

Any of these blocks the architecture from becoming the default:

- the BT600 remains generically bound, does not load the exact firmware, or the immutable image
  lacks the pinned matching config asset;
- HFP negotiates narrowband only when mSBC is required, eSCO/SCO is one-way, or a live call loses
  its route;
- either role changes after a port move or enumeration-order change;
- a speaker operation reaches the BT600 or a call recovery resets the BT500;
- a USB reset, HCI timeout, controller re-enumeration, crackling/reconnect loop, or unexplained
  dropout recurs under the production topology; or
- any power-loss readiness gate is red.

Rollback is intentionally available throughout qualification:

1. Do not remove the old branch, E16 evidence, sealed deployment, or recovery bundle.
2. Keep onboard Bluetooth enabled and its Pixel bond preserved until the BT600 call campaign passes.
3. If BT600 call qualification fails, disconnect it and remove its Pixel bond only when that bond
   was newly created by this campaign; reconnect the preserved onboard bond. Never remove a
   pre-existing bond under a generic cleanup rule.
4. If a gate fails, stop live mutation, capture synchronized evidence, restore the last sealed
   configuration, and leave durable output/bonds unchanged except for a newly created failed test
   bond that the transactional rules explicitly allow removing.
5. Do not silently swap adapters. If evidence later favors BT500 for HFP, amend ADR-0009, plan the
   speaker-bond migration, and re-run every identity and concurrency gate.
6. If both role assignments fail on the Pi 3 USB root, evaluate a powered hub/newer Pi and the wired
   UAC phone-link fallback as separate, evidence-bearing architecture changes.

## Current verdict

**Architecture accepted; deployment not ready.** The dual-USB design cleanly bypasses the proven
onboard UART framing failure and assigns each available adapter according to the strongest evidence.
The BT500 speaker side is functionally established but still owes production-duration tests. The
BT600 has the right upstream call-oriented quirk and physical transport, but the live Pi lacks its
complete driver/firmware support and no project HFP/eSCO result exists yet.

The next authorized implementation checkpoint is therefore narrow: add and verify complete
RTL8761CU/USB-BT600 support in the immutable image without pairing the Pixel. Only a clean support
gate authorizes controller-identity refactoring and live HFP qualification. No reboot durability or
abrupt-power test is ready now.
