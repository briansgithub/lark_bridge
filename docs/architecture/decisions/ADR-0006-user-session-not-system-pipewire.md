# ADR-0006 — PipeWire runs in a lingering user session, never system-wide

- **Status:** Accepted
- **Date:** 2026-08-15
- **Relates to:** `PLAN.md` §4.1, §11, milestone M2

## Context

This is a headless appliance. There is no desktop, no graphical login, and no active logind seat.
PipeWire and WirePlumber are normally user services started by a session. Two ways to get audio on a
headless box:

1. Run PipeWire system-wide (`--system`-style). Upstream discourages this; it loses per-user
   security boundaries, is a poorly-tested configuration, and several classes of bug only appear
   there. A field report of WirePlumber segfaulting specifically *"when running as a system service"*
   during HFP connection handling is direct evidence of this class of problem, and HFP is our
   critical path.
2. Run them as normal user services for a dedicated unprivileged user whose session persists without
   a login.

Separately, PipeWire's BlueZ monitor defaults to following the active logind seat, which on a
headless machine means "no seat, no Bluetooth audio".

## Decision

Create a dedicated unprivileged user `bridge`, enable `loginctl enable-linger bridge`, and run
`pipewire`, `wireplumber` and `bridged` as **user** units under it. Set
`monitor.bluez.seat-monitoring = "disabled"` in WirePlumber so Bluetooth audio works without an
active seat.

Realtime scheduling comes from `rtkit` via the system D-Bus where available, with explicit
`/etc/security/limits.d/95-bridge-rtprio.conf` as the fallback — not from running as root.

## Consequences

- We stay on the well-tested upstream configuration, which matters most precisely on the path
  (HFP) where we already carry the most risk.
- `systemctl --user -M bridge@ status pipewire wireplumber` works over plain SSH with no login
  session — this is the M2 acceptance test, run across 5 reboots.
- Service ordering is expressible normally: `bridged.service` has `After=wireplumber.service`,
  `PartOf=bridge.target`.
- The appliance keeps a real privilege boundary: nothing in the audio path runs as root. Only
  provisioning (`install.sh`) and `bridge-btfw.service` need root, and the latter does three HCI
  calls and exits.
- Gotcha to document in `operations/troubleshooting.md`: `XDG_RUNTIME_DIR` must be set correctly for
  anything talking to the socket, which is why `bridgectl` resolves it explicitly rather than
  assuming the invoking shell has it.
- Cost: one extra user, and `enable-linger` is an easy step to forget when provisioning by hand —
  so `70-verify.sh` checks for it explicitly.

## Alternatives considered

- **System-wide PipeWire.** Rejected on the evidence above; it concentrates risk exactly where we
  have least margin.
- **Run as the default `pi`/`admin` user.** Rejected: couples the appliance to an interactive
  account whose session state is not under our control, and makes the audio graph vulnerable to
  someone logging in and out.
- **A graphical autologin session to satisfy logind.** Rejected: installing a display stack on a
  headless appliance to work around a config flag is absurd when the flag exists.
