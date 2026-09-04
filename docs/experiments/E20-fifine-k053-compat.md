# E20 — FIFINE K053 lavalier compatibility

**Status:** host implementation complete; live routing qualification not yet run

**Observed:** 2026-09-03
**Retail reference:** FIFINE K053, ASIN `B077VNGVL2`

## Objective

Add the attached K053 as the second microphone candidate: live Lark A1 first, K053 second, and
K054 gooseneck third. It must use the same identity-qualified, break-before-make AEC path as every
other candidate. Its built-in monitor output must never replace the configured Pi AUX output.

## Read-only characterization

The attached unit reported:

| Property | Observation |
|---|---|
| USB identity | `0c76:161f`, USB 1.10 full-speed, revision `0100` |
| Descriptors | product `USB PnP Audio Device`; no manufacturer or serial string |
| Observed topology | `1-1.5` (observation only; not configured) |
| Interfaces | Audio Control, playback streaming, capture streaming, and HID |
| Capture | mono, S16_LE, 48 kHz |
| Playback | stereo, S16_LE, 48 kHz monitor output |
| PipeWire source | `alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback` |
| PipeWire component | `USB0c76:161f` |

A direct two-second native capture completed successfully with:

```sh
arecord -D hw:2,0 -q -t raw -f S16_LE -r 48000 -c 1 -d 2 /dev/null
```

The card number `2` is observation-only and must not appear in configuration.

## Decisions

- Match portably by normalized VID/PID, product text, PipeWire component identity, and native
  capabilities; leave serial and port constraints blank.
- Accept the same residual lookalike-device risk as the K054. Multiple matching physical devices
  remain ambiguous and fail closed unless an operator pins a port.
- Record `capture_only = false` because playback exists, while disabling only the K053
  `Audio/Sink` through a model-specific WirePlumber rule.
- Keep the Pi AUX jack as the sole configured local output.
- Preserve existing Lark transmitter-liveness selection and place K053 ahead of K054.

## Qualification boundary

The descriptor, format, and direct-capture observations establish a usable hardware fingerprint;
they do not establish retail replacement-unit consistency, physical-control behavior, acoustic
quality, AEC performance, hotplug safety, reboot stability, or endurance. Do not record those as
passing until the planned automated checks and abbreviated live routing matrix actually run.

## Host validation

- Bridge resolver, supervisor, CLI, output-policy, and loader suite: `194 passed, 26 subtests
  passed`.
- Affected rig, boot, harness, installer, and release-packaging suite: `154 passed, 1 skipped, 66
  subtests passed`.
- Ruff passed for all changed Python files. Shell syntax and Git whitespace checks passed;
  ShellCheck was unavailable on this workstation.

## K053 gain diagnosis

A synchronized active-call sweep used a fixed synthetic speech source 30–45 cm from the lavalier.
The raw physical source and post-AEC HFP uplink were captured together; temporary audio was deleted
after reduction. PipeWire error counters did not increase and no sample clipped.

| Hardware gain | Raw peak | Post-AEC peak | Verdict |
|---:|---:|---:|---|
| +20 dB | -20.71 dBFS | -21.31 dBFS | Too quiet |
| +23 dB | -31.74 dBFS | -36.04 dBFS | Operator-selected after listening check |
| +25 dB | -13.49 dBFS | -13.76 dBFS | Best measured level |
| +28 dB | -9.01 dBFS | -9.37 dBFS | Insufficient headroom |

At +25 dB, the post-AEC silence floor was -50.69 dBFS, giving 21.56 dB measured SNR against
the -29.13 dBFS speech RMS. The operator subsequently selected +23 dB to reduce the far-end
level further. The K053 control is notably non-linear: the two-dB requested reduction produced
an approximately 18-dB raw peak reduction in the repeated acoustic test. The supervisor therefore
converts the configured dB value to the device's advertised raw step and verifies that exact raw
readback rather than trusting `amixer cset` to interpret a `dB` suffix.
