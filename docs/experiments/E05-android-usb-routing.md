# E05 — Does the Pixel 7a accept the Pico as a USB headset, and route calls to it?

- **Status:** Not started
- **Resolves risks:** R3 (score 12), R7, R15
- **Gates milestones:** M9.1 (toolchain), M9.3 (enumeration), M12 (application routing)
- **Scripts:** `tests/stage-f-pico-usb/`, `tests/stage-h-full-usb/`

## Question

Four separable questions, deliberately ordered cheapest-first:

1. **M9.1 toolchain gate.** Does the TinyUSB bundled with the arduino-pico core contain
   PR #1802 (the fix for the RP2040 `uac2_headset` hang, issue #2838)?
2. **M9.3 enumeration.** Does the Pixel 7a enumerate the Pico as an audio device —
   as **UAC2**, and if not, as **UAC1**?
3. Does Android use it for **capture** (recorder app) and **playback**?
4. **M12 routing.** Does Android route *communication* audio — native cellular calls,
   Discord, other VoIP — to it?

## Why it cannot be answered by reading

Question 1 is a `grep`, and should be done before writing any descriptor code.

Questions 2–4 are the empirical heart of the project. AOSP documents only "limited USB
Audio Class 1 (UAC1) support" in host mode; field reports say Pixel 6+ handle UAC2 fine;
OEMs can gate either. And critically — as the brief itself notes — **successful enumeration
does not imply Android will use the device for every communication application.** Audio
policy for telephony routing to USB is undocumented and app-dependent.

A negative result here is a *successful* experiment. Mode 2 exists as a secondary path
precisely because this is uncertain.

## Method

### Question 1 — toolchain gate (do this first, costs minutes)

```bash
# Locate the core's vendored TinyUSB, then check for the PR #1802 fix.
find ~/.platformio/packages -path '*tinyusb*' -name 'audio_device.c' | head
# Record verbatim:
pio pkg list -d pico            # core version
git -C <tinyusb path> rev-parse HEAD   # if it is a checkout
```

Record the core version and TinyUSB SHA in the table below **before** writing descriptors.
If the fix is absent: bump the core, or demote PlatformIO to editor-only and build via
CMake with a pinned TinyUSB (the escape hatch ADR-0008 exists for).

### Questions 2–3 — enumeration

Flash `pico/test/target/tone_only`, then on the Pixel: does a USB audio device appear? Does
a voice-recorder app capture the 1 kHz tone? Try UAC2 first, then rebuild with
`-DBRIDGE_UAC=1` and repeat. Test on a Linux host first — a failure there is a firmware
bug, a failure only on Android is a policy problem, and distinguishing them saves hours.

### Question 4 — application routing matrix

For each of {Mode 1, Mode 1W, Mode 2} × {native call, Discord, WhatsApp/Signal, Meet,
voice recorder}: does Android offer the device? Use it for mic? For playback? What does the
in-call audio picker show? Does it survive app switch, screen lock, a second incoming call?

## Runs

### Toolchain

| # | Date | arduino-pico version | TinyUSB SHA | PR #1802 present? |
|---|---|---|---|---|
| 1 | | | | |

### Enumeration

| # | Date | UAC | Host | Enumerates? | Capture works? | Playback works? |
|---|---|---|---|---|---|---|
| 1 | | 2 | Linux | | | |
| 2 | | 2 | Pixel 7a | | | |
| 3 | | 1 | Pixel 7a | | | |

### Application routing

| Mode | App | Offered? | Mic? | Playback? | Notes |
|---|---|---|---|---|---|
| 1 | native call | | | | |
| 1 | Discord | | | | |
| 1W | native call | | | | |
| 2 | native call | | | | |
| 2 | Discord | | | | |

## Result

_(fill in)_

## Verdict

_(fill in per question)_

## Consequences for the plan

| Finding | What happens |
|---|---|
| PR #1802 absent | Bump core, or switch to the CMake build. R15 realised; ADR-0008's escape hatch is used. |
| UAC2 enumerates | ADR-0005 confirmed. Ship UAC2. |
| UAC2 refused, UAC1 works | Flip ADR-0005's default to UAC1. Lose the explicit feedback endpoint — R7's drop/dup fallback becomes the primary drift control, not the backup. |
| Neither enumerates | Escalate to `PLAN.md` §15. Mode 2 is not achievable on this phone. |
| Enumerates but calls do not route | **Acceptance criterion 13 is satisfied by documenting this.** Mode 2 becomes a recording/media path, not a call path, and Mode 1/1W carry the product. |

## Follow-up questions this raised

_(fill in)_
