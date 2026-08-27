# ADR-0009 — A2DP media out, HFP microphone only during a communication session

- **Status:** Accepted (provisional), **amended 2026-08-27** — the decision stands; two parts of
  its justification and one implementation guard were refuted by measurement. See *Amendment*.
- **Date:** 2026-08-26
- **Relates to:** `docs/experiments/E19-transparent-phone-audio.md`, E01, E03, E15, E16;
  ADR-0007 (48 kHz internal graph)
- **Supersedes:** nothing. Extends the Mode 1W call path rather than replacing it.

## Context

The operator asked for phone audio to pass through the appliance **transparently**: media out to
the aux speaker, and the selected microphone available to the phone — ideally at all times, not
only during a call.

E19 Steps 1–3 established that this request contains two separable asks with very different
answers, and that conflating them is the main way this project could ship a false success.

### Baseline before this decision

One radio (`hci0`, ASUS USB-BT500). The Pi was the **hands-free unit** and an **A2DP source**; it
advertises Audio Source, Headset, Handsfree and AVRCP, and deliberately **not** Audio Sink —
`bluez5.roles = [ a2dp_source hfp_hf hsp_hs ]`, with a comment stating that omitting `a2dp_sink`
"stops Android from pushing media at us". Confirmed live: no `0000110b` on the adapter.

The supervisor is call-shaped throughout. `tick()` derives `call_up` from the presence of the two
HFP nodes and, when false, returns at `CALL_DOWN` having done nothing but hold the aux volume.
There is no media concept to extend; everything needed here is additive.

### What decides the question

Android, not the appliance. The Pixel 7a is running Android 14, and live E01/E19 evidence shows
that a microphone transport is something a communication application **asks for**, not a
property of being connected. Connected-idle sampling showed no SCO transport and no phone audio
nodes; Discord produced `MODE_IN_COMMUNICATION`, an active SCO device, and the HFP nodes.
Android also removes the A2DP media stream when communication owns SCO, so the implementation
must alternate transports rather than promise concurrent media and microphone audio.

E01 measured the mechanism on this exact handset:

```
SCO state          : SCO_STATE_ACTIVE_INTERNAL
active comm device : bt_sco_hs  larkbridge
audio mode         : MODE_IN_COMMUNICATION  (owner: com.discord)
```

and concluded that "the HFP card materialises only once an SCO transport exists." Raw presence of
the configured phone's HFP/SCO nodes is therefore the appliance's transport truth. The
implementation deliberately reports that separately from accepted controller binding and from a
verified AEC/uplink graph: transport presence does not by itself prove safe call ownership.

### Capability verdicts

| Capability | Verdict | Evidence class |
|---|---|---|
| A2DP media from phone to fixed AUX | **Measured; implementation under promotion validation** | E19 live `a2dp-source` stream and decoy-sink `target.object` trial |
| Microphone during a cellular call | **Works today** | Deployed and proven |
| Microphone during an app-created communication session | **Works today** | **Measured** — E01, `com.discord` owning `MODE_IN_COMMUNICATION` |
| Persistent SCO/HFP outside any session | **Rejected** | Contradicted by AOSP; never demonstrated; would displace media entirely and cap it at 16 kHz |
| Ordinary recorder / camera / plain app microphone | **Not achievable over classic HFP** | AOSP, plus the E01 mechanism |
| USB Audio Class | **Not achievable on this hardware** | The Pi 3B's `dwc_otg` is host-only; ADR-0005 and E05 route UAC through a separate Pico, which is a different product |
| LE Audio (BAP) ordinary-app microphone | **Unknown — hardware is capable** | `btmgmt` reports `cis-central cis-peripheral iso-broadcaster` in *current* settings; the Pixel advertises TMAP `0x1855`; BlueZ/PipeWire maturity unproven |

**The requested universal idle-state microphone is not possible over classic Bluetooth**, and the
reason is Android's audio policy, not a defect in the appliance. **Transparent full duplex —
media and microphone simultaneously — is not possible either** because the measured handset
withdraws its A2DP stream when SCO owns the communication transport.

That is a smaller answer than the request, but it is still useful: cellular calls and the measured
Discord path open communication transport. Other applications are supported only when Android
actually opens HFP/SCO for them; WhatsApp, Signal, Meet, and Assistant have not been qualified by
this experiment.

## Decision

Adopt **A2DP media output with a microphone that is present exactly when Android opens a
communication transport**, and state the exclusions as plainly as the inclusions.

The supervisor reports this as an orthogonal `PhoneTransport`: `ABSENT`, `MEDIA_READY`,
`MEDIA_ACTIVE`, `SWITCHING`, `CALL`, `MEDIA_RESTORED_APP_PAUSED`, or `DEGRADED`. `DEGRADED` is
never presented as media-ready.

1. Restore `a2dp_sink` to the WirePlumber role set so the Pixel can present media to the
   appliance.
2. Route that media deterministically to the configured output — currently the aux jack — under
   supervisor ownership, never via default-device state.
3. On a communication session opening, enter `SWITCHING`, remove and verify removal of the
   A2DP-owned playback path, then build and verify AEC before unmuting. Do not switch the BlueZ
   card profile: measurement found one combined `audio-gateway` profile.
4. On the session ending, destroy the call graph, enter `MEDIA_RESTORED_APP_PAUSED`, and wait for
   Android to create a new A2DP stream. Route it on arrival — **not** "playback resumed", which
   Android alone controls.
5. Preserve every existing microphone guarantee unchanged: Lark transmitter liveness, Lark-first
   then FIFINE, same-name replug generations, break-before-make, post-AEC `bridge.mic` ownership,
   verified gain and mute, fail-closed ambiguity handling, and no physical-microphone-to-phone
   bypass.
6. Report the absence honestly. When the phone is connected with no communication session, status
   must say that the microphone is **selected and ready but that Android has not opened a
   microphone transport** — never that a microphone is "live".

Because `a2dp_sink` re-opens what the current role set was written to prevent, two guards are
part of this decision rather than implementation detail:

- A rule pinning the phone's `a2dp-source` stream to the configured output via
  `target.object`. The pre-E19 `65-bridge-hfp-no-autolink.conf` rule is scoped to
  `headset-audio-gateway` only. Commit `9ec30d7` installs the separate
  `66-bridge-a2dp-source-target.conf` through both installer paths. **Note the mechanism:
  `node.autoconnect = false` was measured to break media outright and must not be applied to the
  A2DP stream — see the Amendment.**
- The phone must remain excluded from the **output** candidate list. `outputs.py` distinguishes
  speakers from the phone by the remote's A2DP Sink UUID; enabling our own sink role does not
  change the Pixel's advertised UUIDs, but Step 10 must confirm it.

## Consequences

- Relative to the pre-E19 baseline, the operator gains media through the aux speaker while keeping
  the existing communication microphone behavior.
- The operator does **not** get a microphone for ordinary recording apps, and does not get media
  and microphone at the same time. Both exclusions must appear in operator documentation in
  plain language, attributed to Android rather than to the appliance.
- Media will be interrupted by calls. Android removes the A2DP stream; after teardown the
  appliance waits for and routes a newly arriving Android stream but cannot resume the application.
- E03's radio-contention risk is **reduced**, not incurred. Android refuses the concurrent patch
  before SCO and A2DP ever compete for slots on the one radio.
- The 48 kHz internal graph (ADR-0007) is retained. A2DP negotiation may land at 44.1 kHz under
  `55-bridge-a2dp-coex.conf`; that is one resample on a path whose ceiling is already set
  elsewhere, and does not justify reshaping the graph.
- LE Audio is **not** adopted here and **not** foreclosed. It is the only documented Bluetooth
  route to an ordinary-application microphone, the controller is capable, and it deserves its own
  experiment rather than being smuggled into this one.
- This ADR is **provisional**. The microphone mechanism, A2DP sink behavior, combined profile,
  pause behavior, and deterministic AUX pin are measured. E19 row 3 is confirmed; live
  confirmation of rows 4, 7 and 8 remains a gate before promotion.


## Amendment — 2026-08-27, after measuring on the hardware

The decision is unchanged: A2DP media out, microphone only during a communication session. Three
things behind it were wrong, and two of them would have produced broken code.

**1. Profiles are not mutually exclusive.** The Context above argued from PipeWire's
`device.profile` semantics that `a2dp-sink` and `headset-audio-gateway` could not be active
together. Measured, the Pixel's card exposes a *single combined* profile —
`65536 audio-gateway | "Audio Gateway (A2DP Source & HSP/HFP AG)"` — with only `off` beside it.
There is nothing to switch between. The conclusion that survives is Android's, not PipeWire's:
the phone still refuses a concurrent A2DP patch while SCO is up.

**2. The guard mechanism was wrong and would have shipped a dead feature.** This ADR called for
`node.autoconnect = false` on the phone's media node. Controlled A/B with playback verified
running:

| Rule | BlueZ transport | Nodes | Links |
|---|---|---|---|
| none | `active` | 1 | 4 |
| `node.autoconnect = false` | `idle` | 0 | 0 |

The phone's media arrives as a `Stream/Output/Audio` **client stream**, so transport acquisition
is driven *by* the session manager linking it. Disable autoconnect and nothing links it, the
transport is never acquired, the node never exists, and no audio flows. The correct mechanism is
`target.object`, which pins the stream to the configured sink while leaving acquisition intact —
measured to route to aux even with a decoy sink installed as the default, where the unpinned
control followed the decoy.

**3. The media node does not persist across pause.** It is destroyed on pause and recreated on
resume, so `MEDIA_READY` and `MEDIA_RESTORED_APP_PAUSED` are indistinguishable from the graph and
separable only by supervisor-side history. Routing must therefore be arrival-driven
reconciliation, re-asserted every time the node reappears, not a link established once.

Also corrected: the handset is on **Android 14** (SDK 34), not 16 or 17, so the AOSP *Audio
Managed SCO rearchitecture* page cited in the Context describes a model that does not apply here.
The microphone conclusion now rests on E01's measurement and on the Step 3 matrix rather than on
that page.

### Rapid-loop implementation note

E19's electrical AUX loop exposed an intermittent underrun on the fixed 48 kHz analog sink while
the 44.1 kHz A2DP source and AUX sink ran at a 512-frame graph quantum. The accepted correction is
an AUX-only WirePlumber rule setting `api.alsa.headroom = 960`, one additional 480-frame ALSA
period. It deliberately leaves period size, Bluetooth latency, and graph quantum unchanged. Two
consecutive YouTube Music captures on immutable candidate `be38d43349-787d0cd69f2a` then passed
with more than 40 dB margin over the calibrated electrical floor, zero clipping, zero route loss,
and zero new AUX or BlueZ errors. This closes the rapid media checkpoint, not the ADR's remaining
Discord communication-session and post-call restoration gate.
