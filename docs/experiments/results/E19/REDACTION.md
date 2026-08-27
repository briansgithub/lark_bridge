# Redaction applied to this evidence set

Bluetooth addresses belonging to the operator's own unrelated devices -- a car kit, a
phone, earbuds, a speaker and others that appear in the Pixel's bond list -- were replaced
with stable pseudonyms of the form `XX:XX:XX:XX:<hash>`. The same device maps to the same
pseudonym everywhere, so any correlation the evidence depended on still holds.

Project hardware is NOT redacted, because it is already documented throughout this
repository and the evidence is unreadable without it:

| Address | Device |
|---|---|
| `5C:33:7B:CB:BF:C5` | Pixel 7a (device under test) |
| `A0:AD:9F:73:6C:24` | ASUS USB-BT500 call controller |
| `C9:5C:FD:6E:28:46` | Monoprice Boombox |
| `50:D7:1B:74:34:D6` | iWorld |
| `98:47:44:CD:73:DE` | Soundcore Space A40 |

9 unrelated addresses were pseudonymised across 43 files.
`A2-bluetooth-manager.txt` was additionally trimmed from 10,193 lines to the LarkBridge-relevant ones; its own header records what was kept and why.
