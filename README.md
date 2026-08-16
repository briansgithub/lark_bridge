# rpi-lark-bridge

Make a **Hollyland Lark A1** USB-C lavalier microphone work as the call microphone on a
**Google Pixel 7a** (Android 14), with call audio routed somewhere else entirely.

Android will not treat the Lark's receiver as a communication headset, because it enumerates as a
**capture-only** USB Audio Class device with no playback endpoint. This project builds an external
bridge that presents Android with a device it *will* accept — either a Bluetooth HFP headset or a
full-duplex USB headset — while splitting the microphone and playback sides behind the scenes.

```
  Lark A1 ──USB──► Raspberry Pi 3B ──┬── Bluetooth HFP ──► Pixel 7a      (Mode 1 / 1W)
                                     └── Pi Pico ──USB──► Pixel 7a       (Mode 2)
```

**Status: pre-implementation.** The architecture, risks and milestones are specified in
[`PLAN.md`](PLAN.md). Three hardware spikes (S1–S3) gate everything else — see
[Getting started](#getting-started).

---

## Hardware

| Item | Notes |
|---|---|
| Raspberry Pi 3 Model B v1.2 | 1 GB RAM, onboard BCM43438 Bluetooth, 4× USB 2.0, Ethernet |
| Raspberry Pi Pico (RP2040) | USB device capability; presents as the USB headset in Mode 2 |
| Hollyland Lark A1 | USB-C receiver, capture-only UAC device |
| Google Pixel 7a | Android 14 |
| USB audio dongle | Cheap CM108-class UAC1 output — **recommended**, see Mode 1W |
| Bluetooth headphones or car stereo | A2DP sink, for Mode 1 |

Wiring for the Pi↔Pico link is in [`docs/hardware/wiring-pi-pico.md`](docs/hardware/wiring-pi-pico.md).
**Read [`docs/hardware/power.md`](docs/hardware/power.md) before first power-on** — there is one way
to power the Pico that back-feeds the phone, and it is not obvious.

## Operating modes

| Mode | Microphone path | Call audio out | Radio does | Status |
|---|---|---|---|---|
| **1** Bluetooth bridge | Lark → Pi → HFP → Pixel | A2DP headphones/car | HFP **+** A2DP | Gated on spike S3 |
| **1W** Bluetooth + wired | Lark → Pi → HFP → Pixel | USB DAC / 3.5 mm jack | HFP only | **Recommended default** |
| **2** USB headset bridge | Lark → Pi → Pico → Pixel | Pixel → Pico → Pi → any sink | nothing | Independent track |
| **3** Diagnostics | raw devices exposed | — | — | Always available |

Mode 1W exists because putting HFP/eSCO and A2DP on the Pi 3's single Broadcom radio is the
project's biggest unknown. 1W removes that risk from the critical path, is roughly 200 ms lower
latency on the audio you hear, and differs from Mode 1 by one config string. See `PLAN.md` §1.4.

## Repository layout

| Path | Contains |
|---|---|
| `PLAN.md` | The specification. Architecture, risks, milestones, test matrix, acceptance criteria. |
| [`docs/BRINGUP-REPORT.md`](docs/BRINGUP-REPORT.md) | **Start here.** What is proven on real hardware, what broke, and the traps. |
| `docs/` | Architecture, ADRs, hardware, operations, and **experiment reports with raw data** |
| `pi/` | Daemon (`bridged`/`bridgectl`), PipeWire/WirePlumber/BlueZ config, systemd, udev, DT overlays |
| `pico/` | RP2040 firmware. PlatformIO is the primary build; CMake is the CI reference build. |
| `config/` | User configuration (device MACs, mode, gains) + JSON schema |
| `scripts/` | Provisioning, pairing, recovery, log collection |
| `tests/` | One directory per validation stage A–I; each emits a machine-readable `result.json` |
| `tools/` | Audio analysis, Bluetooth capture/parsing, PipeWire graph inspection |

## Getting started

Nothing here is built yet. The first work is **three timeboxed spikes**, because two of them can
change the shape of the project and both are answerable in a day:

```bash
sudo ./tests/stage-b-hfp/s1-sco-over-hci.sh        # Does SCO reach the host at all on this radio?
./tests/stage-b-hfp/s2-hfp-hf-role.sh              # Can the Pi be an HFP Hands-Free unit for the Pixel?
./tests/stage-e-concurrent/s3-coexistence-smoke.sh # Can one radio carry HFP + A2DP?
```

Each writes a report into `docs/experiments/` and raw evidence into `docs/experiments/results/`.
Read `PLAN.md` §8 before running them — the pass/fail branches matter more than the scripts.

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
