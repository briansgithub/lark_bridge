# ESP firmware

This directory contains the two-board LarkBridge port. It is intentionally separate from the
proven Raspberry Pi implementation under `pi/`.

| Directory | Target | Responsibility |
|---|---|---|
| `common/` | native + both chips | Protocol, rings, PCM, rate control, route FSM |
| `s31/` | ESP32-S31-Function-CoreBoard-1 | LARK USB, Pixel HFP/A2DP/AVRCP, AEC, coordinator |
| `output/` | ESP32-DevKitC-32UE | I2S receive, A2DP source, AVRCP target |
| `tests/host/` | native | Hardware-free unit and integration tests |
| `tools/` | host Python | Build/flash/monitor and evidence collection |

The exact upstream pins are in `toolchain.lock.json`. Do not build S31 from a floating ESP-IDF
branch: it remains a preview target. Generated `dependencies.lock` files belong in Git after the
first successful component resolution.

## Audio and control links

- I2S: S31 master TX to output ESP32 slave RX, 48 kHz, S16 little-endian stereo, Philips format.
- UART: 115200 8N1 initially, COBS frames terminated by `0x00`, CRC-16/CCITT-FALSE.
- Audio is never transported over UART.

See `docs/architecture/decisions/ADR-0010-esp32-dual-radio-bridge.md` and
`firmware/docs/wiring.md` before connecting hardware.

## Build status vocabulary

- `HOST PASS`: native core tests passed.
- `BUILD PASS`: the firmware compiled against the pinned toolchain.
- `PENDING HARDWARE`: no physical claim has been made.
- `HIL PASS`: the named hardware test produced retained evidence and passed its gate.

`BUILD PASS` must never be shortened to `PASS` in reports.
