# E02 — Can the Pi act as an HFP Hands-Free unit for the Pixel, stably?

- **Status:** Not started
- **Resolves risk:** R4 (probability 3, impact 4, score 12)
- **Gates milestone:** M4
- **Script:** `tests/stage-b-hfp/s2-hfp-hf-role.sh`

## Question

Three parts, all of which must be yes:

1. Does PipeWire register **UUID `0000111e` (Handsfree)** — meaning we present as a
   hands-free *unit* — rather than `0000111f` (Handsfree Audio Gateway)?
2. Does an Android Audio Gateway complete a service level connection with us, producing
   `handsfree-head-unit` nodes in the PipeWire graph?
3. Does WirePlumber survive 30 minutes of repeated HFP transitions without restarting?

## Why it cannot be answered by reading

Parts 1 and 2 are *mostly* answerable from source, and that reading is already done — it
is what produced the role-naming table below. What reading cannot settle is part 3.

There is a field report of WirePlumber **segfaulting specifically when the HFP path is
exercised as a system service**, with the reporter's workaround being to drop to HSP
entirely. That is a report about a configuration we deliberately avoid (ADR-0006 keeps us
in a user session), on unknown versions, so it may not apply to us at all — but it lands
directly on our critical path, and the cost of finding out empirically is one afternoon.

## The role naming, settled from source

This is the single easiest thing to get backwards on this project. Values in
`bluez5.roles` describe the **remote device**, not us:

| Value | Remote is | We act as | Want? |
|---|---|---|---|
| `hfp_ag` | Audio Gateway (the Pixel) | **Hands-Free unit** | ✅ |
| `hfp_hf` | Hands-Free unit (a headset) | Audio Gateway | ❌ |
| `a2dp_sink` | Sink (headphones) | **A2DP Source** | ✅ |
| `a2dp_source` | Source (a phone) | A2DP Sink | ❌ |

Evidence: `spa_bt_profile_from_uuid()` maps the remote's advertised UUID `0000111e` to
`SPA_BT_PROFILE_HFP_HF`, and `backend-native.c` then dispatches the **AG** state machine
for that profile:

```c
case SPA_BT_PROFILE_HFP_HF: rfcomm_process_events(rfcomm, buf, true, rfcomm_hfp_ag);
```

So our config uses **`hfp_ag`** to become the hands-free unit. Confirming this empirically
via `sdptool browse local` is part 1 of the experiment.

## Method

```bash
./tests/stage-b-hfp/s2-hfp-hf-role.sh              # 30-minute soak
./tests/stage-b-hfp/s2-hfp-hf-role.sh --soak 300   # 5-minute smoke first
```

Runs as the ordinary user, not root — PipeWire is a user service (ADR-0006).

During the soak: place and end several calls (this is what forces SCO setup/teardown),
toggle phone Bluetooth off and on, and leave one call running for several minutes.

## Runs

| # | Date | Soak | 111e? | SLC? | WP restarts | PW restarts | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | | | | | | | |

## Result

_(fill in)_

## Verdict

_(PASS / UNSTABLE / FAIL)_

## Consequences for the plan

| Verdict | What happens |
|---|---|
| PASS | R4 drops to probability 1. Proceed to M4 with HFP + mSBC. |
| UNSTABLE | Fall back to `bluez5.roles = [ hsp_ag a2dp_sink ]`. HSP is **CVSD only, 8 kHz** — record the quality cost, update ADR-0007's resample table (48↔8 kHz), and update the `bridgectl status` codec expectations. |
| FAIL, no `0000111e` | Config error, not a defect. Check in order: (1) `hfp_hf` used where `hfp_ag` belongs, (2) `ofono` installed but unconfigured — remove it, (3) `bluez5.hfphsp-backend` is not `"native"`. |
| FAIL, 111e present but no SLC | Android is refusing us. Check Class of Device (`0x000408`) and whether the phone's UI offers "Phone calls" at all. |

## Follow-up questions this raised

_(fill in — e.g. does the phone re-establish HFP after a Pi reboot without re-pairing?)_
