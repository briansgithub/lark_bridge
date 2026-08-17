# E08 — Half-open HFP link: the Pi holds a link the phone has already abandoned

- **Status:** OPEN — observed 2026-08-16, cleared manually, cause not yet established
- **Relates to risk:** R17 (unattended recovery). Directly threatens the car use case.
- **Severity:** **high** — the failure is silent, self-sustaining, and locks the user out.

## What happened

After a Discord call ended, the two ends disagreed about whether they were connected, and
stayed that way indefinitely.

Phone (`dumpsys audio`):

```
mScoAudioState: SCO_STATE_INACTIVE
Computed Preferred communication device: null
Active communication device: ... type:earpiece ... name:Pixel 7a
```

Pi, at the same moment:

```
Connections:
    > ACL  5C:33:7B:CB:BF:C5 handle 12 state 1 lm PERIPHERAL AUTH ENCRYPT
    > eSCO 5C:33:7B:CB:BF:C5 handle 6  state 1 lm PERIPHERAL
```

`bluetoothctl info` reported `Connected: yes`. The eSCO link was not merely registered but
*live*: `pw-top` showed `bluez_output…` running at S16LE/1ch/16000 with real quantum timings,
i.e. the Pi was still transmitting mSBC frames into a call that no longer existed.

**Operator symptom:** the Pixel's Bluetooth settings could not connect to `larkbridge`, and
because it would not connect, no call could be routed to it. The system was unusable and did
not recover on its own.

## Why this matters more than it looks

This is the exact class of event the product must survive. From `PLAN.md` §15 (answered) the
bridge must tolerate *"sudden disconnects if/when the car turns off during a call or when the
call is ended by the phone."* This is the second of those two, and it did not merely fail to
recover — **it actively blocked reconnection**.

The failure is also invisible from the Pi's own health checks. Every indicator we monitor looked
healthy: `hci0` `UP RUNNING`, no HCI errors (`errors:0` both directions), SCO frames flowing at
the nominal rate, supervisor reporting both legs `verified`. A liveness check built on any of
those would have reported the bridge as fine.

## Recovery that worked

```bash
bluetoothctl disconnect 5C:33:7B:CB:BF:C5
```

Clean and immediate — `Disconnection successful`, connection table empty. The controller did
**not** need resetting, so this is **not** the E07 wedge and must not be conflated with it. The
supervisor then correctly observed `call DOWN` and tore down both loopbacks, which is the
teardown path behaving exactly as designed.

Cost: one command, but a command **nobody is present to type** in a car.

## What is not yet known

1. **What left eSCO up.** The call ended during a period when the supervisor was restarting
   loopbacks repeatedly (the `-P` bug, fixed in 3751890). Whether that churn caused it, or
   merely coincided with it, is unestablished. It must be reproduced on the *fixed* build
   before any cause is claimed.
2. **Whether it is reproducible at all.** n=1.
3. **Which side is at fault** — whether the phone sent a Disconnect the Pi ignored, or never
   sent one. Deciding this needs a `btmon` capture across the call teardown, which was not
   running. **Run `btmon` for the reproduction attempt** — without it this stays a guess.
4. **Whether link supervision timeout would eventually clear it.** The link was killed by hand
   after minutes; it was not left to see whether the controller ever times it out. If the phone
   is genuinely gone (ignition cut), supervision timeout *should* fire — but here the phone was
   present and responsive, which is precisely the case where it will not.

## Candidate fix, once the cause is known

A watchdog is the obvious shape, but note the trap: **SCO frame flow is not proof of liveness.**
The Pi was happily transmitting into a dead call. Any liveness test must be based on something
the *phone* asserts — e.g. HFP indicator state over RFCOMM (`+CIEV` call status), or an
`AT+CIND?` poll — not on whether audio is moving.

Cheaper and more robust alternative, worth evaluating first: on `call DOWN`, have the supervisor
verify the *link* also went away, and drop it if it did not. That reuses a transition we already
detect reliably rather than adding a new polling mechanism.

Deliberately **not** implemented yet: with n=1 and no capture, any fix would be guessing at a
mechanism, and a watchdog that disconnects on a false positive would drop live calls — a cure
worse than the disease.

## Reproduction protocol

1. Start `btmon -w e08.btsnoop` on the Pi **before** the call.
2. Establish HFP, route a call to `larkbridge`, confirm audio both ways.
3. End the call **from the phone**.
4. Within 30 s check `hcitool con` and `bluetoothctl info` on the Pi.
5. If ACL/eSCO persist, leave them for 5 minutes to test whether supervision timeout clears it.
6. Then attempt to reconnect **from the phone** and record whether it is refused.

Repeat ×5 before drawing any conclusion; a fault that appears once in five is still fatal in a
car but needs a different fix from one that happens every time.
