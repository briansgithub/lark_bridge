# RF placement and power qualification matrix

These are arrival-day characterizations. Preserve logs and do not choose release limits after
seeing only one run.

## RF matrix (H13)

Keep Pixel, car/speaker, codec, volume, stimulus, and firmware fixed. Run at least ten minutes of
media plus a five-minute active call in every cell.

| Antenna separation | 32UE antenna orientation | Wi-Fi state | Purpose |
|---:|---|---|---|
| 30 cm | parallel to S31 PCB antenna | disabled | clean baseline |
| 15 cm | parallel | disabled | intended bench starting point |
| 5 cm | parallel | disabled | compact worst case |
| 5 cm | orthogonal | disabled | orientation mitigation |
| final enclosure | final orientation | disabled | candidate topology |
| final enclosure | final orientation | enabled but idle | interference characterization only |

Wi-Fi remains disabled in the release baseline unless a later requirement explicitly adds it.
For each cell record upstream/downstream RSSI where available, output FIFO min/max and correction,
missing/dropped frames, underruns/overruns, UART errors/reliable failures, audible discontinuities,
media latency, call stability, and reconnect time. Reject any topology with resets, a lost call,
unbounded FIFO motion, or repeatable audible dropouts. Use the 30 cm result to distinguish basic
firmware failure from near-field coexistence failure.

The delivered antenna identity, gain, connector sex, pigtail loss, board revisions, enclosure, and
actual wire lengths belong in H0 inventory. Until those fields are recorded, antenna certification
and final minimum spacing remain open.

## Bench-power characterization

First integration uses two isolated programming USB supplies with only signal ground shared and
both 5 V header pins disconnected. Measure each board independently, then the assembled system, at:

| State | Measurements |
|---|---|
| boot/inrush | minimum 5 V rail, peak current, reset reason |
| idle/unpaired | steady current, rail ripple |
| discovery/reconnect | peak current, brownout/reset count |
| media | average/peak current, rail minimum, USB-host current |
| active call + AEC | average/peak current, rail minimum, minimum heap |
| LARK hot-plug | transient minimum, USB/UAC recovery |
| simultaneous radio reconnect | worst transient and recovery |

Only after the functional gates pass may a protected 5 V star harness replace the two supplies.
Verify polarity unpowered, then confirm neither programming connector back-feeds the common rail.

## Vehicle power (H17) is deferred

The ordered boards and a generic 5 V/3 A bench supply do not constitute an automotive power
design. H17 remains `PENDING HARDWARE` until a specific fused, reverse-polarity-protected,
transient/load-dump-rated DC/DC supply and shutdown policy are selected. At that point document its
part number and ratings, then test crank, ignition cut, rapid cycling, undervoltage, recovery, state
integrity, and programming-USB back-feed in the final topology. Do not connect either dev board
directly to the vehicle's nominal 12 V rail.
