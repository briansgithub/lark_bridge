# D5 ESP-SR AEC research, 2026-11-24

The boot-loop image faults while storing through a null pointer returned by a
PSRAM-only allocation in ESP-SR 2.5.1. The board has PSRAM, but the crash images
ran with PSRAM disabled. The two trials keep the AEC engine and uplink
publishing off in ordinary images.

## Evidence

- The target is the **ESP32-S31**, a RISC-V chip. The repository builds the
  `esp32s31` target with ESP-IDF `v6.1-rc1`; the linked archive is under
  `espressif__esp-sr/lib/esp32s31`. The board guide lists WROOM-3 with
  16 MB flash and 16 MB PSRAM for the Function-CoreBoard-1.
  [Espressif board guide](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s31/esp32-s31-function-coreboard-1/user_guide.html)
- `d12de50` records the two failures. In
  `../lark_bridge-evidence/20261124-pixel-path/run1-crash/bench.jsonl`,
  HFP opens at 16 kHz, `dl rfft` reports a failed FFT table, and core 0
  takes a store-access fault. The boot probe in `91fa85b` faults on core 1
  before Bluetooth brings up any profiles. The preceding identity line
  reports 232,864 free 8-bit bytes and a largest block of 180,224 bytes.
  The boot-loop capture consistently reports `MEPC=0x400cd508`,
  `MCAUSE=7`, `MTVAL=0`, `S0=0`, and `A0=4`. On this fault
  the store address comes from the saved trap frame and faulting
  instruction sequence. The 4 in the printed A0 is consistent with the
  increment around the faulting store.
- The pinned RISC-V `objdump -dr` places the boot-loop PC at
  `esp_aec3_nlp_init+0x19c`, `fsw fa5,0(a0)`. The bytes extracted at
  the PC from `run2-bootloop/s31.bin` match the archive's initialization
  loop (`22 85 a2 95 1c e1 11 05 e3 1e b5 fe`, with `1c e1` at the
  PC). The loop loads its base from `s0`, which holds the unchecked
  return of `heap_caps_aligned_calloc` at offset `0xc0`. The call loads
  its caps argument with `0x404`, the pinned IDF values for
  `MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT`. The caller's `MALLOC_CAP_8BIT`
  setting does not override that allocation.
- The lockfile pins `dl_fft` 0.6.0. Its `dl_rfft_s16_init` allocates
  aligned twiddle tables in the requested capability class and can log
  the observed warning before returning NULL. The run1 warning does not
  establish the exact allocation that failed there. The raw boot image
  establishes the PSRAM-only failure independently.
- The baseline `sdkconfig` has `CONFIG_SPIRAM` off. There is no external
  heap or SPIRAM malloc policy in use, and Bluetooth's PSRAM-first flag
  is off. FreeRTOS runs two cores. Bluedroid and the controller are pinned
  to core 0. The call-time constructor runs in the unpinned `s31_aec`
  worker (priority 10, 8192-byte stack) and the boot-probe commit placed
  its constructor in the init path. The generated cache values are 16 KiB
  I-cache, 64 KiB D-cache and 64-byte cache lines. The recorded exception
  is a data store to address zero, with no cache-disabled fault. Neither
  trial changes task affinity, stack size, flash instruction placement,
  or XIP settings.
- The application supplies one mic, one reference and one output channel,
  a 16 kHz sample rate, filter length 4, FD low-cost mode and aggressive
  NLP. The FD-low-cost constructor derives a 512-sample frame from
  32 ms at 16 kHz. The app queries it after construction, bounds it to
  2048 samples and allocates aligned PCM buffers afterward. Those app
  buffers cannot cause the observed constructor fault. The pinned
  header and current target documentation describe the mode and API.
  [ESP32-S31 AEC API guide](https://docs.espressif.com/projects/esp-sr/en/latest/esp32s31/acoustic_echo_cancellation/README.html)
- The public ESP-SR changelog lists S31 support in 2.4.4 and no matching
  S31 NLP allocation fix through 2.6.1. The reported 2.5.4 internal-RAM
  regression concerns NSNet3 rather than this minimal AEC entry point.
  A package upgrade needs its own pinned reproduction and qualification.
  [ESP-SR changelog](https://github.com/espressif/esp-sr/blob/master/CHANGELOG.md) · [ESP-SR issue #250](https://github.com/espressif/esp-sr/issues/250)

This is an offline crash replay and a disassembly match. The constructor
links a target-specific precompiled RISC-V archive. An executable host test
would require a faithful model of the S31 allocator and target instructions.

## Candidate order

| Rank | Commit | Trial and prediction |
| --- | --- | --- |
| 1 | `3717506` | **FD with PSRAM.** Apply `sdkconfig.aec-fd`. It initializes octal PSRAM at 40 MHz and registers it with the capability allocator while keeping ordinary malloc internal by default. A boot without registered PSRAM reports D5 and leaves the worker idle. If the missing capability caused the fault, the same FD constructor should return a valid frame size with PSRAM registered. The guard checks heap presence. It cannot make the vendor constructor safe under arbitrary memory exhaustion. |
| 2 | `28b7014` | **Linear-only, internal heap.** Apply `sdkconfig.aec-linear`. The pinned archive sets the nonlinear channel count to zero for `SR_LOW_COST`; the initializer branches around `esp_aec3_nlp_init`. Process with `aec_linear_process`. With PSRAM still off, a successful constructor would confirm that the failing NLP path can be bypassed. This mode has different echo suppression and needs its own qualification. |
| 3 | Deferred | Ask Espressif for allocation checks and a documented capability policy in the S31 archive. Test a newer pinned package only if its matching archive fixes this path. A stack/core change, filtering change, or a larger internal-heap margin cannot supply the hard-coded capability. |

## Build and hardware handoff

Both profiles passed a pinned Docker build at their corresponding commits.
Generated configs confirm the test image and AEC engine are on, while
uplink publishing and bond erase are off. FD has capability-only PSRAM
enabled. Linear has PSRAM disabled. Both generated app offsets are
`0x10000`.

Host artifacts are under `/tmp/lark-d5-build.eNesZc/firmware/s31`.
Use the `larkbridge_s31_gateway.bin` and matching ELF from each build
directory. The harness APK is from `46af695`.

| Artifact | SHA-256 |
| --- | --- |
| `build-fd/larkbridge_s31_gateway.bin` | `55c5e00dd5b13c3deaeed3c3da41a801c5f8c651c0dfd6d79860bf4f77ec29a4` |
| `build-linear/larkbridge_s31_gateway.bin` | `7009e1c98f877c518d5c7941456b9dcf3ccbfc3302b5eac4d9594a3c53bf280e` |
| `rig/esp/pixel-harness/build/larkbridge-harness-debug.apk` | `b002512d45376b9c265ef401db5a4cb076c79b3a9007d437caf7c1a7357f4c53` |

Both profiles apply after `sdkconfig.test`. Build each at its corresponding
commit. From the repository root, with access to the pinned Docker image:

```bash
profile=fd                    # linear for 28b7014
rev=$(git rev-parse --short=12 HEAD)
docker run --rm -v "$PWD/firmware:/work/firmware" -w /work/firmware/s31 \
  -e "PROFILE=$profile" -e "REV=$rev" espressif/idf:v6.1-rc1 bash -ec '
    test "$(git -C "$IDF_PATH" rev-parse HEAD)" = 44f0c59f7c81a72a5868a52d5f6dfbbf88829704
    idf.py --preview -B "build-$PROFILE" -D IDF_TARGET=esp32s31 \
      -D "SDKCONFIG=sdkconfig.$PROFILE.generated" \
      -D "SDKCONFIG_DEFAULTS=sdkconfig.defaults;sdkconfig.test;sdkconfig.aec-$PROFILE" \
      -D "PROJECT_VER=$REV-$PROFILE-test" build
  '
```

1. Keep the known-good AEC-off app available. Use the existing app-only
   procedure with the test build's generated app offset. Preserve NVS and
   the paired phone. Keep `CONFIG_LARKBRIDGE_S31_EXPERIMENTAL_AEC_UPLINK`
   and bond erase off. [Flashing runbook](../docs/flashing.md)
2. Try FD first. Capture boot and console logs, confirm octal PSRAM is
   detected and registered, and record the image version. If PSRAM fails
   to register, the D5 guard should leave Bluetooth usable without starting
   AEC. A call on that image does not test the FD constructor.
3. With the Lark receiver and transmitter active, start a self-managed
   Pixel harness call, explicitly select Bluetooth, and wait for S31 HFP
   audio at 16 kHz. Start the harness voice stimulus for at least 15 s
   so the worker has a playback reference. Keep logging through
   `CALL_END`, then repeat three times without rebooting. Save the
   first `D5: AEC create` line, frame-size line, processed-frame and
   max-process-us telemetry, and any FFT warnings or reset trace.
   A successful trial needs constructor and frame processing on every call,
   and digital-silence uplink with publishing locked.
4. Flash the linear image and repeat the same sequence. Expect mode 0 and
   zero PSRAM in its D5 telemetry. If FD fails with registered PSRAM,
   retain both the app ELF and the full panic for address decoding.
   Restore the known-good AEC-off app if either image reboots.
5. The harness APK can also be installed as an in-place update. Its signing
   certificate matches the existing hardware-checkout APK. For H9 capture,
   keep the harness activity visible, grant microphone permission, and set
   `--ez record_uplink true` on `CALL_START`. Confirm
   `uplink.routed_input.type=BLUETOOTH_SCO` and compare the reported
   continuity, RMS and frequency against the controlled Lark stimulus.
   Both D5 trials should still give a silence baseline.
   [Harness H9 measurement instructions](../../rig/esp/pixel-harness/README.md#h9-uplink-measurement)
