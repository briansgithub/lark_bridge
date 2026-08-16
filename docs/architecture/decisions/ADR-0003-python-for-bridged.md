# ADR-0003 — `bridged` is Python; nothing in the sample path is

- **Status:** Accepted
- **Date:** 2026-08-15
- **Relates to:** `PLAN.md` §4.1, §4.9

## Context

The brief asks for an explicit language choice per component, warns against custom native software
where PipeWire/BlueZ already solve the problem, and equally warns against complex shell pipelines
where a small persistent daemon would be more robust.

After ADR-0002 the daemon's scope is much smaller than it first appears. It does **not** move audio
and does **not** manage links. It owns: the mode state machine, BlueZ connection lifecycle, health
checks, the recovery ladder, and the status/IPC surface. That is an I/O-and-policy component:
D-Bus in, JSON out, subprocess to `pw-dump`.

## Decision

`bridged` and `bridgectl` are **Python 3.11+**, shipped as one package with two console entry points,
installed into a venv owned by the `bridge` user.

The rule that makes this safe, recorded in `docs/development/style.md`:

> If a component touches PCM samples in the steady state, it is not Python — and preferably it does
> not exist, because PipeWire already does that job.

## Consequences

- Python's async D-Bus support (`dbus-fast`) is the best available for BlueZ's `ObjectManager`
  interface, which is the daemon's single largest external surface.
- The component is readable under pressure. Debuggability is an explicit design goal in the brief and
  this component exists mostly to explain what went wrong.
- No GC pause or interpreter latency can affect audio, because no audio passes through it. The
  falsifiable form of this claim is ADR-0002's acceptance criterion: kill the daemon mid-call and
  audio continues.
- `bridgectl` talks only over the IPC socket, never directly to PipeWire or BlueZ, so the CLI and the
  daemon can never report contradictory state.
- Cost: a Python runtime and venv on the appliance (~30 MB), and a dependency set to keep current.
  Acceptable on 1 GB of RAM.

## Alternatives considered

- **Rust with `pipewire-rs`.** Tempting for the single static binary and event-driven registry
  listening. Rejected because ADR-0002 removed the event-driven link management that would have
  justified it; what remains is D-Bus plumbing where Rust's ergonomics are worse and iteration is
  slower. Revisit only if a measured need appears.
- **Shell + `pw-link` + `bluetoothctl`.** Rejected: exactly the "complex shell pipeline" the brief
  warns about. No state machine, no health model, no structured status.
- **C/C++.** Rejected: no performance requirement justifies it, and it would make the most
  frequently-read component the least readable.
- **Extend WirePlumber in Lua instead of a separate daemon.** Rejected for the same reasons as in
  ADR-0002, plus: pairing, recovery ladders and a CLI surface do not belong in a session manager.
