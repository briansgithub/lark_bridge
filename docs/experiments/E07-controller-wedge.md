# E07 — What wedges the BCM43438 controller?

- **Status:** In progress — **3 occurrences. Occurrence 3 falsifies the "second device" hypothesis
  and puts Mode 1W at risk too.**
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

### Occurrence 3 — **no second device at all**, single HFP link, Mode 1W

The most informative occurrence, because it removes the previous hypothesis' trigger entirely.
**No A2DP device was connected or paged.** One HFP link to the Pixel, wired output to dongle A.

Timeline from `dmesg -T` and `journalctl`:

```
22:39:18  Bluetooth: hci0: Frame reassembly failed (-84)   x3     <- -EILSEQ
22:39:42  Bluetooth: hci0: Frame reassembly failed (-84)   x8
22:39:48  Bluetooth: hci0: command 0x0406 tx timeout              <- HCI_Disconnect
22:39:50  bluetoothd: No matching connection for device
22:39:52  Bluetooth: hci0: command 0x0406 tx timeout
22:43:15  Bluetooth: hci0: command 0x0406 tx timeout
```

Then, with the controller dead:

| Check | Result |
|---|---|
| `hciconfig hci0 version` | **`Connection timed out (110)`** — will not answer a local read |
| `hciconfig hci0` | `UP RUNNING PSCAN ISCAN` — **looks perfectly healthy** |
| HCI counters | `acl:76 events:337` — **byte-identical** across 15 minutes and many phone attempts |
| `btmon -w` | **508 bytes, not growing.** Zero HCI traffic |
| Operator | Pixel could not connect to `larkbridge` at all |
| Frame reassembly failures, this boot | **2029** (ring buffer wrapped — this is a *floor*) |

Recovered at **rung 6** again — third confirmation that only the firmware reload works.

#### This also invalidates E08's recovery claim

E08 recorded that `bluetoothctl disconnect` cleanly cleared a stale link and that "the
controller did NOT need resetting, so this is NOT the E07 wedge." **Wrong.** Opcode `0x0406`
*is* that Disconnect, and it **timed out**. The `Disconnection successful` message came from
bluetoothd's local bookkeeping, not from the controller. The half-open link in E08 and this
wedge are very likely **the same event**: the controller stopped processing HCI, so the call
teardown was never applied, leaving the host holding a link that no longer existed.

#### Confounder, stated

This occurred while `bridge-supervisor` was restarting `pw-loopback` repeatedly (the `-P` bug,
fixed in 3751890). Those restarts do not page devices, but starting and stopping streams on the
HFP sink can make BlueZ cycle the **SCO audio connection**, which is Bluetooth-level churn. So
occurrence 3 is not a clean "steady state wedged on its own" datapoint. It does, however,
definitively remove *paging a second device* as a necessary condition.

## Working hypothesis

**Superseded.** The former hypothesis — *paging a second device while eSCO is active* — is
falsified by occurrence 3, where no second device existed.

### New leading hypothesis: the HCI UART is losing bytes

`Frame reassembly failed (-84)` is `-EILSEQ`: the H4 parser received bytes it could not resolve
into a packet. That is a **transport** fault between the SoC and the BCM43438, not a radio or
scheduling fault. Byte loss on a UART means overrun or clock error. Once the H4 stream desyncs,
host and controller disagree about framing and every subsequent command times out — which is
exactly the observed progression: reassembly bursts, then `tx timeout`, then total silence.

Measured facts that bear on it:

- BT is on the **PL011** (`/soc/serial@7e201000`, `status=okay`) at **`max-speed = 921600`**.
- The mini-UART (`serial@7e215040`) is `disabled`, and `console=serial0,115200` in `cmdline.txt`
  therefore **never bound** — `/proc/consoles` lists only `tty1`. **A console/HCI collision on
  the same UART is ruled out.**
- We route **SCO over HCI** (E01), so every call puts two 16 kHz mSBC streams onto that same
  921600-baud link on top of ACL. This is the load the design added, and it is the obvious
  suspect for overrun.
- **`dtoverlay=disable-wifi` is NOT in `config.txt`** — contrary to what the rig plan requires
  before U20. The 2.4 GHz Wi-Fi radio shares a die with the Bluetooth controller and is powered
  (Imager injects `rfkill unblock wifi`). This is also E03's untried mitigation #4.

This hypothesis is attractive because it explains all three occurrences (all involved heavy SCO)
without needing a second device, and it explains why only a **firmware reload** recovers: a
desynced H4 stream cannot be resynchronised by any host-side action short of reattaching.

**It is not yet tested.** Reassembly errors have been *correlated* with SCO load since E03 but
never deliberately provoked or eliminated.

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

### Runs added after occurrence 3 — these now take priority

The above all assume a second device. Occurrence 3 shows that is not required, so the UART
hypothesis is tested first. **Instrument every run with a reassembly-error rate**, not a total:
the counter is what turns "it eventually broke" into a dose-response curve.

| # | Test | Purpose |
|---|---|---|
| 6 | Fresh boot, single HFP call, **count reassembly errors per minute** | Establish the baseline error rate under plain Mode 1W. Occurrence 3 says it is not zero. |
| 7 | Same, with **`dtoverlay=disable-wifi`** | Cheapest real variable, already required by the rig plan and already listed as E03 mitigation #4. Removes a powered 2.4 GHz radio from the shared die. |
| 8 | Same, at a **higher BT UART baud** (e.g. `3000000`) | If the fault is overrun, headroom should reduce the error rate. If the rate is unchanged, overrun is wrong and clock/flow-control is implicated instead. |
| 9 | HFP connected, SLC up, **no call** (no SCO), left for 30 min | Isolates SCO from ACL. E03 step B already suggests this is clean; confirm with error counts. |
| 10 | Deliberate PipeWire stream churn on the HFP sink during a call | Reproduces occurrence 3's confounder on purpose, to see whether SCO cycling alone provokes the burst. |

Run 9 is the key control: if reassembly errors are near zero without SCO and climb sharply with
it, the SCO-over-HCI load is confirmed as the driver and the design decision from E01 becomes
the thing to revisit.

## Required regardless of cause: automatic recovery

The product must survive this unattended in a car. Three occurrences, one manual `bt-reset.sh`
each, and a user who cannot type commands while driving.

The hard part is **detection**, and occurrence 3 shows why every obvious signal is a trap:

| Signal | Said during the wedge |
|---|---|
| `hciconfig hci0` | `UP RUNNING PSCAN ISCAN` — healthy |
| HCI error counters | `errors:0` both directions |
| `bridge-supervisor` | both legs `verified` |
| SCO frame flow (earlier occurrences) | nominal rate, still transmitting |

A watchdog built on any of these reports green on a dead radio. What actually discriminated was
**issuing a command and seeing whether it is answered** — `hciconfig hci0 version` timing out is
the cheapest positive test, and frozen `events:` counters plus a rising reassembly count are
corroborating evidence. Any auto-recovery must be built on an active probe, not on passive
counters.

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
any sequence requiring a second connection during an active call.

**Mode 1W is no longer exempt.** That exemption rested on the second-device hypothesis, which
occurrence 3 falsified: the wedge happened with a single HFP link and a wired output. Mode 1W
still avoids the *A2DP* contention measured in E03, but it does **not** avoid this. Since Mode
1W is the shipped default and the operator's daily path, E07 is now the **top product risk**,
ahead of R1.

That would make the brief's "document it as a measured limitation rather than silently
designing around it with another Bluetooth adapter" the operative outcome.
