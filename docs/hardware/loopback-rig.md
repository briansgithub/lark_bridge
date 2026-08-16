# The measurement rig: audio hardware

How the Pi hears its own output, so dropouts get counted by a script instead of by a human
listening for an hour. Measured on real hardware 2026-08-16 — this is not a proposal.

## Devices and roles

Roles are bound to **USB port paths**, never to ALSA card numbers or names.

| Role | Port | USB ID | Device | Streams |
|---|---|---|---|---|
| **Lark** — mic under test | `1-1.3` | `3547:0407` | Hollyland Wireless Microphone | capture |
| **Dongle A** — DUT wired output | `1-1.5` | `001f:0b26` | Generic AB13X | capture + playback |
| **Dongle B** — instrument | `1-1.4` | `0d8c:0014` | C-Media USB Audio Device | capture + playback |

`rig/pi/measure/devices.sh` resolves role → card at run time.

### Why port paths, not card numbers or serials

Two independent reasons, both measured:

1. **ALSA card order follows enumeration order** and changes across reboots and replugs. The
   two AB13X dongles appeared as `Audio` and `Audio_1` purely by which enumerated first.
2. **Both AB13X dongles report the identical USB serial `202405280846`.** Serial numbers cannot
   distinguish them. This is exactly the failure the plan predicted for cheap dongles.

## Measured capabilities

**Lark A1** — 48 000 Hz **only**, S16_LE / S24_3LE, 2 channels, no mixer controls at all.
The fixed 48 kHz is a good result: it matches the graph rate in ADR-0007 exactly, so there is
**zero resampling at the first hop**. The absence of any gain control confirms the plan's
decision to apply mic gain on the PipeWire `bridge.mic` node rather than a device mixer — that
turned out to be the only option, not a preference.

**Dongle B (C-Media)** — the instrument:

| | |
|---|---|
| Capture | **mono**, 44 100 / 48 000 Hz, S16_LE, gain **−12 … +23 dB** |
| Playback | stereo, 44 100 / 48 000 Hz, S16_LE, −37 … 0 dB |

## The AB13X combo-jack trap

**Symptom:** after inserting a 3.5 mm plug, both AB13X dongles stopped offering capture. `pcm0c`
vanished, `arecord -l` no longer listed them, and `arecord` failed with
`audio open error: No such file or directory`.

**Cause:** the AB13X is a **single combo-jack** adapter. Inserting a 3-pole TRS plug makes it
decide "headphones, no microphone" and it **re-enumerates with a different descriptor set that
has no AudioStreaming input interface at all.** The kernel log shows this plainly:

```
usb 1-1.4: reset full-speed USB device number 6 using dwc_otg
usb 1-1.4: device firmware changed
usb 1-1.4: USB disconnect, device number 6
usb 1-1.4: new full-speed USB device number 7 using dwc_otg
usb 1-1.4: [2] FU [PCM Playback Volume] ch = 2, val = -11520/0/1     <- playback only
```

Confirmed by reversal: unplugging the cable restored `pcm0c` on both.

**Consequence:** an AB13X cannot be the instrument. It can only ever be an *output*, which is why
dongle A stayed an AB13X (it only drives the Mode 1W wired output) and dongle B was replaced with
a C-Media card that has two physically separate jacks.

**Rule:** any device whose capture interface can disappear when a plug is inserted must not be
used for measurement.

## Mixer state for measurement

Applied by `rig/pi/measure/set-mixer.sh`. **Re-run after every reboot or replug** — ALSA mixer
state does not reliably persist, and a silently-different mixer invalidates every measurement.

| numid | Control | Value | Why |
|---|---|---|---|
| 9 | Auto Gain Control | **off** | Ships **on**. AGC continuously rescales the input — it flattens the level differences we are measuring and can hide dropouts by pumping gain during silence. **An instrument with AGC is not an instrument.** |
| 8 | Mic Capture Volume | 0 (−12 dB) | Minimum, for maximum headroom against hot sources |
| 7 | Mic Capture Switch | on | |
| 3 | Mic Playback (sidetone) | **off** | Otherwise the input leaks into the output and fakes a loopback |
| 6 | Speaker Playback Volume | 37 (0 dB) | Reference level |

Set by **numid**, not by name: `amixer sset` name matching is ambiguous on this card — `Mic`
carries both a playback and a capture volume — and it fails *silently*, leaving the old value.

## Cabling states

Dongle B has one input, and more than one thing wants it. Moving one patch cable between test
groups is an accepted limitation of a two-device rig.

| Tests | Dongle B green (out) | Dongle B pink (in) |
|---|---|---|
| **U13** calibration | patch cable ─┐ | └─ same cable (loop B to itself) |
| **U14/U15** Lark path | speaker | free |
| **U20–U22**, Mode 1 | speaker | BT receiver line-out |
| Mode 1W capture | speaker | dongle A's output |

## Open issue: input level

The C-Media input is **mic level** (~10 mV expected). A Bluetooth receiver's line-out is
500 mV – 1 V, roughly 30–40 dB too hot. Available software attenuation is only 12 dB, so an
inline attenuator is likely required. **U13 measures the actual figure** rather than assuming it.
