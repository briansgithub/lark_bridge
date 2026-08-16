# E07 — What wedges the BCM43438 controller?

- **Status:** In progress — **2 occurrences, one clear trigger hypothesis, not yet reproduced on demand**
- **Relates to:** R1 (single-radio coexistence), and a new risk **R17** (controller lockup)
- **Gates:** spike S3, and therefore Mode 1 — which is the operator's primary target (car stereo)

## Question

Under what conditions does the Pi 3B's BCM43438 stop responding to HCI commands, and is
that condition on the path Mode 1 requires?

## Why it cannot be answered by reading

No vendor documents the controller's internal scheduling. The symptom is not in any
kernel bug tracker in this form.

## Observations

### Occurrence 1 — after ~7 minutes of SCO, following heavy churn

```
Bluetooth: hci0: command 0x0406 tx timeout     (0x0406 = HCI_Disconnect)
Can't init device hci0: Connection timed out (110)
```

RX byte counters frozen; TX still climbing. Preceded by roughly an hour of
connect/disconnect cycling, adapter down/up, `bluetooth.service` restarts and a
`loginctl terminate-user`.

### Control — 30-minute stable SCO soak: **clean**

A single HFP link, established once and left alone, ran **29.97 minutes / 350 samples**
with zero stalls, zero disconnects and no wedge (E02 part 3). This ruled out "sustained SCO
alone" as the cause and produced a provisional conclusion that *connection churn* was to
blame.

### Occurrence 2 — connecting a SECOND device while SCO is active

**The provisional conclusion was too vague.** Immediately after the clean soak, with the
HFP/SCO link healthy, a connect was issued to the A2DP receiver (`50:D7:1B:74:34:D6`):

| Moment | Observation |
|---|---|
| Before | SCO rx/tx symmetric at ~137 pps, 21 reassembly errors total |
| Paging begins | iWorld ACL appears in state 5 (connecting), handle 3840 (pending) |
| Immediately | **SCO rx drops to 0** and stays there; tx continues at ~670 per 5 s |
| Within ~30 s | iWorld never connects; its pending ACL disappears |
| Result | Controller **wedged**: `Read Local Name` unanswered, RX frozen |
| Errors | reassembly errors **21 → 177** (a burst of **+156**) |
| Android | `SCO_STATE_INACTIVE`, `active communication device: None` |
| Audio | downlink node produces digital silence (−200 dBFS) |

The SCO **tx** counter continuing to climb into a dead controller is exactly the failure the
soak monitor was designed to catch: **counters alone lie**.

## Working hypothesis

**Paging a second device while an eSCO link is active wedges the controller.**

Sharper and more damaging than the earlier "churn" hypothesis, because this is not
incidental — **it is precisely what Mode 1 requires**: hold HFP to the phone, then bring up
A2DP to the car stereo.

## Confidence, stated honestly

**n = 1 for this specific trigger.** The correlation is strong (immediate SCO rx stall, a
156-error burst, and a wedge, all within seconds of issuing the connect) but a single
observation is not a reproduction. Alternatives not yet excluded:

- the iWorld itself misbehaving while being paged (it had already failed to connect twice
  earlier with `br-connection-page-timeout`)
- cumulative damage from the session's earlier churn rather than this specific act
- RF conditions at that moment

## Next runs

| # | Test | Purpose |
|---|---|---|
| 1 | Fresh boot → connect A2DP **first**, then HFP | Does order matter? |
| 2 | Fresh boot → HFP established, then page A2DP | Direct reproduction attempt |
| 3 | As 2, but with a **different** A2DP device | Isolates the iWorld as a variable |
| 4 | As 2, with Wi-Fi disabled | Isolates 2.4 GHz coexistence |
| 5 | Both connected **before** any call starts, then start the call | The likely production sequence — may avoid the trigger entirely |

Run 5 matters most for the product: in a car, both links would be established on ignition
*before* any call, so the dangerous ordering might never occur in normal use.

## Recovery, established

Only rung 6 of `scripts/bt-reset.sh` recovers it — unbind/rebind of `hci_uart_bcm`, forcing
a firmware reload. Rungs 1–5 (disconnect, HCI disconnect, adapter down/up, rfkill,
`bluetooth.service` restart) all fail. Confirmed twice.

**A firmware reload resets SCO routing to PCM**, so recovery must re-apply it or HFP returns
half-duplex. `bt-reset.sh` does this and verifies. It must also restart WirePlumber, and
**verify the endpoints re-registered** — observed once coming back with the adapter UP but
no A2DP/HFP endpoints at all, which reads as success and is not.

## Consequence if confirmed

Mode 1 (Bluetooth output to the car stereo) would be **unachievable on this hardware** in
any sequence requiring a second connection during an active call. Mode 1W (wired output)
is unaffected — it never asks the radio to do two things.

That would make the brief's "document it as a measured limitation rather than silently
designing around it with another Bluetooth adapter" the operative outcome.
