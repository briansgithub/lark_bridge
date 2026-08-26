# ADR-0010: Port LarkBridge to a two-controller ESP architecture

- **Status:** Accepted for pre-hardware implementation
- **Date:** 2026-08-25

## Context

The Raspberry Pi 3 implementation proved the LARK A1 USB microphone, Pixel HFP headset role,
wired output, objective AEC measurement, and recovery methodology. It also exposed the central
product risk: one Bluetooth controller cannot be trusted to carry the Pixel's deadline-sensitive
HFP/eSCO call and a second-device A2DP output stream at the same time.

The ordered hardware is:

- ESP32-S31-Function-CoreBoard-1;
- ESP32-DevKitC-32UE (ESP32-WROOM-32UE);
- a U.FL-to-RP-SMA pigtail and an existing 2.4/5 GHz antenna.

ESP32-S31 supports USB 2.0 High-Speed host and Bluetooth Classic. The original ESP32 supports
Bluetooth Classic and is sufficient as a dedicated A2DP source. ESP-IDF support for S31 is still
preview quality, so compile success is not evidence of working hardware.

## Decision

Use two Bluetooth controllers, joined by a short internal wired link:

```text
LARK TX ~~~> LARK USB receiver --USB UAC1--> S31
Pixel 7a <--HFP + A2DP + AVRCP--> S31
S31 --48-kHz S16 stereo I2S + framed UART--> ESP32-DevKitC-32UE
ESP32-DevKitC-32UE <--A2DP + AVRCP--> car/speaker
```

The S31 is the system coordinator and I2S clock master. It owns the LARK receiver, Pixel bond,
HFP-HF, A2DP Sink, AVRCP Controller, audio focus, AEC, and durable configuration. The legacy
ESP32 owns only the downstream A2DP Source, AVRCP Target, its downstream bond, and an elastic
audio buffer.

The S31 never runs A2DP Source. The output ESP32 never connects to the Pixel and never runs HFP.
This keeps A2DP Sink and Source on different controllers and prevents a reconnect policy from
silently collapsing the design back onto one radio.

The external runtime connections are wireless except power. The LARK receiver and inter-board
link are enclosed physical connections. Audio must not be sent between boards over Wi-Fi,
Bluetooth, ESP-NOW, or UART.

## Audio contract

- Inter-board I2S is Philips standard, signed little-endian S16, stereo.
- Inter-board rate is fixed at 48,000 frames/s. The S31 accepts Pixel SBC stereo at 44.1 or
  48 kHz and causally converts 44.1 kHz to the fixed inter-board rate.
- A2DP media is decoded on the S31 and re-encoded on the output ESP32.
- HFP mSBC is 16-kHz mono. The S31 converts call downlink to 48-kHz stereo for I2S and converts
  the 48-kHz LARK capture to 16-kHz mono for HFP uplink.
- Call audio has priority over media. A call transition must mute/fill silence atomically; stale
  media must never enter the call.
- The output ESP32 never blocks its Bluetooth callback on I2S. It drains an elastic ring and
  zero-fills underruns.
- FIFO and correction telemetry are returned over UART. The output ESP32 owns a bounded local
  elastic-buffer actuator: every 100 ms a PI controller selects at most ±500 ppm, and the consumer
  occasionally interpolates one extra or one fewer input frame without stopping I2S.

AEC uses a 16-kHz mono microphone plus the exact 16-kHz call-downlink reference. AEC is fail
closed when required: loss, initialization failure, or an unqualified algorithm must mute HFP
uplink rather than expose the raw LARK signal. The first implementation uses ESP-SR 2.5.1's
full-duplex low-cost AEC in a dedicated worker, with a configurable initial reference-delay
alignment. Its processed output is deliberately qualification-locked by default; the existing
objective 10 dB suppression gate remains authoritative before publication is enabled.

## Control contract

UART carries COBS-delimited, CRC-protected, versioned frames. Protocol v1 carries boot-session
HELLO/capabilities, heartbeat/readiness, fenced route commands, FIFO/stats, faults, ACKs, and AVRCP
events. The audio format is the fixed I2S contract and UART never carries audio.
Unknown versions/types are rejected. Duplicate reliable commands are acknowledged without being
executed twice. Heartbeat loss forces the output to silence and makes the S31 report not-ready.
Every route change is a two-board transaction: both sides first enter silence, the output
acknowledges its flush, a drain guard elapses, the output acknowledges the target route, and only
then may the S31 authorize audio and advertise that route in its heartbeat.

Car play/pause/next/previous press events arrive at the output ESP32 as AVRCP Target events and
cross UART as reliable atomic taps. The S31 then issues the matching AVRCP Controller press and
release to the Pixel locally. Stop-and-wait delivery, duplicate suppression, and session-boundary
queue discard prevent both reordering and a UART reset from stranding a pressed key.
Phone answer/end buttons are a separate HFP behavior and are not claimed until measured on the
2015 CR-V head unit.

## Toolchain decision

- S31: ESP-IDF `v6.1-rc1`, commit `44f0c59f7c81a72a5868a52d5f6dfbbf88829704`.
- ESP32: ESP-IDF `v6.0.2`, commit `7101770dc6db2667b3c477cc31365dd1acd6db4e`.
- USB UAC host: `espressif/usb_host_uac` `1.5.0`.
- ESP-SR: `espressif/esp-sr` `2.5.1`, treated as preliminary on S31.

Generated component lockfiles are committed. CI must build from exact commits and archive the
firmware manifests. A future stable S31 IDF is an explicit upgrade and regression-test decision,
not an automatic floating dependency update.

## Consequences

Advantages:

- HFP and car A2DP use physically separate radios.
- Full Pixel stereo media relay is possible because Sink and Source are on different controllers.
- Continuous audio has a deterministic wired transport and an inspectable clock master.
- The output controller is small, inexpensive, replaceable, and independently recoverable.

Costs and remaining risks:

- Two SBC stages add latency and generation loss.
- The two free-running Bluetooth clocks require a measured elastic-buffer/rate-control loop.
- Two nearby 2.4-GHz radios can desensitize each other; antenna separation and TX-power tuning are
  hardware gates.
- S31 Bluetooth, USB, and ESP-SR support are preview/preliminary.
- Steering media controls depend on the car's AVRCP behavior.
- AUX requires a later line-level I2S DAC and is not part of this wireless build.

## Rejected alternatives

- **One S31 for both phone and output:** retains the simultaneous eSCO/ACL radio scheduler risk and
  cannot run A2DP Sink and Source together in the current stack.
- **Wireless inter-board audio:** adds another 2.4-GHz flow and makes bounded latency/recovery
  harder.
- **Keep the Pi as production runtime:** larger, slower to boot, and preserves the controller and
  OS-stack failure modes this port is intended to remove.
- **Treat build-only results as acceptance:** impossible without the ordered boards, LARK, Pixel,
  and downstream sinks; all such gates remain explicitly pending.
