# E16 — phone-driven output discovery baseline and contract

Baseline recorded 2026-08-24. The baseline capture was read-only: no pairing,
scanning, connection, deployment, reboot, or Pixel capture was performed. The
Pi implementation described in the status section below was completed later
from host fixtures only and likewise did not operate live Bluetooth hardware.

The implementation contract below was recorded later the same day. It fixes the
product, protocol, transaction, cleanup, UI, and acceptance decisions for the
implementation pass. The Pi status section distinguishes host-implemented and
host-tested behavior from Android and hardware acceptance that remain open.

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

## Pi implementation status — 2026-08-24

The Pi half of this contract is implemented in the checkpoint containing this
update. No Android repository was modified and no command was deployed to or run
against the Pi, Pixel, speaker controller, or a real Bluetooth device.

Implemented behavior:

- `btadapters.py` now owns the fixed 12-second BR/EDR discovery session, filters
  timestamped BlueZ monitor events to the explicit adapter object, retains the
  strongest RSSI, and guarantees `scan off`, monitor/owner process-group reap,
  temporary `NoInputNoOutput` agent removal, explicit-path `Pair`,
  `RemoveDevice`, trust writes, and A2DP-only `ConnectProfile` cleanup.
- `outputs.py` adds the public `setup_state`, makes a wrong-controller bond
  visible but unavailable/nonselectable/nonroutable, preserves valid offline
  sticky selection, and shapes deterministic E16 discovery results including
  confidence and duplicate-name discriminators.
- `output_remote.py` preserves `list`/`status`/`set`, adds `call_active`, and
  implements correlated progress plus final framing, one in-memory 60-second
  scan token, strict identifier/MAC/controller validation, all seven
  `pair_select` phases, new-bond-only rollback, and save ordering of pairing
  seal → A/B config → runtime desire → supervisor route confirmation.
- `bridgectl.py` refuses `needs_setup`, never falls back from a permanent
  controller address to `hciX`, preserves the dedicated controller when wired
  is selected, and exposes exact-byte A/B config restoration for transaction
  rollback. `bt_watchdog.py` shares the advisory radio lock and skips a busy
  speaker page without spending an attempt or moving its retry deadline; call
  recovery remains outside that lock.

Exact host validation from the Windows checkout, using
`B:\Desktop\W\Hardware_write\rpi_lark_mic_bridge\.venv\Scripts\python.exe`:

- `make test-py` could not start because GNU Make is not installed in this
  checkout environment (`make` was not recognized). Its exact Python
  constituents were run individually:
  - from `pi/bridged`, `python -m pytest tests -q` → **133 passed, 5 subtests
    passed**;
  - `python -m unittest discover -s rig/boot/tests -p 'test_*.py'` → **10
    passed**;
  - `python -m unittest discover -s pi/powerloss/tests -p 'test_*.py'` → **15
    passed, 1 skipped**;
  - `python -m unittest discover -s rig/powerloss/tests -p 'test_*.py'` → **2
    passed**;
  - `python -m unittest discover -s scripts/powerloss/tests -p 'test_*.py'` →
    **2 passed**.
- Ruff 0.16.3 on the five changed Pi modules and their six focused test files →
  **all checks passed**.
- After the final root/user lock-file open-order hardening,
  `pytest tests/test_btadapters.py tests/test_bt_watchdog.py -q` → **21 passed**;
  scoped Ruff and mypy for those two modules also passed.
- Mypy 2.3.1 with `--follow-imports=skip` on `bt_watchdog.py`,
  `btadapters.py`, `outputs.py`, `output_remote.py`, and `bridgectl.py` →
  **success, no issues in 5 source files**. The repository Make constituent
  (`mypy bridge_supervisor.py bt_watchdog.py`) still reports the pre-existing,
  untouched `bridge_supervisor.py:730` optional-value warning on this Windows
  platform.
- Black 26.5.1 check-only was run without rewriting files. It requests
  reformatting for both touched files and untouched files read directly from
  the contract commit (including `bridge_supervisor.py`, `bt_watchdog.py`, and
  `outputs.py`), so this workspace/tool-version formatting mismatch remains
  recorded rather than expanding E16 into a repository-wide rewrite.

Hardware/Pixel acceptance, BlueZ command-output confirmation on version 5.82,
live RFCOMM cancellation timing, reboot/power-cut persistence, and all Android
UI/tile/permission work remain explicitly unvalidated here.

## Decision-complete implementation contract

### Product boundary and controller identity

The Pixel is a remote control, not the Bluetooth scanner or pairing host. The Pi
performs inquiry, pairing, service resolution, A2DP connection, PipeWire
verification, selection, and persistence. Android therefore needs no
`BLUETOOTH_SCAN` permission; `BLUETOOTH_CONNECT` remains the only Bluetooth
runtime permission used by this feature.

The configured `[devices.output].adapter` value is mandatory for `scan` and
`pair_select` and is a permanent, uppercase controller address, never an `hciX`
name. At the beginning of each operation the Pi resolves that address to the
current BlueZ adapter object. It does not cache the `hciX` result across
operations and never falls back to the BlueZ default adapter, the first USB
adapter, or the controller that owns the Pixel bond. A missing, unpowered,
rfkilled, BlueZ-invisible, or accidentally call-owning configured controller is
`speaker_adapter_unavailable`. Recovery may power/unblock exactly the resolved
speaker controller; it may not reset or operate on the call controller.
This field identifies the appliance's dedicated speaker radio independently of
the selected output: selecting a wired output must preserve it. Older
configurations without the field may still list/set existing wired outputs but
cannot scan or pair until the controller address is configured.

The permanent-address rule applies to every device path used by the new flow:
discovery is started on the resolved speaker adapter path, pairing creates or
uses `/<speaker hci>/dev_<MAC>`, trust is pinned there, and A2DP is connected on
that exact object. No new flow may use an address-only `bluetoothctl connect`,
`Device1.Connect`, or any operation whose adapter is implicit.

Remembered wired outputs and bonded Bluetooth outputs are returned immediately
by `list`; opening the screen or refreshing it never starts inquiry. Inquiry is
only caused by the explicit **Scan** action. Discovery itself never pairs,
trusts, connects, selects, writes configuration, or changes the remembered
preference.

### Transport compatibility and framing

The RFCOMM UUID, Android-server/Pi-client roles, 64 KiB line limit, UTF-8 JSON
Lines framing, and request `id` correlation remain unchanged. `list`, `status`,
and `set` retain their request names and all current response fields:
`outputs`, `desired_id`, `chosen_id`, `reason`, `accepted_id`,
`accepted_label`, and `message` keep their current meanings and types. New
fields are additive, so an installed older Android client continues to work.
`status` remains an alias of `list`.

Two safety narrowings apply to existing operations:

- Each public output gains additive `setup_state`, with `ready` for wired and
  A2DP bonds on the configured speaker controller and `needs_setup` for a bond
  that exists only on another controller. A wrong-controller entry is reported
  `available=false` even if BlueZ says that other-controller bond is connected.
- Existing `set` continues to select wired and `ready` remembered outputs exactly
  as before. It refuses `needs_setup` without changing trust, connection, route,
  runtime desire, or durable configuration; setup must go through `pair_select`.
  The supervisor likewise never resolves a wrong-controller bond to a playable
  candidate. This is a safety check, not an alternate pairing path.

`list`/`status` also add top-level `call_active`, defined as the supervisor's
`call.hfp_nodes_present` boolean. It is advisory UI state, not a scan gate.

Normal operations still produce one final response object. Long `scan` and
`pair_select` operations may first produce progress objects with the same
request `id`:

```json
{"id":12,"event":"progress","done":false,"phase":"scanning","elapsed_ms":3000}
```

A progress object has `event="progress"`, `done=false`, and one of the phases
defined below. It is not a command result. Each accepted long operation emits
exactly one final object with `done=true` and `ok=true` or `ok=false`. Existing
operations need not add `done`. The Android reader consumes progress objects
until the final object for its outstanding command; commands remain strictly
one-at-a-time on a connection, and the Pi emits no unsolicited object when no
command is outstanding.

New-operation failures have both a stable machine field and human text:

```json
{"id":12,"done":true,"ok":false,"error_code":"stale_result","error":"Scan results expired. Scan again.","phase":"validating"}
```

`error_code` is stable; `error` is displayable and may become more specific.
Malformed legacy requests and unknown operations retain the existing error
shape and are not forced into the new code set.

### `scan` operation

The request has no caller-supplied adapter, duration, transport, or filter:

```json
{"id":12,"op":"scan"}
```

After resolving and exclusively locking the configured speaker controller, the
Pi performs one **12,000 ms BR/EDR-only inquiry**. LE-only observations are
excluded. The implementation uses the already-installed BlueZ command-line and
D-Bus facilities (`bluetoothctl`/`busctl`) from Python's standard library; it
does not add a Python package. The discovery owner is a long-lived child/session
bound to the explicitly selected controller. It applies a BR/EDR discovery
filter, starts discovery, observes BlueZ device additions/property changes for
that controller for the fixed window, and stops discovery in `finally`.

A result is eligible only if that MAC generated an observation event on the
speaker controller after this operation successfully started discovery and
before its 12-second deadline. Pre-existing BlueZ objects, remembered bonds,
and results from an earlier inquiry are not included merely because they are in
`GetManagedObjects`; a remembered device that is actually observed in this
window is included. The Pi may re-read an observed object's final properties
after the window to build the response. The strongest (least negative) RSSI
observed during the window is retained.

Starting a new scan invalidates the previous scan token immediately. A completed
scan is kept only in `output_remote` process memory under a cryptographically
random URL-safe `scan_id`. It is valid for exactly 60 seconds starting at scan
completion. A failed or cancelled scan leaves no valid token. Expiry is checked
using monotonic time; epoch timestamps in the response are informational.

The successful final response adds the normal current output state and:

```json
{
  "id": 12,
  "done": true,
  "ok": true,
  "scan_id": "opaque-url-safe-token",
  "started_at_ms": 1787600000000,
  "completed_at_ms": 1787600012000,
  "valid_until_ms": 1787600072000,
  "duration_ms": 12000,
  "results": [
    {
      "output_id": "a2dp:C9:5C:FD:6E:28:46",
      "label": "Boombox",
      "rssi_dbm": -57,
      "audio_confidence": "confirmed",
      "setup_state": "ready",
      "duplicate_name_discriminator": null
    }
  ]
}
```

Result fields are fixed as follows:

- `output_id` is the future stable output ID, `a2dp:` plus the canonical
  uppercase public device address. It is also the selection key; no separate
  positional index is accepted.
- `label` is BlueZ `Alias`, then `Name`, then `Bluetooth device <last two
  octets>` as fallback, trimmed to non-empty display text.
- `rssi_dbm` is the strongest integer RSSI observed in dBm, or JSON `null` when
  BlueZ supplied no RSSI. Known RSSI sorts strongest first; ties sort by
  case-folded label and then output ID.
- `audio_confidence` is `confirmed` when the speaker-controller Device1 UUIDs
  already contain A2DP Sink `0000110b-0000-1000-8000-00805f9b34fb`, `likely`
  when its BR/EDR Class-of-Device has Audio/Video major class or Rendering
  service class but the UUID is not yet present, and `unknown` otherwise. This
  is presentation guidance only. Only post-pair service resolution authorizes
  connection.
- `setup_state` is `ready` only when a paired speaker-controller bond already
  advertises A2DP Sink; otherwise it is `needs_setup`. Thus a device bonded only
  on the call controller is visibly `needs_setup`, as is a new unpaired device.
- `duplicate_name_discriminator` is JSON `null` unless two results have the
  same trimmed, case-folded `label`. Every member of such a duplicate group gets
  its final two uppercase address octets (for example `28:46`). Android renders
  this as `Boombox · 28:46`; it never silently merges equal names.

Scan emits `phase="scanning"` progress at start and approximately once per
second, including `elapsed_ms` and `duration_ms`. A call does not prohibit the
operation; the Android warning policy below controls explicit consent.

### `pair_select` operation

Selecting a discovery result is a separate explicit action:

```json
{"id":13,"op":"pair_select","scan_id":"opaque-url-safe-token","output_id":"a2dp:C9:5C:FD:6E:28:46"}
```

For a device that is not already `ready` on the speaker controller, both
`scan_id` and `output_id` must match the current unexpired in-memory result.
Missing, superseded, expired, malformed, or mismatched results fail
`stale_result` before any Bluetooth or persistent mutation. Expiry is checked
when the operation is accepted; the transaction may finish after the 60-second
deadline. A failed transaction may be retried with the same result while it is
still valid. A live paired speaker-controller bond that currently advertises
A2DP Sink may be passed by `output_id` without a `scan_id`; this makes retry
after a lost final RFCOMM reply safe and idempotent. No other unscanned target is
accepted.

The operation holds the same exclusive speaker-radio transaction lock as scan
and executes these phases in order:

1. **`validating`** — resolve the configured controller again, validate the
   token or already-ready bond, snapshot whether a paired bond existed on that
   controller, and snapshot the current runtime desire, chosen route, active
   configuration, and durable output choice. A `new_bond` is one for which
   `Paired=true` did not exist on the speaker controller at this point, even if
   an unpaired discovery object or an old bond on another controller existed.
2. **`pairing`** — when no speaker-controller bond exists, register exactly one
   temporary BlueZ `NoInputNoOutput` agent and call Pair for the explicit target
   object. The pairing deadline is 45 seconds. Ordinary speaker/headset Just
   Works authorization is accepted. Any PIN, passkey entry/display, or numeric
   comparison requirement is declined as `pin_not_supported`; it is not relayed
   to Android. Timeout, disappearance, cancellation, or failure to reach
   `Paired=true` is `pairing_timeout` unless a PIN/passkey requirement was
   positively observed. Pairing may create the transient ACL needed by BlueZ,
   but this phase never calls a general profile connect.
3. **`resolving_services`** — after Pair returns, wait up to 15 seconds for the
   explicit Device1 UUID list to settle. A2DP Sink UUID `0000110b-...` is
   mandatory. Class-of-Device, name, a generic Audio/Video class, HFP/HSP UUIDs,
   and a PipeWire node are not substitutes. Absence of A2DP Sink is
   `not_audio_output`.
4. **`pinning_trust`** — set `Trusted=true` on the validated
   speaker-controller bond first. Only after that succeeds, set `Trusted=false`
   on duplicate bonds for the same device on every other controller, including
   the call controller. Do not remove those bonds. If target trust cannot be
   established, fail `connection_failed` without untrusting the other bonds.
5. **`connecting`** — call `Device1.ConnectProfile` on the explicit
   speaker-controller object with only remote A2DP Sink UUID `0000110b-...` and
   a 45-second deadline. Never call `Device1.Connect`, and never request HFP,
   HSP, AVRCP, or another profile. A ConnectProfile error or failure to observe
   `Connected=true` is `connection_failed`.
6. **`waiting_for_audio`** — for up to 10 seconds, poll PipeWire for a
   `bluez_output` node matching the target MAC whose
   `api.bluez5.profile` is `a2dp-sink`. An arbitrary Bluetooth node or the
   Pixel's HFP node does not qualify. Timeout is `connection_failed`.
7. **`saving`** — only now may the user-visible choice change. Immediately seal
   the valid live BlueZ pairing state through the existing pairing snapshot
   facility, then commit `[devices.output]` through the existing checksummed A/B
   `config-write` path, including the target output ID, device address,
   permanent speaker-controller address, mode `bluetooth`, fallback enabled,
   and reconnect enabled. After those durable writes succeed, atomically write
   the runtime desire. Wait up to 3 seconds for supervisor status to report both
   `desired_id` and `chosen.id` equal to the target. Pairing seal, configuration,
   runtime desire, or route-confirmation failure is `persistence_failed`; the
   old runtime desire and durable configuration must be restored before the
   failure response.

The success response is emitted only after all seven phases and contains
`accepted_id`, `accepted_label`, the refreshed normal output state, and
`setup_state="ready"`. The previous output link is left in place until the
supervisor performs its existing verified link switch. The old output bond and
connection are not explicitly disconnected or removed after success; only its
audio route ceases to be selected.

### Stable error meanings and rollback

The complete stable code set for the new operations is:

| Code | Meaning | Retry/UI direction |
| --- | --- | --- |
| `stale_result` | Scan token/result is absent, superseded, expired, malformed, or mismatched, and the target is not an already-ready speaker-controller bond. | Discard displayed discovery results and offer Scan again. |
| `pairing_timeout` | The selected device did not complete Just Works pairing in 45 seconds, disappeared, or pairing was cancelled without a positive PIN/passkey signal. | Keep old choice; ask the user to return the device to pairing mode and rescan/retry while valid. |
| `pin_not_supported` | Pairing required PIN entry, passkey entry/display, or numeric comparison. | Explain that only automatic Just Works speakers/headsets are supported. |
| `not_audio_output` | Resolved services did not advertise A2DP Sink. | Keep old choice and remove the result from the actionable list. |
| `speaker_adapter_unavailable` | The configured permanent speaker controller cannot safely perform the operation. | Keep old choice; show a Pi/speaker-radio availability error. |
| `connection_failed` | Trust pinning, A2DP-only connection, connected-state verification, or A2DP PipeWire node creation failed. | Keep old choice; permit retry. |
| `persistence_failed` | Pairing seal, A/B config commit, active config mirror, runtime desire commit, route confirmation, or required rollback failed. | State that setup was not selected and surface service/repair guidance. |

Before pairing, the operation records all pre-existing bonds for the target on
all controllers. It never removes any of them. It removes the explicit
speaker-controller device object only when this transaction created that bond
and the transaction then fails in `pairing` or `resolving_services`, including
`pairing_timeout`, `pin_not_supported`, and `not_audio_output`. It never removes
a pre-existing speaker-controller bond. Once a new bond has passed A2DP service
validation, it is useful and is retained on later trust, connection, node, or
persistence failure so a retry does not require pairing again.

Trust pinning occurs only after A2DP validation. Consequently pairing and
validation failures leave old trust flags untouched. After validation, a
wrong-controller bond is preserved but untrusted even if a later connection or
persistence phase fails; this prevents it from ever becoming a call-controller
route. All failure paths leave the pre-operation chosen audio route, runtime
desired ID, and durable configured choice unchanged (or restore their exact
snapshots before replying). A connected but unselected validated target may
remain connected after a later failure; it receives no bridge audio link.

### Process lifetime, cancellation, and concurrency

There is at most one scan or pair/select transaction in the Pi process and at
most one outstanding Android command on the RFCOMM stream. A process-wide mutex
and an advisory lock at `/run/user/<uid>/bridge-output-radio.lock` cover the
entire controller operation. Android disables competing actions while busy and
does not enqueue a second long operation. Repeated `list` refreshes wait behind
the active command rather than observing a half-committed transaction.

`bt_watchdog` participates in the advisory lock for speaker reconnect only. If
scan or pair/select holds it, the watchdog skips the speaker page without
consuming an attempt or advancing its retry deadline. Call-controller probes
and recovery remain independent and continue. If a call-controller recovery
restarts BlueZ and invalidates the speaker operation, that operation cleans up
and returns `speaker_adapter_unavailable` or `connection_failed` according to
the phase; it never retargets to the call controller.

The discovery owner and pairing agent are separate child process groups. The
discovery child lives only for the scan, and `StopDiscovery` is attempted in a
`finally` block only for discovery this operation started. The pairing agent is
registered immediately before Pair and unregistered immediately after Pair; it
is never left as the default agent during service resolution, connection, idle
RFCOMM time, or another scan. Normal cleanup asks a child to exit, waits at most
2 seconds, then terminates/kills its process group. No discovery, monitor,
agent, pairing, or connection child may survive the request or service.

Progress writes act as RFCOMM liveness checks during long operations. If the
RFCOMM stream is lost, the current operation is cancelled: discovery stops and
its result cache is discarded; pairing/validation children stop and the
stage-appropriate new-bond rollback runs. If the commit completed before the
socket loss, the successful route and durable choice stand—a missing final
acknowledgement is recovered by reconnecting and listing state, not by undoing
a committed user choice. Reissuing `pair_select` is safe because a committed
target is then an already-ready bond.

`SIGTERM`, `SIGINT`, service stop, and Python exceptions use the same cleanup
and rollback path as RFCOMM loss. The service closes its RFCOMM socket to wake a
blocked read, stops discovery, unregisters the agent, reaps all children,
releases locks, and only then exits. Systemd `Restart=always` reconnects after
the existing delay. Scan result tokens are intentionally lost on process
restart.

### Android state and interaction contract

The full app keeps two separate collections: remembered `outputs` from
`list`/`status`, and ephemeral discovery `results` from the last successful
scan. Remembered outputs render immediately and stay visible during scan and
setup. A visible **Scan for speakers** button is the only scan entry point.

Android maps protocol progress to these states: `idle`, `scanning`, `pairing`,
`resolving_services`, `pinning_trust`, `connecting`, `waiting_for_audio`, and
`saving`. The corresponding copy is respectively a 12-second countdown,
`Pairing…`, `Checking audio support…`, `Securing speaker radio…`,
`Connecting A2DP…`, `Waiting for audio…`, and `Saving choice…`. Remembered
selection remains checked until success; an in-progress discovery result gets a
spinner, not the selected radio button. Cancel/back may leave the screen but
does not claim success; RFCOMM loss invokes Pi cancellation as specified above.

When `call_active=true`, tapping Scan first presents a modal warning:

> Scanning during a call can briefly interrupt sound to your Bluetooth output.
> Continue only if a temporary breakup is okay.

Actions are **Cancel** and **Scan anyway**. Only the affirmative action sends
`scan`. When `call_active=false`, Scan starts directly. The Pi does not reject a
scan merely because a call begins after the last status snapshot; this is an
explicit informed-warning policy, not a hard interlock.

Discovery rows show label, RSSI (or `Signal unavailable`), confidence
(`Audio output`, `Likely audio output`, or `Audio support unknown`), and setup
state. Duplicate labels append the supplied discriminator. `ready` rows say
`Ready on LarkBridge`; `needs_setup` rows say `Needs setup on LarkBridge` even
when Android itself has a bond of the same name. Tapping a discovery row is the
explicit selection and invokes `pair_select`; the app never asks Android to
bond with that speaker. On success, discovery results clear, a fresh `list` is
rendered, and the newly remembered target becomes selected. On failure, the old
remembered selection stays checked and the stable-code guidance above is shown.

No manifest or runtime request for `BLUETOOTH_SCAN`, location, or nearby-device
scanning is added. Android only reads/writes the already bonded Pi RFCOMM socket.

The Quick Settings output tile is deliberately narrower than the full app. It
may refresh and cycle only remembered outputs that are `setup_state=ready` and
`available=true`; it never scans, pairs, selects a discovery result, selects a
`needs_setup` bond, or bypasses the in-call warning. While scan or pair/select is
busy, a tile tap performs no output mutation and its subtitle is `Open app to
finish speaker setup`. With no ready alternative it refreshes state and directs
the user to the app.

### Acceptance criteria

#### Unit and host integration

- All existing Pi output/output-remote tests continue to pass unchanged, and
  tests assert byte-compatible field names/types and unchanged behavior for
  valid `list`, `status`, and `set` requests.
- Pi unit tests use fake monotonic time and BlueZ/PipeWire fixtures to prove the
  exact 12-second BR/EDR window, observed-during-this-scan filtering, strongest
  RSSI, deterministic sort, 60-second expiry/supersession, stable output IDs,
  confidence states, setup states, and duplicate discriminators.
- Controller tests prove scan, Pair, RemoveDevice, trust, and ConnectProfile all
  use the object path resolved from the configured permanent speaker address;
  absence/misconfiguration never falls back to the call/default controller.
- Protocol tests cover progress/final framing and every stable error code.
  `stale_result` is proven to execute no Bluetooth or persistence command.
- Pair transaction tests cover unpaired Just Works success, already-ready
  idempotent success without a scan token, wrong-controller-only bond setup,
  PIN/passkey refusal, pair timeout, missing A2DP UUID, target-trust failure,
  A2DP-only connect failure, wrong-profile/missing PipeWire node, pairing-seal
  failure, config failure, route-confirmation failure, and RFCOMM loss at every
  phase.
- Rollback assertions prove all pre-existing bonds survive; only a newly
  created speaker-controller bond is removed on pairing/A2DP-validation
  failure; a validated bond survives later failure; old call-controller bonds
  are untrusted only after target validation; and old runtime/durable choices
  are byte-for-byte unchanged on failure.
- Cleanup tests prove StopDiscovery, agent unregister, child process-group reap,
  lock release, and shutdown/RFCOMM cancellation. Watchdog tests prove a scan or
  pair operation suppresses only speaker reconnect without spending its retry
  budget, while call-controller probing still runs.
- Android tests cover additive response parsing, all progress states, retained
  remembered selection during setup, call-warning Cancel/Scan-anyway behavior,
  duplicates, stable-code messages, stale-result rescan, and tile restrictions.
  A manifest assertion proves `BLUETOOTH_SCAN` and location permissions are
  absent.
- Host integration uses a scripted BlueZ command/D-Bus harness and a fake
  PipeWire graph to run the complete transaction, including a second bond for a
  wrong-controller device and persistence rollback, without real radio
  mutation. No new Pi Python dependency is installed or declared.

#### Hardware and Pixel

- With the Pixel bonded on the call controller and an ordinary Just Works
  speaker in pairing mode, the app first lists remembered outputs without radio
  inquiry. One explicit scan lasts 12 seconds, finds only devices observed by
  the configured speaker controller, and causes no Pair, trust, A2DP transport,
  PipeWire speaker node, desired-output change, or persistent write.
- Selecting that result creates/uses the bond on the speaker controller,
  resolves A2DP Sink, trusts only that controller, connects only A2DP, exposes
  the matching `a2dp-sink` PipeWire node, switches the route, and reports success
  only after the durable choice is visible in status. HCI/BlueZ evidence shows
  no inquiry, page, bond, or audio profile for the target on the Pixel call
  controller and no HFP/HSP connection to the speaker.
- During a live call, Cancel in the warning performs no scan. Scan anyway runs
  on the speaker controller, and any temporary Bluetooth-output breakup is
  visibly acknowledged; the call controller, Pixel bond, SCO/uplink, old route,
  and remembered choice survive. The test records whether output audio breaks
  and confirms it recovers without a reboot.
- A duplicate-name pair of devices renders distinct discriminators and selecting
  either routes to the chosen MAC. A non-audio BR/EDR device fails
  `not_audio_output`; a legacy PIN/passkey device fails `pin_not_supported`;
  neither changes the route or durable choice.
- A device bonded only on the call controller is listed `needs_setup`, is never
  selectable through legacy `set` or the tile, and `pair_select` creates a
  separate speaker-controller bond. The old bond remains present and becomes
  untrusted; the speaker controller owns A2DP and the call controller never
  routes it.
- Pulling the speaker dongle, losing RFCOMM, stopping the service, or restarting
  BlueZ in each long-operation phase leaves no scan/agent child and satisfies
  the specified bond and selection rollback. The Bluetooth watchdog does not
  page the old desired speaker during the exclusive scan/pair transaction.

#### Persistence and restart

- Immediately after success, the live BlueZ tree and the newly sealed pairing
  snapshot contain the speaker-controller bond, and the selected A/B config slot
  plus active `bridge.toml` contain the target ID, target address, and permanent
  controller address. Storage health remains `READY` and persistent.
- After orderly reboot and after the established power-cut test procedure, all
  pre-existing phone/speaker bonds still exist, the wrong-controller duplicate
  remains untrusted, the new speaker reconnects only on the dedicated
  controller when available, and `desired_id`/`chosen_id` return to the new
  output (or the documented wired fallback while it is absent) without Pixel
  interaction.
- Fault injection at pairing seal, config-slot write, active mirror rename,
  runtime desire write, and supervisor confirmation returns
  `persistence_failed`. The pre-operation durable config and route survive; a
  validated new bond may remain for retry, but it is not silently selected.
- A power cut during scan or before A2DP validation cannot replace the old
  durable choice or remove an old bond. A power cut after the success response
  restores the new bond and choice; there is no acknowledged-success window in
  which only RAM knows the selection.
