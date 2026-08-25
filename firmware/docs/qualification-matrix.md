# ESP hardware qualification matrix

All rows are `PENDING HARDWARE`. Build or simulation success does not change a hardware row to pass.
Store each run under `docs/experiments/results/E18/<UTC timestamp>-<gate>/` with commands, serial
logs, measurements, firmware/toolchain manifests, and SHA-256 hashes.

| Gate | Scenario | Objective pass condition | Status |
|---|---|---|---|
| H0 | Visual identity and antenna | Correct SKUs/revisions; one sound antenna path; no damage | PENDING HARDWARE |
| H1 | Independent flash/boot | Exact pinned builds boot; identity/reset/heap logged; missing peers report not-ready | PENDING HARDWARE |
| H2 | UART protocol | 10 min clean heartbeat; corrupt frames rejected; duplicate command executes once | PENDING HARDWARE |
| H3 | Independent reset | Heartbeat loss mutes in bounded time; unaffected bond/radio survives; link recovers | PENDING HARDWARE |
| H4 | I2S electrical/PCM | 48 kHz S16LE stereo, correct channels/amplitude; continuous deterministic silence | PENDING HARDWARE |
| H5 | FIFO/drift | 30 min synthetic stream; bounded fill/correction; zero steady-state over/underruns | PENDING HARDWARE |
| H6 | LARK UAC1 | Descriptor identity/alternate setting proven; 30 min capture; 100 reconnects | PENDING HARDWARE |
| H7 | Output Bluetooth | Speaker and car pair/reconnect; A2DP source stable; AVRCP target receives controls | PENDING HARDWARE |
| H8 | Pixel media | S31 A2DP sink and AVRCP controller work; stereo relay reaches downstream sink | PENDING HARDWARE |
| H9 | Pixel call | HFP-HF bidirectional audio is intelligible and stable with synthetic uplink | PENDING HARDWARE |
| H10 | LARK call + AEC | At least 10 dB objective suppression, ≤0.01% clipping, safe peak; fail-closed on AEC loss | PENDING HARDWARE |
| H11 | Audio focus | Call preempts media; no media leak; media format/state restores after call | PENDING HARDWARE |
| H12 | Steering controls | Play/pause/next/previous each act exactly once on Pixel; unsupported buttons are safe | PENDING HARDWARE |
| H13 | RF coexistence | Both BT streams operate with antennas separated; loss/retry/latency recorded | PENDING HARDWARE |
| H14 | Fault injection | UAC loss, either reset, BT loss, malformed UART, FIFO limits all fail safely and recover | PENDING HARDWARE |
| H15 | Repetition | Ten resumable end-to-end cycles pass with no hard failure | PENDING HARDWARE |
| H16 | Endurance | Full 3,600-second production-topology soak; no reset/leak/drift or gate violation | PENDING HARDWARE |
| H17 | Vehicle power | Protected one-cable supply survives starts/cuts without back-feed or corrupt state | PENDING HARDWARE |

## Timing and quality measurements

Record, rather than guess, these values on first hardware:

- media latency and call round-trip latency;
- A2DP SBC encode/decode configuration and observable quality loss;
- I2S FIFO target, minimum, maximum, correction ppm, underruns, and overruns;
- UART heartbeat interval, timeout, CRC/replay counts, and recovery time;
- UAC packet/rate errors and USB current;
- HFP codec selected (CVSD or mSBC), packet loss, and reconnect time;
- free heap/minimum heap, task high-water marks, and reset reason;
- current and rail voltage at idle, media, call, reconnect, and maximum USB-host load.

## Release interpretation

- A pre-hardware build may be labeled `BUILD-READY`, never `HARDWARE-QUALIFIED`.
- A shortened 5–10 minute smoke soak is useful during iteration but never satisfies H16.
- Any hard failure invalidates the current cycle and stops the campaign until its cause is recorded.
- A pass applies only to the exact firmware commit, IDF commits, component locks, wiring revision,
  paired peers, and power topology captured in that run.
