# E16 — phone-driven output discovery baseline

Baseline recorded 2026-08-24. This is a read-only starting point for the next
implementation pass; no pairing, scanning, connection, deployment, reboot, or
Pixel capture was performed.

## Source checkpoints

| Repository | Branch | HEAD |
| --- | --- | --- |
| Pi | `claude/mode1-bluetooth-output` | `d4ea16e81ae429be748e1cf680524b5fe06986b2` |
| Android | `codex/larkbridge-control` | `4c73ad67b14dd67ab823daa35bbe45119cea2dab` |

Recent Pi commits are `d4ea16e Keep speaker recovery independent of call radio`,
`105be47 Keep active config aligned with durable output`, and `30c949 Add phone
control for durable output selection`. The Android HEAD adds LarkBridge call
speaker control.

## Existing protocol and selection behavior

`pi/bridged/outputs.py` is read-only candidate enumeration. Wired ALSA sinks are
first-class candidates. Bonded devices advertising A2DP Sink UUID `0000110b`
are included even when offline; stable IDs are `wired:<PipeWire node>` and
`a2dp:<uppercase MAC>`. Duplicate bonds across adapters collapse to one entry,
preferring a connected adapter, then the configured speaker adapter, then the
lowest adapter path. Resolution never rewrites the desired ID: an unavailable
desired output temporarily falls back (wired-first in the current mode).

`pi/bridged/output_remote.py` and Android `BridgeOutputController` use paired
Bluetooth RFCOMM, UUID `6e0e6e72-3f13-4f7e-9d3f-87b6f5a43c11`, one JSON object per
line. Android serves `list`/`status`/`set`; the Pi remains the source of truth.
`set` invokes `bridgectl output set ... --remember --no-chime`, so it is the
mutating path and was not exercised here. The Android side only accepts a bonded
peer whose name contains `larkbridge`.

## Live Pi read-only snapshot

- `bluetoothctl --version`: **5.82**. `bluetoothd -v` is not on PATH.
- Controllers and stable mappings:
  - `hci0` UART, address `B8:27:EB:43:8D:51`, name `larkbridge-v2`, Broadcom,
    HCI 4.1. This is the call-radio controller.
  - `hci1` USB, address `A0:AD:9F:73:6C:24`, name `larkbridge #2`, Realtek,
    HCI 5.4. This is the currently connected speaker controller.
  - Both were powered and running; `hci1` is the default controller.
- Bonded output candidates from live `bridge-status.json`:
  - `a2dp:C9:5C:FD:6E:28:46`, label Boombox, bonded/trusted/connected, PipeWire
    node `bluez_output.C9_5C_FD_6E_28_46.1`, adapter `hci1`.
  - `a2dp:50:D7:1B:74:34:D6`, label iWorld, bonded/trusted but disconnected,
    adapter `hci0`.
  - `a2dp:98:47:44:CD:73:DE`, label Soundcore Space A40, absent/offline,
    adapter `hci0` (candidate retained in status despite not appearing in the
    unprivileged `bluetoothctl devices` output).
  - Wired candidate `wired:alsa_output.platform-3f00b840.mailbox.stereo-fallback`
    is present and connected.
- Current chosen and desired output are both Boombox; reason is `desired output
  is available`; bridge state is `CALL_DOWN`.
- `bluetoothctl info` confirmed iWorld and Boombox are paired, bonded, and
  trusted; iWorld is disconnected and Boombox connected. No mutating Bluetooth
  command was run.
- Active services include system `bluetooth.service`, `bridge-btwatchdog.service`,
  `bridge-storage-guard.service` (exited successfully), and user
  `bridge-output-remote.service`, `bridge-supervisor.service`, PipeWire, and
  WirePlumber.
- Root is an `overlay` mount (`rw` upper layer); `/var/lib/larkbridge-persist`
  and `/var/lib/bluetooth` are persistent ext4 mounts from `/dev/mmcblk0p3`,
  with `commit=1`. Unprivileged inspection of the immutable recovery Bluetooth
  tree was denied, but the deployed storage guard reports `state=READY`,
  `persistent=true`, `config_slot=a`, `config_source=slot-a`, and
  `pairing_action=live-valid` in `/run/larkbridge/storage-health.json`.

## Baseline checks

- Pi targeted host tests (run from `pi/bridged` with the existing workspace
  Python):
  `B:\Desktop\W\Hardware_write\rpi_lark_mic_bridge\.venv\Scripts\python.exe -m pytest tests/test_outputs.py tests/test_output_remote.py -q`
  → **26 passed in 0.07s**.
- An attempted per-repository venv invocation was unavailable because
  `rpi_lark_mic_bridge-mode1\.venv` does not exist; bare Python also lacks
  pytest. This is an environment limitation, not a production failure.
- Android: `H:\Desktop\widgets\android_lark_a1-larkbridge-control\gradlew.bat test assembleDebug --no-daemon`
  → **BUILD SUCCESSFUL**, 39 actionable tasks; unit-test task reported
  `NO-SOURCE` (there are no JVM unit tests).

## Blockers and constraints for the next agent

- The requested implementation must preserve the two-radio split: call traffic
  is on UART `hci0`; speaker output currently uses USB `hci1`.
- The phone-driven mutating path was intentionally not exercised. The active
  remote service had no verified live phone session in this read-only snapshot;
  any discovery/pairing/selection test requires the Pixel and must be a later
  hardware-authorized step.
- The Pi-side `powerloss_verify.py` is not installed at the guessed
  `/usr/share/rpi-lark-bridge/powerloss_verify.py` path; use the deployed
  `/usr/local/lib/rpi-lark-bridge/powerloss/` tooling or the storage-health JSON
  for future checks.
- Do not assume the status candidate list is the same as unprivileged
  `bluetoothctl devices`: the offline Soundcore entry is retained by the Pi's
  bonded-state enumeration.
