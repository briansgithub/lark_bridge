# E15 — Does a second Bluetooth controller make Mode 1 viable?

- **Status:** IN PROGRESS — first results 2026-08-23. Encouraging, **not conclusive**.
- **Resolves risk:** R1 (E03 raised it to probability 5, score 20)
- **Owner / date:** Claude / 2026-08-23

## Question

E03 concluded that HFP/eSCO and A2DP on the Pi 3B's single BCM43438 are **PARTIAL —
intermittent**: our own stack tears the A2DP link down within seconds to ~2 minutes once SCO
is active, and config-level mitigations were exhausted. An Asus USB Bluetooth dongle
(`0b05:1bf6`, Realtek, LMP subversion 0x8761, HCI 5.4) is now available as a second
controller. Does splitting the two profiles across two radios clear E03's bar of
**<1 dropout/minute with zero SCO drops**?

## Fixture

- Pixel 7a in a live Discord call with the control PC, routed to the Pi over HFP/eSCO (mSBC).
- **Orientation A**: call on `hci0` (onboard BCM43438), A2DP on `hci1` (dongle).
- A2DP sinks: **iWorld** `50:D7:1B:74:34:D6` (instrumented — 3.5 mm line-out into the C-Media
  dongle B pink jack) and **Monoprice MP43247 Boombox** `C9:5C:FD:6E:28:46` (realistic —
  a Bluetooth speaker with no line-out).
- The instrument was verified against its own 2026-08-16 calibration before use.

## Instruments

| Tool | Role |
|---|---|
| `rig/tests/unit/U13.sh` | rig error floor, re-verified |
| `rig/pi/measure/a2dp-cal.sh` | A2DP capture-loop gain calibration |
| `rig/pi/measure/a2dp-survival.sh` | `survival_s` — made two-controller aware here |
| `rig/pi/measure/a2dp-dropouts.sh` + `rig/dropoutctl.py` | **new** — dropouts/minute, captured on the Pi, scored on the PC |

## Findings

### 1. The instrument reproduces its original calibration

| Constant | 2026-08-16 | 2026-08-23 |
|---|---|---|
| Noise floor (RMS) | −89.2 dBFS | **−89.26** |
| Loopback gain | −17.7 dB | **−17.63** |
| Linearity max error | ±0.21 dB | **0.18** |
| Usable dynamic range | 71.5 dB | **71.58** |

Every later number inherits this. A2DP capture gain 30 lands the tone at −19.73 dBFS peak
against a −89.56 dBFS floor, 69.8 dB SNR, no clipping at any sweep point.

### 2. The one-radio control reproduces E03's failure

`survival_s = 56 s`, `reason = device_disconnected`, `sco_pps_at_end = 0`, and **both
alive-probes false — the controller wedged.** That sits inside E03's 7 / 120 / 121 s spread and
reproduces its `baseline-3` signature. The failure is real and still present on this build.

### 3. Orientation A cleared the bar over 7 minutes

| Metric | Value |
|---|---|
| `dropouts_per_minute` | **0.0** (bar: <1.0) |
| Total dropouts | **0** across 14 × 30 s segments |
| AVDTP transport | `active` in every segment |
| SCO | never stalled; `sco_delta` median 4030 |
| Both controllers alive at end | yes |
| `same_controller` | **false** |

`survival_s` in the same orientation: **180 s, survived_full_duration**, both controllers
alive, SCO nominal at 136 pps. On one radio the same day: 56 s and a wedge.

### 4. WirePlumber drives two adapters unmodified

HFP (`bluez_output.5C_33_7B_CB_BF_C5.1`, mSBC) on `hci0` and A2DP
(`bluez_output.50_D7_1B_74_34_D6.1`, SBC) on `hci1` coexisted in one graph with no config
change. This was an open risk; it is retired.

### 5. An arbitrary Bluetooth speaker can be paired on a chosen adapter

The Boombox was discovered and bonded on `hci1` from scratch with no prior bond, and appeared
as an A2DP sink. Mode 1 is not tied to the pre-paired devices.

### 6. The watchdog recovered a wedged radio and a live call unaided

When the one-radio control wedged `hci0`, `bridge-btwatchdog` detected it, targeted the right
controller, and — after a forced firmware reload — re-established the phone and the eSCO link
without intervention. `pi/bridged/bt_watchdog.py` listed exactly this as **STILL OWED**
pending a live call. It is now observed once.

### 7. Output switching originally cost 5.9 s of uplink; live retarget is now sub-second

Measured with `rig/pi/measure/output_switch_probe.py` on a live Discord call, wired jack ->
Boombox, sampling `pw-link` at 0.2 s:

| Gap | Seconds |
|---|---:|
| **Uplink** (`output.bridge.mic` -> phone HFP sink) | **5.9** |
| Downlink (the chosen output receiving anything) | 0.5 |

So for ~6 seconds after the user changes speakers, **the far end cannot hear them.** The
mechanism is known and is not mysterious: `CallGraph.teardown()` stops `bridge.mic` along with
`bridge.callout`, and with the AEC enabled the rebuild cannot avoid it -- the AEC module's
playback target is fixed at load, so retargeting means restarting the module, which destroys
`bridge.aec.source`, which is what `bridge.mic` captures from. The 5.9 s is then the sum of
two 2 s supervisor polls plus module start and loopback attach.

Three mitigations were identified:

1. **Relink instead of rebuild.** `echo-cancel-playback` is a real node; unlinking it from the
   old sink and linking it to the new one would avoid restarting the AEC at all. Cheapest for
   the user, most invasive to the graph, and it would need E10/E12's timing re-verified.
2. **Skip the poll wait** for an output-only change, which is worth roughly 2 of the 5.9 s.
3. **Accept and announce it.** A chime already plays on the new output; a "switching" tone on
   the old one would at least make the gap legible rather than alarming.

n=1. Two attempted repeats were correctly refused by the probe's own guards after the speaker
dropped (see finding 8), so the number is one clean observation, not a distribution.

**Continuation (2026-08-24): mitigations 1 and 2 are implemented.** The supervisor now watches
the desired-output file at 50 ms intervals while retaining its slower full-graph poll. For an
output-only change it makes the new playback link before removing the old link, leaving the
Lark uplink, `bridge.mic`, and the AEC host running. Exact playback-target validation prevents
stale duplicate links, and a failed break rolls back the new link.

On the real Pixel HFP + Lark + Boombox hardware topology, with a synthetic call endpoint, the
wired -> A2DP selection applied in **0.476 s**. At 50 ms sampling the observed uplink and
downlink topology gaps were both **0.0 s**, the final graph was `ACTIVE`, and a subsequent
supervisor restart recovered the call graph. A post-reconnect acoustic preflight then confirmed
actual Boombox playback at the Lark mic with a **31.88 dB** level margin and no clipping.

This is strong hardware-topology evidence, but it is not yet the live-call acceptance result.
Repeat the switch during a real Discord or phone call and verify the far end retains the uplink
before closing the original finding.

### 8. The Boombox drops its A2DP link when idle, and nothing re-establishes it

Observed repeatedly: after a couple of minutes with no audio flowing, the speaker disconnects.
`desired` correctly stayed on the Boombox while `chosen` fell back to the wired output, which
is the designed behaviour -- but nothing on the Pi ever pages it back.

`bt_watchdog.py` reconnected the **phone** only, so in a car the speaker would leave mid-drive
and stay gone. **Now fixed and verified:** the watchdog pages the chosen speaker on its own
adapter with `ConnectProfile(0000110b)`, with its own budget (5 attempts, 30 s apart) kept
separate from the phone's. Measured: a force-disconnected Boombox was back **15 s later,
unattended, during a live call**, and the supervisor returned the output to it on its next poll.

The 2026-08-24 continuation closed the boot-time variant too. Output status now publishes the
speaker controller's permanent address (`A0:AD:9F:73:6C:24`) alongside diagnostic `hci1`, and the
watchdog resolves the permanent address on every attempt. With rfkill index 1 deliberately soft
blocked, the first watchdog attempt cleared only that controller, waited for BlueZ to confirm
`Powered=true`, and reconnected the Boombox. A post-recovery acoustic preflight measured the
speaker at the Lark 33.02 dB above idle with no clipping. The initial implementation exposed one
real timing edge -- BlueZ applied `Powered=true` just after returning a D-Bus error -- so power
recovery now owns the verified property after a bounded settle wait rather than the call result.

The speaker's budget is spent on a timer rather than on absence, unlike the phone's. A speaker
that is switched off should cost a few quiet retries and then silence, because pages are ACL
traffic and E03 is explicit about what ACL traffic near an active call costs.

### 10. A mid-call page on the OTHER radio does not disturb SCO

E07 measured paging a device during active SCO as its own failure mode, and that was the
reason to gate mid-call speaker pages. But E07 had **one** controller, where the page and the
voice link competed for the same radio.

Measured here with the speaker on `hci1` and the call on `hci0`: immediately after a
successful page, **SCO on hci0 held at 135 frames/s — exactly nominal** — with the supervisor
`ACTIVE` and `aec_verified: True`. E07's premise does not carry over to two radios.

n=1, so the same-adapter case is still gated. The consequence is a consistency fix: `bridgectl`
previously refused every mid-call page while the watchdog performed them anyway. Two
components disagreeing about one safety question is worse than either answer, so the gate is
now adapter-aware in both.

Not attributable either way: dmesg showed 70 cumulative `Frame reassembly failed` lines, but
that counter spans the whole boot and includes the earlier controller wedge, so no delta was
measured across the page.

### 9. Android will not route a VoIP call to the bridge without a fresh HFP connection

The Discord call sat in `MODE_IN_COMMUNICATION` with `Active communication device:
type:speaker name:Pixel 7a` and `mScoAudioState: SCO_STATE_INACTIVE`, while the bridge was
bonded, ACL-connected, and its HFP state machine read `mCurrentState: Connected`. The blocker
was `HeadsetService.mActiveDevice: null` -- Android had a connected headset it had not elected
as active, and `Telecom.isInCall(): false` because Discord does not register with Telecom, so
nothing in the Bluetooth stack believed a call existed.

**Disconnecting and reconnecting the phone's bond from the Pi fixed it**: Android elects an
active headset on a fresh connection. After the cycle, `mActiveDevice` was the bridge,
`mCurrentState: AudioOn`, `Active communication device: bt_sco_hs larkbridge-v2`,
`mScoAudioState: SCO_STATE_ACTIVE_INTERNAL`, and the supervisor reached ACTIVE with
`aec_verified: True`.

This is the same family as `docs/high-risk-untested.md` item 5 and E13 Finding 2, and it has a
consequence for the roadmap: the reliable lever is `AudioManager.setCommunicationDevice()`,
which needs an app on the phone. The existing `AudioInputRouter` project already implements
exactly that, including `TYPE_BLUETOOTH_SCO` matched by address
(`MicRouteController.kt:483-486`) and an explicit output override that wins over its own
matching logic. It is **not currently installed on the Pixel**.

## Caveats — read these before believing the result

- **n=1 for the dropout run and n=1 for orientation A survival.** E03's own methodology
  finding is explicit: with a 7 → 120 s spread, *n=3 is far too few*, and it set the bar for a
  convincing fix at "10/10 surviving the full duration". **This is 1/1.** Six repeats were
  launched and abandoned when the borrowed instrument had to be returned.
- **7 minutes, not 60.** The bar is defined over 60 minutes. A clean 7-minute run does not
  establish a 60-minute one, and E03 saw failures arrive as late as 121 s.
- **The peer differs between conditions.** The one-radio control used the iWorld; orientation A
  survival used the Boombox; the orientation A dropout run used the iWorld. Peer is therefore a
  partial confound across the survival comparison.
- **The iWorld is an unreliable fixture.** It repeatedly bounced between adapters and refused
  connections with `br-connection-page-timeout`, consistent with E03's suspicion of it.
- **No AEC measurement at all.** Hazard 1 — whether A2DP's added latency breaks echo
  cancellation — is entirely unmeasured. It is the risk most likely to kill Mode 1.
- **Orientation B unmeasured.** Call on the dongle, speaker on the onboard radio was planned
  and never run.
- **Whether btrtl patches this dongle is unresolved.** No `RTL:` firmware lines appeared even
  after a successful bring-up; it may run flash-resident firmware.
- The 7-minute run used a synthetic tone, not call audio. It measures the A2DP transport under
  SCO contention, not what a listener would hear on a call.

## Measurement bugs found and fixed, which invalidated earlier attempts

1. **AVDTP transport path.** `a2dp-survival.sh` matched `dev_XX/sep[0-9]+/fd[0-9]+`. On this
   BlueZ the object is `dev_XX/fd2` — `sep` and `fd` are siblings. The regex matched nothing,
   `transport_state()` returned `gone`, and a healthy link scored `survival_s = 1`. **A false
   negative that would have been written up as "A2DP dies instantly on two radios".**
2. **`bluetoothctl` follows its default adapter**, which became the dongle the moment it
   enumerated. `bluetoothctl info <phone>` answered "Device not available" for a connected
   phone. Three call sites were affected, including `bt_watchdog`'s unattended reconnect.
3. **A device bonded on two adapters** resolved to the lowest-sorted path rather than the one
   it was connected on, pointing the poller at the wrong controller.
4. **`systemd-rfkill` persists soft-block state keyed on USB port path.** The dongle arrived
   soft-blocked and so never ran controller setup, presenting as a dead adapter rather than a
   blocked one. Moving it between ports reproduced the block with a fresh rfkill index.

## What remains

AEC under A2DP latency (hazard 1), orientation B, ≥10 repeats per orientation, the 60-minute
soak, cold-boot dual-link establishment, mid-call power cut, speaker out-of-range recovery, and
the full Mode 1W regression suite.

## Verdict

**Not yet — but no longer blocked for the reason E03 gave.** Two controllers demonstrably clear
the failure that one controller demonstrably still exhibits, measured the same day on the same
rig with the same instrument. That is a real change in the risk.

It is not the bar. Seven minutes at n=1 is not sixty minutes at n=10, and the AEC question the
brief calls the likeliest killer has not been measured at all. Mode 1 stays **an option, not
the default**, until those land.
