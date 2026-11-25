# HIL Rig Bring-Up & SWD Crosstalk Post-Mortem (2026-11-25)

**Host:** BigBoy (`192.168.0.144`)  
**Rig Pi:** `larkbridge-rig` (`192.168.0.251`, Raspberry Pi 3 Model B Rev 1.2, Debian 13 Trixie arm64, kernel `6.18.50+rpt-rpi-v8`, separate non-overlayroot `ext4` SD card)  
**Onboard BT (`LarkRig Fake Car`):** `B8:27:EB:43:8D:51` (USB-BT500 unplugged)  
**Pico:** Raspberry Pi Pico (RP2040, `© 2020`), powered solely by the ESP32-S31 USB-A host port, wired to the rig Pi per `firmware/docs/hil-rig.md` §3.4 with `1 kΩ` series protection resistors on all 5 signal lines (`SWCLK`, `SWDIO`, `RUN`, `TXD`, `RXD`) and direct common `GND`.

---

## 1. Learning Moment: SWDIO → SWCLK Capacitive Crosstalk Behind 1 kΩ Series Resistors

### Symptom
- Electrical DC checks all passed:
  - Holding Pi `GPIO26` (`RUN`) low reset the Pico and dropped Pico `GP0` (`UART TX` → Pi `GPIO15`) from `hi` to `lo`, returning `hi` upon release (proving S31 VBUS power, common `GND`, `RUN`, and `UART TX`).
  - Pi `GPIO24` (`SWDIO`) and `GPIO25` (`SWCLK`) independently pulled back `hi` after a drive-low/float pulse (proving continuity to the RP2040 internal pull-ups and no bridge between lines), while unconnected pins (`GPIO17`, `9`, `10`, `11`, `23`) stayed `lo`.
- Yet **OpenOCD failed identically across every speed and driver**:
  - `bcm2835gpio` at `1000 kHz`, `100 kHz`, `20 kHz`, `5 kHz`
  - `linuxgpiod`
  - Both normal (`swclk=25, swdio=24`) and swapped (`24/25`)
  - Both core 0 (`TARGETSEL = 0x01002927`) and the hardwired RP2040 Rescue DP (`0xF1002927`)
  - Error: `Warn : Too long SWD WAIT, issuing DAPABORT` / `Failed to connect multidrop rp2040.dap0`.
  - Under `openocd -d4`: `JUNK DP read reg 0 = fe1f0a9f` (identical deterministic hex word every time).

### Diagnosis (40/40 Bit-Exact Proof)
A raw Python `/dev/gpiomem` SWD bitbang probe swept clock half-periods from `50 µs` (`10 kHz`) to `2000 µs` (`250 Hz`) and captured the raw 40-bit stream immediately after sending `TARGETSEL` + `DPIDR read (0xA5)`:

- `core0` (`0x01002927`) and `core1` (`0x11002927`) returned the exact same **noise-free, clock-speed-independent** bitstring:
  `1101111110010101000011111000011111111111` (`ack = 0b101`, `dpidr = 0xfe1f0a9f`)
- `rescue` (`0xF1002927`) returned a different deterministic bitstring:
  `1101110101011010001000001000111111111111`
- `bogus` (`0xDEADBEEF`) returned all `1`s (`11111111...`), proving the RP2040 was cleanly receiving `SWCLK` and `SWDIO` on host-to-target writes!

Comparing the observed `core0` bitstring against the true RP2040 `DPIDR = 0x0bc12477` (`ACK = 100` + LSB-first `0x0bc12477` + parity `0`) revealed why clock frequency didn't matter:

- **Physical root cause:** On the Pico side of the `1 kΩ` series resistors, the `SWDIO` (`orange`) and `SWCLK` (`yellow`) jumper leads were still joined together side-by-side in a flat ~20 cm ribbon cable. Because `SWCLK` sits behind a `1 kΩ` series resistor (high source impedance at the Pico pin), whenever the RP2040 drove `SWDIO` **HIGH → LOW (`1 → 0`)** right after a rising `SWCLK` edge (while `SWCLK` was HIGH), the RP2040's fast ~3 ns falling edge (`dV/dt`) capacitively coupled a sharp negative glitch across the parallel ribbon capacitance (`C_m`) onto `SWCLK` and immediately rebounded HIGH. The RP2040 saw that glitch+recovery as an **extra rising `SWCLK` edge**, shifting its SWD output register forward by 1 extra bit on **every `1 → 0` transition of `SWDIO`**.
- **Frequency independence:** Because capacitive crosstalk amplitude depends on the driver's edge slew rate (`dV/dt`) and victim impedance (`1 kΩ`), **slowing the SWD clock from 1 MHz down to 250 Hz had zero effect** — a classic trap that makes a physical crosstalk bug look like a protocol or firmware bug.

Simulating `0x0bc12477` with "advance 1 extra bit on every `1 → 0` transition of `SWDIO`" matched the observed capture **40 out of 40 bits**:

```text
Simulated RP2040 DPIDR (0x0bc12477) + 1→0 crosstalk: 1101111110010101000011111000011111111111
Observed from Pico (50 µs, 500 µs, and 2000 µs):     1101111110010101000011111000011111111111
```

### Fix & Verification
- **Fix:** Physically peeled the `SWDIO` and `SWCLK` ribbon wires apart from each other (both Pico-side and Pi-side) so they do not run side-by-side in parallel. No resistor values or software settings needed to change.
- **Verification:** Immediate first-try pass with OpenOCD (`bcm2835gpio`, non-root as `admin`) at both `100 kHz` and full **`1000 kHz`**:
  - `SWD DPIDR 0x0bc12477 DPv2`
  - `[rp2040.core0] Cortex-M0+ r0p1 processor detected — Examination succeed`
  - `[rp2040.core1] Cortex-M0+ r0p1 processor detected — Examination succeed`

### Takeaway for Future Bench Harness Builds
1. **Never keep `SWCLK` and `SWDIO` joined in a flat ribbon cable when `1 kΩ` series protection resistors are inline.** Separate them physically (or place `GND` between them and mount the `1 kΩ` resistors right next to the Pi header rather than after a long parallel run).
2. If OpenOCD prints `Too long SWD WAIT` / `JUNK DP read` with a **non-`0xFFFFFFFF`, deterministic hex value** that doesn't change when slowing the clock down to a few kHz, suspect **edge-rate (`dV/dt`) crosstalk onto `SWCLK`**, not bit-rate RC rolloff.

---

## 2. Rig Pi Provisioning Notes (Debian 13 Trixie / BlueZ 5.82)

- **SSH isolation:** Added `Host larkbridge-rig` in WSL `~/.ssh/config` with `UserKnownHostsFile ~/.ssh/known_hosts_rig` so the rig SD card (same Pi 3B MAC / IP `192.168.0.251`) never collides with the product SD card's `known_hosts` entry.
- **Fixes committed (`1245c3f`):**
  1. `rig/esp/pi-image/lib.sh`: BlueZ 5.82 `btmgmt info` exits before printing if stdin hits immediate EOF (`</dev/null`, including under systemd). Replaced `</dev/null` with `(sleep 0.5) | timeout 10 btmgmt info`.
  2. `rig/esp/pi-image/selftest.sh`: `pw-dump | grep -q` caused `pw-dump` to receive `SIGPIPE` (exit 141) under `set -o pipefail`, falsely failing `fake_car_sink`. Switched to `grep -F ... >/dev/null`.
- **Verified checks:**
  - `rig-selftest`: **13/13 PASS (`ok: true`)** (`larkbridge-rig-bt-pin.service` active, `hci0 B8:27:EB:43:8D:51`, `larkbridge_fake_car` PipeWire null sink present, `/dev/serial0 -> /dev/ttyS0`, `throttled=0x0`, `45.1 C`).
  - `fake-car status`: **`ok: true`** (`LarkRig Fake Car`, `class: 0x440420`, powered, `sink_present: true`).
  - OpenOCD `bcm2835gpio` works **non-root** as user `admin` via `/dev/gpiomem` at `1000 kHz` (resolves `rig/esp/pi-image/README.md` Open Question #3).
