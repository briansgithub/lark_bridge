# Reproducible build, flash, and pairing reset

Use the container builds for CI and artifact creation. Native ESP-IDF checkouts are needed only to
flash and monitor Windows serial ports. Keep the two IDF versions separate.

## Exact toolchains

| Target | ESP-IDF tag | Required commit |
|---|---|---|
| output ESP32 | `v6.0.2` | `7101770dc6db2667b3c477cc31365dd1acd6db4e` |
| S31 gateway | `v6.1-rc1` | `44f0c59f7c81a72a5868a52d5f6dfbbf88829704` |

Example one-time PowerShell setup, replacing the destination roots if necessary:

```powershell
git clone --recursive --branch v6.0.2 https://github.com/espressif/esp-idf.git C:\Espressif\esp-idf-v6.0.2
git -C C:\Espressif\esp-idf-v6.0.2 rev-parse HEAD
C:\Espressif\esp-idf-v6.0.2\install.ps1 esp32

git clone --recursive --branch v6.1-rc1 https://github.com/espressif/esp-idf.git C:\Espressif\esp-idf-v6.1-rc1
git -C C:\Espressif\esp-idf-v6.1-rc1 rev-parse HEAD
C:\Espressif\esp-idf-v6.1-rc1\install.ps1 esp32s31
```

Stop if either printed commit differs from the table. The S31 target and IDF are prerelease; do not
silently replace the RC with a floating branch.

## Build and retain artifacts

From the repository root:

```powershell
py -3 firmware/tools/build_firmware.py host
py -3 firmware/tools/build_firmware.py output
py -3 firmware/tools/build_firmware.py s31
```

Each ESP build writes `build/larkbridge-build-manifest.json` with source state, exact toolchain and
component locks, artifact sizes, and SHA-256 hashes. A hardware result may use only a manifest with
`source_dirty: false`.

The build's `flasher_args.json` and `flash_args` are the authority for offsets. Never reconstruct
offsets from memory. Before arrival, create the per-target ZIP and hash from clean manifests:

```powershell
py -3 firmware/tools/arrival.py bundle --target output
py -3 firmware/tools/arrival.py bundle --target s31
```

Each ZIP contains a generated `FLASH.ps1` and every referenced binary. After extraction and after
activating that target's pinned IDF Python environment, it is independently flashable with
`.\FLASH.ps1 -Port COM_NUMBER`. Dirty-source manifests are refused unless `--allow-dirty` is
supplied explicitly; that override is never acceptable for a scored hardware gate.

## Find ports, flash, and capture boot logs

Leave the boards and LARK disconnected from one another. Connect only the board being programmed.
The output board uses Micro-B; the S31 uses its USB Serial/JTAG Type-C port.

```powershell
py -3 firmware/tools/arrival.py ports
```

If needed, install the serial-only operator dependency with `py -3 -m pip install pyserial`.

Activate the matching native IDF in the same PowerShell and verify its commit again. Have the helper
validate every artifact hash and print the direct esptool command plus monitor command:

```powershell
C:\Espressif\esp-idf-v6.0.2\export.ps1
py -3 firmware/tools/arrival.py flash-command --target output --port COM_OUTPUT
```

For S31, open a fresh PowerShell:

```powershell
C:\Espressif\esp-idf-v6.1-rc1\export.ps1
py -3 firmware/tools/arrival.py flash-command --target s31 --port COM_S31
```

Run the printed lines. Direct esptool flashing avoids reconfiguring the clean Docker-built image;
the final printed `idf.py ... monitor` line only opens the serial monitor.

Use `Ctrl+]` to exit the IDF monitor. The operator helper can print the validated command for a
selected port, and can capture a timestamped serial artifact into an E18 run. Record the COM-port
mapping by unplugging one board and rescanning; port numbers are not permanent.

The boot banner must state project/version, IDF version, chip model/revision/cores, reset reason,
Bluetooth MAC, free/largest heap, and whether bond erase is enabled. Normal images require
`erase_bonds=0`; S31 normal images also require `aec_publish=0` until H10 qualification.

## Exactly one saved peer on each side

Each board deliberately binds one peer address. The output board cannot preserve both a bench
speaker and the final car as selectable peers.

1. Qualify the output board with the bench speaker.
2. Build a one-time output recovery image with
   `CONFIG_LARKBRIDGE_OUTPUT_ERASE_BONDS_ON_BOOT=y`; flash and boot it once with the car and speaker
   out of pairing range.
3. Immediately restore the normal image with that option disabled and confirm `erase_bonds=0` in
   the banner.
4. Pair the car last. It is the final saved output peer.

Use the analogous `CONFIG_LARKBRIDGE_S31_ERASE_BONDS_ON_BOOT=y` recovery image only when replacing
the bound Pixel. Never leave either erase option enabled across ordinary reboots. Store recovery
images separately and label them `ERASE-ON-BOOT — DO NOT DEPLOY`.
