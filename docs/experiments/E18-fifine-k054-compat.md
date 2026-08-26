# E18 — Can a FIFINE K054 safely serve as the Lark A1 fallback microphone?

- **Status:** Implementation/automated integration passed; field qualification is pending
- **Resolves risk:** microphone identity ambiguity, unsafe hotplug routing, and unqualified acoustic-path substitution
- **Gates milestone:** FIFINE-compatible production release
- **Owner / date:** Codex (runtime model identifier unavailable; GPT-5 family), 2026-08-25

## Question

When microphone candidates are ordered `lark-a1`, then `fifine-k054`, does the bridge always select
the highest-priority uniquely usable device and maintain exactly one AEC-protected HFP uplink across
boot, hotplug, fallback, and promotion?

## Why it cannot be answered by reading

The selection and route-ownership behavior can be proven in code and graph fixtures, but the K054's
USB identity, physical controls, acoustic path, and AEC performance depend on the attached hardware.

## Characterization already completed

Read-only SSH inspection of the user-identified K054 on `larkbridge` established:

| Property | Observed value |
|---|---|
| USB identity | `0c76:161e`, USB 1.10 full speed, revision `0100` |
| USB strings | no manufacturer, product `USB PnP Audio Device`, no serial |
| Topology | observed at `1-1.4`, then `1-1.2`; matching is intentionally portable |
| Interfaces | Audio Control, mono capture stream, HID; no playback stream |
| ALSA/PipeWire format | S16_LE/S16LE, 48 kHz, mono only |
| ALSA identity | card id `Device`, PCM 0; numeric card index is not configuration |
| PipeWire source | `alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback` |
| Direct capture | two-second `arecord` at S16_LE/48 kHz/mono passed |
| Mixer | capture switch plus nominal 0–31 dB capture volume |
| HID | mute and volume consumer usages advertised; physical mapping not yet verified |

This evidence identifies a generic USB-audio fingerprint, not the retail model. Another device with
the same fingerprint can be mistaken for the K054. Optional serial/port pinning is the mitigation;
the operator selected portable matching for this installation.

## Method

- Scripts: resolver/supervisor unit tests, `rig/pi/measure/invariants.py`, the host-side
  `rig/bt500_aux/microphone_hotplug.py` orchestrator and its streamed read-only
  `rig/pi/measure/microphone_hotplug.py` sampler, the BT500+AUX campaign, and K054-specific
  characterization tooling added by this change.
- Hardware present: Pi 3 Model B v1.2, Lark A1, user-identified FIFINE K054, USB-BT500, Pixel 7a,
  and Pi AUX speaker fixture.
- Conditions held constant: 48 kHz PipeWire graph, WebRTC AEC mono, 1,920-frame AEC latency, wired
  output volume 0.85.
- Conditions varied: available microphone combination, enumeration order, hotplug order, active-call
  transition, K054 placement/gain/mute, and reboot cycle.

### Pending dynamic hotplug qualification

This is an operator-driven physical test with the phone call established and HFP active throughout;
USB-driver unbind/bind or `CALL_DOWN` simulation does not count toward this field gate. Continuously
sample bridge status and `pw-link -l` during every action, using a configured interval no greater
than 0.20 seconds. A run is admissible only when its measured maximum sample-start gap is no greater
than 0.25 seconds. The host streams the sampler over SSH and writes the result under
`docs/experiments/results/E18/field/`; it does not install a helper, restart a service, edit
configuration, or mutate USB authorization on the Pi.

For each prompted connector change, the operator prepares the connector, types the exact readiness
phrase, and waits for the separate `NOW` prompt before physically acting. The harness captures a
fresh service-restart snapshot before starting the transition timer and a second fresh snapshot at
the result boundary. If the pre-action snapshot fails, `NOW` is withheld and the campaign aborts.
The evidence provenance records the local commit/dirty state and hashes of every local
decision-critical helper, plus the deployed release commit/archive hash and hashes of the installed
snapshot, invariant, supervisor, resolver, and preserved configuration files. The deployed Pi tree
is an archive without Git metadata, so its identity is taken from the validated
`/etc/larkbridge/DEPLOYED.json` record rather than inferred from a nonexistent checkout.

With the call left up, run this physical both/either/neither matrix in order:

1. Start with only FIFINE connected and verify it is selected and `ACTIVE`.
2. Connect Lark while leaving FIFINE connected; verify promotion to Lark.
3. Disconnect Lark while leaving FIFINE connected; verify fallback to FIFINE.
4. Reconnect Lark, then disconnect and reconnect the inactive lower-priority FIFINE; Lark must remain
   selected with the same instance token and graph generation throughout both FIFINE actions.
5. Disconnect both microphones; verify `WAITING_MIC`, no selected microphone, no microphone uplink,
   and no AEC owner.
6. Reconnect only FIFINE; verify the same candidate name is selected with a different instance token
   and an advanced graph generation relative to its previous physical instance.
7. Reconnect Lark while leaving FIFINE connected; verify promotion back to Lark.

Then, still using physical plug/unplug actions during a live active call, run 20 Lark-promotion
cycles, 20 Lark-fallback cycles, and 20 FIFINE-replug cycles. The promotion/fallback campaign
alternates connecting and disconnecting Lark while FIFINE remains connected. Each FIFINE-replug
cycle disconnects the only microphone into `WAITING_MIC` and reconnects that same FIFINE unit.
Capture service restart counters before and after each transition and across each complete campaign.

## Runs

| # | Date | Variant / conditions | Artifact dir | Verdict |
|---|---|---|---|---|
| 1 | 2026-08-25 | Passive USB/ALSA/PipeWire characterization | planning-session SSH transcript | CONFIRMED for the attached unit |
| 2 | 2026-08-25 | Host resolver, graph, readiness, release, and qualification suites | commit `1ab27980c5dea31e56c8e8f87e20ead99788623a` | PASS: 227 + 101 tests, 2 hardware skips |
| 3 | 2026-08-25 | Read-only live resolution with both microphones attached | `docs/experiments/results/E18/live-resolver-preflight-20260825.json` | PASS |
| 4 | 2026-08-25 | Transactional deployment, one CALL_DOWN failover/promotion cycle, and warm boot | `docs/experiments/results/E18/live-integration-20260825.json` | PASS with deferred gates |
| 5 | pending | Live active-call physical matrix; 20 promotion, 20 fallback, and 20 FIFINE-replug cycles; physical controls, reboot, acoustic/AEC, and endurance | `docs/experiments/results/E18/field/` | pending |

## Acceptance gates

- Both/either/neither boot combinations produce Lark, Lark, FIFINE, and no-uplink respectively.
- The live active-call physical matrix completes in the stated order with the expected selected
  microphone and state at every step.
- Continuous link sampling uses a configured interval of at most 0.20 seconds and records no
  sample-start gap greater than 0.25 seconds.
- Exactly 20 physical cycles each of Lark promotion, Lark fallback, and FIFINE replug are recorded.
  For each transition kind, at least 19 of 20 complete in the expected `ACTIVE` state within
  30 seconds. Every cycle completes within 60 seconds or reaches an actionable safe state by the
  60-second deadline; a safe-state outcome does not count among the required 19 fast completions.
- An actionable safe state is `SAFE` or `WAITING_MIC` with no link-invariant violation, no HFP
  input, and a recorded selection reason suitable for diagnosis.
- Across every sample and cycle there are zero raw, inactive, or duplicate HFP uplinks: only
  `output.bridge.mic` may feed the HFP sink. There are also zero restart-count deltas for
  `bridge-supervisor.service`, `pipewire.service`, and `wireplumber.service`.
- Every same-name FIFINE replug selects `fifine-k054` with a changed instance token and an advanced
  graph generation. Disconnecting or reconnecting inactive lower-priority FIFINE while Lark is
  selected causes no selected-token or graph-generation churn.
- Native K054 S16_LE/48 kHz/mono remains visible while active, with no input resampler.
- Five independent 60-second K054 AEC captures each meet the E17 suppression/double-talk gates.
- A full 3,600-second active-call soak completes without XRUNs, restarts, new USB errors, or
  unexplained gaps.

## Result

The implementation and automated graph-safety matrix pass. Release commit `1ab2798` is deployed
with the ordered configuration in persistent slot B. The final power-loss verifier returned
`ready=true`: the lower root and boot partition were read-only, the storage state was `READY`, the
supervisor had zero restarts, `lark-a1` was selected at priority 0, and `fifine-k054` was usable at
priority 1 as native mono S16LE/48 kHz with null USB serial.

One CALL_DOWN USB-driver unbind/bind cycle passed. Removing and restoring the inactive FIFINE did
not change the selected instance or graph generation. Removing Lark selected FIFINE with reason
`lark-a1 absent; using fifine-k054`; restoring Lark promoted it with a new PipeWire instance token.
Across 38 status samples there were no HFP endpoints, AEC owner, graph links, or service restarts.
A final warm-boot qualification passed idle readiness in 74.389 seconds. Three BT500 startup lines
matched the broad HCI-failure pattern, but the controller, required call watchdog, failed-unit scan,
and readiness gate passed; the lines are retained in the evidence rather than silently discarded.

The lower-root wrapper could not return the lower filesystem to read-only immediately after each
successful online install. Each install was followed immediately by a normal reboot, which restored
the designed read-only mount before qualification. This deployment observation remains relevant to
future release procedures.

Hardware identity/format characterization remains confirmed only for the attached unit. Physical
controls, replacement-unit stability, physical reboot/hotplug repetition, active-call switching,
and acoustic/AEC behavior remain unmeasured. The live active-call matrix and repeated-cycle
campaigns defined above remain pending; no dynamic timing or zero-violation result is claimed here.

## Verdict

**IMPLEMENTATION PASS / FIELD QA INCONCLUSIVE.** Code may merge with the fallback documented as
field-QA pending; production release promotion remains gated until every deferred acceptance gate
passes.

## Consequences for the plan

- Keep the 48 kHz graph and 1,920-frame AEC baseline unchanged.
- Do not transfer Lark acoustic qualification to the K054.
- Keep Lark first, K054 second, and fail closed on higher-priority ambiguity.
- Record every later result with the selected candidate identity and graph generation.

## Follow-up questions this raised

- Do replacement K054 units retain `0c76:161e` and the same capture-only capability fingerprint?
- Do the physical gain and mute controls alter ALSA controls, HID events, internal DSP, or some
  combination of them?
- What placement and physical gain produce at least 20 dB speech SNR without clipping in the target
  cabin/room?
