# Inter-board protocol v1

This is the operator reference for the S31 gateway-to-output ESP32 control link. Audio never
crosses this UART; it uses the separate I2S wires.

## Physical and frame format

- UART: 115200 baud, 8 data bits, no parity, 1 stop bit, no flow control.
- Each raw packet is COBS encoded and terminated by one `00` byte.
- Multibyte integers are little-endian.
- Maximum payload is 128 bytes.
- CRC is CRC-16/CCITT-FALSE: polynomial `0x1021`, initial value `0xffff`, no reflection, no final
  XOR. It covers the six-byte header and payload. The two CRC bytes are appended low byte first.

Raw packet before COBS:

| Offset | Bytes | Field |
|---:|---:|---|
| 0 | 1 | protocol version (`1`) |
| 1 | 1 | message type |
| 2 | 2 | sequence |
| 4 | 2 | payload length |
| 6 | N | payload |
| 6+N | 2 | CRC-16 |

An invalid version, type, length, COBS code, or CRC is discarded. An overlong frame is discarded
through the next delimiter, after which decoding resumes.

## Messages and payloads

| Type | Value | Length | Payload |
|---|---:|---:|---|
| `HELLO` | 1 | 7 | role u8, capability mask u16, random boot-session nonce u32 |
| `HEARTBEAT` | 2 | 2 | effective route u8, readiness mask u8 |
| `ROUTE` | 3 | 1 | `SILENCE=0`, `A2DP=1`, or `HFP=2` |
| `AVRCP_COMMAND` | 4 | 2 | AVRCP passthrough opcode u8, state u8 |
| `STATUS` | 5 | 41 | telemetry layout below |
| `FAULT` | 6 | 2 | fault code u16 |
| `ACK` | 7 | 4 | acknowledged type u8, sequence u16, status u8 |

Roles are `S31_GATEWAY=1` and `OUTPUT=2`. Readiness bits are BT `0x01`, I2S `0x02`, UART `0x04`,
UAC `0x08`, AEC `0x10`, peer `0x20`, and audio `0x40`. Capability definitions are authoritative in
`firmware/common/include/bridge_link.h`.

AVRCP state `TAP=2` is the normal wire operation. One reliable TAP becomes an adjacent local Pixel
press/release pair on S31. Legacy `PRESSED=0` and `RELEASED=1` remain decodable, but the output
firmware forwards car controls only as TAPs.

`STATUS` layout:

| Offset | Bytes | Meaning |
|---:|---:|---|
| 0 | 1 | readiness mask |
| 1 | 2 | FIFO fill, stereo frames |
| 3 | 4 | underrun count |
| 7 | 4 | overrun count |
| 11 | 2 | signed correction, ppm |
| 13 | 4 | signed cumulative adaptive input-frame delta |
| 17 | 4 | missing frames |
| 21 | 4 | dropped frames |
| 25 | 4 | I2S errors |
| 29 | 2 | observed FIFO minimum |
| 31 | 2 | observed FIFO maximum |
| 33 | 4 | protocol errors |
| 37 | 4 | reliable-message failures |

ACK status is `OK=0`, `UNSUPPORTED=1`, `INVALID=2`, or `NOT_READY=3`. `NOT_READY` is retriable;
the sender preserves the same packet and sequence. The other rejection statuses are terminal.

## Sessions, replay, and retries

Each boot generates a random session nonce in HELLO. A changed peer nonce starts a new replay
domain, discards pending work, clears the 32-sequence replay window, and forces silence. Repeated
HELLO from the same boot is idempotent.

ROUTE and AVRCP_COMMAND are reliable. They use a 250 ms retry period and at most five sends. Every
duplicate receives an ACK, but its side effect executes once within the current boot session. A
TAP whose press enqueue succeeded but release enqueue did not returns `NOT_READY`; retransmission
of that same sequence attempts only the pending release.

## Route fence

Every desired-route identity change increments a generation, including an `A2DP -> HFP -> A2DP`
change that occurs between link-task polls. Opening a route is a two-phase transaction:

1. Both boards locally force silence.
2. S31 sends reliable `ROUTE(SILENCE)` and waits for its ACK.
3. Both stay silent for the configured drain guard (default 350 ms).
4. S31 sends the desired ROUTE and waits for its ACK.
5. S31 authorizes only the matching route and generation, then advertises it by heartbeat.

An obsolete ACK, timeout, peer reboot, malformed command, heartbeat loss, or local Bluetooth/audio
boundary returns the system to silence. The output board also requires current local BT, peer,
audio, I2S, UART, remote capability, heartbeat, and committed-route gates before its A2DP callback
can read nonzero PCM.
