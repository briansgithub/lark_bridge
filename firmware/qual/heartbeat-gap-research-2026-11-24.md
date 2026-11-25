# S31 heartbeat gap research, 2026-11-24

Compared the evening bench evidence with the merged branch. The Claude
baseline is `aa382b3` (merge `13f96fd`), followed by Astra `5bbddd4`
(merge `e25082a`). The logs establish a gap in **S31 application-level
liveness refreshes**. They do not locate the delay relative to the 32UE
transmit call, either UART driver's buffering, or the S31 task.

## What the captures show

The evening report describes multiple failing smokes and an intermittent
maximum of 18 timeouts. An offline replay of
`../lark_bridge-evidence/20261124-final/h5/bench.jsonl` found 18 S31
`hb_timeout` records across the 105.051 s file. Ten fall inside the
60.397 s -50 dBFS stream window and two inside the 21.108 s end-to-end
window. The others are outside those scored windows. The events reported
ages of 1,400–1,440 ms. The S31 decode-error counter stayed at 11 through
the capture and the output emitted no timeout event. The sampled
heartbeat-only maximums were 1,460 ms on S31 and 1,073 ms on output.
Some S31 samples
show zero heartbeat increments over one stats interval and four in the
next. Host console arrival timestamps and aggregate counts cannot tell
whether the frames were transmitted or decoded in bursts.

The longer `20261124-final/bench.jsonl` has a complete 1,800.214 s
stream window with zero timeout events. Its S31 timeout counter started
and ended at 1. Its decoder counters did increase, so this comparison
does not establish a clean wire for every run. The failed H5 summary
reports FIFO minimum zero with no new underruns, overruns, or I2S errors.
That is consistent with route invalidation and FIFO flushing following a
timeout. [Evening bench report](bench-report-2026-11-24-evening.md)

## Paths and blocking points

| Side | Normal path | Credible delays |
| --- | --- | --- |
| 32UE TX | `output_control.c` runs the unpinned priority-16 `bridge_uart` task. Its loop reads up to 64 bytes with a 10 ms wait, decodes RX and sends any synchronous ACK, services the 16-entry outbound queue and retries, runs test hooks and policy timeout processing, then sends due HELLO, 500 ms HEARTBEAT and 1,000 ms STATUS. Heartbeats bypass the outbound queue. | The due heartbeat still waits behind that work. The queue drain has no explicit per-pass budget, although one in-flight reliable edge normally limits AVRCP traffic, and the H5 tone has no key flood. `uart_write_bytes` can wait for TX ring space. The 1,024-byte UART2 TX ring would drain in about 89 ms at 115,200 baud with a healthy ISR and no flow control. Normal wire bandwidth cannot account for a 1.4 s stall. A higher-priority I2S reader (18), Bluetooth stack work, cross-core locks, or the periodic synchronous audio/heap log can delay the task or interrupt service. No existing evidence singles one out. |
| S31 RX and timeout | `link_uart.c` runs the unpinned priority-8 `s31_link` task. It reads up to 256 bytes with a wait capped at 50 ms and at its next 500 ms outgoing deadline. It decodes the batch before checking age. A valid same-session HEARTBEAT **or STATUS** refreshes the timestamp after a valid HELLO, regardless of the ready bits. Age at least 140 ticks at 100 Hz invalidates output readiness, resets the route fence and forces silence. | The pinned IDF UART implementation reuses the timeout for each partial ring receive. The timeout caps an individual wait, so a trickle can stretch the call and batch application timestamps. Normal small periodic traffic alone does not prove that happened. The S31 also shares CPU time with I2S TX and USB tasks at priority 9, UAC/reconnect at 8, and core-0 Bluetooth work. The AEC worker at 10 is disabled on the affected default images. Link-side ACK and periodic TX writes, route work, and synchronous drift, timeout, and fault logs all run in the same loop. Console traffic travels on another UART/USB path but can still block these tasks through logging and scheduling. |

The S31 time check uses unsigned tick subtraction and samples after parsing.
A task dispatched late may drain fresh buffered frames before evaluating a
timeout. Conversely, a frame still in the driver or outside the just-decoded
batch cannot refresh the deadline. The old test `hb_gap_max_ms` measures
HEARTBEAT only and clears its previous-sample marker on timeout. It cannot
measure the precise HEARTBEAT-or-STATUS gap across the mute. Losing only
HEARTBEAT traffic should leave the 1 s STATUS path able to renew liveness
if those frames are consumed on time. Both UART installs use no driver
event queue, so an unchanged decode counter cannot rule out loss of a
complete frame or UART FIFO/ring overflow.

Astra `08aeaa7` changes sequence allocation from one global counter to one
per message type. That fixes sparse reliable route/AVRCP commands falling
outside their replay windows after hours of traffic. S31 accepts both
liveness types without filtering either by sequence. Output RX does use a
heartbeat tracker, but a few missed packets remain well within its forward
half-range. The change does not alter either loop, readiness expiry, or
UART buffering. It makes matching frames in the new trace easier.

## Candidate fixes, in trial order

1. **Bound the S31 receive pass.** If output attempts are regular while
   `rx_loop_max_ms` grows, use a one-byte bounded wait followed by a
   zero-wait read of the remaining buffer. Keep decoder state across calls,
   drain available bounded work before checking expiry, and keep the
   1,400 ms policy. Compare loop gap and liveness gap with the diagnostic
   baseline. For a patch, exercise fragmented frames and continuous
   short chunks with a fake UART, a liveness frame already buffered near
   the deadline, a real silence timeout at 1,400 ms, and tick rollover.
2. **Schedule due 32UE heartbeats ahead of optional TX work.** If
   `live_tx.gap_ms` grows while write calls return promptly, move the
   deadline check ahead of queue draining while keeping RX parsing and
   policy expiry first. Bound non-heartbeat work per pass and move
   periodic INFO logging off the control task. Advance the
   deadline from the planned cadence and skip missed periods to prevent
   a catch-up burst. Test a continuously replenished queue,
   retry ordering and a delayed iteration crossing several periods. If
   the trace instead shows long UART write calls, changing queue order
   alone will not help. Check UART TX driver service first.

These are candidate changes, with neither applied to the timing baseline.
The timeout and fail-closed route policy are unchanged.

## Diagnostic and hardware comparison

`2bd9862` adds test-image fields to existing periodic UART JSON, without
per-frame printing or wire changes. The timing snapshot is copied under
a short lock and serialized after releasing it.

- On the output, `uart.live_tx` records the start of actual UART write
  attempts for HEARTBEAT (type 2) and STATUS (type 5). Deliberate test
  mutes are excluded. `live_write_max_ms` measures time spent in a
  completed write call, including descheduling. `live_write_err` counts
  short or failed writes. A returned write means driver enqueue, so
  it does not timestamp the wire.
- On S31, `uart.live_rx` records dispatch of either liveness type once
  the peer HELLO is valid. It spans a timeout within the same peer session.
  `rx_loop_max_ms` measures the lifetime maximum between UART read
  returns, including all work and scheduling between them.
- Both nested records include count, latest sender-local or receiver-local
  milliseconds, message type and sequence, and the maximum gap with the
  type and sequence of the first frame after it. Match `(type, seq)`
  within one session and compare intervals on each board. The clocks
  have separate epochs. An unchanged lifetime maximum may predate the
  current phase, so retain the initial snapshots and session counts.

On the rig, use both test images from the diagnostic commit. Save the
startup snapshots and both consoles during the existing H5 -50 dBFS
speaker replay. The evidence directory retains `h5/smokes.py` and
`soak.py` for the short and long captures. Use fresh evidence directories
so neither original log is overwritten. Repeat the short run until a timeout is captured, then
run the 30-minute window after the diagnostic identifies the delayed
link stage. Keep the release timeout unchanged. Interpret the result as
follows:

| Trace | Next discriminator |
| --- | --- |
| TX gap grows, write time stays short | Test candidate 2 and compare loop work around the largest sequence gap. |
| TX write time or failures rise | Check the UART2 TX ring and interrupt service. Compare a logic trace of the same frame, if available. |
| TX is regular, S31 RX and RX-loop gaps grow together | Test candidate 1, then distinguish read wait from other S31 task stalls. |
| TX is regular and the S31 read loop remains regular, yet dispatch gaps rise | Capture UART TX and RX or enable bounded driver overflow/error telemetry. Application timestamps alone cannot separate wire loss from driver loss. |

The native host suite checks first observation, 32-bit clock wrap,
16-bit sequence wrap, a 1,450 ms gap ending on STATUS, retention of its
identity after a later heartbeat, and a clean session reset. The eight
CTest targets passed. Both test images also built incrementally in Docker
with the pinned toolchain tags (output `v6.0.2`, S31 `v6.1-rc1`). No
board, phone, speaker, or Pi was accessed.
