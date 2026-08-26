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

- Scripts: resolver/supervisor unit tests, `rig/pi/measure/invariants.py`, the BT500+AUX campaign,
  and K054-specific characterization tooling added by this change.
- Hardware present: Pi 3 Model B v1.2, Lark A1, user-identified FIFINE K054, USB-BT500, Pixel 7a,
  and Pi AUX speaker fixture.
- Conditions held constant: 48 kHz PipeWire graph, WebRTC AEC mono, 1,920-frame AEC latency, wired
  output volume 0.85.
- Conditions varied: available microphone combination, enumeration order, hotplug order, active-call
  transition, K054 placement/gain/mute, and reboot cycle.

## Runs

| # | Date | Variant / conditions | Artifact dir | Verdict |
|---|---|---|---|---|
| 1 | 2026-08-25 | Passive USB/ALSA/PipeWire characterization | planning-session SSH transcript | CONFIRMED for the attached unit |
| 2 | 2026-08-25 | Host resolver, graph, readiness, release, and qualification suites | commit `1ab27980c5dea31e56c8e8f87e20ead99788623a` | PASS: 227 + 101 tests, 2 hardware skips |
| 3 | 2026-08-25 | Read-only live resolution with both microphones attached | `docs/experiments/results/E18/live-resolver-preflight-20260825.json` | PASS |
| 4 | 2026-08-25 | Transactional deployment, one CALL_DOWN failover/promotion cycle, and warm boot | `docs/experiments/results/E18/live-integration-20260825.json` | PASS with deferred gates |
| 5 | pending | Physical controls, repeated hotplug/reboot, active-call, acoustic/AEC and endurance | `docs/experiments/results/E18/field/` | pending |

## Acceptance gates

- Both/either/neither boot combinations produce Lark, Lark, FIFINE, and no-uplink respectively.
- Every sampled transition has one or zero microphone uplinks, never two, raw, or AEC-bypassing.
- Twenty cycles each of FIFINE replug, Lark fallback, and Lark promotion meet the timing gate.
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
and acoustic/AEC behavior remain unmeasured.

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
