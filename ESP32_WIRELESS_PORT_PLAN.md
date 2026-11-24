# LarkBridge ESP32 wireless port: forward plan

- **Branch / worktree:** `claude/esp32-wireless-port` at
  `C:\Users\user1\Desktop\Projects\lark_bridge-esp32-wireless`. It is `main` plus a merge of
  `origin/codex/esp32-dual-radio-port` (merge `3cab0bd`).
- **Planned by:** Claude Opus 5.5 (`claude-opus-5-5`). Revision 2, 2026-11-22, after syncing
  with the hardware hand-off.
- **Design:** [ADR-0010](docs/architecture/decisions/ADR-0010-esp32-dual-radio-bridge.md). The
  bench sequence and thresholds are in [`firmware/docs/resume-here.md`](firmware/docs/resume-here.md)
  and the [qualification matrix](firmware/docs/qualification-matrix.md).
- **Rig hardware and setup:** [`firmware/docs/hil-rig.md`](firmware/docs/hil-rig.md).
- **This document's job:** the order of work and the autonomy layer (test tiers, rig, agent
  loop). It does not repeat gate thresholds.

## 1. Where the work stands (2026-11-22)

| Item | State | Evidence |
|---|---|---|
| Boards on hand | **Arrived.** No H0 or H1 record exists yet | `README.md`, `resume-here.md` (7df8115) |
| Board-to-board link (D1) | **Decided: wired I2S + UART inside the enclosure.** Every external link is Bluetooth | The owner's hand-off keeps ADR-0010 |
| Firmware | Two ESP-IDF apps and a portable C core. BUILD PASS only (August; Docker-pinned IDF) | E18-esp32 report |
| Provenance | Resolved: the inputs at `e7f8180` match the historical `b608dc3` checkpoint, and fresh builds record their own commit | `resume-here.md` |
| Python tooling | `arrival.py`: `new-run`, `record`, `finalize`, `ports`, `capture`, `flash-command`, `bundle`. **10/10 tests pass on this PC** | `py -3 -m unittest discover -s firmware/tools/tests -p 'test_*.py'` (run 2026-11-22) |
| C host tests | Not runnable here: no cmake or compiler | — |
| Device observability | **Free-text `ESP_LOGI` only.** No stats records, no console commands, no test tone or tap, no test image | grep of `firmware/*/main` |
| HIL runner, fake peers, phone harness | **Not started.** The hand-off lists the HIL runner and the qualification serial interface as open work | `resume-here.md` |
| Behavior contract (Pi policies ported to C) | Not started. The output board has a basic peer policy; the S31 has simple reconnect requests | `output_policy.c`, `bt_gateway.c` |
| This PC | Missing Docker, ESP-IDF, esptool, pyserial, adb, sigrok, cmake, and a compiler | `command -v` checks (2026-11-22) |
| Experiment-number collision | Still open: `E18-esp32-prehardware-port.md` sits beside `E18-fifine-k054-compat.md` | `docs/experiments/` |

### 1.1 How this changes the revision 1 plan

- **D1 is settled (D1-A).** The BLE link experiment (E22) drops out of scope.
- **The separate S31 toolchain spike becomes part of H1.** The first S31 boot of the existing
  image already shows whether Classic HFP + A2DP initialize on `v6.1-rc1`. A separate
  example-build spike, or a move to v6.1.x, is needed only if H1 shows a Bluetooth init failure.
  A toolchain change also counts as an ADR-level decision.
- **The real Pixel comes before a fake Pixel.** A small harness app on the Pixel provides calls
  that dial no one, and ADB provides media and Bluetooth on/off control. Together these cover H8,
  H9, H10, and H11 without building a Linux HFP Audio Gateway, which was the riskiest piece of
  rig software. A fake AG becomes an optional later add-on (§3, R4).
- **The real Lark comes before the fake Lark.** The real Lark already covers H6's 30-minute
  capture. The Pico emulator is needed only for 100/100 hot-plug runs, negative-descriptor tests,
  and deterministic AEC stimuli, so it moves later.
- **Observability is the first code task.** Every later autonomous gate depends on
  machine-readable device output.

## 2. Forward plan

Each stage lists its least-cost capable route: `opus` (Claude Opus 5.5), `sonnet`
(Claude Sonnet 5.5), or `haiku` (Claude Haiku 5.5). Use a route only if that model is available to
the session; otherwise run on the current model.

Stage A and Stage B start together. Track B1 (host-only) can run in a sibling worktree at any time.

### Stage A: Bench bring-up with the existing tooling (one bench session)

1. **Install the host toolchain** (`hil-rig.md` §4): Docker Desktop or pinned native ESP-IDF,
   esptool, pyserial, Android platform-tools, sigrok-cli, and CMake + Ninja + a C compiler.
2. **Build the baseline.** Run `build_firmware.py host`, then `output`, then `s31`. Record the
   manifests. This gives HOST PASS and BUILD PASS at a clean, fresh commit.
3. **H0 (person + agent).** A person takes photos and checks the antenna path and the 32UE
   module. The agent records the run with `arrival.py new-run --gate H0`.
4. **H1 (agent).** Flash each board on its own with nothing else attached, then capture the boot
   logs.
   - The S31 log decides Classic Bluetooth support on `v6.1-rc1`. A Bluetooth init failure is
     **kill trigger K1** (§3).
5. **Harness check (person).** Do the unpowered continuity checks from `wiring.md` and leave the
   harness connected.

- **Exit:** H0 and H1 recorded, the S31 Bluetooth verdict known, and the harness verified.
- **Route:** `sonnet` (a K1 diagnosis: `opus`).

### Stage B: The observability layer (code; starts in parallel with A)

The deliverable is the device contract in `hil-rig.md` §6:

- **Telemetry and commands.** JSON-lines events plus a 1 Hz `stats` record on each board's USB
  console, and console commands (`status`, `selftest`, `tone`, `tap`, `fault`, `peers`).
- **Test images.** Test hooks are compiled only into `-test` images, which the manifests mark.
- **Stage-isolation tones.** Tone generators at S31 USB-in, HFP-in, A2DP-in, and I2S-out, and at
  the output board's I2S-in and A2DP-out.
- **`rig/esp/hil.py` v0.** It wraps `arrival.py` with a rig lease, app-only flashing, console
  capture, reducers that reuse `rig/analysis`, a `firmware/qual/gates.toml` registry, and a
  per-run verdict written to `docs/experiments/results/E21/…`.
- **E-number fix.** Renumber the ESP series from E18 to E21, update the paths in the matrix and
  runbooks, and keep a pointer stub at the old name.

- **Exit:** a T2 smoke test (flash → boot JSON matches the manifest) passes on both boards, run
  by the agent with no person present.
- **Route:** `sonnet`.

### Track B1: Behavior contract (host only, parallel, any time)

Port the proven Pi policies into pure C state machines in `firmware/common`, with table-driven
tests derived from the Pi pytest cases:

- **Reconnect and bond repair:** the 8 s + 12 s windows; cancel and retry once; the
  profile-ready definition; the 15 s cooldown; `manual_hold` on a double Disconnect; bond
  deletion only on the exact four-step `InProgress` signature; the rollback-safe repair window.
- **Phone transport states** (ADR-0009).
- **Readiness.**
- **Output selection** with a power-safe A/B NVS commit.

Write `firmware/docs/behavior-contract.md`, mapping each policy to its Pi source, its owner board,
and its test or hardware gate.

- **Exit:** every contract row has a T0 test or names its HIL gate.
- **Route:** `sonnet` implements; `opus` reviews.

### Stage C: Two-board gates with no phone (fully autonomous once A and B are done)

- **H2:** link protocol. Fault hooks inject garbage and duplicates, and the logic analyzer
  decodes the UART.
- **H3:** reset either board with RTS while the other runs, and measure the heartbeat mute.
- **H4:** I2S. The analyzer decodes the S31's tone pattern.
- **H5:** 30 minutes of the S31 tone through I2S → 32UE → **fake car** (the rig Pi's onboard
  Bluetooth as an A2DP Sink) to confirm the FIFO stays bounded and the ppm correction stays within
  ±500.
- **H7, T3 part:** the 32UE pairs and reconnects with the fake car. The fake car sends AVRCP
  commands through BlueZ.

- **Exit:** H2, H3, H4, H5, and the H7 fake-car part pass at T3.
- **Route:** `sonnet` (stack or timing faults: `opus`).

### Stage D: The S31 USB and Pixel side

1. **H6 with the real Lark:** 30 minutes of capture, with zero transfer errors and the format
   verified from `stats` and `tap`.
2. **H8, real Pixel media:** ADB plays a test file, the path runs S31 → I2S → 32UE → fake car,
   and the Pi's recording is scored for continuity and latency. AVRCP from the fake car must
   change the Pixel's media-session state, read with `dumpsys media_session`.
3. **H9, real Pixel call:** the harness app runs a self-managed ConnectionService "call" that
   dials no one. It plays a known downlink stimulus, which is captured at the fake car and at the
   S31 tap. The uplink must be digital silence in the normal image and carry the Lark stimulus in
   the test image. The eSCO counters are compared with the Pi baseline.
4. **H11:** ten harness calls interrupt fake-car media. Check for no stale media and a restore
   within 15 s.
5. **Hardware rows of the contract:** ADB switches Pixel Bluetooth off and on, the Pixel moves
   out of range, and a double Disconnect is sent. The bond is never erased except in a dedicated
   pairing gate.

- **Exit:** H6, H8, H9, and H11 pass. H7 passes with a real speaker.
- **Route:** `opus` for Bluetooth and USB-stack faults; `sonnet` for campaigns.

### Stage E: Lark uplink and AEC (H10)

1. **Build the Pico fake Lark** (`rig/esp/fake-lark/`). Spike S3 decides between a custom UAC1
   class driver and TinyUSB's support. It presents the real Lark's descriptors, plays stimuli
   chosen over its control UART, and can unplug, replug, apply rate skew, and present descriptor
   variants. This also completes H6's 100/100 hot-plug runs and its negative tests.
2. **T3:** uncorrelated near-end speech from the Pico and far-end speech from the harness. The
   agent sweeps the reference delay and runs a bounded ESP-SR parameter grid, scoring with
   `rig/analysis/aec_metrics.py`: at least 10 dB suppression and at most 0.01% clipping.
3. **T4, the acoustic fixture:** the far end plays from the real Bluetooth speaker, which is the
   echo source. Near-end speech plays from the rig's instrument speaker. Run three calibrated
   runs.

- **Exit:** H10 passes, and only the qualified image publishes the processed uplink.
- **Route:** `opus` tunes; `haiku` runs the sweeps.

### Stage F: Robustness and endurance

- **H14:** the full fault matrix. It includes Pico unplugs, resets, Pixel Bluetooth toggles,
  malformed frames, and FIFO limits forced with Pico rate skew. Hard power cuts need the
  switchable hub (`hil-rig.md` §2, optional items).
- **H15:** ten resumable cycles.
- **H16:** an overnight 3,600 s active-call soak.
- **H13:** RF cells. A person moves the antenna for each cell and the agent runs the cell.

- **Exit:** H13 bench cells done; H14, H15, and H16 pass.
- **Route:** `haiku` (failures: `opus`).

### Stage G: Human acceptance and release

- **Acceptance:** CR-V pairing, steering controls (H12), and calls; in-car RF; and the vehicle
  supply (H17), which is a T5 gate.
- **Release:** the Bluetooth control channel for the companion app; release bundles; docs and
  ADR updates.
- **Route:** `sonnet`.

## 3. Kill triggers and pivots

| ID | Trigger | Response |
|---|---|---|
| K1 | The S31 cannot initialize Classic HFP-HF and A2DP Sink together on `v6.1-rc1` | Rebuild on the newest v6.1.x. If that also fails, choose (your call) between waiting for IDF and the **role swap**: the 32UE faces the Pixel and the S31 keeps USB, AEC, and A2DP to the car, with full-duplex I2S |
| K2 | The Lark fails UAC on the S31 | Stop at H6 and report. No workarounds around a broken transport |
| K3 | RF failure at the 30 cm baseline | Firmware or stack fault. A failure only in the near field means enclosure or antenna work |
| K4 | ESP-SR misses 10 dB after the bounded sweep | Uplink publication stays locked. Porting WebRTC AEC is your call |
| R4 | The Pixel is unavailable for unattended runs | Add a fake Pixel AG (oFono + phonesim on the rig Pi, or a spare classic ESP32 running IDF `hfp_ag`) for the call-side gates that don't need the phone |

## 4. The agent loop and rules

```text
select  next gate in gates.toml whose prerequisites passed
change  smallest fix; T0 + T1 must pass before any hardware time
flash   changed board only, app partition only (bonds/NVS kept)
T2      boot JSON == manifest, else stop
run     hil.py run <gate>  (under the rig lease)
reduce  raw → metrics.json → PASS | FAIL | INCONCLUSIVE
record  docs/experiments/results/E21/<UTC>-<gate>/ ; commit validated checkpoint
```

Tier rules carried over:

- A lower tier never satisfies a higher-tier gate.
- A gate must pass T3 before it runs at T4.
- Every verdict names the exact firmware, IDF, lock, harness, and rig revision.

Stop and escalation rules:

- After two fix cycles on the same failure, summarize and escalate.
- Stop on any of these:
  - a reset loop or brownout;
  - an unknown USB device;
  - a rig self-test failure;
  - an unrequested bond change;
  - any dial attempt.
- Never erase bonds outside pairing gates. Never tap an Android pairing dialog without approval
  in that session.
- Steps that need a person go into `firmware/qual/human-queue.md`, and the agent continues on
  other gates.
- Only one agent holds the rig lease. Other agents stay at T0/T1 in sibling worktrees.
- Agents read reduced verdicts, never raw logs.
