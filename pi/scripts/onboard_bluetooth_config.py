#!/usr/bin/env python3
"""Manage the qualified USB-call-controller block in Raspberry Pi config.txt.

The boot partition is outside overlayroot, so the caller must record a preimage
before invoking this helper.  This file owns only its marked block and refuses
to adopt an unmarked ``dtoverlay=disable-bt`` directive.
"""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path

BEGIN = "# BEGIN rpi-lark-bridge qualified USB-BT500 call controller"
END = "# END rpi-lark-bridge qualified USB-BT500 call controller"
BLOCK = (BEGIN, "[all]", "dtoverlay=disable-bt", END)
DISABLE_BT = re.compile(r"^\s*dtoverlay\s*=\s*disable-bt\s*(?:#.*)?$")


class ConfigError(RuntimeError):
    """The boot configuration is ambiguous or cannot be safely managed."""


def _marker_range(lines: list[str]) -> tuple[int, int] | None:
    starts = [index for index, line in enumerate(lines) if line.strip() == BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == END]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ConfigError("qualified BT500 marker block is malformed or duplicated")
    start, end = starts[0], ends[0]
    if tuple(line.strip() for line in lines[start : end + 1]) != BLOCK:
        raise ConfigError("qualified BT500 marker block was edited")
    return start, end


def configure(text: str, *, enabled: bool) -> str:
    """Return config.txt with the exact managed block enabled or removed."""
    lines = text.splitlines()
    marker = _marker_range(lines)
    marker_indexes: set[int] = set()
    if marker is not None:
        marker_indexes.update(range(marker[0], marker[1] + 1))
    unmanaged = [
        index + 1
        for index, line in enumerate(lines)
        if index not in marker_indexes and DISABLE_BT.fullmatch(line)
    ]
    if unmanaged:
        locations = ", ".join(map(str, unmanaged))
        raise ConfigError(
            f"unmanaged dtoverlay=disable-bt directive at line(s) {locations}"
        )

    if enabled:
        if marker is not None:
            return text
        result = text
        if result and not result.endswith("\n"):
            result += "\n"
        return result + "\n".join(BLOCK) + "\n"

    if marker is None:
        return text
    start, end = marker
    del lines[start : end + 1]
    result = "\n".join(lines)
    return result + ("\n" if text.endswith("\n") and result else "")


def managed_disable_present(text: str) -> bool:
    """True only when the exact, unambiguous managed block is present."""
    lines = text.splitlines()
    marker = _marker_range(lines)
    if marker is None:
        return False
    marker_indexes = set(range(marker[0], marker[1] + 1))
    if any(
        DISABLE_BT.fullmatch(line)
        for index, line in enumerate(lines)
        if index not in marker_indexes
    ):
        raise ConfigError("an unmanaged dtoverlay=disable-bt directive is also present")
    return True


def replace(path: Path, text: str) -> None:
    """Atomically replace one existing boot file and preserve its permission mode."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise ConfigError(f"cannot stat {path}: {exc}") from exc
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.new-",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        temporary_name = None
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError as exc:
        raise ConfigError(f"cannot replace {path}: {exc}") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("/boot/firmware/config.txt"))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--disable-qualified", action="store_true")
    action.add_argument("--restore-managed", action="store_true")
    action.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        original = arguments.path.read_text(encoding="utf-8")
        if arguments.check:
            if not managed_disable_present(original):
                raise ConfigError("qualified BT500 disable-bt block is absent")
            print("qualified BT500 disable-bt block verified")
            return 0
        updated = configure(original, enabled=bool(arguments.disable_qualified))
        if updated != original:
            replace(arguments.path, updated)
        print("qualified BT500 disable-bt block updated")
        return 0
    except (OSError, UnicodeError, ConfigError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
