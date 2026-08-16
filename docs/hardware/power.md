# Powering the Pi and the Pico

**Read this before you connect the Pico to anything.** There is exactly one wiring mistake
here that can push current into a phone, and it looks reasonable if you have not thought
about it.

## The rule

```
  Pi 5V (header pin 2 or 4) ──►|── Pico VSYS (physical pin 39)
                          Schottky
                     (1N5817 / SS14 / MBR120)

  Pi GND (pin 6 and pin 39) ─────  Pico GND (pin 3 and pin 38)
```

- **Feed `VSYS` (pin 39) through a Schottky diode.** Never feed `VBUS` (pin 40).
- Band (cathode) of the diode goes to the **Pico** side.
- Tie the grounds together. Use two ground wires, not one.

## Why `VSYS` and not `VBUS`

The Pico already has an internal Schottky from `VBUS` to `VSYS`. Adding a second Schottky
from the Pi's 5 V into `VSYS` creates a diode-OR: whichever source is higher wins, and
neither source can push current back into the other. This is the arrangement the Pico
datasheet documents for exactly this situation.

**If you instead connect the Pi's 5 V to `VBUS` (pin 40), you are wiring the Pi's supply
directly to the USB VBUS line — which is connected to the Pixel when the phone is plugged
in.** The Pixel is the USB *host* in Mode 2 and is supplying VBUS itself. Two supplies
driving the same rail is how you damage a phone's USB port. Do not do it.

## Why power the Pico from the Pi at all

The phone would happily power it. Three reasons not to let it:

1. **Deterministic plug/unplug.** With Pi power, the Pico stays alive and its I2S link to
   the Pi stays up when the phone is unplugged. Enumeration on replug then starts from a
   known state instead of a cold boot, which is what makes recovery test I4 pass reliably.
2. **Battery.** Mode 2 is for calls, and calls are when you least want a peripheral
   draining the phone.
3. **Debuggability.** The Pico's CDC diagnostic console stays connected across phone
   unplug events, so you can watch what happens during a disconnect.

## Supply sizing

The Pi 3B needs a genuine **5 V, ≥2.5 A** supply. On its budget simultaneously:

| Load | Typical |
|---|---|
| Pi 3B itself, under load with Bluetooth active | 700–1000 mA |
| Lark A1 receiver | ~100 mA |
| USB audio dongle (Mode 1W) | ~50 mA |
| Pico | ~25 mA |
| Ethernet | ~100 mA |

Under-powering a Pi 3 does not produce a clean failure. It produces intermittent USB
resets, audio dropouts, and Bluetooth instability — symptoms that look exactly like the
bugs this project is trying to measure. That is why `scripts/bootstrap/70-verify.sh`
checks `vcgencmd get_throttled` and fails the install on a non-zero result.

```bash
vcgencmd get_throttled
```

`throttled=0x0` is the only acceptable answer. Bit 0 set means under-voltage is happening
*now*; bit 16 means it happened since boot. Either invalidates any measurement you take.

## Voltage levels on the signal wires

Both the Pi's GPIO and the RP2040's GPIO are **3.3 V CMOS**, so the I2S signals connect
directly with no level shifting.

**Neither is 5 V tolerant.** Do not route the Pi's 5 V pins to any Pico GPIO, and do not
route anything from a 5 V device onto these lines. The only 5 V wire in this build is the
one going through the Schottky into `VSYS`.

## Pre-power-on checklist

Run through this once, with everything unplugged, before the first power-up:

- [ ] Continuity: Pi GND ↔ Pico GND
- [ ] **No** continuity: Pi 5 V ↔ Pico pin 40 (`VBUS`)
- [ ] Diode present between Pi 5 V and Pico pin 39 (`VSYS`), band toward the Pico
- [ ] Diode orientation confirmed with a multimeter in diode mode, not by eye
- [ ] No Pi 5 V pin connected to any Pico GPIO
- [ ] I2S wires under ~15 cm
- [ ] Pi supply is rated ≥2.5 A

Then power the Pi alone, confirm `vcgencmd get_throttled` reads `0x0`, and only then
connect the Pico.
