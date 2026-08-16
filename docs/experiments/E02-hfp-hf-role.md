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

## The role naming, settled by MEASUREMENT (2026-08-16)

`bluez5.roles` names the role **this machine plays**. Verified by setting each value and reading
back `bluetoothctl show`:

| Config value | Adapter advertises | Pi acts as | Want? |
|---|---|---|---|
| `a2dp_source` | Audio Source `0000110a` | **A2DP Source** | YES |
| `a2dp_sink` | Audio Sink `0000110b` | A2DP Sink | no |
| `hfp_hf` | Handsfree `0000111e` | **HFP Hands-Free** | YES |
| `hfp_ag` | Handsfree AG `0000111f` | Audio Gateway | no |
| `hsp_hs` | Headset `00001108` | HSP Headset | fallback |

Working config: **`bluez5.roles = [ a2dp_source hfp_hf hsp_hs ]`**, which yields
`Audio Source` + `Handsfree` + `Headset` on the adapter — exactly the three roles this
project needs.

**Correction:** an earlier revision claimed the opposite convention, inferred from
`spa_bt_profile_from_uuid()`. That function maps a REMOTE device's UUIDs and does not govern
this key. Part 1 of this experiment is therefore already answered, empirically.

## Method

```bash
./tests/stage-b-hfp/s2-hfp-hf-role.sh              # 30-minute soak
./tests/stage-b-hfp/s2-hfp-hf-role.sh --soak 300   # 5-minute smoke first
```

Runs as the ordinary user, not root — PipeWire is a user service (ADR-0006).

During the soak: place and end several calls (this is what forces SCO setup/teardown),
toggle phone Bluetooth off and on, and leave one call running for several minutes.

## Result — parts 1 and 2 CONFIRMED, 2026-08-16

**Part 1 — do we register UUID `0000111e`?** Yes. With
`bluez5.roles = [ a2dp_source hfp_hf hsp_hs ]` the adapter advertises `Handsfree (0000111e)`
and `Audio Source (0000110a)`.

**Part 2 — does an Android AG complete a service level connection with us?** Yes. Pixel 7a
(`5C:33:7B:CB:BF:C5`) paired, bonded and trusted. The full HFP SLC completes, captured in
`btmon`:

```
AT+BRSF=695      -> +BRSF: 3951, OK      feature exchange (only an HF sends AT+BRSF)
AT+BAC=1,2,3     -> OK                    codecs offered: CVSD, mSBC, LC3-SWB
AT+CIND=?        -> +CIND: ("call"...)    indicator list
AT+CIND?         -> +CIND: 0,0,1,3...     indicator values
AT+CMER=3,0,0,1  -> OK                    <-- SLC COMPLETE
AT+CHLD=? / CLIP / CCWA / CMEE / CLCC -> OK
```

PipeWire's own debug log confirms which side we are:

```
spa.bluez5.native profile_new_connection: NewConnection
    path=/org/bluez/hci0/dev_5C_33_7B_CB_BF_C5, fd=42, profile /Profile/HFPHF
spa.bluez5.native rfcomm_hfp_hf: AG indicator state: call=0 callsetup=0 service=1
                                 signal=3 roam=0 battchg=4 callheld=0
                  AT+CHLD supported: 1 (0xF)
                  Created virtual battery for 5C:33:7B:CB:BF:C5
```

`/Profile/HFPHF` registered, `rfcomm_hfp_hf` state machine running, phone AG indicators being
read. **The Pi is functioning as an HFP Hands-Free unit.** `AT+BAC=1,2,3` means mSBC is offered,
so wideband speech is reachable.

**Android's own view agrees** (`adb shell dumpsys bluetooth_manager`):

```
Profile: HeadsetService
  mActiveDevice: XX:XX:XX:XX:8D:51     <- larkbridge
  mAudioRouteAllowed: true
  isInbandRingingEnabled: true
```

Android has made the Pi its **active headset**.

### Two supporting findings

**Class of Device matters.** Out of the box the adapter reported `0x00400000` — no device class
at all. Setting `Class = 0x000408` in `/etc/bluetooth/main.conf` yields `0x00680408`
(major Audio/Video, minor Hands-free, service bits Audio + Telephony), after which Android lists
`larkbridge` with a **headset icon** rather than as a generic device.

**Profile bitmask decode**, from `spa_bt_device_check_profiles`:

| Mask | Value | Meaning |
|---|---|---|
| `profiles` | `0x148` | A2DP_SOURCE + HSP_AG + HFP_AG — the **phone's** roles |
| `connectable` | `0x144` | A2DP_SINK + HSP_AG + HFP_AG — what we can pair with, given our enabled roles |

Note the asymmetry: internal `SPA_BT_PROFILE_*` constants describe the **remote's** role, while the
`bluez5.roles` config key names **our own**. Both readings are correct about different things —
which is exactly why the earlier inference went wrong.

## Open: no PipeWire card for the phone

The SLC completes but no `bluez_card.5C_33_7B_CB_BF_C5` appears in the graph, so there is no node
to route audio through yet. The iWorld A2DP device does get a card, so the monitor works. Most
likely the HFP card materialises only once an SCO transport exists — i.e. during an actual call —
but that is **not yet verified**. This is the next thing to resolve, and it gates part 3 and S1.

## Runs

| # | Date | Soak | 111e? | SLC? | WP restarts | PW restarts | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-16 | n/a (bring-up) | **yes** | **yes** | 0 | 0 | parts 1+2 PASS |
| 2 | | 30 min | | | | | (part 3 soak pending) |

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
