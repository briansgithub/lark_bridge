#!/usr/bin/env python3
"""Executable, resumable, evidence-first E17 qualification harness.

Safe by default: without ``--execute`` this records the exact scenario graph and
commands but never touches the Pi, Pixel, PC audio, pairing, or service state.  A
short soak is an abbreviated Phase-4 check only; 3,600 seconds is mandatory Phase 7.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = REPO / "docs" / "experiments" / "results" / "E17"
SCHEMA = 2
SPEAKER_WAKE_EXIT = 78
BTMON_TIMEOUT_EXIT = 124
MIN_MARGIN_DB = 10.0
MAX_CLIPPING_PCT = 0.01
MIN_AEC_DB = 10.0
MAX_RECOVERY_SECONDS = 3.0
FULL_SOAK_SECONDS = 3600
ABBREVIATED_SOAK_MIN = 300
ABBREVIATED_SOAK_MAX = 600
TRANSPORT_RE = re.compile(
    r"hci command timeout|usb reset|urb error|frame reassembly failed|controller reset",
    re.IGNORECASE,
)
MUTATION_RE = re.compile(r"\b(Pair|ConnectProfile|Connect|Trusted|Bonded)\b")
PROTECTED_ANDROID_MARKERS = ("confirm your passkey", "pair with", "allow access to contacts")


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class ManagedProcess(Protocol):
    def terminate(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


class Runner(Protocol):
    def run(self, command: list[str], *, timeout: float = 45.0) -> CommandResult: ...
    def start(self, command: list[str]) -> ManagedProcess: ...


class SubprocessRunner:
    def run(self, command: list[str], *, timeout: float = 45.0) -> CommandResult:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
        return CommandResult(command, result.returncode, result.stdout, result.stderr)

    def start(self, command: list[str]) -> ManagedProcess:
        flags = 0x08000000 if os.name == "nt" else 0
        return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)


@dataclass(frozen=True)
class FaultWindow:
    target_role: str
    started_utc: str
    ended_utc: str
    allowed_events: tuple[str, ...] = ("teardown", "re-enumeration")


class RedGate(RuntimeError):
    """A measured transport or objective gate is red; the graph stops immediately."""


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_object(text: str, label: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RedGate(f"{label} returned malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RedGate(f"{label} did not return a JSON object")
    return data


def parse_events(text: str) -> list[dict[str, Any]]:
    """Parse JSON-lines evidence, retaining recognized plain transport evidence too."""
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if TRANSPORT_RE.search(line):
                events.append({"detail": line, "kind": "transport"})
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fault_event_allowed(event: dict[str, Any], window: FaultWindow) -> bool:
    try:
        timestamp = _time(str(event["timestamp_utc"]))
        return (
            event.get("role") == window.target_role
            and event.get("kind") in window.allowed_events
            and _time(window.started_utc) <= timestamp <= _time(window.ended_utc)
        )
    except (KeyError, ValueError):
        return False


def transport_failures(events: Iterable[dict[str, Any]], window: FaultWindow | None = None) -> list[str]:
    failures: list[str] = []
    for event in events:
        detail = str(event.get("detail", ""))
        if TRANSPORT_RE.search(detail) and not (window and fault_event_allowed(event, window)):
            failures.append(detail)
    return failures


def mutation_failures(events: Iterable[dict[str, Any]], selected_at_utc: str) -> list[str]:
    selected = _time(selected_at_utc)
    failures: list[str] = []
    for event in events:
        operation = str(event.get("operation", ""))
        if not MUTATION_RE.fullmatch(operation):
            continue
        try:
            before = _time(str(event["timestamp_utc"])) < selected
        except (KeyError, ValueError):
            failures.append("candidate mutation lacks a valid timestamp")
            continue
        if before:
            failures.append(f"candidate {operation} occurred before Pixel selection")
    return failures


def protected_android_ui(xml: str) -> bool:
    return any(marker in xml.casefold() for marker in PROTECTED_ANDROID_MARKERS)


def objective_failures(preflight: dict[str, Any], aec: dict[str, Any], recovery_seconds: float | None) -> list[str]:
    failures: list[str] = []
    if preflight.get("verdict") != "ready":
        return [f"speaker preflight is {preflight.get('verdict')!r}"]
    for key in ("level_margin_db", "tone_margin_db"):
        value = preflight.get(key)
        if not isinstance(value, (int, float)) or value < MIN_MARGIN_DB:
            failures.append(f"{key} is below {MIN_MARGIN_DB:.0f} dB")
    active = preflight.get("active")
    if not isinstance(active, dict):
        failures.append("speaker preflight has no active capture")
    else:
        if float(active.get("peak_dbfs", 1.0)) > -0.1:
            failures.append("speaker-to-Lark peak is unsafe")
        if float(active.get("clipped_pct", 100.0)) > MAX_CLIPPING_PCT:
            failures.append("speaker-to-Lark clipping exceeds 0.01%")
    metrics = aec.get("metrics")
    if not isinstance(metrics, dict):
        failures.append("AEC result has no metrics object")
    else:
        suppression = metrics.get("suppression_db")
        if not isinstance(suppression, (int, float)) or suppression < MIN_AEC_DB:
            failures.append(f"AEC suppression is below {MIN_AEC_DB:.0f} dB")
    if recovery_seconds is None:
        failures.append("output recovery is unmeasured")
    elif recovery_seconds > MAX_RECOVERY_SECONDS:
        failures.append(f"output recovery {recovery_seconds:.3f}s exceeds 3s")
    return failures


def soak_kind(seconds: int) -> str:
    if ABBREVIATED_SOAK_MIN <= seconds <= ABBREVIATED_SOAK_MAX:
        return "abbreviated-not-endurance-qualified"
    if seconds == FULL_SOAK_SECONDS:
        return "full-phase7-required"
    raise ValueError("soak must be 300–600 seconds or exactly 3,600 seconds")


def scenario_names(soak_seconds: int) -> list[str]:
    soak_kind(soak_seconds)
    return ["baseline", "selection-proof", "objective-speaker-recovery", *[f"cycle-{number:02d}" for number in range(1, 11)], "soak"]


class E17Harness:
    def __init__(self, directory: Path, runner: Runner, *, host: str, adb: str, execute: bool, stereo_mix: str, switch_target: str = "") -> None:
        self.directory = directory
        self.runner = runner
        self.host = host
        self.adb = adb
        self.execute = execute
        self.stereo_mix = stereo_mix
        self.switch_target = switch_target
        self.state_path = directory / "state.json"
        self.commands_path = directory / "commands.jsonl"
        self.processes: list[ManagedProcess] = []
        self.state = self._load_state()

    @classmethod
    def create(cls, output: Path, runner: Runner, *, host: str = "larkbridge", adb: str = "adb", execute: bool = False, stereo_mix: str = "", switch_target: str = "") -> E17Harness:
        output.mkdir(parents=True, exist_ok=False)
        return cls(output, runner, host=host, adb=adb, execute=execute, stereo_mix=stereo_mix, switch_target=switch_target)

    @classmethod
    def resume(cls, directory: Path, runner: Runner, *, host: str = "larkbridge", adb: str = "adb", execute: bool = False, stereo_mix: str = "", switch_target: str = "") -> E17Harness:
        if not (directory / "state.json").is_file():
            raise RedGate(f"cannot resume {directory}: state.json is absent")
        return cls(directory, runner, host=host, adb=adb, execute=execute, stereo_mix=stereo_mix, switch_target=switch_target)

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            state = parse_object(self.state_path.read_text(encoding="utf-8"), "state")
            if state.get("schema") != SCHEMA:
                raise RedGate("unsupported E17 state schema")
            for item in state.get("scenarios", {}).values():
                if item.get("status") == "running":
                    item["status"] = "pending"
                    item["interrupted"] = True
            self._save(state)
            return state
        state = {
            "schema": SCHEMA,
            "created_utc": datetime.now(UTC).isoformat(),
            "mode": "execute" if self.execute else "plan-only",
            "scenarios": {},
            "first_red": None,
            "human_action": None,
            "fault_injection_allowlist": {
                "phase": 7,
                "only": "target teardown/re-enumeration inside exact timestamped window",
                "off_target_or_outside_window": "FAIL",
            },
        }
        self._save(state)
        return state

    def _save(self, state: dict[str, Any] | None = None) -> None:
        self.state_path.write_text(json.dumps(self.state if state is None else state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _mark(self, scenario: str, status: str, **detail: Any) -> None:
        self.state["scenarios"][scenario] = {"status": status, "updated_utc": datetime.now(UTC).isoformat(), **detail}
        self._save()

    def _command(self, name: str, command: list[str], *, timeout: float = 45.0, allowed: tuple[int, ...] = (0,)) -> CommandResult:
        result = CommandResult(command, 0) if not self.execute else self.runner.run(command, timeout=timeout)
        with self.commands_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"name": name, "executed": self.execute, **asdict(result)}, sort_keys=True) + "\n")
        if result.returncode not in allowed:
            raise RedGate(f"{name} failed with exit {result.returncode}")
        return result

    def _ssh(self, name: str, script: str, *, timeout: float = 45.0, allowed: tuple[int, ...] = (0,)) -> CommandResult:
        return self._command(name, ["ssh", self.host, script], timeout=timeout, allowed=allowed)

    def _adb(self, name: str, *args: str) -> CommandResult:
        return self._command(name, [self.adb, *args])

    def discover_stereo_mix(self) -> list[str]:
        command = ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_SoundDevice | Where-Object Name -match 'Stereo Mix' | Select-Object -ExpandProperty Name | ConvertTo-Json -Compress"]
        result = self._command("stereo-mix-discovery", command)
        if not self.execute:
            return [self.stereo_mix] if self.stereo_mix else []
        raw = json.loads(result.stdout or "[]")
        choices = [raw] if isinstance(raw, str) else raw
        if not isinstance(choices, list) or not all(isinstance(item, str) for item in choices):
            raise RedGate("Stereo Mix discovery returned malformed device data")
        if self.stereo_mix and self.stereo_mix not in choices:
            raise RedGate("configured Stereo Mix device is not present")
        if not self.stereo_mix:
            if len(choices) != 1:
                raise RedGate("Stereo Mix selection is missing or ambiguous")
            self.stereo_mix = choices[0]
        return choices

    def _start(self, name: str, command: list[str]) -> None:
        if self.execute:
            self.processes.append(self.runner.start(command))
        with self.commands_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"name": name, "background": True, "executed": self.execute, "command": command}) + "\n")

    def start_keepalive(self, stimulus: Path) -> None:
        quoted = str(stimulus).replace("'", "'\\''")
        script = "while :; do pw-play --target \"$(jq -r '.output.chosen.node' /run/user/1000/bridge-status.json)\" '" + quoted + "'; sleep 20; done"
        self._start("speaker-keepalive", ["ssh", self.host, script])

    def start_light_monitor(self, seconds: int) -> None:
        script = f"deadline=$(($(date +%s) + {seconds})); while [ $(date +%s) -lt $deadline ]; do date --iso-8601=ns; cd /home/admin/rpi-lark-bridge && python3 rig/pi/measure/invariants.py --json; sleep 5; done"
        self._start("lightweight-monitor", ["ssh", self.host, script])

    def start_stereo_mix_capture(self, seconds: int) -> None:
        if not self.stereo_mix:
            raise RedGate("Stereo Mix must be selected before PC audio capture")
        self._start("stereo-mix-capture", ["ffmpeg", "-y", "-f", "dshow", "-i", f"audio={self.stereo_mix}", "-t", str(seconds), str(self.directory / "stereo-mix.wav")])

    def drive_stereo_mix(self, stimulus: Path) -> None:
        self._command("stereo-mix-playback", ["ffplay", "-nodisp", "-autoexit", str(stimulus)], timeout=90)

    def prepare_stimulus(self, seconds: int) -> Path:
        stimulus = self.directory / "stimulus.wav"
        self._command(
            "stereo-mix-stimulus",
            [sys.executable, str(REPO / "tools" / "audio" / "tone_gen.py"), "--mode", "speech", "--seconds", str(seconds + 30), "--rate", "48000", "--channels", "1", "--dbfs", "-18", "--out", str(stimulus)],
            timeout=30,
        )
        return stimulus

    def cleanup(self) -> None:
        for process in reversed(self.processes):
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    continue
        self.processes.clear()

    def _events(self, result: CommandResult, window: FaultWindow | None = None) -> list[dict[str, Any]]:
        events = parse_events(result.stdout + "\n" + result.stderr)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        failures = transport_failures(events, window)
        if failures:
            raise RedGate("red transport gate: " + "; ".join(failures))
        return events

    def _preflight(self) -> dict[str, Any] | None:
        result = self._ssh("speaker-preflight", "cd /home/admin/rpi-lark-bridge && python3 rig/pi/measure/speaker_preflight.py --out /tmp/e17-preflight", allowed=(0, SPEAKER_WAKE_EXIT))
        if result.returncode == SPEAKER_WAKE_EXIT:
            return None
        return parse_object(result.stdout, "speaker preflight") if self.execute else {"verdict": "ready", "level_margin_db": 10.0, "tone_margin_db": 10.0, "active": {"peak_dbfs": -1.0, "clipped_pct": 0.0}}

    def _speaker_ready(self, scenario: str) -> dict[str, Any] | None:
        preflight = self._preflight()
        if preflight is None:
            self._mark(scenario, "paused", reason="speaker_wake_required")
            return None
        failures = objective_failures(preflight, {"metrics": {"suppression_db": MIN_AEC_DB}}, 0.0)
        if failures:
            raise RedGate("red speaker preflight gate: " + "; ".join(failures))
        return preflight

    def _aec(self) -> dict[str, Any]:
        result = self._ssh("aec-bench", "cd /home/admin/rpi-lark-bridge && python3 rig/pi/measure/aec_bench.py --out /tmp/e17-aec", timeout=120)
        return parse_object(result.stdout, "AEC bench") if self.execute else {"metrics": {"suppression_db": 10.0}}

    def _recovery_seconds(self) -> float | None:
        if self.execute and not self.switch_target:
            raise RedGate("--switch-target is required for a live recovery measurement")
        result = self._ssh(
            "output-switch-probe",
            "cd /home/admin/rpi-lark-bridge && python3 rig/pi/measure/output_switch_probe.py "
            f"--to {self.switch_target or 'planned-target'} --external-switch --max-uplink-gap 3 --max-downlink-gap 3 --max-apply-seconds 3",
            timeout=60,
        )
        if not self.execute:
            return 0.0
        report = parse_object(result.stdout, "output switch probe")
        if report.get("verdict") != "PASS":
            raise RedGate("output switch probe failed")
        samples = (
            (report.get("selection_apply_seconds"), "selection apply"),
            ((report.get("uplink_gap") or {}).get("seconds"), "uplink gap"),
            ((report.get("downlink_gap") or {}).get("seconds"), "downlink gap"),
        )
        values: list[float] = []
        for value, label in samples:
            if not isinstance(value, (int, float)):
                raise RedGate(f"output switch probe did not measure {label}")
            values.append(float(value))
        return max(values)

    def _targeted_btmon(self, scenario: str) -> list[dict[str, Any]]:
        result = self._ssh(f"btmon-{scenario}", "sudo -n timeout 20s btmon -T -c never", timeout=30, allowed=(0, BTMON_TIMEOUT_EXIT))
        return self._events(result)

    def _pixel_selection_attempt(self, scenario: str) -> bool:
        self._adb("adb-bluetooth-intent", "shell", "am", "start", "-a", "android.settings.BLUETOOTH_SETTINGS")
        hierarchy = self._adb("adb-selection-ui", "exec-out", "uiautomator", "dump", "/dev/tty").stdout
        if self.execute and protected_android_ui(hierarchy):
            self.state["human_action"] = {"state": "pixel_protected_confirmation", "instruction": "Tap the one Android Bluetooth confirmation, then resume this result directory.", "created_utc": datetime.now(UTC).isoformat()}
            self._mark(scenario, "paused", reason="protected_android_ui")
            return False
        return True

    def _run_one(self, scenario: str, soak_seconds: int) -> None:
        if scenario == "baseline":
            roles = self._ssh("controller-roles", "cd /home/admin/rpi-lark-bridge && python3 pi/bridged/controller_roles.py --policy final-usb status")
            if self.execute and not parse_object(roles.stdout, "controller roles").get("ready"):
                raise RedGate("red transport gate: configured controller roles are not ready")
            self.discover_stereo_mix()
            self._ssh("pi-evidence", "cd /home/admin/rpi-lark-bridge; cat /run/user/1000/bridge-status.json; cat /run/larkbridge/storage-health.json; busctl --system tree org.bluez; XDG_RUNTIME_DIR=/run/user/1000 pw-dump; rfcomm -a || true; systemctl --no-pager --full status bluetooth.service bridge-btwatchdog@call.service bridge-btwatchdog@output.service; journalctl -k -n 120 -o short-precise", timeout=60)
            self._adb("adb-audio", "shell", "dumpsys", "audio")
            self._adb("adb-route", "shell", "dumpsys", "bluetooth_manager")
            self._adb("adb-ui-hierarchy", "exec-out", "uiautomator", "dump", "/dev/tty")
            return
        if scenario == "selection-proof":
            if not self._pixel_selection_attempt(scenario):
                return
            events = self._targeted_btmon(scenario)
            selected = datetime.now(UTC).isoformat()
            failures = mutation_failures(events, selected)
            if failures:
                raise RedGate("red selection gate: " + "; ".join(failures))
            return
        if scenario == "objective-speaker-recovery":
            preflight = self._speaker_ready(scenario)
            if preflight is None:
                return
            aec = self._aec()
            recovery = self._recovery_seconds()
            failures = objective_failures(preflight, aec, recovery)
            if failures:
                raise RedGate("red objective gate: " + "; ".join(failures))
            return
        if scenario.startswith("cycle-"):
            if self._speaker_ready(scenario) is None:
                return
            self._targeted_btmon(scenario)
            return
        if scenario == "soak":
            kind = soak_kind(soak_seconds)
            if self._speaker_ready(scenario) is None:
                return
            stimulus = self.prepare_stimulus(soak_seconds)
            self.start_light_monitor(soak_seconds)
            self.start_keepalive(stimulus)
            self.start_stereo_mix_capture(soak_seconds)
            self.drive_stereo_mix(stimulus)
            self._mark(scenario, "completed", soak_seconds=soak_seconds, classification=kind, endurance_qualified=False)
            return
        raise AssertionError(scenario)

    def run_graph(self, soak_seconds: int) -> dict[str, Any]:
        names = scenario_names(soak_seconds)
        try:
            for scenario in names:
                previous = self.state["scenarios"].get(scenario, {})
                if previous.get("status") == "completed":
                    continue
                self._mark(scenario, "running")
                self._run_one(scenario, soak_seconds)
                if self.state["scenarios"].get(scenario, {}).get("status") == "paused":
                    break
                if self.state["scenarios"].get(scenario, {}).get("status") != "completed":
                    self._mark(scenario, "completed")
        except RedGate as exc:
            current = next((name for name in names if self.state["scenarios"].get(name, {}).get("status") == "running"), "unknown")
            self.state["first_red"] = {"scenario": current, "error": str(exc), "at_utc": datetime.now(UTC).isoformat()}
            self._mark(current, "failed", error=str(exc))
            raise
        finally:
            self.cleanup()
        return self.state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--host", default="larkbridge")
    parser.add_argument("--adb", default=shutil.which("adb") or "adb")
    parser.add_argument("--stereo-mix", default=os.environ.get("E17_STEREO_MIX", ""))
    parser.add_argument("--switch-target", default=os.environ.get("E17_SWITCH_TARGET", ""))
    parser.add_argument("--soak-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    if bool(args.output) == bool(args.resume):
        parser.error("pass exactly one of --output or --resume")
    runner = SubprocessRunner()
    harness = E17Harness.resume(args.resume, runner, host=args.host, adb=args.adb, execute=args.execute, stereo_mix=args.stereo_mix, switch_target=args.switch_target) if args.resume else E17Harness.create(args.output, runner, host=args.host, adb=args.adb, execute=args.execute, stereo_mix=args.stereo_mix, switch_target=args.switch_target)
    try:
        print(json.dumps(harness.run_graph(args.soak_seconds), indent=2))
        return 0
    except RedGate as exc:
        print(json.dumps({"directory": str(harness.directory), "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
