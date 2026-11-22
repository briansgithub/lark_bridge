# Provisional two-board wiring

This pinout is selected from exposed, non-flash pins and is committed so firmware and wiring cannot
drift independently. It remains **PENDING HARDWARE** until continuity and board schematics are
checked on the delivered revisions.

## Inter-board signals

| Purpose | S31 J2 / GPIO | Direction | ESP32-DevKitC GPIO |
|---|---|---|---|
| I2S BCLK | J2-13 / GPIO47 | S31 → ESP32 | GPIO26 |
| I2S WS/LRCK | J2-14 / GPIO48 | S31 → ESP32 | GPIO25 |
| I2S DATA | J2-15 / GPIO45 | S31 → ESP32 | GPIO22 |
| UART control | J2-17 / GPIO43 TX | S31 → ESP32 | GPIO16 RX2 |
| UART control | J2-18 / GPIO44 RX | ESP32 → S31 | GPIO17 TX2 |
| Signal ground | J2-1 or J2-2 / G | both | GND |

Both boards use 3.3-V logic. Keep the I2S/UART harness short. Do not add level shifters unless the
delivered board documentation contradicts the 3.3-V interface.

Use keyed housings if the delivered headers permit them. Otherwise label both ends before assembly;
the suggested prototype colors are black=GND, blue=BCLK, green=WS, yellow=DATA, orange=S31-TX,
and white=S31-RX. Color is only an aid—the signal label and continuity result are authoritative.
Record connector type, contact/pigtail part number, color, and finished length as harness revision
`H1`. Start at 15 cm or shorter; shortening further is an H13 RF/electrical decision.

## Power

- During bench programming and first integration, power each board through its own programming USB
  connector. Join signal ground but leave the boards' 5-V header pins disconnected.
- After functional qualification, use one regulated 5-V supply sized for both boards and the LARK
  receiver; 5 V / 3 A is the prototype baseline.
- Split final 5 V and ground in a protected star harness. Do not power one board from the other
  board's 3.3-V regulator, and prevent either programming USB connector from back-feeding this rail.
- The S31 USB-A host port powers the LARK receiver and is limited to 500 mA.
- For a vehicle installation, the final supply needs a fuse, reverse-polarity protection, load-dump
  and transient protection, and ignition/power-loss qualification. A bench USB supply does not
  satisfy that gate.

## USB and antennas

- Connect the LARK USB-C receiver to the S31 USB-A host using a data-capable adapter.
- The S31 uses its onboard PCB antenna.
- Connect exactly one lead of the existing 2T2R motherboard antenna through the SparkFun pigtail to
  the 32UE's U.FL connector. Secure the unused antenna lead; never combine the two leads.
- Start qualification with the two Bluetooth antennas separated by the full pigtail length and
  Wi-Fi disabled. Compact only after logging RSSI, retries, and audio discontinuities.

## First-power rule

Before applying power, verify continuity for ground, absence of a 5-V-to-3.3-V short, UART
cross-over, and that neither I2S line is connected to `EN`, `BOOT`, or a power pin. First power each
board separately; join the signal harness only after both standalone firmware images boot.

With both boards and USB cables disconnected, use these initial meter gates:

- each intended end-to-end signal and ground connection is below 1 ohm after subtracting lead
  resistance;
- every signal-to-adjacent-signal, signal-to-5 V, and 5 V-to-3.3 V check is above 1 megaohm after
  the reading settles;
- UART is crossed TX-to-RX, while I2S is one-way S31-to-output;
- no 5 V header connection exists during the two-USB bench topology.

If board protection components prevent a resistance reading from meeting the open-circuit number,
stop and reconcile the delivered schematic rather than waiving the check. Photograph both header
orientations and mark pin 1; online product photos are not wiring evidence.
