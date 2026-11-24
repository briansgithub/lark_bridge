# H2-H5 six-wire harness

Use the S31 Type-C board and the ESP32-DevKitC-32UE. Orient each header using the
board's pin-1 markings. The two boards use 3.3 V logic and have separate USB
power connections.

| Signal | S31 pin | DevKitC pin | Direction | Analyzer |
|---|---|---|---|---|
| I2S BCLK | J2-13 / GPIO47 | GPIO26 | S31 to DevKitC | CH0 |
| I2S WS | J2-14 / GPIO48 | GPIO25 | S31 to DevKitC | CH1 |
| I2S data | J2-15 / GPIO45 | GPIO22 | S31 to DevKitC | CH2 |
| UART S31 TX | J2-17 / GPIO43 | GPIO16 / RX2 | S31 to DevKitC | CH3 |
| UART S31 RX | J2-18 / GPIO44 | GPIO17 / TX2 | DevKitC to S31 | CH4 |
| Signal ground | J2-1 or J2-2 | GND | Common | GND |

Leave both boards' 5 V headers disconnected. For continuity, disconnect both USB
power cables. Subtract meter lead resistance and check for less than 1 ohm
end-to-end on each wire. Check for more than 1 megohm between adjacent signals,
each signal and 5 V, and 5 V and 3.3 V after the meter settles. Check that no wire
lands on EN, BOOT, or a supply pin. Photograph both headers and mark pin 1.

For H4, clip an analyzer that supports 24 MS/s onto CH0-CH2 and signal ground.
Reconnect each board to its own USB programming port. See [wiring.md](../docs/wiring.md)
and [hil-rig.md](../docs/hil-rig.md#32-board-harness-and-analyzer-taps).
