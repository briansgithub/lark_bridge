#!/usr/bin/env python3
"""Dump NON-SCO HCI activity in a time window from a btmon btsnoop capture.

Why not `btmon -r`: it pretty-prints every record including hex dumps, and an overnight SCO
capture is hundreds of MB. Decoding occurrence 5's 426 MB file that way takes over an hour on a
Pi 3. This walks the fixed-size record headers instead, skips SCO payloads without decoding them,
and only formats what falls inside the window — seconds instead of an hour.

    btsnoop_window.py capture.btsnoop "2026-08-17 02:54:00" "2026-08-17 02:56:30"

Format: btmon writes btsnoop with datalink type 2001 (monitor). Each record is a 24-byte header
followed by packet data. The monitor opcode lives in the low 16 bits of `flags`; the adapter
index is in the high 16.
"""

from __future__ import annotations

import struct
import sys
from datetime import datetime, timezone

# btsnoop timestamps are microseconds since midnight 1 Jan 0 AD (proleptic Gregorian).
BTSNOOP_EPOCH_DELTA_US = 62_168_256_000_000_000

OPCODES = {
    0x0000: "New Index", 0x0001: "Delete Index",
    0x0002: "Command", 0x0003: "Event",
    0x0004: "ACL TX", 0x0005: "ACL RX",
    0x0006: "SCO TX", 0x0007: "SCO RX",
    0x0008: "Open Index", 0x0009: "Close Index",
    0x000A: "Index Info", 0x000B: "Vendor Diag",
    0x000C: "System Note", 0x000D: "User Logging",
}
SCO = (0x0006, 0x0007)

# Event codes worth naming: these are the ones that would explain a burst.
EVENTS = {
    0x05: "Disconnection Complete", 0x07: "Remote Name Req Complete",
    0x08: "Encryption Change", 0x0C: "Read Remote Version Complete",
    0x0E: "Command Complete", 0x0F: "Command Status",
    0x13: "Number of Completed Packets",
    0x14: "Mode Change",                 # <-- sniff/active transitions
    0x1B: "Max Slots Change",
    0x2C: "Synchronous Connection Complete",
    0x2D: "Synchronous Connection Changed",
    0x30: "QoS Setup Complete",
    0x38: "Link Supervision Timeout Changed",
    0x3E: "LE Meta", 0xFF: "Vendor",
}
MODES = {0x00: "Active", 0x01: "Hold", 0x02: "Sniff", 0x03: "Park"}


def main() -> int:
    path, start_s, end_s = sys.argv[1], sys.argv[2], sys.argv[3]
    start = datetime.fromisoformat(start_s).replace(tzinfo=timezone.utc).timestamp()
    end = datetime.fromisoformat(end_s).replace(tzinfo=timezone.utc).timestamp()

    counts: dict[str, int] = {}
    sco_in_window = 0
    shown = 0

    with open(path, "rb") as f:
        hdr = f.read(16)
        if not hdr.startswith(b"btsnoop\x00"):
            print("not a btsnoop file", file=sys.stderr)
            return 1

        while True:
            rh = f.read(24)
            if len(rh) < 24:
                break
            _olen, ilen, flags, _drops, ts = struct.unpack(">IIIIq", rh)
            data = f.read(ilen)
            if len(data) < ilen:
                break

            unix = (ts - BTSNOOP_EPOCH_DELTA_US) / 1e6
            if unix < start:
                continue
            if unix > end:
                break

            op = flags & 0xFFFF
            if op in SCO:
                sco_in_window += 1
                continue

            name = OPCODES.get(op, f"op 0x{op:04x}")
            detail = ""
            if op == 0x0003 and data:                       # Event
                ev = data[0]
                detail = EVENTS.get(ev, f"0x{ev:02x}")
                if ev == 0x14 and len(data) >= 6:           # Mode Change
                    detail += f" -> {MODES.get(data[5], hex(data[5]))}"
                elif ev in (0x0E, 0x0F) and len(data) >= 5:
                    opc = int.from_bytes(data[3:5], "little")
                    detail += f" (opcode 0x{opc:04x})"
            elif op == 0x0002 and len(data) >= 2:           # Command
                detail = f"opcode 0x{int.from_bytes(data[0:2], 'little'):04x}"

            key = f"{name} {detail}".strip()
            counts[key] = counts.get(key, 0) + 1
            stamp = datetime.fromtimestamp(unix, timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            print(f"  {stamp}  {name:<28} {detail}")
            shown += 1

    print(f"\n  --- {shown} non-SCO records, {sco_in_window} SCO frames in window ---")
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {v:>6}  {k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
