# E18 — ESP32 dual-radio pre-hardware port

**Status:** PRE-HARDWARE COMPLETE — BUILD-READY / `PENDING HARDWARE`
**Started/completed:** 2026-08-25
**Branch:** `codex/esp32-dual-radio-port`
**Firmware source checkpoint:** `b608dc3a3cec883501c4ab55bdee87586e751128`

## Question

How much of the Raspberry Pi LarkBridge can be ported, reviewed, compiled, and fault-tested before
the ordered ESP32-S31 and ESP32-DevKitC-32UE arrive?

## Result

The pre-hardware port is complete. The repository now has two real, independently buildable
ESP-IDF applications, a shared portable protocol/audio core, deterministic two-endpoint fault
tests, pinned toolchains and managed components, fail-closed AEC publication, evidence tooling,
standalone flash bundles, and a first-arrival qualification procedure.

This is not a hardware pass. No USB, Bluetooth, RF, acoustic, latency, power, vehicle-control, or
endurance claim is made. Every physical H0–H17 gate remains `PENDING HARDWARE`.

## Fixed architecture

ADR-0010 is accepted. The S31 owns the LARK and Pixel-facing HFP-HF/A2DP-Sink/AVRCP-Controller
profiles. The legacy ESP32 owns the downstream A2DP-Source/AVRCP-Target link. I2S carries only
48-kHz S16 stereo audio; framed UART carries only control and telemetry. Route generation fencing,
session-scoped replay protection, heartbeat muting, bounded clock-drift correction, and atomic
AVRCP taps are implemented on both endpoints.

The S31 runs the pinned ESP-SR AEC worker, but processed HFP uplink publication is disabled in the
normal image. Initialization, starvation, route/session loss, queue overrun, or an unqualified AEC
path produces digital silence; raw LARK audio is never the fallback.

## Evidence table

| Gate | Evidence | Verdict |
|---|---|---|
| Isolated Git worktree | `B:\Desktop\W\Hardware_write\larkbridge-esp32`, named branch | PASS |
| Historical BT600 plan retained | `archive/bt600_plan*.md`; original also retained at `H:\Downloads\bt600_plan.md` | PASS |
| Toolchain/component pins | output IDF `v6.0.2`; S31 IDF `v6.1-rc1`; UAC `1.5.0`; ESP-SR `2.5.1` | PASS |
| Architecture/protocol contracts | ADR-0010, protocol-v1, wiring, arrival and AEC runbooks | PASS |
| Native Windows campaign | four C tests, including 2,000 deterministic protocol packets | PASS |
| Native Linux sanitizer campaign | four C tests under ASan + UBSan | PASS |
| Operator/build tooling | ten Python unit tests | PASS |
| Output exact-IDF build | clean manifest at source checkpoint; 1,044,608-byte app | BUILD PASS |
| S31 exact-IDF build | clean manifest at source checkpoint; 1,339,408-byte app | BUILD PASS |
| Output standalone flash bundle | 12 embedded entries; CRC, size, SHA-256, and bundle checksum verified | PASS |
| S31 standalone flash bundle | 13 embedded entries; CRC, size, SHA-256, and bundle checksum verified | PASS |
| Workflow definition | YAML parsed; actions and IDF inputs pinned to commits | PASS (remote run not performed) |
| LARK USB enumeration/capture | delivered S31 and receiver required | PENDING HARDWARE |
| Pixel HFP/A2DP/AVRCP | delivered S31 and live Pixel session required | PENDING HARDWARE |
| Car/speaker A2DP/AVRCP | delivered output board and sink required | PENDING HARDWARE |
| Objective AEC | complete acoustic loop required | PENDING HARDWARE |
| RF, power, cycles, and soak | complete production topology required | PENDING HARDWARE |

## Commands actually run

```powershell
py -3 firmware/tools/build_firmware.py host
py -3 -m unittest discover -s firmware/tools/tests -p 'test_*.py' -v
py -3 firmware/tools/build_firmware.py output --no-pull
py -3 firmware/tools/build_firmware.py s31 --no-pull
py -3 firmware/tools/arrival.py bundle --target output
py -3 firmware/tools/arrival.py bundle --target s31
```

The local host campaign passed 4/4 tests. The Python campaign passed 10/10 tests. Both ESP builds
verified the exact IDF commit before compiling and wrote manifests with
`source_commit=b608dc3a3cec883501c4ab55bdee87586e751128` and `source_dirty=false`.

The portable C campaign was also rebuilt inside the pinned Linux container with the default
ASan/UBSan flags:

```powershell
$lbFirmware=(Resolve-Path firmware).Path
docker run --rm -v "${lbFirmware}:/work/firmware" -w /work/firmware `
  espressif/idf:v6.0.2 /bin/bash -lc `
  'cmake -S . -B /tmp/larkbridge-host -G Ninja && cmake --build /tmp/larkbridge-host && ctest --test-dir /tmp/larkbridge-host --output-on-failure'
```

That campaign passed 4/4 tests with no sanitizer report. The result schema JSON and workflow YAML
also parsed successfully, and `git diff --check` was clean.

## Retained build identities

| Target | App SHA-256 | App partition headroom | Bundle | Bundle SHA-256 |
|---|---|---:|---|---|
| output ESP32 | `00b09924e6a7716b2295b477c8b0f2b45fb9631ce8a2979bf8deec00c035d104` | 50% | `larkbridge-output-b608dc3a3cec.zip` | `2ecf4948f866831700b70ee40237d8ab24bdc4801786203a6dfe061a95e4337d` |
| S31 gateway | `dc976558c037469391aee5d39889b8e7eeb5834897195cdfa973e768c8b99793` | 13% | `larkbridge-s31-b608dc3a3cec.zip` | `5cf6c47bba74d0a1f2b01c24045fb170fd78b0d7b8b6a85bea8e677027f100aa` |

The ignored local bundles are under `firmware/dist/`; their adjacent `.sha256` files contain the
same bundle hashes. The build manifests retain hashes for the bootloader, partition table, flash
arguments, application, ELF, and map. Bundles can be regenerated only from a matching clean
checkpoint if the ignored copies are unavailable.

## Remaining qualification and risks

- S31 silicon/IDF and its Bluetooth/AEC support remain preview or preliminary.
- Output static DRAM is 81.8% used, leaving 22,677 bytes; actual minimum heap is a mandatory gate.
- S31 has only 13% application-partition headroom; later dependency upgrades need a size review.
- LARK UAC descriptors, HFP mSBC behavior, Pixel profile coexistence, downstream AVRCP behavior,
  clock-drift convergence, antenna isolation, and shared-power integrity are unmeasured.
- H10 must objectively qualify AEC before processed microphone audio can be published.
- The 5–10 minute iteration soak does not satisfy release. The separate 3,600-second active-call
  production-topology soak remains mandatory.

On arrival, start at `firmware/docs/resume-here.md`, stop at the first failed physical gate, and
retain the incomplete evidence rather than tuning around a broken transport.

## Rules

- A simulator or build result cannot satisfy a USB, Bluetooth, RF, acoustic, latency, or durability
  gate.
- Never score a dirty-source manifest or an AEC qualification image as the normal release image.
- Preserve the original Pi branch and evidence as the rollback/reference implementation.
