# E18 — ESP32 dual-radio pre-hardware port

**Status:** IN PROGRESS — hardware-dependent results are `PENDING HARDWARE`
**Started:** 2026-08-25
**Branch:** `codex/esp32-dual-radio-port`

## Question

How much of the Raspberry Pi LarkBridge can be ported, reviewed, compiled, and fault-tested before
the ordered ESP32-S31 and ESP32-DevKitC-32UE arrive?

## Fixed architecture

See ADR-0010. The S31 owns the LARK and Pixel-facing profiles. The legacy ESP32 owns the
downstream A2DP/AVRCP link. I2S carries audio; framed UART carries control and telemetry.

## Evidence table

| Gate | Evidence | Verdict |
|---|---|---|
| Isolated Git worktree | branch/worktree recorded in Git | PASS |
| Historical BT600 plan retained | `archive/bt600_plan*.md` | PASS |
| Toolchain and component pins | `firmware/toolchain.lock.json` | PASS |
| Architecture/protocol contracts | ADR-0010 and firmware docs | IN PROGRESS |
| Native shared-core tests | command and result to be recorded | NOT RUN |
| S31 exact-IDF build | command, manifest, artifact hashes | NOT RUN |
| ESP32 exact-IDF build | command, manifest, artifact hashes | NOT RUN |
| Deterministic two-board simulation | command and result | NOT RUN |
| LARK USB enumeration/capture | physical board required | PENDING HARDWARE |
| Pixel HFP/A2DP/AVRCP | physical board required | PENDING HARDWARE |
| Car/speaker A2DP/AVRCP | physical board required | PENDING HARDWARE |
| Objective AEC | complete acoustic loop required | PENDING HARDWARE |
| RF coexistence, cycles, soak | complete topology required | PENDING HARDWARE |

## Rules

- Update this report only with commands that actually ran and artifacts that exist.
- A simulator result cannot satisfy a USB, Bluetooth, RF, acoustic, latency, or durability gate.
- Stop at the first red transport gate when hardware arrives; do not tune AEC around a broken link.
- Preserve the original Pi branch and evidence as the rollback/reference implementation.
