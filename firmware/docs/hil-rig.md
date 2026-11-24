# Hardware-in-the-loop rig: hardware and setup

This rig lets an agent build, flash, stimulate, measure, and judge the ESP32 bridge with no person
present for most gates. The forward plan is in
[`ESP32_WIRELESS_PORT_PLAN.md`](../../ESP32_WIRELESS_PORT_PLAN.md). The board wiring is in
[`wiring.md`](wiring.md), and the power rules are in [`rf-power-matrix.md`](rf-power-matrix.md).

## 1. Topology

```text
                     Windows PC (agent host)
   Docker/IDF builds · esptool · pyserial · adb · sigrok-cli · ssh · rig/esp/hil.py
     │USB (hub)             │USB (hub)            │USB (hub)        │USB (hub)       │Ethernet
     ▼                      ▼                     ▼                 ▼                ▼
 ESP32-S31 Type-C      ESP32-DevKitC-32UE    Pixel 7a          Logic analyzer   Rig Pi 3B
 (USB Serial/JTAG)     Micro-B               (ADB + harness)   (taps harness)   (second SD card)
     │USB-A host                                                                  onboard BT = fake car
     ├── Lark USB-C receiver  (real mic)        ── or ──   Pico fake Lark ◄─SWD+UART─┤ (A2DP Sink, AVRCP CT)
     │                                                                            USB: C-Media dongle
     ╞══ 6-wire I2S + UART harness ══ 32UE                                         → instrument speaker
     ~~BT~~ Pixel                    32UE ~~BT~~ fake car (rig Pi) / BT speaker / car
```

## 2. Bill of materials

### Already owned (from the repo inventory)

| Item | Rig role |
|---|---|
| ESP32-S31-Function-CoreBoard-1, ESP32-DevKitC-32UE, SparkFun 18569 pigtail, one 2.4 GHz antenna lead | Devices under test (DUT) |
| Hollyland Lark A1 transmitter and USB-C receiver | Real microphone |
| Google Pixel 7a | Real phone |
| Raspberry Pi 3B with onboard BCM43438 | Rig Pi: fake car, Pico controller, instrument host |
| ASUS USB-BT500 | Kept for an optional fake Pixel AG later (plan R4). Leave it unpowered in the rig image until then |
| C-Media USB audio dongle (`0d8c:0014`) | Plays near-end speech into the acoustic fixture |
| Raspberry Pi Pico (RP2040) | Fake Lark (Stage E) |
| Small speaker used by the Pi loopback rig | Instrument (near-end talker) speaker |

### Required purchases (to start autonomous work)

| Qty | Item | Why |
|---:|---|---|
| 1 | microSD card, 16 GB or larger | A rig image for the Pi. The product card stays untouched as the rollback |
| 1 | 8-channel, 24 MS/s USB logic analyzer supported by sigrok (fx2lafw class) with grabber clips | Scripted UART and I2S decoding for H2, H4, and H5 |
| 1 | Powered USB hub, 4 or more data ports | The S31, 32UE, Pixel, and analyzer all connect to the PC |
| 1 | Bluetooth A2DP speaker, if none is on hand | Real-sink interop (H7) and the echo source for the acoustic AEC test (H10) |
| 1 | USB-A male to Micro-B data cable | Connects the Pico to the S31's USB-A host port |
| 1 | USB-A male to USB-C female data adapter (from the BOM, if not already bought) | Connects the Lark receiver to the S31 host port |
| ~12 | Short female-to-female jumper leads and four 1 kΩ resistors | The board harness and the Pico's control lines |

### Optional items (buy when the named stage needs them)

| Item | Needed for |
|---|---|
| A USB hub with per-port power switching that uhubctl supports, attached to the rig Pi (uhubctl does not run on Windows) | Hard power cuts and brownouts in H14 and H16. RTS resets cover everything before that |
| USB power meter and a current-limited bench supply | Bench power characterization (H1, H6) and the H17 preparation |
| A spare classic ESP32 DevKitC (or an Android phone running Android 14 or later) | Plan R4: a fake Pixel AG, if the Pixel cannot stay on the bench overnight |
| A USB 2.0 2:1 switch controlled by a GPIO | Switching between the real Lark and the Pico on the S31 port without a person. Without it, the swap is queued as a human step |
| Multimeter (if none is on hand) | H0 and the harness continuity checks |

## 3. Physical setup

### 3.1 Bench layout

- Use one bench in a quiet room. Keep the S31's PCB antenna and the 32UE's external antenna at
  least 15 cm apart (the H13 starting cell). Keep Wi-Fi disabled on both boards.
- Plug the S31, the 32UE, the Pixel, and the analyzer into the powered hub, and the hub into the
  PC.
- Power each ESP board **only from its own programming USB port.** Join the boards' grounds
  through the harness, and never connect either board's 5 V header pin.
- Connect the rig Pi to the router over Ethernet, as the existing rig does.

### 3.2 Board harness and analyzer taps

Follow `wiring.md` exactly, then run its unpowered continuity gate. Clip the analyzer onto the
harness:

| Analyzer | Signal | S31 | 32UE |
|---|---|---|---|
| CH0 | I2S BCLK | GPIO47 | GPIO26 |
| CH1 | I2S WS | GPIO48 | GPIO25 |
| CH2 | I2S DATA | GPIO45 | GPIO22 |
| CH3 | UART S31→32UE | GPIO43 (TX) | GPIO16 (RX) |
| CH4 | UART 32UE→S31 | GPIO44 (RX) | GPIO17 (TX) |
| GND | Ground | G | GND |

Sample at 12 MS/s or faster.

### 3.3 Microphone port on the S31 (one USB-A host port)

- **Stages A–D:** the real Lark receiver, through the data adapter.
- **Stage E onward:** the Pico, through the Micro-B cable. The S31's VBUS powers it.
- Swapping between them is a queued human step unless the optional USB switch is fitted.

### 3.4 Pico fake Lark wiring to the rig Pi

The Pico's USB goes to the S31 and is the Pico's only power source. **Never connect the Pi's
5 V or 3.3 V to the Pico.** Put a 1 kΩ resistor in series in each signal line so that a powered
Pi cannot back-power an unpowered Pico. The rig software must also leave these pins as inputs
whenever the S31 is off.

| Signal | Pi BCM (physical pin) | Pico |
|---|---|---|
| SWCLK | GPIO25 (22) | Debug header SWCLK |
| SWDIO | GPIO24 (18) | Debug header SWDIO |
| Reset | GPIO26 (37) | `RUN` (pin 30) |
| Control UART Pi→Pico | GPIO14 TXD (8) | GP1 RX (pin 2) |
| Control UART Pico→Pi | GPIO15 RXD (10) | GP0 TX (pin 1) |
| Ground | GND (6) | GND (pin 3) and the debug header GND |

The Pi reflashes the Pico over SWD with OpenOCD, as proven in R15 of the bring-up report. No
BOOTSEL press or unplugging is needed.

### 3.5 Acoustic fixture (Stage E, T4 part of H10)

- Mount the Lark transmitter on a stand at a fixed, labeled position.
- Place the **Bluetooth speaker** that the 32UE drives at about 30–50 cm. It is the echo source.
- Place the **instrument speaker**, driven by the rig Pi's C-Media dongle, at a second labeled
  position. It is the near-end talker.
- Record both position IDs and the volumes in `rig/inventory.toml`. Moving anything invalidates
  the calibration.

## 4. PC software

Installing these needs your approval; some steps need admin.

| Software | Purpose |
|---|---|
| Docker Desktop (WSL 2) | Pinned ESP-IDF builds (`build_firmware.py`) and the C host tests in the IDF container. This avoids a native compiler |
| `py -3 -m pip install esptool pyserial` (esptool at the version `flashing.md` pins) | Flashing and console capture |
| Android SDK Platform-Tools | `adb` |
| sigrok-cli | Analyzer capture and I2S/UART decoding |
| JDK 17 and the Android command-line tools (Stage D) | Building the harness app |
| OpenSSH key, plus `Host larkbridge-rig` in `~/.ssh/config` | Rig Pi control |

## 5. Rig Pi image (second SD card)

- **OS:** Raspberry Pi OS Lite (64-bit), headless, Ethernet, SSH key authentication only.
- **Fake car:** BlueZ + PipeWire/WirePlumber with only the `a2dp_sink` role (plus AVRCP CT) on the
  **onboard** controller, selected by controller address. Keep the USB-BT500 powered off.
- **Tools:**
  - `btmon` capture;
  - `alsa-utils` and `sox` for C-Media playback and fake-car recording;
  - Python 3 with the `rig/analysis` dependencies;
  - OpenOCD with an RP2040 target over GPIO;
  - `enable_uart=1`, which puts the mini UART on GPIO14/15 and leaves the PL011 for Bluetooth.
- **Rig lease:** `/run/larkbridge-rig.lease`, so only one agent drives the hardware at a time.
- Provisioning will be scripted in `rig/esp/pi-image/` (Stage B/C), so a person only flashes the
  card and boots it.

## 6. Device observability contract (Stage B builds this)

Both firmwares provide:

- **JSON-lines output on the USB console.**
  - Per-event records: state transitions with reason codes.
  - A 1 Hz `stats` record:
    - build ID and image kind;
    - reset reason;
    - heap free and minimum, and task high-water marks;
    - eSCO rx/tx/bad/lost counts and the codec;
    - A2DP frames, underruns, and overruns;
    - FIFO level, minimum, maximum, and ppm correction;
    - UART CRC, replay, and retry counts;
    - UAC packet and rate errors.
- **Line commands:** `status`, `selftest`, `peers`, `route`, `tone <stage> <hz> <dbfs>`,
  `tap <stage>`, and `fault <kind>`. `tap` returns a CRC/RMS digest of one stage.
- **Restrictions:** `tone`, `tap`, `fault`, and `forget <peer>` exist only in `-test` images.
  Release images reject them, and the manifest records the image kind.

## 7. Pixel setup

- **Ideally, dedicate the Pixel to the bench.** If it is your daily phone, unattended runs are
  limited to times it is docked, and plan item R4 (a fake AG) becomes the priority.
- Turn on Developer options: USB debugging, and Stay awake while charging. Connect it by USB to
  the hub and accept the PC's ADB key once.
- Keep Bluetooth on. Use no secure lock screen on the bench, because ADB cannot unlock one, or
  accept attended-only runs.
- **Pair once with the S31:** tap `LarkBridge` and approve the dialog. The agent flashes only the
  app partition, so the bond survives reflashes.
- Sideload the harness app when Stage D builds it. Its calls are self-managed and local, and never
  dial a number.

## 8. What still needs a person

| When | Task |
|---|---|
| Once | Assemble the hardware, run the H0 photos and continuity checks, and prepare and boot the rig SD card |
| Once | Pair the Pixel; take the analyzer and fixture placement photos |
| As queued | Swap the Lark and Pico on the S31 port (without the optional switch) |
| As queued | Approve a re-pair after a deliberate pairing gate |
| As queued | Move the antenna for each H13 cell |
| Final | The car, the vehicle power supply (H17), a cellular call to a nominated number, and the listening sign-off |
| Any time | Respond to any physical anomaly the agent stops on: heat, smell, a reset loop, or an unknown USB device |
