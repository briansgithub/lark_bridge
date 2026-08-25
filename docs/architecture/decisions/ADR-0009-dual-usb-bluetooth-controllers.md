# ADR-0009 — Use two permanently identified USB Bluetooth controllers

- **Status:** Accepted
- **Date:** 2026-08-24
- **Supersedes:** the target use of the Pi 3B onboard BCM43438 as the Pixel HFP/eSCO controller
- **Relates to:** E01, E03, E07, E15, E16, and E17

## Context

The original Mode 1 design used the Pi 3B onboard BCM43438 for the Pixel HFP call and an ASUS
USB Bluetooth adapter for A2DP speakers. The onboard controller communicates with the SoC over a
921600-baud PL011 UART. E01 deliberately routed mSBC SCO audio over that HCI transport because the
BCM43438 PCM/I2S pins are not routed on the Pi 3B PCB.

E07 proved that the resulting H4 stream can lose byte alignment during a call. The measured path
uses programmed I/O rather than DMA, has a 32-byte receive FIFO (about 347 microseconds of slack at
921600 baud), and competes with the Pi 3 USB controller's high-priority interrupt work. The precise
event that drops a byte remains an interrupt-latency inference, but the framing loss, bogus HCI
events, command timeouts, and controller wedge are observed facts. E16 reproduced reassembly bursts
even after the practical mitigations were applied and while using a lightweight monitor.

A watchdog can recover a wedged controller, but recovery interrupts the call for tens of seconds.
That is not an acceptable substitute for a sound transport.

Two ASUS USB adapters are now available. USB transport bypasses the onboard PL011 H4 path entirely,
and independent controllers keep the continuous HFP/eSCO workload separate from speaker discovery,
pairing, and A2DP. E15 proved the separation concept only with onboard HFP plus one USB A2DP adapter;
it did not qualify USB-hosted HFP or two simultaneous USB controllers.

## Decision

Use the following target roles, resolved by permanent controller address on every boot:

| Physical adapter | Permanent controller address | Target role |
| --- | --- | --- |
| ASUS USB-BT600 | `B0:82:E2:25:18:36` | Pixel HFP/HF and mSBC/eSCO call audio; re-establish the Pixel RFCOMM control service here after pairing |
| ASUS USB-BT500 supplied with this project | `A0:AD:9F:73:6C:24` | A2DP speaker discovery, pairing, connection, selection, and recovery |
| Pi 3B onboard BCM43438 | `B8:27:EB:43:8D:51` | Retired from the target runtime after the USB call controller passes qualification |

The USB-BT500 label above describes the physical supplied product. Its live USB ID is
`0b05:1bf6`, rather than the commonly documented ASUS USB-BT500 ID `0b05:190e`; therefore neither
the product name nor role may be inferred from USB ID alone. The permanent controller address,
expected USB identity, controller capabilities, and administrator-recorded physical product must
agree. A mismatch is a blocking identity error, not permission to guess.

The following rules are architectural invariants:

1. The call and output permanent addresses must be distinct, and each must resolve to exactly one
   controller whose physical and USB identity matches its configured role.
2. Never persist or make a routing decision from `hci0`, `hci1`, `hci2`, USB topology, physical
   port, enumeration order, or BlueZ's current default adapter. Those values may change after a
   reboot, port move, re-enumeration, or recovery.
3. Resolve the current BlueZ object path from the configured permanent address before every
   controller-scoped operation. Refuse the operation if the assigned controller is absent or its
   observed identity conflicts with the configured physical adapter.
4. Pair the Pixel only on the BT600 call controller. Pair and connect speakers only on the BT500
   output controller. Preserve but untrust wrong-controller **speaker** duplicates, following E16's
   transactional rules. Preserve the onboard Pixel bond and trust until the controlled BT600
   cutover succeeds; the speaker-duplicate rule does not govern the call bond.
5. Recover each adapter independently. A speaker fault must not reset the call controller; a call
   fault must not tear down the speaker route. A global BlueZ restart is an explicitly coordinated
   last resort, not a normal watchdog action.
6. Keep the current Android product model: the Pixel app asks the Pi to scan and select; the Pixel
   itself does not scan for speakers. Speaker scan and pairing always execute on the BT500.
7. Do not disable onboard Bluetooth or migrate the Pixel bond until the BT600 support and live-call
   gates in E17 pass. The current deployment and its recovery bundle remain the rollback baseline.

The BT600 is not production-ready on the current Pi image. USB ID `0b05:1d70` currently binds
generically, but the installed kernel modules lack the RTL8761CU mapping and device-specific
`BTUSB_REALTEK | BTUSB_WIDEBAND_SPEECH` entry, and the image lacks the matching firmware bundle.
The controller mapping, USB entry/quirks, and `rtl_bt/rtl8761cu_fw.bin` load are mandatory. Upstream
declares `.config_needed = false` for RTL8761CU, so `rtl_bt/rtl8761cu_config.bin` is not strictly
required for driver initialization; this project will still install and hash the matching upstream
config as a controlled qualification asset and record whether the driver uses it. No Pixel pairing
may occur before the support set is positively verified.

## Consequences

- The unreliable onboard UART is removed from both call control and real-time SCO audio.
- The already hardware-proven BT500 speaker implementation, bonds, and phone UI can remain in
  place while the call side changes.
- Both USB adapters share the Pi 3B's single USB 2.0 host. USB bandwidth, interrupt behavior, power,
  and failure isolation therefore remain acceptance risks rather than assumed improvements.
- Bluetooth bonds belong to a controller. The Pixel must be newly paired on the BT600; existing
  speaker bonds stay with the BT500.
- Services, watchdogs, diagnostics, deployment checks, and test tools that assume `hci0` or `hci1`
  must be converted to role-to-permanent-address resolution.
- The inline USB power meter is an installation and measurement variable, never an identity input.
  Decisive tests must record its readings and either include it in the final supported topology or
  repeat without it.
- The Android UI should not expose adapter numbers or ports. It may describe a wrong-controller
  speaker as “Needs setup on LarkBridge,” as E16 already specifies.

## Qualification and rollback

E17 is the controlling qualification report. At minimum, the transition must prove model-specific
BT600 firmware initialization, cold-boot and port-independent identity, Pixel HFP/mSBC/eSCO on the
BT600, simultaneous BT500 A2DP, targeted unplug/recovery behavior, acoustic call quality, a
production-length soak, and only then persistence through the appliance's abrupt-power workflow.

Until those gates pass, the deployed onboard-call configuration remains recoverable and onboard
Bluetooth remains enabled. If BT600 firmware support or HFP qualification fails, restore that
configuration and do not reinterpret a short smoke test as acceptance. A role swap requires a new
ADR amendment because it would migrate the proven speaker workload and its bonds.

## Alternatives considered

- **Keep onboard HFP and tune the UART/watchdog further.** Rejected as the target. Mitigations
  reduce or recover failures but E16 shows that framing corruption still recurs, and recovery
  itself causes an unacceptable call outage.
- **Use one USB controller for both HFP and A2DP.** Rejected for this transition. E03 reproduced
  failure when both profiles shared the onboard BCM43438; it does not prove every modern USB
  controller would fail. Separate controllers nevertheless preserve the established speaker path,
  isolate recovery, and avoid reintroducing the combined-profile failure mode during this pivot.
- **Swap BT500 and BT600 roles.** Retained only as a documented fallback after evidence. The BT500
  already has project-specific A2DP pairing and selection evidence, while upstream marks the
  BT600 as wideband-speech capable. Moving both workloads now would increase uncertainty.
- **Route SCO over PCM/I2S.** Rejected for this hardware: the required onboard-controller pins are
  not routed on the Pi 3B PCB.
- **Use a wired USB Audio Class phone link.** Strongest fallback if dual USB Bluetooth fails, but it
  changes cabling, charging, phone routing, and ground-loop requirements.
- **Move to newer Pi hardware or a powered hub.** Valid escalation options if the shared Pi 3 USB
  root or power budget fails qualification; neither is assumed to solve the problem without the
  same evidence campaign.
