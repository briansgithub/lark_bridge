# E17 — Pi 3 USB-BT500 call controller with wired AUX output

**Status:** provisional live validation; five-cycle gate deferred; active-call soak unavailable  
**Branch:** `codex/bt500-aux-fast` from `4dba442`  
**Fixture:** Pi 3 Model B v1.2, Pixel 7a, Lark A1, ASUS USB-BT500, Pi AUX to Harmony

## Scope

This experiment closes the project around one call radio and one wired output:

```text
Lark A1 ─USB→ Pi ─HFP/HSP over USB-BT500→ Pixel 7a
                    └─Pi 3.5 mm AUX→ Harmony
```

Bluetooth speaker output, the BT600, onboard-Bluetooth production use, Android media-control
forwarding, steering-wheel controls, brownout qualification, and repeated power-cut durability are
outside this result. Their older evidence remains useful history but cannot satisfy E17.

## Fixed identity and audio contract

- Pixel: `5C:33:7B:CB:BF:C5`.
- Call controller: `A0:AD:9F:73:6C:24`, USB `0b05:1bf6`, product `ASUS USB-BT500`.
- Output: `wired:alsa_output.platform-3f00b840.mailbox.stereo-fallback` at `0.85`.
- AEC: WebRTC, 48 kHz mono, fail closed, `node_latency_frames=1920`.
- Output-controller status is present but explicitly reports `required=false`, `configured=false`,
  `ready=true`, `reason="wired-output"`.

No role is inferred from an HCI index. Final resolution must match the permanent controller address,
USB bus and VID/PID, and it records the current HCI name, sysfs parent/interface, driver, and rfkill
index. A connected Pixel object on the onboard controller or on two controllers fails closed.

## Implemented control plane

- Strict controller resolution and HFP-node binding in `pi/bridged/controller_roles.py` and the
  supervisor.
- A call-only watchdog that may recover only the resolved BT500 rfkill index or btusb interface.
  There is no output-role guess and no global BlueZ, PipeWire, WirePlumber, or all-USB reset.
- Deterministic AUX selection and set/read-back verification at `0.85`.
- A WirePlumber rule that prevents automatic raw Lark-to-HFP links; the supervisor owns call links.
- Mode-aware installation, readiness, and unit verification for a required USB call radio and no
  Bluetooth output radio.
- Exact-commit ZIP packaging, SHA-256 sidecar and per-file manifest, plus a guarded lower-root
  installer.
- Explicit `--onboard-bluetooth disable-qualified` boot promotion. It owns one marked
  `dtoverlay=disable-bt` block, records `/boot/firmware/config.txt` and `hciuart.service` preimages,
  and is rollback-verifiable. It must not be used before live qualification.
- A resumable `python -m rig.bt500_aux` campaign with baseline, individual cycle, campaign, detached
  soak, collection, checkpoints, hard-failure stop, and evidence manifests.

## Live bring-up record

The predeployment snapshot is
[`20260825T193346Z-bt500-aux-predeploy`](results/E16/20260825T193346Z-bt500-aux-predeploy/).
It preserved the mixed live state before hot deployment. The onboard Pixel bond was left intact as
rollback, the legacy watchdog was stopped, and the single-controller files were copied only into the
volatile overlay.

An early capture named
[`20260825T194649Z-bt500-aux-pair-gate-fail`](results/E16/20260825T194649Z-bt500-aux-pair-gate-fail/)
records an incomplete pairing sequence. Review of Android bond history showed that one later cached
coordinate selected an unrelated changing discovery-list row, so it was not counted as a second
BT500 bond attempt.

The corrected Android-initiated path selected the accessibility element whose text was exactly
`LarkBridge BT500`, required an exact-title confirmation dialog, and then accepted its `Pair`
element. BlueZ created a stored link key, and both sides reported the bond. The Pi then set `Trusted`
only on `/org/bluez/hci0/dev_5C_33_7B_CB_BF_C5`; the durable pairing writer sealed slot `b`.
The passive post-pair snapshot is
[`20260825T200431Z-bt500-aux-pair-success`](results/E16/20260825T200431Z-bt500-aux-pair-success/).

At the pairing checkpoint:

- the Pixel/BT500 ACL is authenticated and encrypted;
- strict final-USB controller resolution passes;
- the supervisor reports `CALL_DOWN` with `controller_binding_accepted=true`;
- the Lark and wired AUX endpoints are present;
- SCO and HFP nodes are correctly absent until a call is established;
- no immutable lower-root, Netplan, onboard-disable, or reboot promotion has occurred.

## Live transport and AEC evidence

The two-minute AEC-disabled transport proof
[`20260825T201035Z-bt500-aux-aec-disabled-transport`](results/E16/20260825T201035Z-bt500-aux-aec-disabled-transport/)
passed before AEC tuning resumed. It kept the call on the exact USB-BT500 controller and carried
bidirectional HFP/SCO audio to the Lark and wired AUX paths without a transport failure.

The objective AEC campaign record is
[`20260825T202917Z-bt500-aux-campaign`](results/E17/20260825T202917Z-bt500-aux-campaign/).
Its relevant 60-second captures are:

| Capture | Suppression | Near-end/double-talk evidence | Classification |
|---|---:|---|---|
| Cycle 1, attempt 2 | 37.47 dB | None; user confirmed no speech | Echo-only diagnostic pass |
| Cycle 1, attempt 3 | 16.09 dB | User-confirmed near-end speech and at least 15 seconds of independent double-talk | Acceptance-eligible pass |
| Cycle 2, attempt 1 | 12.69 dB | User-confirmed near-end speech and at least 15 seconds of independent double-talk; no crackle, dropout, or unintelligible audio reported | Acceptance-eligible pass |
| Cycle 3, attempt 1 | 37.95 dB | None; user confirmed no speech | Echo-only diagnostic pass |

Both acceptance-eligible captures exceeded the 10 dB suppression threshold, retained the verified
AEC graph at a 1,920-frame quantum, and completed fresh-session rejoin checks. The echo-only
captures show strong far-end echo attenuation, but they are not credited toward the double-talk
acceptance count.

After the last diagnostic, the user stated that it would be the final test and that AEC was
acceptable for now. That decision is preserved in
[`operator-decision.json`](results/E17/20260825T202917Z-bt500-aux-campaign/operator-decision.json).
It provisionally accepts AEC but explicitly stops and defers the remaining interactive cycles. The
original five-cycle gate is therefore incomplete: two acceptance-eligible cycles passed, while
three required cycles were not completed.

## Current unattended step

A 3,600-second active-call pre-persistence soak was launched, but the Pixel became unavailable
after the final session recycle. The opening snapshot was correctly rejected in `CALL_DOWN`, so
the run accumulated zero soak seconds and is neither a pass nor a soak failure. The launch and
not-started result are archived in
[`20260825T205935Z-pre-persistence-soak`](results/E17/20260825T205935Z-pre-persistence-soak/).
The required active-call endurance work waits for the Pixel to return.

No exact archive has been installed into the immutable lower root, and no Netplan fast-path,
persistent onboard-Bluetooth disablement, promotion reboot, or post-reboot call qualification has
occurred.

## Remaining gates

1. With the Pixel available, run the active-call endurance campaign; the pre-persistence attempt
   accumulated no soak time.
2. Either complete the remaining three interactive cycles or formally revise the original
   five-cycle acceptance contract; the current user-directed deferral is not a five-cycle pass.
3. Package and install the exact clean commit, commit the A/B configuration, promote the Netplan
   fast path and onboard disablement transactionally, and reboot once.
4. With the Pixel available, establish a fresh post-reboot Discord call and pass the complete final
   3,600-second active-call soak, teardown, and clean rejoin.

Until all four finish, E17 is **not** a release qualification and makes no durability claim.
