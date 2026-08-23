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
