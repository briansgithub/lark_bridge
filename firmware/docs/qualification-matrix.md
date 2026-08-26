# ESP hardware qualification matrix

All rows are `PENDING HARDWARE`. Build or simulation success does not change a hardware row to pass.
Store each run under `docs/experiments/results/E18/<UTC timestamp>-<gate>/` with commands, serial
logs, measurements, firmware/toolchain manifests, and SHA-256 hashes.

| Gate | Scenario | Objective pass condition | Status |
|---|---|---|---|
| H0 | Visual identity and antenna | Correct SKUs/revisions; one sound antenna path; no damage | PENDING HARDWARE |
| H1 | Independent flash/boot | Exact pinned builds boot; identity/reset/heap logged; missing peers report not-ready | PENDING HARDWARE |
| H2 | UART protocol | 10 min clean heartbeat; zero accepted corrupt frames; next delimiter recovers; duplicate command executes once | PENDING HARDWARE |
| H3 | Independent reset | Nonzero output mutes no later than 1.5 s after last heartbeat; unaffected bond survives; same-session and reboot-session recovery complete within 60 s | PENDING HARDWARE |
| H4 | I2S electrical/PCM | 48 kHz S16LE stereo, correct channels/amplitude; continuous deterministic silence | PENDING HARDWARE |
| H5 | FIFO/drift | After 60 s warm-up, 30 min stream stays within 1,024–7,168 of 8,192 frames, correction within ±500 ppm, zero new over/underruns | PENDING HARDWARE |
| H6 | LARK UAC1 | `3547:0407`, UAC1 48 kHz/S16/stereo alternate proven; 30 min capture with zero transfer errors; 100/100 reconnects | PENDING HARDWARE |
| H7 | Output Bluetooth | Speaker and car pair/reconnect; A2DP source stable; AVRCP target receives controls | PENDING HARDWARE |
| H8 | Pixel media | S31 A2DP sink and AVRCP controller work; stereo relay reaches downstream sink | PENDING HARDWARE |
| H9 | Pixel call | HFP-HF codec and downlink stable for 10 min; normal qualification-locked image returns digital silence on uplink; zero reset/starvation | PENDING HARDWARE |
| H10 | LARK call + AEC | Three calibrated runs each achieve ≥10 dB correlated suppression and ≤0.01% clipping; no starvation/drop; every eligibility loss fails to silence | PENDING HARDWARE |
| H11 | Audio focus | Ten call interruptions each preempt media with no stale-media leak; media format/state restores within 15 s after call | PENDING HARDWARE |
| H12 | Steering controls | Play/pause/next/previous each act exactly once on Pixel; unsupported buttons are safe | PENDING HARDWARE |
| H13 | RF coexistence | Complete the fixed spacing/orientation matrix; no reset/lost call/unbounded FIFO; characterize dropouts and freeze release spacing afterward | PENDING HARDWARE |
| H14 | Fault injection | UAC loss, either reset, BT loss, malformed UART, FIFO limits all fail safely and recover | PENDING HARDWARE |
| H15 | Repetition | Ten resumable end-to-end cycles pass with no hard failure | PENDING HARDWARE |
| H16 | Endurance | Full 3,600-second active-call production-topology soak; zero reset/fault/starvation/overrun/underrun; warm current heap falls by neither >8 KiB nor >2% | PENDING HARDWARE |
| H17 | Vehicle power | After an automotive supply is selected: protected one-cable topology survives the recorded crank/cut/cycle matrix without back-feed, reset loop, or corrupt state | PENDING HARDWARE |

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
- H13 placement limits and H17 electrical limits are first-hardware characterization gates. Freeze
  their release thresholds from independent baseline runs before scoring the final topology.
- Any hard failure invalidates the current cycle and stops the campaign until its cause is recorded.
- A pass applies only to the exact firmware commit, IDF commits, component locks, wiring revision,
  paired peers, and power topology captured in that run.
