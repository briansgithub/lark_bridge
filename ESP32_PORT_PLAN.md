# LarkBridge ESP32 port plan

This is the active implementation plan on `codex/esp32-dual-radio-port`. `PLAN.md` and the Pi
experiment reports remain the evidence-bearing specification for the proven Raspberry Pi work;
they are not silently rewritten as ESP results.

## Definition of the pre-hardware deliverable

Before the ordered boards arrive, the repository shall contain:

1. Two independently buildable, version-pinned ESP-IDF applications.
2. An allocation-free shared core with native tests for framing, rings, PCM conversion, rate
   control, focus/routing, recovery, and fail-safe behavior.
3. S31 adapters for UAC1 capture, HFP-HF, A2DP Sink, AVRCP Controller, ESP-SR AEC, I2S master TX,
   UART link, NVS configuration, and structured diagnostics.
4. ESP32 adapters for I2S slave RX, elastic buffering, A2DP Source, AVRCP Target, UART link, NVS
   downstream selection, and structured diagnostics.
5. A deterministic two-board simulator and serial qualification tool using the same wire-format
   constants as firmware.
6. CI that builds host tests and both firmware images from pinned toolchains and preserves build
   metadata.
7. Wiring, flashing, first-boot, pairing, RF-placement, rollback, and hardware acceptance
   instructions.
8. Every hardware-dependent claim marked `PENDING HARDWARE`; no simulated pass may satisfy a
   physical gate.

## Runtime architecture

### S31 coordinator

- Enumerate only LARK VID:PID `3547:0407` as the production microphone.
- Require UAC1 capture, 48 kHz, two channels; prefer 16-bit and reject unsupported layouts.
- Expose one Pixel-facing headset identity named `LarkBridge` with HFP-HF, A2DP Sink, and AVRCP
  Controller.
- Maintain `CALL > MEDIA > SILENCE` focus.
- Feed the exact downlink reference and LARK microphone to 16-kHz VOIP AEC before HFP uplink.
- Produce a continuous 48-kHz stereo I2S stream, including deterministic silence during changes.
- Forward downstream AVRCP events to the Pixel only while the matching Pixel AVRCP session exists.
- Own system readiness and publish reason-coded failures.

### ESP32 output controller

- Identify as an A2DP Source/AVRCP Target named `LarkBridge Output`.
- Pair with one downstream SBC-capable sink at a time.
- Receive 48-kHz stereo PCM as I2S slave and buffer it without blocking Bluetooth callbacks.
- Zero-fill underruns, drop oldest data on overrun, and count both.
- Forward supported AVRCP button events to the S31; never interpret them as local pairing/reset.
- Mute within one heartbeat timeout if the S31 link is lost.

## Phases

### P0 — contracts and deterministic core

- Pin toolchains/components and record upstream provenance.
- Implement COBS/CRC protocol, sequence/ack handling, streaming resynchronization, and heartbeat.
- Implement SPSC rings, PCM mapping, fixed 48↔16 kHz conversion, FIFO rate control, and route FSM.
- Run native tests with sanitizers and deterministic randomized streams.

Exit: host tests pass; no ESP-IDF dependency is required for the core.

### P1 — output ESP32 firmware

- Initialize NVS, UART, I2S slave RX, Bluetooth Classic, AVRCP Target, and A2DP Source.
- Negotiate/lock 48-kHz S16 stereo with the S31.
- Feed A2DP from the elastic ring without blocking; implement silence, counters, and reconnect.
- Map downstream AVRCP events into protocol frames.

Exit before hardware: exact ESP-IDF build passes and artifacts are reproducible enough to compare
hashes after path/timestamp normalization.

### P2 — S31 firmware

- Initialize USB host/UAC, select the exact LARK capture interface, and handle hot removal.
- Initialize HFP-HF HCI audio plus A2DP Sink/AVRCP Controller on the Pixel session.
- Implement call/media focus and PCM conversion.
- Integrate ESP-SR AEC behind a fail-closed adapter.
- Initialize I2S master TX, UART coordinator, persistent configuration, and diagnostics.

Exit before hardware: exact preview IDF build passes. USB, RF, call, and AEC behavior remain
pending.

### P3 — simulated integration and operator tooling

- Run two endpoint state machines through corrupted, duplicated, delayed, and dropped UART frames.
- Exercise call interruption, media restoration, board reset, heartbeat loss, FIFO drift, UAC loss,
  and Bluetooth reconnect order permutations.
- Provide build/flash/monitor wrappers and a serial JSON evidence collector.
- Generate a hardware-arrival checklist and timestamped result directory.

Exit: host simulation is deterministic, recovery assertions pass, and the tooling can operate on
mock serial endpoints.

### P4 — hardware arrival gates (`PENDING HARDWARE`)

1. Inspect boards, connector orientation, power, and antenna placement before energizing.
2. Flash each board separately and record chip revision, MAC, IDF version, heap, and reset reason.
3. Prove UART framing/recovery, then I2S clock/tone/silence, before enabling either Bluetooth radio.
4. Prove LARK descriptors, 48-kHz S16 capture, hot-plug, and 30-minute error-free USB operation.
5. Prove output ESP32 A2DP/AVRCP with each available sink.
6. Prove S31 A2DP/AVRCP with Pixel, then HFP/mSBC bidirectionally with synthetic audio.
7. Enable the LARK uplink, then AEC; require the existing objective 10 dB suppression gate.
8. Run full media relay, call interruption/restoration, steering controls, fault isolation, ten
   cycles, and the deferred 3,600-second endurance soak.

No later gate can waive an earlier red transport gate.

## Initial acceptance thresholds

- UART: zero accepted corrupt frames; recovery after arbitrary garbage by the next valid delimiter;
  duplicate command executes once.
- I2S: 48,000 ± measured clock tolerance; correct LR ordering; no unbounded FIFO trend.
- USB: exact VID:PID and format; zero transfer errors in the qualification window.
- HFP: mSBC preferred; no bad/lost/discarded packet growth outside an injected fault.
- A2DP: no audible/recorded discontinuity outside injected faults; underrun/overrun counters remain
  zero in steady state.
- Recovery: targeted endpoint recovery; unrelated radio and bonds remain intact.
- AEC: at least 10 dB measured suppression, safe peak, and no more than 0.01% clipping.
- Durability: ten complete cycles plus 3,600 seconds in production topology.

## Deliberate exclusions

- AUX output hardware and firmware.
- Wi-Fi, cloud control, and OTA update service during initial qualification.
- Multiple simultaneous downstream sinks.
- Call answer/end steering buttons until the CR-V's actual Bluetooth profile behavior is captured.
- A claim of production readiness while S31 support remains preview or any P4 gate is pending.
