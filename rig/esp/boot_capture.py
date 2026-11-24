#!/usr/bin/env python3
"""Reset a board through esptool and capture its boot log across USB re-enumeration.

The S31's USB Serial/JTAG console disappears while the chip resets, so a plain capture opened
after ``--after hard-reset`` misses the bootloader and app banner. This resets the board with
esptool's own reset sequence (``read-mac``; it writes nothing), then reopens the port as soon as
it comes back, with DTR and RTS deasserted, and writes timestamped lines.

Run under Windows Python for COM ports:
    py -3 rig/esp/boot_capture.py --chip esp32s31 --port COM3 --seconds 20 --out boot.log
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "firmware" / "tools"))
import arrival  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chip", required=True, choices=("esp32", "esp32s31"))
    parser.add_argument("--port", required=True)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--no-reset", action="store_true", help="capture only")
    args = parser.parse_args()
    if not arrival.PORT_RE.fullmatch(args.port):
        parser.error("unsupported port name")

    import serial  # type: ignore

    factory = arrival._quiet_serial_factory(serial.Serial)
    with args.out.open("w", encoding="utf-8", newline="\n") as log:
        if not args.no_reset:
            command = [sys.executable, "-m", "esptool", "--chip", args.chip, "--port", args.port,
                       "--before", "default-reset", "--after", "hard-reset"]
            if args.chip == "esp32s31":
                command.append("--no-stub")
            command.append("read-mac")
            result = subprocess.run(command, capture_output=True, text=True)
            log.write(f"[{utc_now()}] RESET rc={result.returncode} via esptool read-mac\n")
            if result.returncode != 0:
                log.write(result.stdout + result.stderr)
                return 2
        deadline = time.monotonic() + args.seconds
        connection, reopens, buffer = None, 0, b""
        while time.monotonic() < deadline:
            if connection is None:
                try:
                    connection = factory(port=args.port, baudrate=115200, timeout=0.05)
                    reopens += 1
                    log.write(f"[{utc_now()}] PORT_OPEN {args.port} (#{reopens})\n")
                except (OSError, serial.SerialException):
                    time.sleep(0.02)
                    continue
            try:
                chunk = connection.read(4096)
            except (OSError, serial.SerialException):
                connection.close()
                connection = None
                log.write(f"[{utc_now()}] PORT_LOST\n")
                continue
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                log.write(f"[{utc_now()}] {line.decode('utf-8', 'replace').rstrip()}\n")
            log.flush()
        if buffer:
            log.write(f"[{utc_now()}] {buffer.decode('utf-8', 'replace').rstrip()}\n")
        if connection is not None:
            connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
