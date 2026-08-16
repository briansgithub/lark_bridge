# ADR-0005 — UAC2 primary, UAC1 fallback, decided empirically at M9.3

- **Status:** Accepted (provisional — M9.3 may flip the primary)
- **Date:** 2026-08-15
- **Relates to:** `PLAN.md` §7.1, §7.2, risk R3, R15

## Context

The brief asks explicitly whether UAC1 is preferable to UAC2 for Android compatibility and Full-Speed
bandwidth. The evidence points in two directions at once:

- AOSP's own documentation commits only to "limited USB Audio Class 1 (UAC1) support" in host mode,
  listing PCM 16/24/32-bit at rates including 48 kHz. It does not promise UAC2.
- Field reports say Pixel 6 and later handle UAC2 devices (USB-C IEMs) fine, and Android 10 added
  broader UAC2 support — but OEMs can and do gate it.
- TinyUSB's mature, maintained, widely-used example is `uac2_headset` (UAC2). TinyUSB does support
  UAC1 descriptors, but with far less exercised example code.

So UAC2 is the better-supported *firmware* path and the less-documented *Android* path. UAC1 is the
reverse. Bandwidth does not decide it: 48 kHz S16 stereo out (192 B/frame) plus mono in (96 B/frame)
is trivial against the Full-Speed isochronous limit of 1023 B/frame.

## Decision

Build **both** descriptor sets from day one behind `-DBRIDGE_UAC=2|1`, sharing all non-descriptor
code (`desc_uac2.c` / `desc_uac1.c`, one selected at compile time). Ship UAC2 as the default.
Make "does the Pixel 7a enumerate it and route audio to it" an explicit **milestone gate at M9.3**,
tested on the actual phone, with the result recorded in `docs/experiments/E05`.

Both variants are built in CI (`PICO_ENVS = pico pico_uac1`) so the fallback can never bit-rot.

## Consequences

- The compatibility question is answered by the Pixel, not by us guessing. This is the correct
  epistemic posture: the brief itself warns that successful enumeration does not imply Android will
  *use* the device for communication.
- Descriptor sets are **static arrays in git**, diffable and reviewable. We explicitly reject
  libraries that synthesise descriptors at runtime — the descriptor bytes are the contract with
  Android and must be inspectable without running anything (see ADR-0008, risk R15).
- Deliberate descriptor choices, both variants: **one** sample rate (48 kHz) and **one** non-zero alt
  setting per streaming interface. This directly avoids TinyUSB issue #1728, which is a state-machine
  failure during repeated `Set Interface` / rate-change cycles.
- Mono microphone IN, stereo playback OUT — what a real headset looks like, and half the IN bandwidth.
- Cost: two descriptor files to maintain and two CI build variants.

## Alternatives considered

- **UAC2 only.** Rejected: leaves no move if the Pixel refuses it, and AOSP documents only UAC1.
- **UAC1 only.** Rejected: TinyUSB's UAC1 path is less exercised, and it forfeits UAC2's explicit
  feedback endpoint, which is our primary drift-control mechanism under ADR-0004.
- **Runtime-switchable via a control request or GPIO.** Rejected as premature: it doubles the
  enumeration state space to solve a problem we can settle with one experiment and a rebuild.
