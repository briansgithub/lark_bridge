# E17 — Pi 3 USB-BT500 call controller with wired AUX output

**Status:** corrective release persisted and reboot-verified; Pixel-dependent final gates deferred<br>
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

## Pre-persistence soak result

A 3,600-second active-call pre-persistence soak was launched, but the Pixel became unavailable
after the final session recycle. The opening snapshot was correctly rejected in `CALL_DOWN`, so
the run accumulated zero soak seconds and is neither a pass nor a soak failure. The launch and
not-started result are archived in
[`20260825T205935Z-pre-persistence-soak`](results/E17/20260825T205935Z-pre-persistence-soak/).
The required active-call endurance work waits for the Pixel to return.

## Persistence and corrective reboot

The first promoted reboot reached the intended single-controller `CALL_DOWN` state but failed the
strict readiness baseline because wired AUX volume was not applied or observed while the call was
down. The evidence in
[`20260825T211935Z-bt500-aux-postboot-readiness`](results/E17/20260825T211935Z-bt500-aux-postboot-readiness/)
records `desired=0.85`, `observed=null`, and `verified=false`. This exposed an idle-state supervisor
defect rather than a Bluetooth transport failure.

Commit `c63c823ab9d6dfa1837b05517f903d25c6b5c96a` corrects wired-volume application and read-back in
`CALL_DOWN`. The exact persisted artifact is
`LarkBridge-bt500-aux-c63c823ab9d6-20260825T212148Z.zip`, whose SHA-256 is
`c085897770cdd54d1a9ff1b39c688cc6ef2dbbd9e41e9f79e87379505075c1d3`. Its 450-file
`MANIFEST.sha256` was verified before promotion.

The matching source archive is `LarkBridge-bt500-aux-source-c63c823ab9d6.zip` (SHA-256
`33c152c49f5c979f62564615a94c6f024ba5bdcc0cf40d867bc888d691809f26`). The provisional evidence
bundle is `LarkBridge-bt500-aux-evidence-c63c823ab9d6-20260825T213549Z.zip` (SHA-256
`0ed568a8f126e2ea7e113d24ab984c95628ccc8fa1172d42e60ec0e83b8778b8`); its embedded evidence
manifest verifies 196 payload files. It is provisional because the Pixel-dependent gates below
remain open.

The corrective reboot has boot ID `925eb01c-f17d-4cea-96ee-d505f20db7a3`. The passive readiness
snapshot
[`20260825T213025Z-bt500-aux-post-c63c-readiness`](results/E17/20260825T213025Z-bt500-aux-post-c63c-readiness/)
passes and establishes:

- `/media/root-ro` and `/boot/firmware` are mounted read-only; `/` is the expected writable runtime
  overlay above that immutable lower root;
- BlueZ exposes only `A0:AD:9F:73:6C:24`, resolved as the exact USB-BT500 `0b05:1bf6` at sysfs
  `1-1.5`; the onboard controller is absent;
- the supervisor passes strict `CALL_DOWN` readiness with the wired AUX target observed and
  verified at `0.85`;
- Bluetooth, the call-only watchdog, storage guard, tuning, supervisor, PipeWire, and WirePlumber
  are active with successful main status and zero restarts;
- total boot time is 18.132 seconds, so the Netplan fast path remains enabled.

This is a persisted idle-readiness result. Because the Pixel is unavailable, it does not prove a
fresh post-reboot HFP/SCO session, AEC graph, call teardown/rejoin, or active-call endurance.

## 2026-09-01 Pixel reconnect and abrupt-power qualification

The Pixel 7a later returned for qualification of the hardened reconnect changes deployed at
`892af46bd30e56b08816fc21b46d0e8c0227bd3d`. Five user-accepted genuine cold starts passed with
the phone locked, Bluetooth enabled, and no pairing interaction. Their Pi-monotonic
power-to-connected times were 19.701, 19.556, 19.406, 19.433, and 19.312 seconds: 5/5 below the
25-second requirement.

Commit `175b104` adds a seeded `pixel-chaos` profile to the host power-loss controller. It keeps
the mandatory hashed-backup/physically-booted-recovery-card gate, records the complete five-cut
schedule before starting, enforces at least 12 seconds cold-off, and requires every recovery to
retain the exact pairing identity, pass the full hardened-storage probe, leave pairing repair
idle, and reconnect the Pixel within 25 seconds.

The completed seed-`20260901` campaign is retained locally at
`.artifacts/powerloss/pixel-chaos-175b104-restart-1`. One operator-missed sequence is preserved in
the separate original campaign and is not counted. The completed campaign produced:

| Abrupt cut | Pixel connected | Storage | Pairing identity | Result |
|---|---:|---|---|---|
| 1 second after power-on | 19.169 s | `READY` | unchanged | Pass |
| 6 seconds after power-on | 19.138 s | `READY` | unchanged | Pass |
| 12 seconds after power-on | 19.067 s | `READY` | unchanged | Pass |
| During seeded Bluetooth recovery | 19.601 s | `READY` | unchanged | Pass |
| During seeded persistent-state write | 19.387 s | `READY` | unchanged | Pass |

Campaign status reports five passed, zero failed, zero active, and zero remaining. The final
standalone verifier also reports `ready=true`, no failures, Pixel `bond_state=connected`, repair
idle, `/boot/firmware` read-only, `/media/root-ro` read-only, and the expected writable tmpfs root
overlay plus journaled `LARKDATA`. These are approximate human-timed physical cuts, not a
programmable brownout waveform or a long cumulative-durability campaign; no claim is made for
subsecond voltage sag or dozens of repeated cuts.

## Remaining gates

1. With the Pixel available, establish a fresh post-reboot Discord call and re-verify the exact
   BT500 HFP/SCO transport, AEC graph, Lark uplink, and wired AUX downlink.
2. Either complete the remaining three qualifying interactive cycles or formally revise the
   original five-cycle acceptance contract; the current user-directed deferral is not a five-cycle
   pass.
3. Pass the complete final 3,600-second active-call soak, teardown, and clean rejoin.

Until all three finish, E17 is **not** a full release qualification and makes no active-call
durability claim for the persisted build.
