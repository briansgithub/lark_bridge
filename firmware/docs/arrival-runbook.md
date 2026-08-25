# First-hardware arrival runbook

Do these gates in order. Stop at the first failure and preserve its serial log; later stages assume
every earlier electrical and software contract has passed.

## 1. Inventory without power

1. Photograph both board labels and record their exact revisions and serial markings.
2. Confirm the output module is `ESP32-WROOM-32UE`, not the PCB-antenna `32E` variant.
3. Inspect for bent headers, debris, cracked USB connectors, or antenna damage.
4. Mate the U.FL pigtail straight down using fingertip pressure. Never lever or twist the U.FL end.
5. Confirm the pigtail and one existing antenna lead mate without force. Insulate the unused 2T2R
   lead.

Do not enable the output board's Bluetooth radio without its antenna connected.

## 2. Program each board separately

Leave the inter-board harness and LARK receiver disconnected.

1. Build both images from their pinned containers:

   ```text
   python firmware/tools/build_firmware.py output
   python firmware/tools/build_firmware.py s31
   ```

2. Connect the output board through its Micro-B programming port, flash `firmware/output`, and save
   a complete boot log.
3. Disconnect it. Connect the S31 through its USB Serial/JTAG Type-C port, flash `firmware/s31`, and
   save a complete boot log.
4. Record each chip revision, Bluetooth address, reset reason, IDF commit, free heap, and firmware
   Git commit in an E18 result directory.

Expected state is `NOT_READY` or silence while peers and the inter-board link are absent. A firmware
that reports ready at this stage fails the gate.

## 3. Wire the low-voltage link

With both boards unpowered, connect the six signal/ground wires exactly as listed in
[`wiring.md`](wiring.md). Check continuity and adjacent-pin shorts before reconnecting USB.

For bench work:

- power each board from its own programming USB cable;
- connect the boards' grounds;
- **leave both 5 V header pins disconnected**;
- place the two board antennas at least 15 cm apart initially;
- keep Wi-Fi disabled in firmware.

This avoids back-feeding one USB host from the other. The final 5 V star harness is not installed
until all functional gates pass.

## 4. Prove control before audio

1. Boot both boards and require reciprocal `HELLO`, matching protocol version, and expected
   capability masks.
2. Run for ten minutes with heartbeats and require zero CRC errors, zero unexpected resets, and no
   false-ready transition.
3. Reset each board independently. The other board must mute on heartbeat timeout, then recover
   without erasing either Bluetooth bond.
4. Inject a duplicate reliable command and corrupt frame. The command must execute once, the corrupt
   frame must be rejected, and the next valid delimited frame must decode.

## 5. Prove I2S with synthetic audio

Keep Bluetooth disabled. Generate 48-kHz signed 16-bit stereo tone, silence, and left/right marker
patterns on S31. Require on the output board:

- correct BCLK, WS, data polarity, channel order, and sample amplitude;
- no FIFO overrun or underrun after startup;
- deterministic zeros whenever route or heartbeat is unavailable;
- bounded FIFO correction with telemetry visible in both logs.

Run this stage for 30 minutes before adding any radio.

## 6. Qualify the LARK USB receiver

Plug the receiver into the S31 USB-A host through the data adapter. Save the complete USB and UAC
descriptor dump. The runtime must match the intended VID/PID and a supported UAC1 capture alternate
setting before marking the microphone ready. Exercise cold plug, hot plug, unplug during capture,
and 100 reconnects. A descriptor mismatch must remain fail-closed.

## 7. Add one Bluetooth side at a time

1. Output side: pair the ESP32 output board to a bench speaker, then the car. Prove A2DP Source,
   reconnect, and receipt of AVRCP play/pause/next/previous events.
2. S31 side: pair the Pixel to the S31. Prove A2DP Sink/AVRCP Controller first, then HFP-HF with
   synthetic 16-kHz audio.
3. Preserve the two peer addresses and bonds. Do not rely on device discovery order.

## 8. Integrate media, call, and controls

Run the qualification matrix in [`qualification-matrix.md`](qualification-matrix.md). In particular:

- media is Pixel → S31 → I2S → output ESP32 → car;
- call downlink follows the same output path while LARK → AEC → HFP is the uplink;
- `CALL > MEDIA > SILENCE` focus is deterministic;
- car media buttons cross UART and are reissued to the Pixel once per press/release;
- raw LARK audio is never sent during a call until AEC is initialized and qualified.

## 9. Convert to one-cable power only after qualification

Use a protected 5 V star harness sized for both boards and the S31's USB-host load. Validate polarity,
idle/peak voltage, current, brownout margin, and power-cycle recovery with programming USB cables
removed. The carrier must prevent either programming connector from back-feeding the shared rail.

The final release gate remains ten complete cycles plus a 3,600-second production-topology soak.
