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

## Implemented 2026-08-23 — bounded attempts, not reason codes

The premise this document was written on turned out to be wrong in a way worth recording.

**The phone does not reconnect on its own.** The design here assumed the Pi should mostly stay out
of the way and let Android re-initiate, with reconnect logic reserved for unusual cases. Measured on
the hardened card: after a power cut the Pixel was watched for **130 s** while the Pi sat
discoverable, bonded and trusted, and it never re-initiated. A Pi-initiated connect succeeded in
20 s. In a car there is nobody to tap the phone, so the Pi has to make the call in the *ordinary*
case, not the exceptional one.

**What was implemented** (`cd5dbd4`) is the bounded-attempt half of this policy, not the
reason-code half:

- When the controller is healthy but the phone is absent, wait `RECONNECT_DELAY` (so Android is not
  raced when it *does* choose to re-initiate), then initiate.
- At most `RECONNECT_ATTEMPTS` (default 3) attempts. The budget resets **only** on a successful
  connection.

**The btmon reason-code consumer was not built.** The bounded budget approximates the intent:
after an unintentional drop the Pi re-establishes within about 40 s, and after a deliberate
departure it makes three failed attempts and then goes quiet rather than fighting the operator —
the failure mode of `e6f4139`, where the bridge fought another app for the communication route.

This approximation is weaker than the agreed policy in one specific way: **a deliberate
disconnection still costs three attempts.** If the operator switches Bluetooth off and stays
nearby, the Pi will try three times before giving up. That is a few seconds of pointless radio
activity, not a functional problem, which is why the reason-code work was not treated as blocking.
Build it if the three attempts ever prove to be a nuisance in practice.
