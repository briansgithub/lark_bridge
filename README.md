# rpi-lark-bridge

Abrupt-power-loss image preparation and the manual 50-cut acceptance campaign are
documented in [`docs/power-loss-hardening.md`](docs/power-loss-hardening.md).

Make a **Hollyland Lark A1** USB-C lavalier microphone work as the preferred call microphone on a
**Google Pixel 7a** (Android 14), with a wired **FIFINE K054** as the fallback and call audio
routed somewhere else entirely.

Android will not treat the Lark's receiver as a communication headset, because it enumerates as a
**capture-only** USB Audio Class device with no playback endpoint. This project builds an external
bridge that presents Android with a device it *will* accept — either a Bluetooth HFP headset or a
full-duplex USB headset — while splitting the microphone and playback sides behind the scenes.

```
  Lark A1 ──USB──┐
                 ├──► Raspberry Pi 3B ──┬── Bluetooth HFP ──► Pixel 7a  (Mode 1 / 1W)
  FIFINE K054 ───┘                      └── Pi Pico ──USB──► Pixel 7a   (Mode 2)
```

**Current branch status (`codex/fifine-k054-compat`, 2026-08-25): implementation complete; FIFINE
field qualification pending.** Ordered resolution and fail-closed Lark/FIFINE switching pass the
host suites, and a read-only live resolver preflight selected the attached Lark while reporting the
attached K054 as a native mono S16LE/48 kHz usable fallback with no USB serial. The Pi still runs
the prior E17 baseline release described below; production promotion remains blocked on E18 field
QA. The single-controller implementation binds the Pixel call role to the USB-BT500 by permanent address,
USB VID/PID, and current sysfs identity, and routes call output only to the Pi AUX sink. The Pixel
is paired, bonded, and trusted specifically beneath that controller, and the two-minute
AEC-disabled HFP/SCO transport gate passed. With AEC restored, objective AEC measurements passed:
two acceptance-eligible double-talk cycles reached 16.09 dB and 12.69 dB suppression, while two
echo-only diagnostics reached 37.47 dB and 37.95 dB. The user then stopped and deferred the
remainder of the original five-cycle gate after provisionally accepting AEC. The exact
`c63c823ab9d6dfa1837b05517f903d25c6b5c96a` release is now installed in the immutable lower root.
Its corrective reboot passes the strict `CALL_DOWN` readiness baseline with AUX verified at `0.85`,
only the exact USB-BT500 controller present, and every required service at zero restarts. The
immutable lower root and boot filesystem are read-only, and the retained Netplan fast path produced
an 18.132-second boot. Only two of five qualifying double-talk cycles are complete; a fresh
Pixel-dependent call and the final 3,600-second active-call soak remain deferred, so this branch is
not a fully qualified release. Bluetooth speaker output, BT600, and steering-wheel control
forwarding are deliberately deferred. See
[`docs/BRINGUP-REPORT.md`](docs/BRINGUP-REPORT.md) for what is proven and what broke;
[`docs/experiments/E17-bt500-aux-fast.md`](docs/experiments/E17-bt500-aux-fast.md) for this branch;
[`PLAN.md`](PLAN.md) for the architecture.

---

## Hardware

| Item | Notes |
|---|---|
| Raspberry Pi 3 Model B v1.2 | 1 GB RAM, onboard BCM43438 Bluetooth, 4× USB 2.0, Ethernet |
| ASUS USB-BT500 | Required call controller for the current branch (`0b05:1bf6`) |
| Raspberry Pi Pico (RP2040) | USB device capability; presents as the USB headset in Mode 2 |
| Hollyland Lark A1 | USB-C receiver, capture-only UAC device |
| FIFINE K054 | Wired USB-A gooseneck, capture-only mono fallback; field qualification pending |
| Google Pixel 7a | Android 14 |
| AUX speaker | Pi 3.5 mm output; Harmony boombox is the current fixture |
| Bluetooth headphones or car stereo | A2DP sink, for Mode 1 |

Wiring for the Pi↔Pico link is in [`docs/hardware/wiring-pi-pico.md`](docs/hardware/wiring-pi-pico.md).
**Read [`docs/hardware/power.md`](docs/hardware/power.md) before first power-on** — there is one way
to power the Pico that back-feeds the phone, and it is not obvious.

## Operating modes

| Mode | Microphone path | Call audio out | Radio does | Status |
|---|---|---|---|---|
| **1** Bluetooth bridge | preferred Lark, K054 fallback → Pi → HFP → Pixel | A2DP car stereo | HFP + A2DP | **Deferred on this branch.** No Bluetooth-output claim |
| **1W** Bluetooth + wired | preferred Lark, K054 fallback → Pi → HFP → Pixel | Pi 3.5 mm jack | USB-BT500 HFP only | Lark baseline persisted; K054 field qualification pending |
| **2** USB headset bridge | selected microphone → Pi → Pico → Pixel | Pixel → Pico → Pi → any sink | nothing | Independent track |
| **3** Diagnostics | raw devices exposed | — | — | Always available |

The current target is intentionally narrower than the later dual-controller experiments: the
onboard controller is retained only as rollback until USB-BT500 qualification passes, and output
is wired AUX. See `PLAN.md` §1.4 and
[`E17`](docs/experiments/E17-bt500-aux-fast.md).

## Choosing call output

> The selector below documents the broader project interface. The current BT500+AUX release profile
> fixes output to wired AUX; A2DP discovery/switching and the phone-side selector are not acceptance
> claims for this branch.

The selector accepts a list number, friendly-name fragment, or canonical id. A live choice is
kept in RAM so changing it cannot churn persistent storage. Add `--remember` only when that
choice should also become the next-boot default; it commits through the hardened image's
checksummed A/B configuration slots without restarting an active call.

```bash
rig/rig output
rig/rig output set boombox
rig/rig output set boombox --remember
rig/rig output set wired --remember
```

The Android companion exposes the same candidates under **Where call sound plays** and adds a
**Call speaker** Quick Settings tile. It talks to `output_remote.py` over the already-paired phone
Bluetooth link, so it needs neither Wi-Fi nor an account. The Pi validates every id and performs
the same `--remember --no-chime` transaction; the app is a thin control surface, not a second copy
of selection state.

## Microphone priority

Microphone order is configuration, not enumeration order: the Lark A1 is selected whenever it is
uniquely usable; otherwise the bridge falls back to the K054. A higher-priority ambiguous match
holds the uplink safe instead of silently choosing a different microphone. Inspect the live choice
and every candidate with:

```bash
python3 pi/bridged/bridgectl.py microphone list
python3 pi/bridged/bridgectl.py microphone status --json
```

The K054 implementation uses its verified native S16LE/48 kHz/mono mode, but physical controls,
replacement-unit identity, acoustic performance, and AEC endurance remain field-QA gates; see
[`E18`](docs/experiments/E18-fifine-k054-compat.md).

## Pixel connection and pairing repair

The call watchdog connects the configured Pixel through the USB-BT500 immediately at boot. It may
replace a bond automatically only after the exact repeated-`InProgress` stale-key signature; an
out-of-range phone or disabled phone Bluetooth never causes bond deletion. Inspect the live bond,
repair deadline, reconnect timing, and instructions with:

```bash
bridgectl phone status
bridgectl phone status --json
```

If status reports `pairing_required`, run `sudo bridgectl phone repair`, then tap **LarkBridge
BT500** and approve pairing on the Pixel within 120 seconds. See
[`docs/bluetooth-reconnect-policy.md`](docs/bluetooth-reconnect-policy.md) for the rollback and
exact-device safety rules.

## Repository layout

| Path | Contains |
|---|---|
| `PLAN.md` | The specification. Architecture, risks, milestones, test matrix, acceptance criteria. |
| [`docs/BRINGUP-REPORT.md`](docs/BRINGUP-REPORT.md) | **Start here.** What is proven on real hardware, what broke, and the traps. |
| `docs/` | Architecture, ADRs, hardware, operations, and **experiment reports with raw data** |
| [`docs/operations/connecting.md`](docs/operations/connecting.md) | How to reach the Pi over the router or a direct cable, and the traps in both. |
| [`docs/operations/microphones.md`](docs/operations/microphones.md) | Ordered microphone identity, failover, status, deployment, and qualification. |
| `pi/` | Daemon (`bridged`/`bridgectl`), PipeWire/WirePlumber/BlueZ config, systemd, udev, DT overlays |
| `pico/` | RP2040 firmware. PlatformIO is the primary build; CMake is the CI reference build. |
| `config/` | User configuration (device MACs, mode, gains) + JSON schema |
| `scripts/` | Provisioning, pairing, recovery, log collection |
| `tests/` | One directory per validation stage A–I; each emits a machine-readable `result.json` |
| `tools/` | Audio analysis, Bluetooth capture/parsing, PipeWire graph inspection |

## Getting started

The rig drives the Pi over SSH and the phone over ADB. The original one-radio S3 result remains
the negative control; E15 contains the two-radio continuation:

```bash
sudo ./tests/stage-b-hfp/s1-sco-over-hci.sh        # Does SCO reach the host at all on this radio?
./tests/stage-b-hfp/s2-hfp-hf-role.sh              # Can the Pi be an HFP Hands-Free unit for the Pixel?
./tests/stage-e-concurrent/s3-coexistence-smoke.sh # Can one radio carry HFP + A2DP?
```

Each writes a report into `docs/experiments/` and raw evidence into `docs/experiments/results/`.
Read `PLAN.md` §8 before running them — the pass/fail branches matter more than the scripts.

The BT500+AUX campaign has a resumable control plane. A cycle is never credited unless it proves
the exact controller binding, AEC graph and timing, zero new transport errors, fresh call teardown
and rejoin, and at least 10 dB measured suppression:

```bash
python -m rig.bt500_aux baseline
python -m rig.bt500_aux cycle --campaign artifacts/bt500-aux/campaign-...
python -m rig.bt500_aux campaign --campaign artifacts/bt500-aux/campaign-...
python -m rig.bt500_aux soak --campaign artifacts/bt500-aux/campaign-...
python -m rig.bt500_aux collect --campaign artifacts/bt500-aux/campaign-...
```

The current live record contains two such acceptance-eligible cycles, not five. Echo-only captures
are retained as useful diagnostics but are not credited as double-talk acceptance cycles. The
pre-persistence soak attempt is retained as not-started evidence rather than a pass or failure;
the exact corrective release has since passed immutable installation and reboot readiness. Full
qualification still requires a fresh post-reboot Pixel call and the complete 3,600-second
active-call soak.

## Development

```bash
make help          # list targets
make lint          # shellcheck, ruff, black --check, clang-format, dtc syntax
make test-host     # pytest + Pico host unit tests. No hardware required.
make pio           # regenerate committed .pio.h headers from .pio sources
make pico          # build firmware (PlatformIO)
make pico-cmake    # build firmware (CMake reference build)
```

Firmware sources are **Arduino-API-free plain C** so both build front-ends compile identical code —
see [ADR-0008](docs/architecture/decisions/ADR-0008-platformio-arduino-pico-build.md). On Windows,
enable Win32 NTFS long paths or the arduino-pico package install will fail.

## License

MIT — see [`LICENSE`](LICENSE). Third-party components and their licenses are listed in
[`third_party/NOTICE.md`](third_party/NOTICE.md).
