#!/usr/bin/env python3
"""Send a raw HCI command to a local Bluetooth controller and print the Command Complete event.

Why this exists
---------------
Configuring SCO routing on the Pi 3's BCM43438 requires the Broadcom vendor command
``Write_SCO_PCM_Int_Param`` (OGF 0x3F, OCF 0x1C).  The traditional way to send it is
``hcitool cmd``, but ``hcitool`` is deprecated and is not guaranteed to be present on
current Debian/Raspberry Pi OS images.  This tool talks to the kernel's HCI raw socket
directly, so it works regardless of which userspace utilities are installed.

Requires root (CAP_NET_RAW) and an adapter that is *down*, or a kernel that permits raw
socket writes on an up adapter.  If the controller rejects the write while up, the caller
should bring the adapter down, send, and bring it back up.

Usage
-----
    sudo ./hci_vendor_cmd.py --ogf 0x3f --ocf 0x1c 0x01 0x02 0x00 0x01 0x01
    sudo ./hci_vendor_cmd.py --sco-routing-transport        # the same thing, named

Reference: Documentation/devicetree/bindings/net/broadcom-bluetooth.yaml documents the
same five parameters as ``brcm,bt-pcm-int-params``:
    <sco-routing pcm-interface-rate frame-type sync-mode clock-mode>
    sco-routing: 0=PCM 1=Transport(HCI) 2=Codec 3=I2S
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time

# --- constants the Python stdlib does not expose -----------------------------------
AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_RAW = 0
HCI_CHANNEL_USER = 1

SOL_HCI = 0
HCI_FILTER = 2

HCI_COMMAND_PKT = 0x01
HCI_EVENT_PKT = 0x04

EVT_CMD_COMPLETE = 0x0E
EVT_CMD_STATUS = 0x0F

# The one command this project actually needs, pre-named so nobody has to remember it.
SCO_ROUTING_TRANSPORT = [0x01, 0x02, 0x00, 0x01, 0x01]


def opcode(ogf: int, ocf: int) -> int:
    """HCI opcode = OGF in the top 6 bits, OCF in the bottom 10."""
    return ((ogf & 0x3F) << 10) | (ocf & 0x03FF)


def build_hci_filter(type_mask: int, event_mask: int) -> bytes:
    """struct hci_filter { uint32 type_mask; uint32 event_mask[2]; uint16 opcode; }"""
    return struct.pack("<LLLH", type_mask, event_mask & 0xFFFFFFFF, event_mask >> 32, 0)


def send_command(dev_id: int, ogf: int, ocf: int, params: bytes, timeout: float) -> bytes:
    op = opcode(ogf, ocf)
    pkt = struct.pack("<BHB", HCI_COMMAND_PKT, op, len(params)) + params

    sock = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)
    try:
        # Accept event packets, and specifically Command Complete / Command Status.
        flt = build_hci_filter(
            type_mask=1 << HCI_EVENT_PKT,
            event_mask=(1 << EVT_CMD_COMPLETE) | (1 << EVT_CMD_STATUS),
        )
        sock.setsockopt(SOL_HCI, HCI_FILTER, flt)
        sock.bind((dev_id,))
        sock.settimeout(timeout)

        sys.stderr.write(
            f"-> HCI cmd opcode=0x{op:04x} (OGF=0x{ogf:02x} OCF=0x{ocf:04x}) "
            f"plen={len(params)} params={params.hex(' ')}\n"
        )
        sock.send(pkt)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = sock.recv(260)
            except socket.timeout:
                break
            if len(data) < 4 or data[0] != HCI_EVENT_PKT:
                continue
            evt, plen = data[1], data[2]
            body = data[3 : 3 + plen]
            if evt == EVT_CMD_COMPLETE and len(body) >= 3:
                got_op = struct.unpack("<H", body[1:3])[0]
                if got_op == op:
                    return body[3:]
            elif evt == EVT_CMD_STATUS and len(body) >= 4:
                got_op = struct.unpack("<H", body[2:4])[0]
                if got_op == op:
                    return body[0:1]
        raise TimeoutError(f"no Command Complete for opcode 0x{op:04x} within {timeout}s")
    finally:
        sock.close()


def parse_byte(text: str) -> int:
    value = int(text, 0)
    if not 0 <= value <= 0xFF:
        raise argparse.ArgumentTypeError(f"{text} is not a byte")
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("params", nargs="*", type=parse_byte, help="command parameters as bytes (0x.. or decimal)")
    ap.add_argument("--dev", type=int, default=0, help="adapter index, i.e. 0 for hci0 (default: 0)")
    ap.add_argument("--ogf", type=lambda s: int(s, 0), default=0x3F, help="opcode group field (default: 0x3f, vendor)")
    ap.add_argument("--ocf", type=lambda s: int(s, 0), default=0x1C, help="opcode command field (default: 0x1c)")
    ap.add_argument("--timeout", type=float, default=3.0, help="seconds to wait for the event")
    ap.add_argument(
        "--sco-routing-transport",
        action="store_true",
        help="shorthand for the Broadcom Write_SCO_PCM_Int_Param that routes SCO over HCI",
    )
    args = ap.parse_args()

    params = list(args.params)
    if args.sco_routing_transport:
        if params:
            ap.error("--sco-routing-transport takes no positional parameters")
        params = list(SCO_ROUTING_TRANSPORT)
        sys.stderr.write("using Write_SCO_PCM_Int_Param: sco-routing=Transport(HCI), 512kbps, short, master, master\n")

    if not params:
        ap.error("no command parameters given")

    try:
        ret = send_command(args.dev, args.ogf, args.ocf, bytes(params), args.timeout)
    except PermissionError:
        sys.stderr.write("ERROR: permission denied — run as root\n")
        return 77
    except OSError as exc:
        sys.stderr.write(f"ERROR: HCI socket failed on hci{args.dev}: {exc}\n")
        return 1
    except TimeoutError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2

    status = ret[0] if ret else 0xFF
    sys.stderr.write(f"<- return params: {ret.hex(' ') if ret else '(none)'}\n")
    if status == 0x00:
        sys.stderr.write("STATUS: 0x00 SUCCESS — controller accepted the command\n")
        return 0
    sys.stderr.write(
        f"STATUS: 0x{status:02x} FAILURE — controller rejected the command.\n"
        "        0x01=Unknown HCI Command means this controller/firmware does not implement it.\n"
        "        Record this verbatim in docs/experiments/E01-sco-over-hci.md.\n"
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
