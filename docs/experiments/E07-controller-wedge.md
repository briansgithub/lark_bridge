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

### Occurrence 4 — **captured on `btmon`. Mechanism identified.**

The first occurrence caught with a running HCI capture and a clean baseline (0 reassembly errors
after a fresh `bt-reset.sh`). Raw trace: `artifacts/E07/run6.btsnoop`, 3.8 MB.

Conditions: single HFP link, Mode 1W, **no A2DP, no second device**. Three USB audio devices
streaming (Lark capture, dongle A playback, dongle B idle).

| wall clock | event |
|---|---|
| 22:55:44.988 | call routed to `larkbridge`; SCO starts |
| 22:56:02.185 | **last good `SCO Data RX`** |
| 22:56:02.193 | **first misparsed `HCI Event: Vendor (0xff) plen 244`** — 8 ms later |
| 22:56:42.912 | operator plugs headphones into dongle A → USB re-enumeration |
| 22:57:04.184 | last misparsed vendor event; controller silent from here |
| 22:59:28.577 | `Frame reassembly failed (-84)` finally logged |
| 23:00:06.516 | host **still** transmitting SCO into a dead controller |

**SCO survived 17.2 seconds.**

#### The mechanism is H4 frame desynchronisation, and the capture proves it

```
> SCO Data RX: Handle 6 flags 0x00 dlen 60   #4682  311.185982   <- normal, every 7.5 ms
> HCI Event: Vendor (0xff) plen 244          #4684  311.193751   <- desync begins
```

| | before first vendor event | after |
|---|---|---|
| `SCO Data RX` frames | 2294 | **0** |
| bogus `Vendor (0xff)` events | 0 | 709 |

SCO reception does not degrade — it **stops dead** and is replaced by 244-byte "vendor events"
whose payloads are high-entropy and repeat stale trailing bytes between consecutive events. They
are not vendor events at all: they are **the mSBC SCO byte stream being misparsed**. The kernel
lost byte alignment, read an audio byte as an H4 event code (`0xff`), the next as a length
(`244`), and never recovered. Once alignment is lost, every subsequent byte is misframed, which
is why **only a firmware reload via rung 6 recovers** — nothing host-side can resynchronise it.

The host then transmitted **14 837 further SCO frames** over the next three minutes into a
controller that could no longer parse them.

#### Two things this rules out

1. **The USB re-enumeration did not cause it.** The operator plugged headphones into dongle A at
   22:56:42.9 — **40 seconds after** the link was already dead. The apparent correlation
   ("I plugged them in and it disconnected") is real as an observation and wrong as a cause. An
   earlier analysis in this session reported the USB event first and the Bluetooth failure
   second; that ordering came from `dmesg` alone and was corrected only by absolute timestamps
   from `btmon -r -T`. **Do not infer ordering across two log sources without absolute time.**
2. **`Frame reassembly failed` is a lagging indicator.** It appeared at 22:59:28 — **3 minutes
   26 seconds after** the desync at 22:56:02. It is useless as an early warning and misleading
   as a trigger timestamp. Every previous occurrence in this file dated the failure by that
   message and therefore dated it *late*.

#### The honest detection signal

`SCO Data RX` stops while `SCO Data TX` continues. That transition is visible in the capture at
the exact millisecond, whereas `hciconfig`, HCI error counters and the supervisor's own leg
verification all still read healthy minutes later.

### Occurrence 5 — production config, overnight: **37 min 3 s**, then dead for 8.5 hours

The first run of the *shipped* configuration: **one** USB audio device (Lark), output on the
Pi's onboard jack, one Bluetooth link, PCM at 0 dB. Instrumented with `rig/pi/soak/sco-sampler.sh`
(active probe, one sample/minute). Timeline: `artifacts/E07/occurrence5-timeline.log.gz`.

| wall clock | event |
|---|---|
| 02:18:23 | soak start; `esco=1 controller=yes`, 133 SCO frames/s, RX and TX in lockstep |
| 02:55:26 | `reassembly_1min=8` — **still `controller=yes`** |
| 02:56:26 | **first probe failure** (`controller=NO`) — **+37 min 3 s** |
| 02:58:21 | phone gives up: `resetBluetoothSco`, routes back to earpiece |
| 02:58:28 | `RX bytes` freezes at 29759349 while TX climbs for another 8.5 h |
| 11:19 | still dead — 503 of 541 samples were `controller=NO` |

**This is the headline result and it cuts both ways.** Removing USB audio devices bought a
**129× improvement** — 17.2 s → 37 min — which strongly confirms the load hypothesis. It did
**not** eliminate the fault. One full-speed USB capture device is still enough.

#### The dose-response, complete

| USB audio devices | time to desync |
|---|---|
| 3 (Lark + dongles A and B) | 17.2 s |
| 1 (Lark only, onboard output) | **37 min 3 s** |
| 0 | no desync in 228 s |

#### Correction: reassembly errors were *not* lagging here

Occurrence 4 recorded them 3 m 26 s late. Here they appeared at 02:55:26, **one minute before**
the probe failed. The lesson survives in weakened form — do not *date* a failure by that message —
but it is not reliably late either. The active probe is what should be trusted, and it worked:
it flipped within a minute of the real event.

Note also that `RX bytes` kept climbing for ~2 minutes *after* the probe started failing. That is
consistent with the occurrence 4 mechanism: bytes still arrive and are counted, they are simply
misframed. **A frozen RX counter confirms a wedge; a climbing one does not refute it.**

#### Was it triggered by anything external? No — checked directly

The operator asked whether the phone's 30-minute screen timeout could be responsible. Worth
testing rather than dismissing: an Android screen-off often changes Bluetooth link power
management, and a **burst** of HCI traffic is exactly what could overrun a 32-byte FIFO.

`rig/analysis/btsnoop_window.py` parses the btsnoop record headers directly instead of decoding
through `btmon` — 426 MB takes over an hour to pretty-print on a Pi 3 and about a minute to walk.
Whole-capture totals, 02:14:18 → 11:43:31 (4 909 818 records):

| monitor opcode | count |
|---|---|
| **SCO TX** | **4 553 817** |
| **SCO RX** | **350 820** |
| Ctrl Open / Ctrl Close | 2231 / 2230 |
| Command | 558 |
| Vendor Diag | 116 |
| Event | 39 |
| **ACL RX** | **2** |

**Two ACL packets in nine and a half hours.** The phone was silent. And the SCO ratio is the wedge
in one line: 4.55 M frames transmitted, 350 k received — 350 820 ÷ 133 per second ≈ 2640 s ≈ the
44 minutes from capture start to 02:58, after which RX is zero and TX runs for another 8.5 hours.

In the 3.5 minutes bracketing the failure there were **30 non-SCO records against 55 977 SCO
frames**, and nearly all 30 were the sampler's own probes — `Command opcode 0x1001`
(Read Local Version) plus its management-socket open/close pairs.

**Absent entirely: `Mode Change` (0x14).** No sniff transition, no park, no
`Synchronous Connection Changed`, no codec renegotiation, no `Max Slots Change`. Nothing from the
phone at any point near the failure. The lone `Vendor Diag` at 02:55:06 looked suspicious until
the full scan showed 116 of them scattered across the night (02:31, 02:37, 03:18, 04:17 …) — it is
routine, not a precursor.

The exact transition, from our own probe:

```
06:55:26.523 UTC  Command 0x1001  ->  Command Complete   (answered)
06:56:26.644 UTC  Command 0x1001  ->  no reply, ever
```

**Conclusion: the screen timeout is ruled out, and so is any external trigger.** The desync
happened in pure steady state, with nothing on the air but SCO frames. That is a stronger result
than finding a trigger would have been: it means no burst is required. A single unlucky latency
spike — one FIQ holding IRQs off for longer than the FIFO's ~347 µs — is sufficient. Which is
exactly why the fault is intermittent and why its rate scales with USB load rather than with
anything the phone does.

#### The operational finding

Nothing was deployed to recover it, so a fault that costs ~20 s to fix cost **8.5 hours of
downtime**. In a car that is the whole product failing. This is what makes `bt-watchdog`
mandatory rather than nice-to-have, independent of whether the root cause is ever eliminated.

## Working hypothesis

**Superseded twice.** The original — *paging a second device while eSCO is active* — is falsified
by occurrence 3. Occurrence 4 replaces the general "UART is losing bytes" guess with a proven
mechanism.

### Why bytes are lost — measured 2026-08-17

| finding | evidence |
|---|---|
| The BT UART runs **PIO, no DMA** | `dmas` and `dma-names` are **absent** from `/proc/device-tree/soc/serial@7e201000` |
| ~347 µs of slack before overrun | 32-byte PL011 FIFO ÷ 92160 bytes/s at 921600 baud |
| It competes with a **FIQ** | `dwc_otg_sim-fiq` **153 M** interrupts vs `uart-pl011` **9.8 M**; a FIQ masks ordinary IRQs |
| USB devices are **full-speed**, not high-speed | `not running at top speed; connect to a high speed hub` in dmesg — so isochronous traffic uses **split transactions**, the most FIQ-expensive mode |
| CPU can run at **600 MHz** | governor `ondemand`, range 600–1200 MHz: the ISR sometimes runs at half clock |

A PIO UART with 347 µs of headroom, sharing a CPU with a FIQ that fires 153 million times, is a
textbook recipe for RX overrun. This explains the dose-response, why only a firmware reload
recovers (H4 alignment is unrecoverable host-side), and why the fault is intermittent rather than
structural — it needs a latency spike, not a sustained overload.

**Established:** the HCI H4 stream loses byte alignment during SCO and never recovers.

**Not established:** *why* a byte is lost. The remaining candidates are all about the receive
path being starved for a few hundred microseconds:

- **PL011 RX FIFO overrun from interrupt latency.** The FIFO is 32 bytes; at 921600 baud that is
  ~347 µs of slack. Throughput is not the problem — SCO is only ~8 kB/s each way against ~92 kB/s
  of capacity — but *latency* plausibly is.
- **`dwc_otg` USB interrupt load.** The Pi 3's USB controller is well known for long interrupt
  disable windows, and this failure occurred with three USB audio devices streaming. This is the
  cheapest and most promising variable to test, and it is **not** in the original run list.
- **2.4 GHz Wi-Fi sharing the die**, still enabled (`dtoverlay=disable-wifi` absent).
- **Baud/clock margin** at 921600.

Note the design coupling: routing **SCO over HCI** was a deliberate decision in E01 (the
alternative, PCM routing, needs I2S wiring the hardware does not have). That decision is what
puts continuous real-time audio on this UART, so if the transport is the limit, E01's conclusion
is what has to be revisited — not the audio graph.

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
| **11** | **SCO call with the USB audio devices unplugged** (HFP only, no Lark, no dongles) | **Highest priority.** Isolates `dwc_otg` interrupt load, the leading suspect for starving the PL011 RX FIFO. If SCO survives indefinitely with USB idle and dies in ~17 s with three streams running, the cause is settled. |

**Measure time-to-desync, not pass/fail.** Occurrence 4 gives a concrete figure — **17.2 s** —
and E03's spread was 7 s to 120 s, so a single surviving run proves nothing. Each run needs
`btmon -w` and the metric below.

### The metric to use from now on

Time from first `SCO Data RX` to the last one before `SCO Data RX` count goes to zero:

```bash
sudo btmon -r run.btsnoop -T > t.txt
grep -m1 "SCO Data RX" t.txt          # start
grep    "SCO Data RX" t.txt | tail -1 # desync moment
```

Do **not** use `Frame reassembly failed` timestamps (3.5 minutes late in occurrence 4) or
`dmesg | grep -c` (the ring wraps and undercounts — `journalctl -k` reported 2868 errors where
`dmesg` showed 2013). This supersedes the counter noted as broken in E03.

Run 9 is the key control: if reassembly errors are near zero without SCO and climb sharply with
it, the SCO-over-HCI load is confirmed as the driver and the design decision from E01 becomes
the thing to revisit.

## Runs 11 and 12 — USB confirmed as the trigger; a fix follows from it

Run 11 executed the test above: identical link, identical phone, call routed to `larkbridge`,
**all three USB audio devices physically unplugged**, supervisor stopped.

| | occurrence 4 (3 USB devices streaming) | run 11 (USB bus empty) |
|---|---|---|
| time to desync | **17.2 s** | **none in 228 s** |
| `SCO Data RX` | 2294, then 0 | **30 354, still flowing** |
| bogus `Vendor (0xff)` events | 709 | **0** |
| `Frame reassembly failed` | 2868/hour | **0** |
| controller at end | wedged | alive |

Run 12 then added back **only the Lark** and routed call audio to the **Pi's onboard 3.5 mm
jack** instead of dongle A — one USB audio device instead of three:

- **84 102** SCO RX frames over **10 min 31 s** of continuous call
- **0** desyncs, **0** reassembly errors, controller alive
- Operator confirmed the far end was audible on the onboard jack

**Caveat on run 12's strength:** only the final ~2–3 minutes had the Lark actually streaming;
the earlier part of that window was the USB-free baseline. So run 12 is **7–10× past the known
failure point, not proof**. Given E03's 7 s–120 s spread, only a 30–60 minute soak settles it.

`dwc_otg` FIQ mitigations were already active throughout (`fiq_enable=Y`, `fiq_fsm_enable=Y`),
so this is not a case of a missing standard workaround — the load is simply too much for the
PL011's 32-byte RX FIFO to ride out.

### Design consequence, applied

`bridge_supervisor.py` now defaults `BRIDGE_WIRED_OUT` to
`alsa_output.platform-3f00b840.mailbox.stereo-fallback` — the Pi's own headphone jack.

The Lark is USB and **cannot** be moved off that bus; it is the microphone. The output can be,
so it is. This costs nothing: the source is 16 kHz HFP call audio, well below what the Pi's PWM
DAC resolves, and it feeds a car aux input either way. It also deletes dongle A from the product
entirely, and with it trap 6 (combo-jack re-enumeration).

Dongle B remains **rig-only** — a measurement tap, never part of the shipped bridge. Any test
that plugs it in is measuring a configuration the product does not use, and that now matters,
because each USB audio device measurably shortens time-to-desync.

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
