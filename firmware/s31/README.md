# ESP32-S31 gateway firmware

This is the phone/LARK-facing half of LarkBridge. It targets the
`ESP32-S31-FUNCTION-COREBOARD-1` using the exact ESP-IDF `v6.1-rc1` pin in
`../toolchain.lock.json`.

Implemented paths:

- Bluetooth Classic HFP Hands-Free, A2DP Sink, and AVRCP Controller profiles
- fixed 48 kHz/S16/stereo I2S master output to the legacy ESP32 board
- CRC16 + COBS UART control/status link using the shared v1 protocol
- USB Host UAC lifecycle filtered to LARK VID:PID `3547:0407`
- fail-closed session routing: call, then media, then digital silence
- two-phase silence/flush/guard/target route fencing, heartbeat timeout,
  route acknowledgement/retry, and duplicate AVRCP suppression
- 44.1- or 48-kHz A2DP input converted to the fixed 48-kHz inter-board format
- ESP-SR 2.5.1 full-duplex low-cost AEC worker with separate raw-reference and
  processed-uplink queues, lifecycle telemetry, and fail-closed publication
- output-side FIFO, correction, adaptive-frame, loss, and I2S-error telemetry

Deliberate pre-hardware gates:

- The real AEC path is present, but `LARKBRIDGE_S31_EXPERIMENTAL_AEC_UPLINK`
  defaults off. HFP microphone packets remain digital silence until its measured
  reference delay is set and H10 passes; raw LARK audio is never a fallback.
- ESP-SR support for S31 is preliminary. CPU time, internal-RAM headroom,
  reference alignment, and suppression quality remain hardware gates.
- The inter-board clock remains fixed. Drift is corrected on the output ESP32 by
  a bounded ±500-ppm elastic consumer; its telemetry must satisfy H5.

Build with the pinned IDF checkout:

```powershell
idf.py --preview set-target esp32s31
idf.py build
```

Run those commands from this directory after exporting the pinned IDF
environment. The committed lock resolves `espressif/usb_host_uac==1.5.0` and
`espressif/esp-sr==2.5.1` plus their exact transitive dependencies.
