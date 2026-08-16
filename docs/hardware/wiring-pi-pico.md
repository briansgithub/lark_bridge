# Pi 3B ↔ Pico wiring

**Power first:** read [`power.md`](power.md) and complete its pre-power-on checklist before
connecting anything. This page covers only the signal wires.

## Pinout

| Signal | Direction | Pi BCM | Pi phys pin | Pico GPIO | Pico phys pin |
|---|---|---|---|---|---|
| BCLK (bit clock) | Pi → Pico | GPIO18 `PCM_CLK` | 12 | GP7 | 10 |
| LRCLK (word select) | Pi → Pico | GPIO19 `PCM_FS` | 35 | GP8 | 11 |
| Data — microphone path | Pi → Pico | GPIO21 `PCM_DOUT` | 40 | GP6 | 9 |
| Data — playback path | Pico → Pi | GPIO20 `PCM_DIN` | 38 | GP9 | 12 |
| Ground | — | GND | 39 | GND | 13 |
| Ground (second) | — | GND | 6 | GND | 38 |
| Pico reset *(optional)* | Pi → Pico | GPIO26 | 37 | `RUN` | 30 |
| USB host present *(optional)* | Pico → Pi | GPIO23 | 16 | GP15 | 20 |

```
   Raspberry Pi 3B                              Raspberry Pi Pico
  ┌────────────────┐                          ┌────────────────────┐
  │ GPIO18 pin 12  │──── BCLK  3.072 MHz ────►│ GP7  pin 10        │
  │ GPIO19 pin 35  │──── LRCLK 48 kHz    ────►│ GP8  pin 11        │
  │ GPIO21 pin 40  │──── mic data        ────►│ GP6  pin  9  (DIN) │
  │ GPIO20 pin 38  │◄─── playback data   ─────│ GP9  pin 12  (DOUT)│
  │ GND    pin 39  │──────────────────────────│ GND  pin 13        │
  │ GND    pin  6  │──────────────────────────│ GND  pin 38        │
  │ GPIO26 pin 37  │──── reset (optional)────►│ RUN  pin 30        │
  │ GPIO23 pin 16  │◄─── vbus present ────────│ GP15 pin 20        │
  └────────────────┘                          └────────────────────┘
        I2S MASTER                                  I2S SLAVE
     (generates clocks)                          (follows clocks)
```

## The direction trap

Read this twice, because the naming is genuinely confusing and getting it backwards costs
an afternoon:

> **The Pi's I2S *playback* substream carries the microphone.**

Lark → Pi → Pico → Pixel is the microphone path, and from the Pi's point of view that is
data going *out*, i.e. playback. Conversely the Pi's I2S *capture* substream carries the
phone's playback audio coming back.

This is why the PipeWire nodes are named `bridge.i2s.to-pico` and `bridge.i2s.from-pico`
and never "playback"/"capture". Keep that discipline everywhere.

## Clocking

The **Pi is the I2S clock master** (ADR-0004). It generates BCLK and LRCLK; the Pico's PIO
state machines follow them.

| Parameter | Value |
|---|---|
| Sample rate (LRCLK) | 48 000 Hz |
| Frame | 64 BCLK per frame (2 channels × 32-bit slots) |
| BCLK | 3.072 MHz |
| Format on the wire | I2S Philips standard, MSB first, data valid on the rising edge |
| Payload | 16-bit audio left-justified in the 32-bit slot |

At 125 MHz system clock the Pico has ~40 clocks per BCLK edge, which is comfortable margin
for a PIO slave implementation.

## Signal integrity

- **Keep wires under ~15 cm.** BCLK at 3.072 MHz is undemanding, but loose jumper wires at
  that rate still couple enough to show up as clock jitter, which appears as audible
  artefacts rather than as an obvious failure.
- Prefer a short ribbon cable with grounds interleaved between the signals.
- Two ground wires, not one — a single ground return shared by four fast signals is the
  most common cause of "it works on the bench and not in the enclosure".
- If you scope the lines and see ringing, add **100 Ω series resistors at the source end**
  of BCLK, LRCLK and each data line. Not needed for short, tidy wiring.

## Optional lines, and why they earn their place

Both are optional and both firmware and daemon must work without them. Fit them anyway if
you are building this more than once:

- **GPIO26 → `RUN`** lets the Pi reset a wedged Pico with no human present. It also removes
  the last manual step from provisioning: `flash-pico.sh` can drive the Pico into its
  bootloader itself instead of asking you to hold BOOTSEL.
- **GP15 → GPIO23** lets the Pi know whether the phone is actually supplying VBUS, which
  distinguishes "the Pico is unplugged from the phone" from "the Pico has crashed". Those
  need different responses from the recovery ladder, and without this line they look
  identical from the Pi.

## When these pins are final

The pinout is chosen to satisfy the PIO adjacency constraint that data-in and the two clock
pins occupy consecutive GPIOs, which is what existing RP2040 I2S slave implementations
require. It should survive.

The one thing that could move it is the exact `in`/`out`/`side-set` pin grouping in
`pico/i2s/i2s_slave.pio` once written. **The pinout freezes at milestone M10**; any change
must be recorded here with the reason.
