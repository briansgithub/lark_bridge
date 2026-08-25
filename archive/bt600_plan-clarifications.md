# Dual-USB Bluetooth Plan — Confirmed Clarifications

Confirmed by the user on 2026-08-24. These clarifications are part of the controlling implementation requirements alongside `bt600_plan.md`.

The archived plan is a line-ending-normalized copy of `H:\Downloads\bt600_plan.md`. The original attachment's SHA-256 is `AD9511ED800E8471327097AF39B0A6FE5A5BF798DB1256348BA436E339D50434`; its text differs from the repository copy only by newline normalization and the repository's final newline.

1. The archived plan is the controlling requirements document and must be included in the Phase 4 independent review context.
2. Controller resolution may use any stable device information needed to disambiguate the supplied ASUS USB-BT500 and USB-BT600. Behavior and role assignment must remain unchanged when either adapter is moved to any Raspberry Pi USB port; USB port, topology, enumeration order, `hciX`, and BlueZ default-adapter state are never identity inputs.
3. The transitional onboard call role is represented with `adapter_bus = "UART"` and omitted USB VID/PID fields. Transitional readiness may accept it; final post-cutover readiness must reject it.
4. Deliberate fault-injection verdicts may allow expected target-only teardown and re-enumeration events inside precisely timestamped injection windows. Any off-target event, target event outside the window, or unexplained disruption remains a failure.
5. Take the source SD card offline/read-only before imaging or hashing it, to protect the sole physical rollback from host automount writes.
6. The diagnose/fix/retest ceiling is five cycles per failure throughout this plan implementation. This supersedes the original two-cycle ceiling in `bt600_plan.md`.
7. Phase 4 may use a configurable abbreviated soak of 5–10 minutes (300–600 seconds) for predeployment harness validation. The harness must remain capable of the full 3,600-second campaign, and Phase 7 must run that full hour before Phase 8. An abbreviated pass must be recorded as not satisfying the endurance qualification gate.
8. Phase 5 may bypass the unsuccessful online zram deployment because the sealed appliance's tmpfs overlay cannot persist it. Preserve the original card unchanged and offline/read-only, image and clone it to the spare card, then apply only the reviewed pure-zram configuration and storage-guard ordering fix to the spare card's immutable lower root before its first acceptance boot. The candidate must show zero failed units and zero restart records before BT600 support is installed.
