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
