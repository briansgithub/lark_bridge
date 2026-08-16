# ADR-0004 — I2S is the Pi↔Pico transport, and the Pi is the clock master

- **Status:** Accepted
- **Date:** 2026-08-15
- **Relates to:** `PLAN.md` §7.4, §7.5, §12, risks R5, R7

## Context

Mode 2 needs a full-duplex PCM link between the Pi 3B and the Pico. Candidates: I2S, PCM/TDM, SPI,
USB serial, or something else. Then, independently: which end generates BCLK and LRCLK.

The Pico is unavoidably a two-clock-domain device in Mode 2 — its USB side is paced by the Pixel's
SOF, its I2S side by whatever generates the bit clock. Something must reconcile them. The clock
master decision determines *where* that reconciliation happens and how hard it is.

## Decision

**Transport: I2S.** **Clock master: the Raspberry Pi.** The Pico is an I2S slave implemented in PIO.

## Consequences

- I2S is synchronous, so the link itself introduces no drift — one clock domain spans the wire.
  SPI would have reintroduced exactly the drift problem we are trying to contain, plus require
  inventing framing.
- `bcm2835-i2s` in **master** mode is the path every Raspberry Pi audio HAT uses. Slave mode on the
  Pi is far less exercised. We take the well-trodden side on the platform that is harder to debug and
  cannot be single-stepped.
- The resampler stays on the Pi, where the FPU and the headroom are. PipeWire already adaptively
  resamples the Lark's USB clock into the graph clock; making the I2S card the graph driver means
  everything converges on one clock with one adaptive resampler.
- Rejecting Pico-as-master avoids a needless frequency error: synthesising 3.072 MHz BCLK from a
  125 MHz system clock gives 125e6/3.072e6 = 40.6901, and the PIO divider's 8 fractional bits round
  to 40 + 176/256 → ≈48 003 Hz, about **+65 ppm**. Survivable, but free to avoid.
- As a slave the Pico needs roughly ~8 system clocks per BCLK edge; at 3.072 MHz BCLK and 125 MHz
  sysclk it has ~40. Comfortable margin.
- **The cost:** the Pico must absorb USB-vs-I2S drift in its ring buffers. Handled by an explicit
  UAC2 feedback endpoint steered by a slow PI controller, with sample drop/duplicate as the fallback
  if Android mishandles feedback (risk R7). This is the standard async-USB-DAC arrangement.
- Full-duplex `bcm2835-i2s` is itself unproven for our case (risk R5) — several reports of "only the
  direction started first works". Mitigated by the M10 ladder: shipped `googlevoicehat-soundcard`
  overlay first, repo-owned overlay second, `snd_pcm_link()` third.

## Alternatives considered

- **SPI.** Rejected: not clocked at the audio rate, so both ends keep independent audio clocks and
  drift returns; framing must be invented; full-duplex SPI at fixed low rates is awkward on the Pi.
- **PCM/TDM.** Same silicon as I2S, more slots than 2 channels needs. Held in reserve if we ever
  carry a separate AEC reference channel.
- **USB CDC from Pi host to Pico.** Rejected: it consumes the Pico's only USB port, which is the
  entire reason the Pico is in the design. Retained only as a bench diagnostic side-channel.
- **UART.** Rejected: ~1.5 Mbit/s of payload with no clocking or framing; strictly worse than I2S.
- **Pico as I2S master.** Rejected per the +65 ppm point above and because it would put the
  authoritative audio clock on the device with less debuggability and no resampler.
