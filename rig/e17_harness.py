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
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable
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
KERNEL_ERROR_ERE = (
    r"hci.*(command|cmd).*timeout|command tx timeout|usb.*reset|"
    r"urb.*(error|failed)|frame reassembly failed|controller.*reset|"
    r"fiq reported nyet|"
    r"transfer to device [0-9]+ endpoint 0x[0-9a-f]+ frame [0-9]+ failed"
)
TRANSPORT_RE = re.compile(KERNEL_ERROR_ERE, re.IGNORECASE)
MUTATION_RE = re.compile(r"\b(Pair|ConnectProfile|Connect|Trusted|Bonded)\b")
PROTECTED_ANDROID_MARKERS = (
    "confirm your passkey",
    "pair with",
    "allow access to contacts",
)


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
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout
        )
        return CommandResult(command, result.returncode, result.stdout, result.stderr)

    def start(self, command: list[str]) -> ManagedProcess:
        flags = 0x08000000 if os.name == "nt" else 0
        return subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )


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
    return datetime.fromisoformat(value)


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


def transport_failures(
    events: Iterable[dict[str, Any]], window: FaultWindow | None = None
) -> list[str]:
    failures: list[str] = []
    for event in events:
        detail = str(event.get("detail", ""))
        if TRANSPORT_RE.search(detail) and not (
            window and fault_event_allowed(event, window)
        ):
            failures.append(detail)
    return failures


def mutation_failures(
    events: Iterable[dict[str, Any]], selected_at_utc: str
) -> list[str]:
    try:
        selected = _time(selected_at_utc)
    except ValueError:
        return ["Pixel selection lacks a valid timestamp"]
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


def selection_evidence_failures(
    events: list[dict[str, Any]], selected_at_utc: str
) -> list[str]:
    failures: list[str] = []
    starts = [event for event in events if event.get("kind") == "monitor-start"]
    ends = [event for event in events if event.get("kind") == "monitor-end"]
    selections = [event for event in events if event.get("kind") == "pixel-selection"]
    if len(starts) != 1 or len(ends) != 1 or len(selections) != 1:
        return [
            "selection evidence requires one monitor-start, pixel-selection, and monitor-end"
        ]
    try:
        started = _time(str(starts[0]["timestamp_utc"]))
        selected = _time(str(selections[0]["timestamp_utc"]))
        ended = _time(str(ends[0]["timestamp_utc"]))
        declared = _time(selected_at_utc)
    except (KeyError, ValueError):
        return ["selection evidence contains an invalid timestamp"]
    if not started <= selected <= ended:
        failures.append("Pixel selection is outside the monitored evidence window")
    if selected != declared:
        failures.append("declared Pixel selection timestamp mismatches event evidence")
    failures.extend(mutation_failures(events, selected_at_utc))
    return failures


def protected_android_ui(xml: str) -> bool:
    return any(marker in xml.casefold() for marker in PROTECTED_ANDROID_MARKERS)


def event_file(path: Path) -> list[dict[str, Any]]:
    return parse_events(path.read_text(encoding="utf-8"))


def dshow_audio_sources(text: str) -> list[str]:
    sources: list[str] = []
    for line in text.splitlines():
        if "(audio)" not in line.casefold():
            continue
        match = re.search(r'"([^"]+)"', line)
        if match and match.group(1) not in sources:
            sources.append(match.group(1))
    return sources


def objective_failures(
    preflight: dict[str, Any], aec: dict[str, Any], recovery_seconds: float | None
) -> list[str]:
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
        if aec.get("verdict") != "PASS" or metrics.get("verdict") != "PASS":
            failures.append("AEC metric tool did not pass")
        suppression = metrics.get("suppression_db")
        if not isinstance(suppression, (int, float)) or suppression < MIN_AEC_DB:
            failures.append(f"AEC suppression is below {MIN_AEC_DB:.0f} dB")
    if recovery_seconds is None:
        failures.append("output recovery is unmeasured")
    elif recovery_seconds > MAX_RECOVERY_SECONDS:
        failures.append(f"output recovery {recovery_seconds:.3f}s exceeds 3s")
    return failures


def pc_capture_failures(report: dict[str, Any], seconds: int) -> list[str]:
    failures: list[str] = []
    duration = report.get("duration_s")
    if not isinstance(duration, (int, float)) or duration < seconds - 1:
        failures.append("Stereo Mix capture duration is incomplete")
    channels = report.get("per_channel")
    if not isinstance(channels, list) or not channels:
        return [*failures, "Stereo Mix capture has no audio channels"]
    peak = max(float(channel.get("peak_dbfs", -200.0)) for channel in channels)
    clipping = max(float(channel.get("clipped_pct", 100.0)) for channel in channels)
    if peak <= -80.0:
        failures.append("Stereo Mix capture is silent or below the measurable floor")
    if clipping > MAX_CLIPPING_PCT:
        failures.append("Stereo Mix capture exceeds 0.01% clipping")
    return failures


def soak_kind(seconds: int) -> str:
    if ABBREVIATED_SOAK_MIN <= seconds <= ABBREVIATED_SOAK_MAX:
        return "abbreviated-not-endurance-qualified"
    if seconds == FULL_SOAK_SECONDS:
        return "full-phase7-required"
    raise ValueError("soak must be 300–600 seconds or exactly 3,600 seconds")


def scenario_names(soak_seconds: int) -> list[str]:
    soak_kind(soak_seconds)
    return [
        "baseline",
        "selection-proof",
        "objective-speaker-recovery",
        *[f"cycle-{number:02d}" for number in range(1, 11)],
        "soak",
    ]


class E17Harness:
    def __init__(
        self,
        directory: Path,
        runner: Runner,
        *,
        host: str,
        adb: str,
        execute: bool,
        stereo_mix: str,
        switch_target: str = "",
        candidate_events: Path | None = None,
        selected_at_utc: str = "",
        fault_events: Path | None = None,
        fault_window: FaultWindow | None = None,
        call_uri: str = "",
        call_package: str = "com.discord",
    ) -> None:
        self.directory = directory
        self.runner = runner
        self.host = host
        self.adb = adb
        self.execute = execute
        self.stereo_mix = stereo_mix
        self.switch_target = switch_target
        self.candidate_events = candidate_events
        self.selected_at_utc = selected_at_utc
        self.fault_events = fault_events
        self.fault_window = fault_window
        self.call_uri = call_uri
        self.call_package = call_package
        self.state_path = directory / "state.json"
        self.commands_path = directory / "commands.jsonl"
        self.processes: list[ManagedProcess] = []
        self.state = self._load_state()

    @classmethod
    def create(cls, output: Path, runner: Runner, **options: Any) -> E17Harness:
        output.mkdir(parents=True, exist_ok=False)
        return cls(
            output,
            runner,
            **{
                "host": "larkbridge",
                "adb": "adb",
                "execute": False,
                "stereo_mix": "",
                **options,
            },
        )

    @classmethod
    def resume(cls, directory: Path, runner: Runner, **options: Any) -> E17Harness:
        if not (directory / "state.json").is_file():
            raise RedGate(f"cannot resume {directory}: state.json is absent")
        return cls(
            directory,
            runner,
            **{
                "host": "larkbridge",
                "adb": "adb",
                "execute": False,
                "stereo_mix": "",
                **options,
            },
        )

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            state = parse_object(self.state_path.read_text(encoding="utf-8"), "state")
            if state.get("schema") != SCHEMA:
                raise RedGate("unsupported E17 state schema")
            mode = "execute" if self.execute else "plan-only"
            if state.get("mode") != mode:
                raise RedGate("resume mode does not match the existing E17 run")
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
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.state if state is None else state, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def _mark(self, scenario: str, status: str, **detail: Any) -> None:
        self.state["scenarios"][scenario] = {
            "status": status,
            "updated_utc": datetime.now(UTC).isoformat(),
            **detail,
        }
        self._save()

    def _command(
        self,
        name: str,
        command: list[str],
        *,
        timeout: float = 45.0,
        allowed: tuple[int, ...] = (0,),
    ) -> CommandResult:
        result = (
            CommandResult(command, 0)
            if not self.execute
            else self.runner.run(command, timeout=timeout)
        )
        with self.commands_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"name": name, "executed": self.execute, **asdict(result)},
                    sort_keys=True,
                )
                + "\n"
            )
        if result.returncode not in allowed:
            raise RedGate(f"{name} failed with exit {result.returncode}")
        return result

    def _ssh(
        self,
        name: str,
        script: str,
        *,
        timeout: float = 45.0,
        allowed: tuple[int, ...] = (0,),
    ) -> CommandResult:
        return self._command(
            name, ["ssh", self.host, script], timeout=timeout, allowed=allowed
        )

    def _adb(self, name: str, *args: str) -> CommandResult:
        return self._command(name, [self.adb, *args])

    def discover_stereo_mix(self) -> list[str]:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-list_devices",
            "true",
            "-f",
            "dshow",
            "-i",
            "dummy",
        ]
        result = self._command("stereo-mix-discovery", command, allowed=(0, 1))
        if not self.execute:
            self.stereo_mix = self.stereo_mix or "Stereo Mix"
            return [self.stereo_mix]
        choices = dshow_audio_sources(result.stdout + "\n" + result.stderr)
        matches = (
            [
                choice
                for choice in choices
                if choice.casefold() == self.stereo_mix.casefold()
            ]
            if self.stereo_mix
            else [choice for choice in choices if "stereo mix" in choice.casefold()]
        )
        if len(matches) != 1:
            raise RedGate(
                "Stereo Mix source is absent or ambiguous; select one exact device name"
            )
        self.stereo_mix = matches[0]
        return choices

    def _start(self, name: str, command: list[str]) -> ManagedProcess | None:
        process = self.runner.start(command) if self.execute else None
        if process is not None:
            self.processes.append(process)
        with self.commands_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "name": name,
                        "background": True,
                        "executed": self.execute,
                        "command": command,
                    }
                )
                + "\n"
            )
        return process

    def start_keepalive(self) -> ManagedProcess | None:
        script = (
            "cd /home/admin/rpi-lark-bridge && "
            "python3 tools/audio/tone_gen.py --mode pips --seconds 2 --dbfs -42 "
            "--out /tmp/e17-keepalive.wav && while :; do "
            "node=$(jq -er '.output.chosen.node' "
            "/run/user/1000/bridge-status.json) || exit 1; "
            'pw-play --target "$node" /tmp/e17-keepalive.wav || exit 1; '
            "sleep 20; done"
        )
        return self._start("speaker-keepalive", ["ssh", self.host, script])

    def light_monitor(self, seconds: int) -> None:
        script = (
            f"deadline=$(($(date +%s) + {seconds})); "
            "while [ $(date +%s) -lt $deadline ]; do "
            "sample=$(jq -c '{state,last_failure,controllers,graph}' "
            "/run/user/1000/bridge-status.json) || exit 92; "
            "printf 'E17_SAMPLE %s\\n' \"$sample\"; "
            "printf '%s' \"$sample\" | jq -e '"
            '.controllers.ready == true and .state != "DEGRADED" and '
            "(.last_failure == null) and "
            "((.graph.missing_links // []) | length == 0) and "
            "((.graph.unexpected_links // []) | length == 0)' >/dev/null || exit 93; "
            "kernel=$(journalctl -k --since '6 seconds ago' -o short-precise "
            "--no-pager); printf '%s\\n' \"$kernel\"; "
            f"printf '%s' \"$kernel\" | grep -Eiq '{KERNEL_ERROR_ERE}' && exit 94; "
            "sleep 5; done"
        )
        result = self._ssh(
            "lightweight-monitor",
            script,
            timeout=seconds + 30,
        )
        self._events(result)
        if self.execute and "E17_SAMPLE " not in result.stdout:
            raise RedGate("lightweight monitor produced no structured state samples")

    def start_stereo_mix_capture(
        self, scenario: str, seconds: int
    ) -> tuple[ManagedProcess | None, Path]:
        if not self.stereo_mix:
            raise RedGate("Stereo Mix must be selected before PC audio capture")
        capture = self.directory / f"{scenario}-stereo-mix.wav"
        process = self._start(
            f"stereo-mix-capture-{scenario}",
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-f",
                "dshow",
                "-i",
                f"audio={self.stereo_mix}",
                "-t",
                str(seconds),
                "-ac",
                "1",
                "-ar",
                "48000",
                str(capture),
            ],
        )
        return process, capture

    def start_stereo_mix_drive(
        self, scenario: str, stimulus: Path, seconds: int
    ) -> ManagedProcess | None:
        return self._start(
            f"stereo-mix-playback-{scenario}",
            [
                "ffplay",
                "-nodisp",
                "-loglevel",
                "warning",
                "-loop",
                "0",
                "-t",
                str(seconds),
                str(stimulus),
            ],
        )

    def prepare_stimulus(self, seconds: int) -> Path:
        stimulus = self.directory / "stimulus.wav"
        self._command(
            "stereo-mix-stimulus",
            [
                sys.executable,
                str(REPO / "tools" / "audio" / "tone_gen.py"),
                "--mode",
                "speech",
                "--seconds",
                str(min(seconds, 30)),
                "--rate",
                "48000",
                "--channels",
                "1",
                "--dbfs",
                "-18",
                "--out",
                str(stimulus),
            ],
            timeout=30,
        )
        return stimulus

    def _finish_process(
        self,
        process: ManagedProcess | None,
        name: str,
        *,
        terminate: bool,
        timeout: float = 10,
    ) -> None:
        if process is None:
            return
        try:
            if terminate:
                process.terminate()
            code = process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            code = process.wait(timeout=5)
        if process in self.processes:
            self.processes.remove(process)
        if not terminate and code != 0:
            raise RedGate(f"{name} failed with exit {code}")

    def audio_session(self, scenario: str, seconds: int) -> dict[str, Any]:
        stimulus = self.prepare_stimulus(seconds)
        keepalive = self.start_keepalive()
        capture, capture_path = self.start_stereo_mix_capture(scenario, seconds)
        drive = self.start_stereo_mix_drive(scenario, stimulus, seconds)
        completed = False
        try:
            self.light_monitor(seconds)
            self._finish_process(capture, "Stereo Mix capture", terminate=False)
            completed = True
        finally:
            self._finish_process(drive, "Stereo Mix playback", terminate=True)
            self._finish_process(keepalive, "speaker keepalive", terminate=True)
            if not completed:
                self._finish_process(capture, "Stereo Mix capture", terminate=True)
        result = self._command(
            f"stereo-mix-analysis-{scenario}",
            [
                sys.executable,
                str(REPO / "rig" / "analysis" / "wav_level.py"),
                str(capture_path),
                "--json",
            ],
            timeout=60,
        )
        report = (
            parse_object(result.stdout, "Stereo Mix analysis")
            if self.execute
            else {
                "duration_s": seconds,
                "per_channel": [{"peak_dbfs": -12.0, "clipped_pct": 0.0}],
            }
        )
        failures = pc_capture_failures(report, seconds)
        if failures:
            raise RedGate("red PC audio gate: " + "; ".join(failures))
        return report

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

    def _events(
        self, result: CommandResult, window: FaultWindow | None = None
    ) -> list[dict[str, Any]]:
        events = parse_events(result.stdout + "\n" + result.stderr)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        failures = transport_failures(events, window)
        if failures:
            raise RedGate("red transport gate: " + "; ".join(failures))
        return events

    def _preflight(self) -> dict[str, Any] | None:
        result = self._ssh(
            "speaker-preflight",
            "cd /home/admin/rpi-lark-bridge && python3 rig/pi/measure/speaker_preflight.py --out /tmp/e17-preflight",
            allowed=(0, SPEAKER_WAKE_EXIT),
        )
        if result.returncode == SPEAKER_WAKE_EXIT:
            return None
        return (
            parse_object(result.stdout, "speaker preflight")
            if self.execute
            else {
                "verdict": "ready",
                "level_margin_db": 10.0,
                "tone_margin_db": 10.0,
                "active": {"peak_dbfs": -1.0, "clipped_pct": 0.0},
            }
        )

    def _speaker_ready(self, scenario: str) -> dict[str, Any] | None:
        preflight = self._preflight()
        if preflight is None:
            self._mark(scenario, "paused", reason="speaker_wake_required")
            return None
        failures = objective_failures(
            preflight,
            {
                "verdict": "PASS",
                "metrics": {"verdict": "PASS", "suppression_db": MIN_AEC_DB},
            },
            0.0,
        )
        if failures:
            raise RedGate("red speaker preflight gate: " + "; ".join(failures))
        return preflight

    def _aec(self) -> dict[str, Any]:
        result = self._ssh(
            "aec-bench",
            "cd /home/admin/rpi-lark-bridge && "
            "python3 rig/pi/measure/aec_bench.py --out /tmp/e17-aec",
            timeout=120,
            allowed=(0, 1),
        )
        return (
            parse_object(result.stdout, "AEC bench")
            if self.execute
            else {
                "verdict": "PASS",
                "metrics": {"verdict": "PASS", "suppression_db": 10.0},
            }
        )

    def _recovery_seconds(self) -> float | None:
        if self.execute and not self.switch_target:
            raise RedGate("--switch-target is required for a live recovery measurement")
        target = shlex.quote(self.switch_target or "planned-target")
        self._ssh(
            "select-wired-before-probe",
            "cd /home/admin/rpi-lark-bridge && "
            "python3 pi/bridged/bridgectl.py output set wired "
            "--no-chime --no-connect",
        )
        monitor, remote_log = self._start_btmon_operation(
            "output-switch-probe", role="output"
        )
        try:
            result = self._ssh(
                "output-switch-probe",
                "cd /home/admin/rpi-lark-bridge && "
                "python3 rig/pi/measure/output_switch_probe.py "
                f"--to {target} --max-uplink-gap 3 --max-downlink-gap 3 "
                "--max-apply-seconds 3",
                timeout=60,
                allowed=(0, 1, SPEAKER_WAKE_EXIT),
            )
        finally:
            self._finish_process(monitor, "targeted btmon", terminate=False, timeout=35)
        capture = self._ssh(
            "btmon-output-switch-probe",
            f"cat {shlex.quote(remote_log)}",
        )
        self._events(capture)
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

    def _start_btmon_operation(
        self, scenario: str, *, role: str
    ) -> tuple[ManagedProcess | None, str]:
        if role not in {"call", "output"}:
            raise ValueError("btmon role must be call or output")
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", scenario)
        remote_log = f"/tmp/e17-{safe}-btmon.log"
        script = (
            "cd /home/admin/rpi-lark-bridge; "
            "hci=$(python3 pi/bridged/controller_roles.py --policy final-usb status "
            f"| jq -er '.{role}.hci') || exit 1; "
            f'sudo -n timeout 25s btmon -i "$hci" -T -c never >'
            f"{shlex.quote(remote_log)} 2>&1; rc=$?; "
            f"[ $rc -eq 0 ] || [ $rc -eq {BTMON_TIMEOUT_EXIT} ]"
        )
        return (
            self._start(f"btmon-operation-{scenario}", ["ssh", self.host, script]),
            remote_log,
        )

    def _targeted_btmon(
        self,
        scenario: str,
        *,
        role: str = "output",
        window: FaultWindow | None = None,
    ) -> list[dict[str, Any]]:
        if role not in {"call", "output"}:
            raise ValueError("btmon role must be call or output")
        result = self._ssh(
            f"btmon-{scenario}",
            "cd /home/admin/rpi-lark-bridge; "
            "hci=$(python3 pi/bridged/controller_roles.py --policy final-usb status "
            f"| jq -er '.{role}.hci'); "
            'sudo -n timeout 20s btmon -i "$hci" -T -c never',
            timeout=30,
            allowed=(0, BTMON_TIMEOUT_EXIT),
        )
        return self._events(result, window)

    def establish_call(self, scenario: str) -> None:
        if self.execute and not self.call_uri:
            raise RedGate("--call-uri is required for executable call scenarios")
        self._adb(
            f"adb-call-start-{scenario}",
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            self.call_uri or "planned-call-uri",
        )
        self._ssh(
            f"wait-call-active-{scenario}",
            "deadline=$(($(date +%s) + 60)); "
            'until jq -e \'.state == "ACTIVE" and '
            ".controllers.call.ready == true and "
            ".endpoints.hfp_source != null and .endpoints.hfp_sink != null' "
            "/run/user/1000/bridge-status.json >/dev/null; do "
            "[ $(date +%s) -lt $deadline ] || exit 78; sleep 1; done",
            timeout=70,
        )

    def teardown_call(self, scenario: str) -> None:
        self._adb(
            f"adb-call-stop-{scenario}",
            "shell",
            "am",
            "force-stop",
            self.call_package,
        )
        self._ssh(
            f"wait-call-down-{scenario}",
            "deadline=$(($(date +%s) + 30)); "
            "until jq -e '.state != \"ACTIVE\"' "
            "/run/user/1000/bridge-status.json >/dev/null; do "
            "[ $(date +%s) -lt $deadline ] || exit 78; sleep 1; done",
            timeout=40,
        )

    def during_call(self, scenario: str, action: Callable[[], Any]) -> Any:
        """Always tear down, while preserving the first measured red failure."""
        self.establish_call(scenario)
        try:
            result = action()
        except RedGate:
            try:
                self.teardown_call(scenario)
            except RedGate:
                pass
            raise
        self.teardown_call(scenario)
        return result

    def _pixel_selection_attempt(self, scenario: str) -> bool:
        self._adb(
            "adb-bluetooth-intent",
            "shell",
            "am",
            "start",
            "-a",
            "android.settings.BLUETOOTH_SETTINGS",
        )
        hierarchy = self._adb(
            "adb-selection-ui", "exec-out", "uiautomator", "dump", "/dev/tty"
        ).stdout
        if self.execute and protected_android_ui(hierarchy):
            self.state["human_action"] = {
                "state": "pixel_protected_confirmation",
                "instruction": "Tap the one Android Bluetooth confirmation, then resume this result directory.",
                "created_utc": datetime.now(UTC).isoformat(),
            }
            self._mark(scenario, "paused", reason="protected_android_ui")
            return False
        return True

    def _run_one(self, scenario: str, soak_seconds: int) -> None:
        if scenario == "baseline":
            roles = self._ssh(
                "controller-roles",
                "cd /home/admin/rpi-lark-bridge && python3 pi/bridged/controller_roles.py --policy final-usb status",
            )
            if self.execute and not parse_object(roles.stdout, "controller roles").get(
                "ready"
            ):
                raise RedGate(
                    "red transport gate: configured controller roles are not ready"
                )
            self.discover_stereo_mix()
            evidence = self._ssh(
                "pi-evidence",
                "cd /home/admin/rpi-lark-bridge; date --iso-8601=ns; uname -a; "
                "lsusb; lsusb -t; "
                "python3 pi/bridged/controller_roles.py --policy final-usb status; "
                "cat /run/user/1000/bridge-status.json; "
                "cat /run/larkbridge/storage-health.json; "
                "python3 pi/powerloss/lark_state.py status; "
                "sha256sum config/bridge.toml; busctl --system tree org.bluez; "
                "ss --bluetooth --all --processes; "
                "XDG_RUNTIME_DIR=/run/user/1000 pw-dump; "
                "systemctl --no-pager --full status bluetooth.service "
                "bridge-btwatchdog@call.service "
                "bridge-btwatchdog@output.service; "
                "journalctl -k -n 120 -o short-precise",
                timeout=60,
            )
            if self.fault_events is not None:
                failures = transport_failures(
                    event_file(self.fault_events), self.fault_window
                )
                if failures:
                    raise RedGate("red fault-window gate: " + "; ".join(failures))
            self._events(evidence)
            self._adb("adb-audio", "shell", "dumpsys", "audio")
            self._adb("adb-route", "shell", "dumpsys", "bluetooth_manager")
            self._adb("adb-ui-hierarchy", "exec-out", "uiautomator", "dump", "/dev/tty")
            return
        if scenario == "selection-proof":
            if not self._pixel_selection_attempt(scenario):
                return
            self._targeted_btmon(scenario, role="output")
            if self.execute and (
                self.candidate_events is None or not self.selected_at_utc
            ):
                raise RedGate(
                    "candidate event evidence and Pixel selection timestamp are required"
                )
            if not self.execute and self.candidate_events is None:
                return
            events = (
                event_file(self.candidate_events)
                if self.candidate_events is not None
                else []
            )
            selected = self.selected_at_utc or datetime.now(UTC).isoformat()
            failures = selection_evidence_failures(events, selected)
            if failures:
                raise RedGate("red selection gate: " + "; ".join(failures))
            return
        if scenario == "objective-speaker-recovery":
            preflight = self._speaker_ready(scenario)
            if preflight is None:
                return
            aec = self._aec()
            recovery = self.during_call(scenario, self._recovery_seconds)
            failures = objective_failures(preflight, aec, recovery)
            if failures:
                raise RedGate("red objective gate: " + "; ".join(failures))
            return
        if scenario.startswith("cycle-"):
            preflight = self._speaker_ready(scenario)
            if preflight is None:
                return
            aec = self._aec()

            def cycle_action() -> tuple[float | None, dict[str, Any]]:
                recovery = self._recovery_seconds()
                return recovery, self.audio_session(scenario, 30)

            recovery, _audio = self.during_call(scenario, cycle_action)
            failures = objective_failures(preflight, aec, recovery)
            if failures:
                raise RedGate("red cycle objective gate: " + "; ".join(failures))
            return
        if scenario == "soak":
            kind = soak_kind(soak_seconds)
            preflight = self._speaker_ready(scenario)
            if preflight is None:
                return
            aec = self._aec()

            def soak_action() -> tuple[float | None, dict[str, Any]]:
                recovery = self._recovery_seconds()
                return recovery, self.audio_session(scenario, soak_seconds)

            recovery, audio = self.during_call(scenario, soak_action)
            failures = objective_failures(preflight, aec, recovery)
            if failures:
                raise RedGate("red soak objective gate: " + "; ".join(failures))
            self._mark(
                scenario,
                "completed" if self.execute else "planned",
                soak_seconds=soak_seconds,
                classification=kind,
                endurance_duration_satisfied=soak_seconds == FULL_SOAK_SECONDS,
                endurance_qualified=False,
                pc_audio=audio,
            )
            return
        raise AssertionError(scenario)

    def run_graph(self, soak_seconds: int) -> dict[str, Any]:
        names = scenario_names(soak_seconds)
        if self.state.get("first_red") is not None:
            failure = self.state["first_red"]
            raise RedGate(
                f"run is sealed at first red scenario {failure['scenario']}: "
                f"{failure['error']}"
            )
        settings = {
            "soak_seconds": soak_seconds,
            "stereo_mix": self.stereo_mix,
            "switch_target": self.switch_target,
            "candidate_events": str(self.candidate_events or ""),
            "selected_at_utc": self.selected_at_utc,
            "fault_events": str(self.fault_events or ""),
            "call_uri": self.call_uri,
            "call_package": self.call_package,
        }
        if self.state.get("campaign") not in (None, settings):
            raise RedGate("resume campaign settings do not match the existing E17 run")
        self.state["campaign"] = settings
        self._save()
        done_status = "completed" if self.execute else "planned"
        try:
            for scenario in names:
                previous = self.state["scenarios"].get(scenario, {})
                if previous.get("status") == done_status:
                    continue
                self._mark(scenario, "running")
                self._run_one(scenario, soak_seconds)
                if self.state["scenarios"].get(scenario, {}).get("status") == "paused":
                    break
                if self.state["scenarios"].get(scenario, {}).get("status") not in {
                    "completed",
                    "planned",
                }:
                    self._mark(scenario, done_status)
        except RedGate as exc:
            current = next(
                (
                    name
                    for name in names
                    if self.state["scenarios"].get(name, {}).get("status") == "running"
                ),
                "unknown",
            )
            if self.state.get("first_red") is None:
                self.state["first_red"] = {
                    "scenario": current,
                    "error": str(exc),
                    "at_utc": datetime.now(UTC).isoformat(),
                }
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
    parser.add_argument(
        "--switch-target", default=os.environ.get("E17_SWITCH_TARGET", "")
    )
    parser.add_argument("--candidate-events", type=Path)
    parser.add_argument("--selected-at")
    parser.add_argument("--fault-events", type=Path)
    parser.add_argument("--fault-target", choices=("call", "output"))
    parser.add_argument("--fault-started")
    parser.add_argument("--fault-ended")
    parser.add_argument("--call-uri", default=os.environ.get("E17_CALL_URI", ""))
    parser.add_argument("--call-package", default="com.discord")
    parser.add_argument("--soak-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    if bool(args.output) == bool(args.resume):
        parser.error("pass exactly one of --output or --resume")
    fault_values = (args.fault_target, args.fault_started, args.fault_ended)
    if any(fault_values) and not all(fault_values):
        parser.error("fault target, start, and end must be supplied together")
    if all(fault_values) and args.fault_events is None:
        parser.error("a fault window requires --fault-events evidence")
    fault_window = (
        FaultWindow(args.fault_target, args.fault_started, args.fault_ended)
        if all(fault_values)
        else None
    )
    runner = SubprocessRunner()
    options = {
        "host": args.host,
        "adb": args.adb,
        "execute": args.execute,
        "stereo_mix": args.stereo_mix,
        "switch_target": args.switch_target,
        "candidate_events": args.candidate_events,
        "selected_at_utc": args.selected_at or "",
        "fault_events": args.fault_events,
        "fault_window": fault_window,
        "call_uri": args.call_uri,
        "call_package": args.call_package,
    }
    harness = (
        E17Harness.resume(args.resume, runner, **options)
        if args.resume
        else E17Harness.create(args.output, runner, **options)
    )
    try:
        print(json.dumps(harness.run_graph(args.soak_seconds), indent=2))
        return 0
    except RedGate as exc:
        print(
            json.dumps({"directory": str(harness.directory), "error": str(exc)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
