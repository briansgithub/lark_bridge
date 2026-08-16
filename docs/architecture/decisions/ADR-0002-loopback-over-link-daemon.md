# ADR-0002 — Routing is declarative via `module-loopback`, not an imperative link daemon

- **Status:** Accepted
- **Date:** 2026-08-15
- **Relates to:** `PLAN.md` §4.3, §5, acceptance criterion 7

## Context

The audio graph must survive: Bluetooth disconnect/reconnect, Lark unplug/replug, Pico
re-enumeration, PipeWire restart, BlueZ restart, and reboot — with no human running `pw-link`. The
brief lists five candidate mechanisms: WirePlumber Lua, PipeWire config, `pw-link`, a custom
PipeWire client, and ALSA loopback devices.

The failure mode we are designing against is subtle. Any approach where *some process observes a
device appearing and then creates links* has a reconnect race: the daemon must be running, must see
the event, must not have crashed, and must not double-link. That process becomes a single point of
failure sitting in the audio path's control plane.

## Decision

Declare persistent named endpoints with `libpipewire-module-loopback` in
`pi/pipewire/pipewire.conf.d/20-bridge-endpoints.conf`:

- `bridge.mic` — capture side follows the Lark; playback side targets the current uplink
- `bridge.callout` — capture side follows the call downlink; playback side targets the current output
- `bridge.tap` — optional diagnostic capture, disabled by default

`bridged` sets only `target.object` on these nodes. It never creates or destroys links.
**`pw-link` is banned from production code**; it is permitted inside `tests/` where an imperative
snapshot is exactly what is wanted.

## Consequences

- A loopback node exists whether or not its target does. When a Bluetooth device returns, PipeWire
  reattaches on its own — **no daemon is in the reconnect path**.
- Falsifiable acceptance criterion: *killing `bridged` mid-call must not interrupt audio.* If it
  does, imperative link management has leaked in and must be removed, not supervised. This is
  criterion 7 in `PLAN.md` §14.
- Each leg gets independent buffering and latency control, and a natural insertion point for gain,
  mute, sidetone and (later) AEC — see ADR-0001's claim that Mode 1↔1W is a one-string change.
- Mode switching becomes metadata writes, so it is atomic-ish and trivially reversible.
- Cost: two extra graph nodes per direction, and a small amount of extra copying. Irrelevant against
  a 48 kHz mono voice path on a Pi 3.

## Alternatives considered

- **`pw-link` from shell on udev/D-Bus events.** Rejected: imperative solution to a declarative
  problem; racy on reconnect; unobservable when it half-fails.
- **Custom PipeWire client that manages links.** Rejected: this is re-implementing
  `module-loopback`, worse, and putting it on the critical path.
- **WirePlumber Lua link policy.** Rejected as the primary mechanism: the API churned significantly
  between 0.4 and 0.5, and Lua stack traces at 3 a.m. on a headless Pi are a poor debugging surface.
  WirePlumber keeps device/profile policy, which is what it is good at.
- **ALSA loopback (`snd-aloop`).** Rejected: adds a second buffering/clock domain and takes the
  routing decision out of PipeWire, which then has to be reconciled with it anyway.
