#!/usr/bin/env python3
"""Low-overhead active AEC profiler for the Raspberry Pi 3."""

from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

TICKS = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
PI3_ARM_CLOCK_HZ = 1_200_000_000


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 3)


def cpu_snapshot() -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        values = [int(value) for value in fields[1:]]
        result[fields[0]] = (sum(values), sum(values[3:5]))
    return result


def process_snapshot(pid: int) -> tuple[int, int] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = stat[stat.rfind(")") + 2 :].split()
        ticks = int(tail[11]) + int(tail[12])
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        rss = next(
            int(line.split()[1])
            for line in status.splitlines()
            if line.startswith("VmRSS:")
        )
        return ticks, rss
    except (OSError, StopIteration, ValueError):
        return None


def relevant_processes(aec_pid: int) -> dict[str, int]:
    result = {"aec-owner": aec_pid}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            cmdline = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
        except OSError:
            continue
        if comm == "pipewire":
            result[f"pipewire-{pid}"] = pid
        elif comm == "wireplumber":
            result[f"wireplumber-{pid}"] = pid
        elif "bridge_supervisor.py" in cmdline:
            result[f"supervisor-{pid}"] = pid
    return result


def mem_available_kib() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def temperature_c() -> float | None:
    try:
        return round(
            int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
            / 1000,
            2,
        )
    except (OSError, ValueError):
        return None


def arm_clock_hz() -> int | None:
    paths = [
        Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"),
        Path("/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq"),
    ]
    for path in paths:
        try:
            return int(path.read_text(encoding="utf-8").strip()) * 1000
        except (OSError, ValueError):
            continue
    return None


def command(*args: str) -> str | None:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def parse_pw_top(path: Path, startup_samples: int = 3) -> dict[str, Any]:
    nodes: dict[str, dict[str, list[float] | list[int]]] = {}
    if not path.exists():
        return {"nodes": {}, "error_delta_total": None}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[0] != "R":
            continue
        try:
            wait_ratio = float(fields[6])
            busy_ratio = float(fields[7])
            errors = int(fields[8])
        except ValueError:
            continue
        name = fields[-1]
        item = nodes.setdefault(
            name, {"wait_ratio": [], "busy_ratio": [], "errors": []}
        )
        item["wait_ratio"].append(wait_ratio)  # type: ignore[union-attr]
        item["busy_ratio"].append(busy_ratio)  # type: ignore[union-attr]
        item["errors"].append(errors)  # type: ignore[union-attr]

    summary: dict[str, Any] = {}
    total_delta = 0
    startup_delta_total = 0
    steady_delta_total = 0
    for name, values in nodes.items():
        waits = list(values["wait_ratio"])
        busy = list(values["busy_ratio"])
        errors = list(values["errors"])
        delta = max(errors) - min(errors) if errors else 0
        split = min(startup_samples, len(errors))
        startup_errors = errors[:split]
        steady_errors = errors[max(split - 1, 0) :]
        startup_delta = (
            max(startup_errors) - min(startup_errors) if startup_errors else 0
        )
        steady_delta = max(steady_errors) - min(steady_errors) if steady_errors else 0
        total_delta += max(delta, 0)
        startup_delta_total += max(startup_delta, 0)
        steady_delta_total += max(steady_delta, 0)
        summary[name] = {
            "samples": len(busy),
            "wait_ratio_p99": percentile(waits, 0.99),
            "busy_ratio_p99": percentile(busy, 0.99),
            "busy_ratio_max": round(max(busy), 3) if busy else None,
            "errors_first": errors[0] if errors else None,
            "errors_last": errors[-1] if errors else None,
            "error_delta": delta,
            "startup_error_delta": startup_delta,
            "steady_error_delta": steady_delta,
        }
    return {
        "nodes": summary,
        "error_delta_total": total_delta,
        "startup_error_delta_total": startup_delta_total,
        "steady_error_delta_total": steady_delta_total,
        "startup_samples": startup_samples,
    }


def longest_core_over(samples: list[dict[str, Any]], threshold: float) -> float:
    longest = 0
    streaks: dict[str, int] = {}
    for sample in samples:
        current = sample.get("cpu_percent") or {}
        cores = {name for name in current if name.startswith("cpu") and name != "cpu"}
        for core in set(streaks) | cores:
            streaks[core] = (
                streaks.get(core, 0) + 1
                if current.get(core, 0) > threshold
                else 0
            )
            longest = max(longest, streaks[core])
    return float(longest)


def gate_failures(summary: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    pw_top = summary.get("pw_top") or {}
    errors = pw_top.get("steady_error_delta_total", pw_top.get("error_delta_total"))
    if isinstance(errors, int) and errors > 0:
        failures.append(f"PipeWire steady-state ERR counters increased by {errors}")
    for name, node in (pw_top.get("nodes") or {}).items():
        busy = node.get("busy_ratio_p99")
        if isinstance(busy, (int, float)) and busy > 0.70:
            failures.append(f"{name} B/Q p99 {busy:.2f} exceeds 0.70")
    total_cpu = summary.get("total_cpu_percent_mean")
    if isinstance(total_cpu, (int, float)) and total_cpu >= 75:
        failures.append(f"average total CPU {total_cpu:.2f}% is not below 75%")
    pinned = summary.get("per_core_over_90_longest_s")
    if isinstance(pinned, (int, float)) and pinned > 5:
        failures.append(f"a CPU core stayed above 90% for {pinned:.1f}s")
    rss_delta = summary.get("aec_rss_kib_delta")
    if isinstance(rss_delta, int) and rss_delta >= 150 * 1024:
        failures.append(f"AEC RSS grew by {rss_delta / 1024:.1f} MiB")
    memory = summary.get("mem_available_kib_min")
    if isinstance(memory, int) and memory < 250 * 1024:
        failures.append(f"available memory fell to {memory / 1024:.1f} MiB")
    temperature = summary.get("temperature_c_max")
    if isinstance(temperature, (int, float)) and temperature >= 75:
        failures.append(f"temperature reached {temperature:.2f} C")
    clock = summary.get("arm_clock_hz_min")
    if isinstance(clock, int) and clock < PI3_ARM_CLOCK_HZ:
        failures.append(f"ARM clock fell to {clock} Hz")
    throttled = summary.get("throttled_samples") or []
    if any(value not in {None, "throttled=0x0"} for value in throttled):
        failures.append("throttle or undervoltage flags were observed")
    return failures


class ActiveProfiler:
    def __init__(
        self, out_dir: Path, aec_pid: int, iterations: int, interval: float = 1.0
    ):
        self.out_dir = out_dir
        self.aec_pid = aec_pid
        self.iterations = iterations
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self.processes = relevant_processes(aec_pid)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.pw_top: subprocess.Popen[str] | None = None
        self.pw_top_handle = None
        self.throttled_start: str | None = None
        self.throttled_end: str | None = None

    def start(self) -> None:
        self.throttled_start = command("vcgencmd", "get_throttled")
        self.pw_top_handle = (self.out_dir / "pw-top.txt").open("w", encoding="utf-8")
        self.pw_top = subprocess.Popen(
            ["pw-top", "-b", "-n", str(self.iterations)],
            stdout=self.pw_top_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.thread = threading.Thread(target=self._sample, daemon=True)
        self.thread.start()

    def _sample(self) -> None:
        previous_cpu = cpu_snapshot()
        previous_process = {
            name: value
            for name, pid in self.processes.items()
            if (value := process_snapshot(pid)) is not None
        }
        previous_time = time.monotonic()
        while not self.stop_event.wait(self.interval):
            now = time.monotonic()
            current_cpu = cpu_snapshot()
            cpu_percent: dict[str, float] = {}
            for name, (total, idle) in current_cpu.items():
                old_total, old_idle = previous_cpu.get(name, (total, idle))
                elapsed = total - old_total
                busy = elapsed - (idle - old_idle)
                cpu_percent[name] = (
                    round(100 * busy / elapsed, 2) if elapsed > 0 else 0.0
                )

            process_values: dict[str, dict[str, float | int]] = {}
            elapsed_time = max(now - previous_time, 0.001)
            current_process: dict[str, tuple[int, int]] = {}
            for name, pid in self.processes.items():
                value = process_snapshot(pid)
                if value is None:
                    continue
                current_process[name] = value
                old = previous_process.get(name, value)
                process_values[name] = {
                    "pid": pid,
                    "cpu_percent_one_core": round(
                        100 * (value[0] - old[0]) / TICKS / elapsed_time, 2
                    ),
                    "rss_kib": value[1],
                }
            self.samples.append(
                {
                    "monotonic_s": round(now, 3),
                    "cpu_percent": cpu_percent,
                    "processes": process_values,
                    "mem_available_kib": mem_available_kib(),
                    "temperature_c": temperature_c(),
                    "arm_clock_hz": arm_clock_hz(),
                    "throttled": command("vcgencmd", "get_throttled"),
                }
            )
            previous_cpu = current_cpu
            previous_process = current_process
            previous_time = now

    def stop(self) -> dict[str, Any]:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.interval + 2)
        if self.pw_top is not None:
            try:
                self.pw_top.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.pw_top.terminate()
                self.pw_top.wait(timeout=3)
        if self.pw_top_handle is not None:
            self.pw_top_handle.close()
        self.throttled_end = command("vcgencmd", "get_throttled")

        aec_cpu = [
            float(sample["processes"]["aec-owner"]["cpu_percent_one_core"])
            for sample in self.samples
            if "aec-owner" in sample["processes"]
        ]
        aec_rss = [
            int(sample["processes"]["aec-owner"]["rss_kib"])
            for sample in self.samples
            if "aec-owner" in sample["processes"]
        ]
        total_cpu = [
            float(sample["cpu_percent"].get("cpu", 0)) for sample in self.samples
        ]
        temperatures = [
            float(sample["temperature_c"])
            for sample in self.samples
            if sample["temperature_c"] is not None
        ]
        memories = [
            int(sample["mem_available_kib"])
            for sample in self.samples
            if sample["mem_available_kib"] is not None
        ]
        clocks = [
            int(sample["arm_clock_hz"])
            for sample in self.samples
            if sample["arm_clock_hz"] is not None
        ]
        summary = {
            "samples": len(self.samples),
            "aec_cpu_percent_one_core_median": (
                round(statistics.median(aec_cpu), 3) if aec_cpu else None
            ),
            "aec_cpu_percent_one_core_p95": percentile(aec_cpu, 0.95),
            "aec_rss_kib_max": max(aec_rss) if aec_rss else None,
            "aec_rss_kib_delta": max(aec_rss) - min(aec_rss) if aec_rss else None,
            "total_cpu_percent_median": (
                round(statistics.median(total_cpu), 3) if total_cpu else None
            ),
            "total_cpu_percent_p95": percentile(total_cpu, 0.95),
            "total_cpu_percent_mean": (
                round(statistics.fmean(total_cpu), 3) if total_cpu else None
            ),
            "per_core_over_90_longest_s": round(
                longest_core_over(self.samples, 90) * self.interval, 3
            ),
            "temperature_c_max": max(temperatures) if temperatures else None,
            "mem_available_kib_min": min(memories) if memories else None,
            "arm_clock_hz_min": min(clocks) if clocks else None,
            "throttled_start": self.throttled_start,
            "throttled_end": self.throttled_end,
            "throttled_samples": [
                sample.get("throttled") for sample in self.samples
            ],
            "pw_top": parse_pw_top(self.out_dir / "pw-top.txt"),
        }
        summary["gate_failures"] = gate_failures(summary)
        (self.out_dir / "runtime-samples.json").write_text(
            json.dumps(self.samples, indent=2) + "\n", encoding="utf-8"
        )
        (self.out_dir / "runtime-summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        return summary
