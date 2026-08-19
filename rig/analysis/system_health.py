#!/usr/bin/env python3
"""Emit a lightweight Pi 3 CPU, memory, temperature, and throttle snapshot as JSON."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def cpu_times() -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        values = [int(value) for value in fields[1:]]
        idle = sum(values[3:5])
        result[fields[0]] = (sum(values), idle)
    return result


def command(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, timeout=3, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def main() -> int:
    before = cpu_times()
    time.sleep(1)
    after = cpu_times()
    utilization: dict[str, float] = {}
    for name, (total, idle) in after.items():
        old_total, old_idle = before.get(name, (total, idle))
        elapsed = total - old_total
        busy = elapsed - (idle - old_idle)
        utilization[name] = round(100.0 * busy / elapsed, 2) if elapsed > 0 else 0.0

    meminfo: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        key, value = line.split(":", 1)
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            meminfo[key] = int(value.split()[0])

    temperature = None
    thermal = Path("/sys/class/thermal/thermal_zone0/temp")
    if thermal.exists():
        temperature = round(
            int(thermal.read_text(encoding="utf-8").strip()) / 1000.0, 2
        )

    processes = command(
        "ps",
        "-C",
        "pipewire,wireplumber,bridge_supervisor.py,pw-cli",
        "-o",
        "pid=,comm=,%cpu=,rss=",
    )
    result = {
        "timestamp": time.time(),
        "load_average": list(os.getloadavg()),
        "cpu_percent": utilization,
        "memory_kib": meminfo,
        "temperature_c": temperature,
        "throttled": command("vcgencmd", "get_throttled"),
        "arm_clock": command("vcgencmd", "measure_clock", "arm"),
        "processes": processes.splitlines() if processes else [],
    }
    json.dump(result, fp=os.sys.stdout, indent=2)
    os.sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
