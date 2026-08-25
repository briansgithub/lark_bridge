#!/usr/bin/env python3
"""Pi-side evidence collector and detached soak sampler for BT500+AUX qualification."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import tomllib

try:
    from .harness import (
        BT500_ADDRESS,
        EvidenceError,
        HardFailure,
        HardwareNotReady,
        atomic_json,
        new_error_lines,
        service_restarts,
        validate_snapshot,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bt500_aux.harness import (  # type: ignore[no-redef]
        BT500_ADDRESS,
        EvidenceError,
        HardFailure,
        HardwareNotReady,
        atomic_json,
        new_error_lines,
        service_restarts,
        validate_snapshot,
    )

REPO = Path(__file__).resolve().parents[2]
RUNTIME_UID = os.getuid() if hasattr(os, "getuid") else 1000
STATUS_PATH = Path(f"/run/user/{RUNTIME_UID}/bridge-status.json")
WATCHDOG_PATH = Path("/run/larkbridge/bt-watchdog/call.json")
CONFIG_PATH = REPO / "config" / "bridge.toml"
CONTROLLER_TOOL = REPO / "pi" / "bridged" / "controller_roles.py"
KERNEL_PATTERNS = (
    re.compile(r"frame reassembly failed", re.IGNORECASE),
    re.compile(r"bluetooth:.*(?:tx timeout|command timeout|timed out)", re.IGNORECASE),
    re.compile(
        r"hci\w*:.*(?:tx timeout|command timeout|unexpected event)", re.IGNORECASE
    ),
)
USB_PATTERNS = (
    re.compile(r"usb .*reset (?:full|high|super)-speed usb device", re.IGNORECASE),
    re.compile(r"device descriptor read.*error", re.IGNORECASE),
    re.compile(r"urb.*(?:error|failed)", re.IGNORECASE),
    re.compile(r"over-current|under-voltage", re.IGNORECASE),
)
SYSTEM_SERVICES = (
    "bluetooth.service",
    "bridge-btwatchdog@call.service",
    "bridge-storage-guard.service",
    "bridge-tuning.service",
)
USER_SERVICES = (
    "bridge-supervisor.service",
    "pipewire.service",
    "wireplumber.service",
)


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(self, command: Sequence[str], *, timeout: float = 15.0) -> Result: ...


class SystemRunner:
    def run(self, command: Sequence[str], *, timeout: float = 15.0) -> Result:
        try:
            result = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result(127, "", f"{type(exc).__name__}: {exc}")
        return Result(result.returncode, result.stdout, result.stderr)


def parse_json_text(raw: str, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        errors.append(f"{label}: malformed JSON: {exc}")
        return {}
    if not isinstance(document, dict):
        errors.append(f"{label}: expected a JSON object")
        return {}
    return document


def read_json_file(path: Path, label: str, errors: list[str]) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{label}: {exc}")
        return {}
    return parse_json_text(raw, label, errors)


def run_text(
    runner: Runner,
    command: Sequence[str],
    label: str,
    errors: list[str],
    *,
    timeout: float = 15.0,
    required: bool = True,
) -> str:
    result = runner.run(command, timeout=timeout)
    if result.returncode != 0:
        if required:
            errors.append(
                f"{label}: exit {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout
    return result.stdout


def service_info(
    runner: Runner, unit: str, *, user: bool, errors: list[str]
) -> dict[str, str]:
    command = ["systemctl"]
    if user:
        command.append("--user")
    command.extend(
        [
            "show",
            unit,
            "--no-pager",
            "--property=ActiveState",
            "--property=NRestarts",
            "--property=ExecMainStatus",
            "--property=ExecMainStartTimestampMonotonic",
        ]
    )
    raw = run_text(runner, command, f"service {unit}", errors)
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    if not {"ActiveState", "NRestarts"}.issubset(fields):
        errors.append(f"service {unit}: incomplete systemd properties")
    return fields


def graph_quantum(raw: str) -> int:
    best = 0
    for line in raw.splitlines():
        columns = line.split()
        if len(columns) < 10 or not columns[0].startswith(("R", "I")):
            continue
        try:
            best = max(best, int(columns[2]))
        except ValueError:
            continue
    return best


def property_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) >= {"data"}:
        return property_value(value["data"])
    if isinstance(value, list):
        return [property_value(item) for item in value]
    return value


def bluez_summary(raw: str, errors: list[str]) -> dict[str, Any]:
    try:
        tree = json.loads(raw)
    except json.JSONDecodeError:
        # busctl tree is intentionally retained as raw evidence. It is not a semantic
        # source, so its text shape is not interpreted here.
        return {"tree": raw}
    if not isinstance(tree, dict):
        errors.append("BlueZ managed-object evidence is not an object")
        return {}
    objects: list[dict[str, Any]] = []
    for path, interfaces in tree.items():
        if not isinstance(interfaces, dict):
            continue
        for interface in ("org.bluez.Adapter1", "org.bluez.Device1"):
            properties = interfaces.get(interface)
            if not isinstance(properties, dict):
                continue
            objects.append(
                {
                    "path": path,
                    "interface": interface,
                    "properties": {
                        key: property_value(value)
                        for key, value in properties.items()
                        if key
                        in {
                            "Address",
                            "Adapter",
                            "Alias",
                            "Bonded",
                            "Connected",
                            "Name",
                            "Paired",
                            "Powered",
                            "Trusted",
                            "UUIDs",
                        }
                    },
                }
            )
    return {"objects": objects}


def health() -> dict[str, Any]:
    report: dict[str, Any] = {}
    try:
        report["temperature_c"] = round(
            int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
            / 1000,
            2,
        )
    except (OSError, ValueError):
        report["temperature_c"] = None
    try:
        report["mem_available_kib"] = next(
            int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
    except (OSError, ValueError, StopIteration):
        report["mem_available_kib"] = None
    return report


def controller_transport(
    runner: Runner,
    controllers: Mapping[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    call = controllers.get("call")
    if not isinstance(call, dict):
        return {
            "hci": None,
            "controller_answers": False,
            "acl": False,
            "sco": False,
        }
    hci = call.get("hci")
    if not isinstance(hci, str) or not re.fullmatch(r"hci\d+", hci):
        return {
            "hci": hci,
            "controller_answers": False,
            "acl": False,
            "sco": False,
        }
    probe = runner.run(["hciconfig", hci, "version"], timeout=20)
    connections = runner.run(["hcitool", "-i", hci, "con"], timeout=10)
    counters = runner.run(["hciconfig", hci], timeout=10)
    if connections.returncode != 0:
        errors.append(f"hcitool {hci} con failed: {connections.stderr.strip()}")
    sco_values = [int(value) for value in re.findall(r"\bsco:(\d+)", counters.stdout)]
    return {
        "hci": hci,
        "address": call.get("observed_address"),
        "controller_answers": (probe.returncode == 0 and "HCI Version" in probe.stdout),
        "acl": "ACL" in connections.stdout,
        "sco": "SCO" in connections.stdout,
        "sco_rx": sco_values[0] if len(sco_values) > 0 else None,
        "sco_tx": sco_values[1] if len(sco_values) > 1 else None,
        "connections": connections.stdout,
    }


def matched_lines(raw: str, patterns: Sequence[re.Pattern[str]]) -> list[str]:
    return [
        line
        for line in raw.splitlines()
        if any(pattern.search(line) for pattern in patterns)
    ]


def snapshot(
    runner: Runner | None = None,
    *,
    full: bool = False,
    status_path: Path = STATUS_PATH,
    watchdog_path: Path = WATCHDOG_PATH,
) -> dict[str, Any]:
    command_runner = runner or SystemRunner()
    errors: list[str] = []
    status = read_json_file(status_path, "bridge status", errors)

    controller_result = command_runner.run(
        [
            sys.executable,
            str(CONTROLLER_TOOL),
            "--config",
            str(CONFIG_PATH),
            "--policy",
            "final-usb",
            "status",
        ],
        timeout=20,
    )
    controllers = parse_json_text(controller_result.stdout, "controller status", errors)
    if controller_result.returncode not in (0, 1):
        errors.append(
            "controller status: "
            f"exit {controller_result.returncode}: {controller_result.stderr.strip()}"
        )

    managed = run_text(
        command_runner,
        [
            sys.executable,
            "-c",
            (
                "import json,sys;"
                f"sys.path.insert(0,{str(REPO / 'pi' / 'bridged').__repr__()});"
                "import btadapters;"
                "print(json.dumps(btadapters.managed_objects()))"
            ),
        ],
        "BlueZ managed objects",
        errors,
        timeout=20,
    )
    bluez = bluez_summary(managed, errors)

    links = run_text(command_runner, ["pw-link", "-l"], "PipeWire links", errors)
    pwtop = run_text(
        command_runner,
        ["timeout", "8", "pw-top", "-b", "-n", "2"],
        "PipeWire quantum",
        errors,
        timeout=10,
    )
    services = {
        "system": {
            unit: service_info(command_runner, unit, user=False, errors=errors)
            for unit in SYSTEM_SERVICES
        },
        "user": {
            unit: service_info(command_runner, unit, user=True, errors=errors)
            for unit in USER_SERVICES
        },
    }

    kernel = run_text(
        command_runner,
        ["journalctl", "-k", "--no-pager", "-n", "1000", "-o", "short-unix"],
        "kernel journal",
        errors,
        timeout=20,
    )
    lsusb = run_text(command_runner, ["lsusb"], "USB inventory", errors)
    lsusb_tree = run_text(command_runner, ["lsusb", "-t"], "USB topology", errors)
    watchdog_errors: list[str] = []
    watchdog = read_json_file(watchdog_path, "watchdog state", watchdog_errors)
    if watchdog_errors:
        watchdog = {"recoveries": 0, "missing": True, "errors": watchdog_errors}

    report: dict[str, Any] = {
        "timestamp": time.time(),
        "status": status,
        "controllers": controllers,
        "bluez": bluez,
        "pipewire_links": links,
        "graph_quantum": graph_quantum(pwtop),
        "services": services,
        "watchdog": watchdog,
        "transport": controller_transport(command_runner, controllers, errors),
        "health": health(),
        "kernel_errors": matched_lines(kernel, KERNEL_PATTERNS),
        "usb_errors": matched_lines(kernel, USB_PATTERNS),
        "usb": {"devices": lsusb, "topology": lsusb_tree},
        "collection_errors": errors,
    }
    if full:
        pw_dump = run_text(
            command_runner,
            ["pw-dump"],
            "PipeWire dump",
            errors,
            timeout=30,
        )
        journal_system = run_text(
            command_runner,
            [
                "journalctl",
                "--no-pager",
                "-n",
                "800",
                "-u",
                "bluetooth.service",
                "-u",
                "bridge-btwatchdog@call.service",
                "-u",
                "bridge-storage-guard.service",
            ],
            "system service journal",
            errors,
            timeout=30,
        )
        journal_user = run_text(
            command_runner,
            [
                "sudo",
                "-n",
                "journalctl",
                "--no-pager",
                "_UID=1000",
                "-n",
                "800",
            ],
            "user service journal",
            errors,
            timeout=30,
        )
        report["pipewire_dump"] = pw_dump
        report["journals"] = {
            "system": journal_system,
            "user": journal_user,
            "kernel": kernel,
        }
        # Full-only commands can append errors after the initial object was assigned.
        report["collection_errors"] = errors
    return report


def _status_state(path: Path) -> str | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document.get("state") if isinstance(document, dict) else None


def _wait_for_state(
    wanted: str,
    *,
    status_path: Path,
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    while clock() < deadline:
        if _status_state(status_path) == wanted:
            return True
        sleep(0.25)
    return _status_state(status_path) == wanted


def recycle_call(
    runner: Runner | None = None,
    *,
    timeout: float = 75.0,
    status_path: Path = STATUS_PATH,
    config_path: Path = CONFIG_PATH,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Create one fresh BT500 ACL/SCO session without guessing an HCI index."""
    command_runner = runner or SystemRunner()
    try:
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
        phone = config["devices"]["phone"]["address"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise EvidenceError(f"cannot read configured phone identity: {exc}") from exc
    if (
        not isinstance(phone, str)
        or re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", phone) is None
    ):
        raise EvidenceError("configured phone address is absent or noncanonical")

    def resolved_call() -> dict[str, Any]:
        result = command_runner.run(
            [
                sys.executable,
                str(CONTROLLER_TOOL),
                "--config",
                str(config_path),
                "--policy",
                "final-usb",
                "status",
            ],
            timeout=20,
        )
        errors: list[str] = []
        document = parse_json_text(result.stdout, "controller status", errors)
        if result.returncode != 0 or errors:
            raise HardwareNotReady(
                f"call controller is not ready: {errors or result.stderr.strip()}"
            )
        call = document.get("call")
        if not isinstance(call, dict) or call.get("ready") is not True:
            raise HardwareNotReady("configured BT500 call controller is not ready")
        if call.get("observed_address") != BT500_ADDRESS:
            raise HardFailure("call recycle resolved a controller other than BT500")
        hci = call.get("hci")
        if not isinstance(hci, str) or re.fullmatch(r"hci\d+", hci) is None:
            raise EvidenceError("controller status omitted a valid runtime HCI name")
        return call

    opening = resolved_call()
    device_suffix = phone.replace(":", "_")
    opening_path = f"/org/bluez/{opening['hci']}/dev_{device_suffix}"
    disconnected = command_runner.run(
        [
            "busctl",
            "--system",
            "call",
            "org.bluez",
            opening_path,
            "org.bluez.Device1",
            "Disconnect",
        ],
        timeout=20,
    )
    if disconnected.returncode != 0:
        raise HardwareNotReady(
            f"could not disconnect Pixel on BT500: {disconnected.stderr.strip()}"
        )
    started = clock()
    down_deadline = started + min(20.0, timeout / 3)
    call_down = _wait_for_state(
        "CALL_DOWN",
        status_path=status_path,
        deadline=down_deadline,
        clock=clock,
        sleep=sleep,
    )
    if not call_down:
        raise EvidenceError("supervisor never observed call teardown")

    current = resolved_call()
    current_path = f"/org/bluez/{current['hci']}/dev_{device_suffix}"
    connected = command_runner.run(
        [
            "busctl",
            "--system",
            "call",
            "org.bluez",
            current_path,
            "org.bluez.Device1",
            "Connect",
        ],
        timeout=30,
    )
    active = _wait_for_state(
        "ACTIVE",
        status_path=status_path,
        deadline=started + timeout,
        clock=clock,
        sleep=sleep,
    )
    if not active:
        command_error = (
            f"; Connect returned {connected.returncode}: {connected.stderr.strip()}"
            if connected.returncode != 0
            else ""
        )
        raise HardwareNotReady(
            "Pixel did not restore BT500 call audio after reconnect" + command_error
        )
    return {
        "verdict": "PASS",
        "adapter_address": current.get("observed_address"),
        "hci_before": opening.get("hci"),
        "hci_after": current.get("hci"),
        "phone_address": phone,
        "call_down_observed": call_down,
        "active_observed": active,
        "connect_returncode": connected.returncode,
        "elapsed_s": round(clock() - started, 3),
    }


def sample_hard_failures(
    opening: Mapping[str, Any], current: Mapping[str, Any]
) -> list[str]:
    failures: list[str] = []
    try:
        validate_snapshot(current, require_active=True)
        for scope, units in (
            ("system", SYSTEM_SERVICES),
            ("user", USER_SERVICES),
        ):
            for unit in units:
                if service_restarts(current, scope, unit) != service_restarts(
                    opening, scope, unit
                ):
                    failures.append(f"service restarted: {scope}:{unit}")
        before_watchdog = opening.get("watchdog")
        after_watchdog = current.get("watchdog")
        if not isinstance(before_watchdog, dict) or not isinstance(
            after_watchdog, dict
        ):
            failures.append("watchdog state malformed")
        elif after_watchdog.get("recoveries") != before_watchdog.get("recoveries"):
            failures.append("controller watchdog recovery occurred")
        if new_error_lines(opening, current, "kernel_errors"):
            failures.append("new Bluetooth kernel error")
        if new_error_lines(opening, current, "usb_errors"):
            failures.append("new USB/kernel error")
    except (EvidenceError, HardFailure, HardwareNotReady) as exc:
        failures.append(str(exc))
    health_block = current.get("health")
    if not isinstance(health_block, dict):
        failures.append("health evidence malformed")
    else:
        temperature = health_block.get("temperature_c")
        if isinstance(temperature, (int, float)) and float(temperature) >= 80.0:
            failures.append(f"temperature reached {temperature} C")
    status = current.get("status")
    if isinstance(status, dict):
        throttled = (status.get("system") or {}).get("throttled")
        if throttled not in (None, "throttled=0x0", "0x0"):
            failures.append(f"firmware throttling reported: {throttled}")
    return failures


def append_jsonl(path: Path, document: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(document, sort_keys=True, separators=(",", ":")))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def run_soak(
    output: Path,
    *,
    duration: int,
    interval: float,
    resume: bool = False,
    snapshot_fn: Callable[..., dict[str, Any]] = snapshot,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "state.json"
    samples_path = output / "samples.jsonl"
    elapsed_before = 0.0
    runs = 0
    opening: dict[str, Any] | None = None
    if resume:
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"cannot resume malformed soak state: {exc}") from exc
        if not isinstance(previous, dict):
            raise EvidenceError("cannot resume non-object soak state")
        if previous.get("status") == "passed":
            return 0
        if previous.get("status") == "failed":
            raise HardFailure("a failed soak cannot be resumed")
        if (
            previous.get("duration_s") != duration
            or previous.get("interval_s") != interval
        ):
            raise EvidenceError("resume parameters do not match the original soak")
        elapsed_before = float(previous.get("elapsed_s", 0.0))
        runs = int(previous.get("runs", 0))
        opening_value = previous.get("opening")
        if isinstance(opening_value, dict):
            opening = opening_value
    else:
        samples_path.write_text("", encoding="utf-8")

    started = clock()
    if opening is None:
        opening = snapshot_fn(full=False)
        validate_snapshot(opening, require_active=True)
    state: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "duration_s": duration,
        "interval_s": interval,
        "elapsed_s": elapsed_before,
        "runs": runs + 1,
        "opening": opening,
        "started_utc": time.time(),
    }
    atomic_json(state_path, state)

    try:
        while elapsed_before + (clock() - started) < duration:
            sample_started = clock()
            current = snapshot_fn(full=False)
            current["soak_elapsed_s"] = round(
                elapsed_before + (sample_started - started), 3
            )
            failures = sample_hard_failures(opening, current)
            append_jsonl(samples_path, current)
            state["elapsed_s"] = min(duration, elapsed_before + (clock() - started))
            state["samples"] = int(state.get("samples", 0)) + 1
            atomic_json(state_path, state)
            if failures:
                event = {
                    "timestamp": time.time(),
                    "event": "hard_failure",
                    "failures": failures,
                }
                append_jsonl(samples_path, event)
                state.update(
                    status="failed",
                    reason="; ".join(failures),
                    completed_utc=time.time(),
                )
                atomic_json(state_path, state)
                return 1
            remaining = duration - (elapsed_before + (clock() - started))
            if remaining <= 0:
                break
            collection_time = clock() - sample_started
            sleep(max(0.0, min(interval - collection_time, remaining)))
    except KeyboardInterrupt:
        state.update(
            status="interrupted",
            elapsed_s=min(duration, elapsed_before + (clock() - started)),
            completed_utc=time.time(),
        )
        append_jsonl(
            samples_path,
            {"timestamp": time.time(), "event": "interrupted"},
        )
        atomic_json(state_path, state)
        return 130

    state.update(
        status="passed",
        elapsed_s=float(duration),
        completed_utc=time.time(),
    )
    append_jsonl(
        samples_path,
        {"timestamp": time.time(), "event": "finished", "elapsed_s": duration},
    )
    atomic_json(state_path, state)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot")
    snap.add_argument("--full", action="store_true")
    soak = commands.add_parser("soak")
    soak.add_argument("--out", type=Path, required=True)
    soak.add_argument("--duration", type=int, default=3600)
    soak.add_argument("--interval", type=float, default=5.0)
    soak.add_argument("--resume", action="store_true")
    state = commands.add_parser("state")
    state.add_argument("--out", type=Path, required=True)
    recycle = commands.add_parser("recycle")
    recycle.add_argument("--timeout", type=float, default=75.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            document = snapshot(full=bool(args.full))
            print(json.dumps(document, sort_keys=True))
            return 0 if not document["collection_errors"] else 1
        if args.command == "state":
            try:
                document = json.loads(
                    (args.out / "state.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                print(json.dumps({"status": "waiting", "reason": str(exc)}))
                return 78
            if not isinstance(document, dict):
                print(
                    json.dumps({"status": "failed", "reason": "state is not an object"})
                )
                return 1
            print(json.dumps(document, sort_keys=True))
            return 0
        if args.command == "recycle":
            document = recycle_call(timeout=args.timeout)
            print(json.dumps(document, sort_keys=True))
            return 0
        return run_soak(
            args.out,
            duration=args.duration,
            interval=args.interval,
            resume=bool(args.resume),
        )
    except HardwareNotReady as exc:
        print(json.dumps({"status": "waiting", "reason": str(exc)}))
        return 78
    except (EvidenceError, HardFailure) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
