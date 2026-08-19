#!/usr/bin/env python3
"""Make an offline Raspberry Pi fstab/cmdline use read-only boot and overlay root."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path


def atomic_text(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def read_only_options(value: str) -> str:
    options = [option for option in value.split(",") if option not in {"rw", "ro"}]
    # mount applies conflicting options from left to right. Keep ro last so an
    # earlier "defaults" token cannot re-enable writes.
    return ",".join([*options, "ro"])


def configure_fstab(path: Path) -> None:
    output: list[str] = []
    seen_root = False
    seen_boot = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output.append(line)
            continue
        fields = stripped.split()
        if len(fields) < 6:
            raise ValueError(f"unsupported fstab line: {line}")
        if fields[1] == "/":
            fields[3] = read_only_options(fields[3])
            fields[5] = "1"
            seen_root = True
        elif fields[1] == "/boot/firmware":
            fields[3] = read_only_options(fields[3])
            fields[5] = "2"
            seen_boot = True
        elif fields[1] == "/var/lib/larkbridge-persist":
            continue
        output.append("\t".join(fields))
    if not seen_root or not seen_boot:
        raise ValueError("fstab must contain both / and /boot/firmware")
    output.append(
        "LABEL=LARKDATA\t/var/lib/larkbridge-persist\text4\t"
        "rw,noatime,nosuid,nodev,noexec,commit=1,errors=remount-ro,nofail,"
        "x-systemd.device-timeout=5s\t0\t2"
    )
    atomic_text(path, "\n".join(output) + "\n")


def configure_cmdline(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise ValueError("cmdline.txt must contain exactly one line")
    tokens = [token for token in lines[0].split() if token not in {"ro", "rw"}]
    if not any(token.startswith("root=") for token in tokens):
        raise ValueError("cmdline.txt has no root= argument")
    root_index = next(
        index for index, token in enumerate(tokens) if token.startswith("root=")
    )
    tokens.insert(root_index + 1, "ro")
    atomic_text(path, " ".join(tokens) + "\n")


def configure_config_txt(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    active = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    values = [line for line in active if line.startswith("auto_initramfs=")]
    if values and values != ["auto_initramfs=1"]:
        raise ValueError("config.txt has a conflicting auto_initramfs setting")
    if not values:
        atomic_text(path, text.rstrip("\n") + "\nauto_initramfs=1\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--boot", type=Path, required=True)
    arguments = parser.parse_args()
    configure_fstab(arguments.root / "etc/fstab")
    configure_cmdline(arguments.boot / "cmdline.txt")
    configure_config_txt(arguments.boot / "config.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
