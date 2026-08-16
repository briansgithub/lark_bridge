# ADR-0008 — PlatformIO + arduino-pico is the primary firmware build; CMake is the reference build

- **Status:** Accepted
- **Date:** 2026-08-15
- **Supersedes:** the pico-sdk/CMake-only assumption in the original `PLAN.md` §4.6
- **Relates to:** `PLAN.md` §7.3, §11, risks R15, R16

## Context

The developer works in **VS Code with PlatformIO** and asked for that toolchain. The original plan
assumed pico-sdk + CMake + `pioasm` + a pinned TinyUSB submodule, because the firmware needs:

1. **Full control over static USB descriptors** — they are the contract with Android and the single
   most uncertain interface in the project (ADR-0005).
2. PIO assembly for the I2S slave, plus DMA chaining and dual-core.
3. A TinyUSB revision containing **PR #1802**, which fixes the RP2040 `uac2_headset` hang
   (issue #2838).

Surveying PlatformIO's RP2040 options as of 2026-08:

| Option | Verdict |
|---|---|
| `wizio-pico` (`framework = baremetal`, real pico-sdk) | **Rejected.** Bundles pico-sdk **1.4.0**; the author has publicly deprioritised maintenance. Cannot satisfy requirement 3. |
| Official `platform-raspberrypi` + Arduino mbed core | Rejected. Heaviest framework, least SDK access. |
| `maxgerhardt/platform-raspberrypi` + arduino-pico (earlephilhower) | **Viable.** Actively maintained, sits on pico-sdk 2.x, gives full `hardware/pio.h`, `hardware/dma.h`, dual-core via `setup1()/loop1()`, and TinyUSB via `-DUSE_TINYUSB`. |

The residual problem: arduino-pico's USB-audio support is its weak spot
([arduino-pico#2707](https://github.com/earlephilhower/arduino-pico/issues/2707), closed with no
documented fix), and the community's working UAC2 path builds descriptors **at runtime** via a large
third-party library — precisely the thing requirement 1 forbids. TinyUSB also arrives vendored inside
`Adafruit_TinyUSB_Arduino` rather than as a submodule we pin.

## Decision

**Keep PlatformIO as the primary, daily-driver build. Make the firmware source build-system-agnostic
so the choice is reversible at any moment.**

1. All firmware logic lives in **Arduino-API-free plain C11** under `pico/src`, `pico/usb`,
   `pico/i2s`. `#include <Arduino.h>` anywhere in those trees is a **lint failure** (`make lint-c`).
2. `pico/pio_main.cpp` is a ~15-line PlatformIO-only shim: `setup()` → `bridge_main_core0()`,
   `setup1()`/`loop1()` → core 1. No logic. Excluded from the CMake build.
3. `pico/platformio.ini` is the primary build; `pico/CMakeLists.txt` is the CI reference build and
   escape hatch. **Both build in CI on every push**, so neither can rot.
4. USB descriptors are **static arrays** registered through a custom TinyUSB class driver
   (`usbd_app_driver_get_cb`). No runtime descriptor synthesis, no descriptor-building library.
5. `.pio` sources are assembled offline by `make pio`; the generated `i2s_slave.pio.h` is
   **committed**, carries a generated-file banner, and CI (`make pio-check`) fails on any drift.
6. **M9.1 is a toolchain gate:** verify the bundled TinyUSB contains PR #1802 before writing
   descriptor code. Record core version + TinyUSB SHA in `docs/experiments/E05`.

## Consequences

- The developer gets the requested workflow: open `pico/`, press Build/Upload, use the serial monitor.
- If the Arduino core fights the descriptors at M9.3, the fallback is **changing a build file**, not
  rewriting firmware. That is the entire point of items 1–3, and it converts risk R15 from
  project-shaping to merely annoying.
- Two build systems must be kept green. Accepted cost; CI enforces it rather than discipline.
- The committed `.pio.h` is a generated artifact in version control, which is normally a smell. It is
  justified here because PlatformIO cannot run `pioasm`, and it is defended by `make pio-check`
  (risk R16).
- Windows developers must enable Win32 NTFS long paths or the arduino-pico package install fails.
  Noted in `README.md` and checked by `docs/development/setup.md`.

## Alternatives considered

- **`wizio-pico` baremetal.** The only PlatformIO route to a raw pico-sdk build; pico-sdk 1.4.0 and
  stalled maintenance make it unusable for requirement 3.
- **CMake only, VS Code + official Raspberry Pi Pico extension.** Best pure technical fit and would
  have been the choice absent the tooling constraint. Retained as the CI build so this remains a
  one-line switch rather than a migration.
- **Adopt `arduino-audio-tools` for UAC2.** Rejected: it synthesises descriptors at runtime, which
  makes the Android contract un-diffable and un-reviewable, and it pulls a large dependency into the
  most safety-critical interface in the project.
