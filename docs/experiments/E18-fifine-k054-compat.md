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

### Dynamic hotplug qualification protocol

This is an operator-driven physical test with the phone call established and HFP active throughout;
USB-driver unbind/bind or `CALL_DOWN` simulation does not count toward this field gate. Continuously
sample bridge status and `pw-link -l` during every action, using a configured interval no greater
than 0.20 seconds. A run is admissible only when its measured maximum sample-start gap is no greater
than 0.25 seconds. The host streams the sampler over SSH and writes the result under
`docs/experiments/results/E18/field/`; it does not install a helper, restart a service, edit
configuration, or mutate USB authorization on the Pi.

For each prompted connector change, the operator prepares the connector, types the exact readiness
phrase, and waits for the separate `NOW` prompt before physically acting. Before `NOW`, the harness
captures a fresh service-restart snapshot, discards the first sample after that synchronous query,
and requires two fresh consecutive samples with the expected, identical raw USB sysfs topology. The
transition timer starts at the first observed raw USB topology edge, not at the operator prompt; a
second identical sample must confirm that edge, and the exact topology must persist through the
completed or actionable-safe result. USB bounce, ambiguity, prechanged state, or identity/generation
mismatch fails closed. Completed and safe-state results are structurally revalidated against the Pi
monotonic clock, the 60-second limit, and at least 0.60 seconds of stable settled evidence. The
harness captures a second fresh restart snapshot at the result boundary. If any pre-action check
fails, `NOW` is withheld and the campaign aborts.

The evidence provenance binds each local decision-critical helper to its Git blob at the declared
local commit. It also binds each installed tracked helper to the blob at the deployed release commit,
in addition to recording the release/archive and file hashes. The preserved local `bridge.toml` is
explicitly recorded as untracked, hash-only configuration. Missing or shallow commits, missing
paths, non-blob paths, source mismatches, or unrelated substituted helpers fail closed. The deployed
Pi working tree itself may contain unrelated dirt; authoritative tracked sources are compared with
the commit named by `/etc/larkbridge/DEPLOYED.json` rather than assumed clean.

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

The repeated campaigns use the fixed `direct10-hub10` connection plan: during cycles 1–10, every
connected test microphone uses a direct Pi USB port. After cycle 10, the continuously running
sampler prompts for one non-timing-gated handoff that moves the FIFINE to the powered external hub
while Lark remains absent. The handoff must recover FIFINE-only `ACTIVE` with a changed USB instance
token, advanced graph generation, no unsafe link, and newly observed external-hub ancestry. The
operator's claim that the external power supply is connected is recorded separately from the USB
ancestry; software does not claim that USB descriptors prove external power. Cycles 11–20 then run
through the hub. During the hub half, any simultaneous Lark/FIFINE samples must share the hub
ancestry observed at the handoff.

## Runs

| # | Date | Variant / conditions | Artifact dir | Verdict |
|---|---|---|---|---|
| 1 | 2026-08-25 | Passive USB/ALSA/PipeWire characterization | planning-session SSH transcript | CONFIRMED for the attached unit |
| 2 | 2026-08-25 | Host resolver, graph, readiness, release, and qualification suites | commit `1ab27980c5dea31e56c8e8f87e20ead99788623a` | PASS: 227 + 101 tests, 2 hardware skips |
| 3 | 2026-08-25 | Read-only live resolution with both microphones attached | `docs/experiments/results/E18/live-resolver-preflight-20260825.json` | PASS |
| 4 | 2026-08-25 | Transactional deployment, one CALL_DOWN failover/promotion cycle, and warm boot | `docs/experiments/results/E18/live-integration-20260825.json` | PASS with deferred gates |
| 5 | 2026-08-26 | Initial active-call harness attempt; rejected stale-status comparison between unsynchronized host and Pi wall clocks before any physical transition | `docs/experiments/results/E18/field/hotplug-20260826T061527Z-9359e505c8c4/` | FAIL — harness diagnostic only |
| 6 | 2026-08-26 | Active-call promotion attempt; harness treated intentional break-before-make silence as missing-route H5 violations | `docs/experiments/results/E18/field/hotplug-20260826T062255Z-dae8f3b0af34/` | INCONCLUSIVE — harness diagnostic only |
| 7 | 2026-08-26 | Complete live active-call physical both/either/neither matrix with continuous link sampling | `docs/experiments/results/E18/field/hotplug-20260826T063853Z-e0089cc5cd0a/` | BEHAVIOR/SAFETY PASS / RUNTIME TIMING INCONCLUSIVE (recorded NOW-origin gate FAIL) |
| 8 | pending | Each repeated gate split into 10 direct + 10 powered-hub cycles; physical controls, reboot, acoustic/AEC, and endurance | `docs/experiments/results/E18/field/` | pending |

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
- For every repeated transition kind, cycles 1–10 are direct and cycles 11–20 are powered-hub
  observations. Exactly one continuously sampled, validated direct-to-hub handoff occurs between
  them. Direct evidence must contain no external-hub ancestor; hub evidence must descend from the
  newly observed handoff hub, and Lark/FIFINE hub samples must share that ancestor.
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

The first physical active-call attempt was rejected before any connector action because its host
freshness check compared the host wall clock with the directly connected Pi's unsynchronized wall
clock. The second attempt reached the first Lark promotion but then rejected intentional empty-link
snapshots during break-before-make teardown as H5 violations. Those runs are retained as harness
diagnostics; they provide no admissible transition timing. Commit `dae8f3b` changed freshness to
compare timestamps from the Pi, and commits `68a2245` and `e0089cc` allow transition silence while
continuing to reject every nonempty route with incorrect ownership.

The subsequent complete physical active-call matrix has evidence verdict `PASS` but its emitted
qualification gate is `FAIL`. All nine phases completed safely within 60 seconds. The recorded
promotion, fallback, and same-name FIFINE replug figures were 43.674202, 30.082559, and 25.505366
seconds respectively, but that harness version timed from `NOW`; those figures include the
operator/chat/physical-action delay and therefore are not admissible device-edge-to-`ACTIVE`
latencies. A post-hoc first-observed PipeWire-effect-to-`ACTIVE` calculation gave 13.949987 seconds
for promotion and 14.699947 seconds for fallback, but the run did not sample the raw USB edge, so
those values are diagnostic only. Product runtime timing for this matrix is consequently
inconclusive rather than failed. The inactive FIFINE removal and restoration held the selected Lark
token, AEC owner, and graph generation 14 unchanged. Removing both microphones reached
`WAITING_MIC` at generation 15 with no AEC owner or uplink. Restoring FIFINE produced a new
object/device/USB instance token and generation 16; restoring Lark promoted it at generation 17 with
verified AEC.

The admissible run contains 4,950 samples at a configured 0.15-second interval; its maximum remote
sample-start gap was 0.153245 seconds. It recorded zero raw, inactive, duplicate, or AEC-bypassing
links, zero supervisor/PipeWire/WirePlumber restarts, no new kernel or USB errors, and only
`output.bridge.mic` feeding HFP. The evidence manifests, byte counts, and SHA-256 hashes verify. This
is functional evidence for automatic Lark-first switching, not completion of the runtime timing or
repeated-cycle gates. Later campaigns use the raw USB-edge protocol above, including stable baseline
and edge confirmation, identity/generation binding, a 0.60-second minimum settle, and structural
revalidation of every claimed completion or actionable safe state.

The lower-root wrapper could not return the lower filesystem to read-only immediately after each
successful online install. Each install was followed immediately by a normal reboot, which restored
the designed read-only mount before qualification. This deployment observation remains relevant to
future release procedures.

Hardware identity/format characterization remains confirmed only for the attached unit. One
active-call matrix is now measured, including a zero-link-violation result; its original NOW-origin
gate failed, while product runtime timing remains inconclusive. The 20-cycle campaigns, physical
controls, replacement-unit stability, physical reboot repetition, acoustic/AEC behavior, and
endurance remain pending.

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
