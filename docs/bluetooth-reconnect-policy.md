# Reconnect policy: only chase the phone when it did not leave on purpose

Status: design agreed, not yet implemented.
Date: 2026-08-23

## Why

`bt_watchdog.py` already has `phone_connected()` and `reconnect_phone()`, and
`bluetoothctl connect` from the Pi demonstrably works — E13 used it to restore the ACL
after the Pixel dropped. But the call is unreachable in the common case.

The loop only reaches it inside the `else` branch, i.e. **after the controller wedges and
`bt-reset.sh` recovers it**. When the controller is healthy and the phone simply
disconnects — out of range, or what happened repeatedly during E12/E13 — the watchdog
does nothing at all. It probes the controller; it never probes the phone.

## The signal

Every disconnect carries an HCI reason code, readable with `btmon` (present on the unit,
verified opening the monitor socket).

| Code | Meaning | Reconnect? |
|---|---|---|
| `0x08` | Connection Timeout — out of range, RF loss | **yes** |
| `0x13` | Remote User Terminated — BT switched off, or disconnected in settings | no |
| `0x16` | Terminated By Local Host — we closed it | no |
| n/a | unpaired (`Paired: no`) | impossible anyway |

## Policy

- **`0x08`** → reconnect with exponential backoff.
- **`0x13` / `0x16`** → stay quiet until something changes.
- **Reason unknown** (after a reboot or a controller reset, when nothing was listening —
  which is exactly the post-power-cut case): **one attempt, then stop.** The unit comes
  back on its own after a power cut without needing a human, while a deliberate
  disconnect is not repeatedly overridden.

## Three things this deliberately does not claim

- **`0x13` is a hint, not proof of intent.** Android emits it when its stack crashes or
  when power management tears down an idle link. Sometimes we will decline to reconnect
  when we should have.
- **"Picked another output" is usually not a disconnect at all.** E13 measured Android
  keeping the ACL up and routing the call to its own earpiece. No disconnect event fires,
  so no reconnect logic can help. Separate problem, still unfixed.
- Nothing here helps if the phone is out of range for a long time; backoff must cap so a
  parked car does not sit retrying.

## Shape

A small `btmon` consumer records the last disconnect reason per device to
`/run/larkbridge/last-disconnect.json`; `bt_watchdog` consults it before calling the
`reconnect_phone()` it already has. Reading HCI is an established pattern in this repo —
see `rig/analysis/btsnoop_window.py`.

Note `/run` is tmpfs, so the record is deliberately lost on reboot. That is correct: a
reboot means we genuinely do not know why we are disconnected, which is the
"unknown → one attempt" case above.
