#!/usr/bin/env python3
"""Idempotent edits of the few system files provision.sh changes in place.

Each command prints ``changed`` or ``unchanged`` and, with ``--check``, writes nothing.

* ``config-txt FILE``  owns one marked block in the Pi's config.txt that sets
  ``enable_uart=1``. It refuses (exit 3) directives that would take the onboard controller
  off the PL011 UART or disable it (``disable-bt``, ``miniuart-bt``) or an
  ``enable_uart=0`` outside the block, because the fake car needs the onboard radio.
* ``cmdline FILE``  removes the kernel's serial console (``console=serial0,...``,
  ``ttyS0`` or ``ttyAMA0``) so the mini UART on GPIO14/15 carries only Pico traffic.
* ``ini-set FILE SECTION KEY=VALUE...``  sets keys in one section of an INI file such as
  /etc/bluetooth/main.conf. It replaces the first commented or live copy of each key in
  that section, or inserts it after the header, and creates the section if it is missing.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile

BEGIN = "# BEGIN larkbridge-rig (rig/esp/pi-image/provision.sh)"
END = "# END larkbridge-rig"
CONFIG_BLOCK = [BEGIN, "[all]", "enable_uart=1", END]
FORBIDDEN = re.compile(r"^\s*dtoverlay\s*=\s*(disable-bt|miniuart-bt|pi3-disable-bt|pi3-miniuart-bt)\b")
UART_OFF = re.compile(r"^\s*enable_uart\s*=\s*0\b")
SERIAL_CONSOLE = re.compile(r"^console=(serial0|serial1|ttyS0|ttyAMA0)(,.*)?$")

EXIT_REFUSED = 3


class Refused(Exception):
    pass


def edit_config_txt(text: str) -> str:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(starts) > 1 or len(ends) > 1 or len(starts) != len(ends) or (
            starts and starts[0] > ends[0]):
        raise Refused("larkbridge-rig block in config.txt is malformed or duplicated")
    outside = lines
    if starts:
        outside = lines[: starts[0]] + lines[ends[0] + 1:]
    for number, line in enumerate(outside, 1):
        if FORBIDDEN.match(line):
            raise Refused(f"config.txt has '{line.strip()}': the fake car needs the onboard "
                          "Bluetooth on the PL011 UART; remove it by hand")
        if UART_OFF.match(line):
            raise Refused("config.txt sets enable_uart=0 outside the larkbridge-rig block")
    while outside and not outside[-1].strip():
        outside = outside[:-1]
    # The block goes last so its [all] cannot change the scope of anything after it.
    return "\n".join(outside + [""] + CONFIG_BLOCK) + "\n"


def edit_cmdline(text: str) -> str:
    tokens = text.split()
    kept = [token for token in tokens if not SERIAL_CONSOLE.match(token)]
    if kept == tokens:
        return text
    return " ".join(kept) + "\n"


def edit_ini(text: str, section: str, pairs: list[tuple[str, str]]) -> str:
    lines = text.splitlines()
    header = f"[{section}]"
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == header)
    except StopIteration:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(header)
        start = len(lines) - 1
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^\s*\[.*\]\s*$", lines[i])), len(lines))
    insert_at = start + 1
    for key, value in pairs:
        wanted = f"{key} = {value}"
        pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
        live = [i for i in range(start + 1, end)
                if pattern.match(lines[i]) and not lines[i].lstrip().startswith("#")]
        commented = [i for i in range(start + 1, end) if pattern.match(lines[i])]
        target = live[0] if live else (commented[0] if commented else None)
        if target is None:
            lines.insert(insert_at, wanted)
            insert_at += 1
            end += 1
        else:
            lines[target] = wanted
    return "\n".join(lines) + "\n"


def _write_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    mode = os.stat(path).st_mode & 0o7777 if os.path.exists(path) else 0o644
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".larkbridge-rig.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report only, write nothing")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config-txt").add_argument("file")
    sub.add_parser("cmdline").add_argument("file")
    ini = sub.add_parser("ini-set")
    ini.add_argument("file")
    ini.add_argument("section")
    ini.add_argument("pairs", nargs="+")
    args = parser.parse_args(argv)

    try:
        with open(args.file, encoding="utf-8") as handle:
            before = handle.read()
    except FileNotFoundError:
        if args.command != "ini-set":
            print(f"missing {args.file}", file=sys.stderr)
            return 1
        before = ""
    try:
        if args.command == "config-txt":
            after = edit_config_txt(before)
        elif args.command == "cmdline":
            after = edit_cmdline(before)
        else:
            pairs = []
            for pair in args.pairs:
                key, sep, value = pair.partition("=")
                if not sep or not key.strip():
                    parser.error(f"expected KEY=VALUE, got {pair!r}")
                pairs.append((key.strip(), value.strip()))
            after = edit_ini(before, args.section, pairs)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if after == before:
        print("unchanged")
        return 0
    if not args.check:
        _write_atomic(args.file, after)
    print("changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
