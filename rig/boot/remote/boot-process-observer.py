#!/usr/bin/env python3
"""Capture short-lived boot helper ancestry to a RAM-backed evidence file."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

OUTPUT = Path("/run/larkbridge-boot-processes.jsonl")
WATCH_NAMES = {"systemctl", "netplan", "netplan-dbus"}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def snapshot(pid: int) -> dict[str, object] | None:
    root = Path("/proc") / str(pid)
    status = read_text(root / "status")
    if not status:
        return None
    parent = 0
    for line in status.splitlines():
        if line.startswith("PPid:"):
            parent = int(line.split()[1])
            break
    try:
        cmdline = (
            (root / "cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode("utf-8", errors="replace")
            .strip()
        )
    except OSError:
        cmdline = ""
    return {
        "pid": pid,
        "ppid": parent,
        "comm": read_text(root / "comm"),
        "cmdline": cmdline,
        "cgroup": read_text(root / "cgroup"),
    }


def ancestry(pid: int) -> list[dict[str, object]]:
    result = []
    visited = set()
    while pid > 0 and pid not in visited:
        visited.add(pid)
        value = snapshot(pid)
        if value is None:
            break
        result.append(value)
        pid = int(value["ppid"])
    return result


def main() -> int:
    seen: set[int] = set()
    with OUTPUT.open("a", encoding="utf-8") as stream:
        while time.monotonic() < 30:
            for path in Path("/proc").glob("[0-9]*/comm"):
                pid = int(path.parent.name)
                if pid in seen:
                    continue
                seen.add(pid)
                value = snapshot(pid)
                if value is None:
                    continue
                name = str(value["comm"])
                command = str(value["cmdline"])
                if name in WATCH_NAMES or any(word in command for word in WATCH_NAMES):
                    stream.write(
                        json.dumps(
                            {
                                "observed_uptime_s": round(time.monotonic(), 6),
                                "ancestry": ancestry(pid),
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
            time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
