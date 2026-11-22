# ESP bridge bill of materials

## Ordered processing and RF parts

| Quantity | Part | Function |
|---:|---|---|
| 1 | `ESP32-S31-FUNCTION-COREBOARD-1` | Pixel-facing HFP/A2DP/AVRCP, LARK USB host, AEC, coordinator |
| 1 | `ESP32-DEVKITC-32UE` | Car-facing A2DP Source and AVRCP Target |
| 1 | SparkFun `18569` | U.FL-to-RP-SMA pigtail for the `32UE`; this is not an antenna |

The existing dual-band 2T2R motherboard antenna is usable only if one of its connectors mates with
the pigtail. Connect exactly one antenna lead to the `32UE`; leave the second 2T2R lead insulated
and disconnected. Do not use a splitter or combine the leads.

## Required assembly items

| Quantity | Item | Notes |
|---:|---|---|
| 1 | USB-A-male to USB-C-female data adapter | Connects the LARK USB-C receiver to the S31 USB-A host port |
| 1 | Data-capable USB-A to USB-C cable | S31 programming/power during bench work |
| 1 | Data-capable USB-A to Micro-B cable | ESP32-DevKitC programming/power during bench work |
| 6+ | Short female jumper leads or keyed harness | I2S BCLK/WS/DATA, UART TX/RX, and common ground |
| 1 | Regulated 5 V, at least 3 A supply | Final one-cable prototype; use star distribution and protection |
| 1 | Fused or protected 5 V distribution harness | Final build only; must prevent programming-cable back-feed |
| — | Standoffs and nonconductive enclosure | Maintain antenna separation and strain relief |

For initial bench work, power each board from its own programming USB cable and **do not connect the
boards' 5 V pins**. Connect only the signal harness and ground. The common one-cable power harness
is a later, separately verified assembly step.

## Existing system parts

- Hollyland LARK A1 transmitter and USB-C receiver.
- Google Pixel 7a.
- Honda CR-V 2015 LX Bluetooth audio system or another A2DP/AVRCP sink.
- One lead of the existing 2.4/5 GHz 2T2R antenna.

## Required-by-gate bench equipment

- H2/H4/H5: logic analyzer capable of decoding 3.3-V UART and I2S.
- H0/H4: multimeter, fine probes, and insulated labels for continuity/orientation evidence.
- H1/H6/H17: USB power meter and current-limited bench supply.
- H7: headphones/speaker independent of the car for first pairing.
- H9/H10: second phone or PC capable of playing a retained stimulus and recording the far-end call.
- H10: quiet, repeatable microphone/speaker fixture or marked positions that can be reconstructed.

On arrival, replace generic inventory descriptions with exact manufacturer/part markings and board
revisions. Do not predeclare the existing antenna connector, gain, or delivered header population
as qualified merely because the parts appear to mate.

No AUX output hardware is included in this build. Adding line-level AUX later requires a separate
stereo I2S DAC; never connect the S31's amplified speaker output to a car AUX input.

References: [S31 board guide](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s31/esp32-s31-function-coreboard-1/user_guide.html),
[ESP32-WROOM-32E/32UE datasheet](https://documentation.espressif.com/esp32-wroom-32e_esp32-wroom-32ue_datasheet_en.html),
[SparkFun 18569](https://www.sparkfun.com/rp-sma-to-u-fl-cable-150mm.html).
