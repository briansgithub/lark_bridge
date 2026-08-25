#!/usr/bin/env python3
"""Resumable, evidence-first E17 qualification harness.

The harness is intentionally safe by default: it writes a complete execution plan but
does not contact the Pi, Pixel, or PC audio devices until ``--execute`` is supplied.
It is the PC-side coordinator for later authorised E17/Phase-7 runs, not a replacement
for the lower transport gates.  In particular, a missing measured signal is never
interpreted as an audio defect: red transport evidence stops the scenario first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = REPO / "docs" / "experiments" / "results" / "E17"
SCHEMA = 1
SPEAKER_PREFLIGHT_EXIT = 78
MIN_MARGIN_DB = 10.0
MAX_CLIPPING_PCT = 0.01
MIN_AEC_SUPPRESSION_DB = 10.0
MAX_OUTPUT_RECOVERY_SECONDS = 3.0
PROTECTED_ANDROID_MARKERS = (
    "confirm your passkey",
    "pair with",
    "allow access to contacts",
    "use bluetooth",
)
TRANSPORT_ERROR_RE = re.compile(
    r"(?:hci command timeout|urb error|usb reset|frame reassembly failed|controller reset)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(self, command: list[str], *, timeout: float = 45.0) -> CommandResult: ...


class SubprocessRunner:
    def run(self, command: list[str], *, timeout: float = 45.0) -> CommandResult:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
        return CommandResult(command, result.returncode, result.stdout, result.stderr)


@dataclass(frozen=True)
class FaultWindow:
    """The only exception to zero transport faults, for the later Phase-7 injection."""

    target_role: str
    started_utc: str
    ended_utc: str
    allowed_events: tuple[str, ...] = ("teardown", "re-enumeration")


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def json_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} did not return JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"{label} did not return a JSON object")
    return value


def evaluate_objective_audio(preflight: dict[str, Any], aec: dict[str, Any] | None) -> list[str]:
    """Return red objective gates; no subjective claim can override one."""
    failures: list[str] = []
    if preflight.get("verdict") != "ready":
        failures.append(f"speaker preflight is {preflight.get('verdict')!r}")
        return failures
    for key in ("level_margin_db", "tone_margin_db"):
        value = preflight.get(key)
        if not isinstance(value, (int, float)) or value < MIN_MARGIN_DB:
            failures.append(f"{key} is below {MIN_MARGIN_DB:.0f} dB")
    active = preflight.get("active") or {}
    if isinstance(active, dict):
        if float(active.get("peak_dbfs", -200.0)) > -0.1:
            failures.append("speaker-to-Lark capture reached an unsafe peak")
        if float(active.get("clipped_pct", 0.0)) > MAX_CLIPPING_PCT:
            failures.append("speaker-to-Lark capture exceeds 0.01% clipping")
    if aec is not None:
        if aec.get("verdict") != "PASS":
            failures.append("AEC metric tool did not pass")
        suppression = aec.get("suppression_db")
        if not isinstance(suppression, (int, float)) or suppression < MIN_AEC_SUPPRESSION_DB:
            failures.append(f"AEC suppression is below {MIN_AEC_SUPPRESSION_DB:.0f} dB")
    return failures


def validate_recovery(seconds: float | None) -> list[str]:
    if seconds is None:
        return ["output recovery is unmeasured"]
    if seconds > MAX_OUTPUT_RECOVERY_SECONDS:
        return [f"output recovery {seconds:.3f}s exceeds {MAX_OUTPUT_RECOVERY_SECONDS:.0f}s"]
    return []


def candidate_mutation_failures(
    events: Sequence[dict[str, Any]], *, selected_at_utc: str
) -> list[str]:
    """Pair/profile/trust activity before explicit Pixel selection is a red gate."""
    selected = datetime.fromisoformat(selected_at_utc.replace("Z", "+00:00"))
    forbidden = {"Pair", "Connect", "ConnectProfile", "Trusted", "Bonded"}
    failures: list[str] = []
    for event in events:
        if event.get("operation") not in forbidden:
            continue
        try:
            timestamp = datetime.fromisoformat(str(event["timestamp_utc"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            failures.append("candidate mutation has no valid timestamp")
            continue
        if timestamp < selected:
            failures.append(f"candidate {event['operation']} occurred before Pixel selection")
    return failures


def fault_event_allowed(event: dict[str, Any], window: FaultWindow) -> bool:
    """Encode the clarification: only target teardown/re-enumeration in its window."""
    try:
        timestamp = datetime.fromisoformat(str(event["timestamp_utc"]).replace("Z", "+00:00"))
        started = datetime.fromisoformat(window.started_utc.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(window.ended_utc.replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return False
    return (
        event.get("role") == window.target_role
        and event.get("kind") in window.allowed_events
        and started <= timestamp <= ended
    )


def transport_failures(events: Sequence[dict[str, Any]], window: FaultWindow | None = None) -> list[str]:
    failures: list[str] = []
    for event in events:
        detail = str(event.get("detail", ""))
        if not TRANSPORT_ERROR_RE.search(detail):
            continue
        if window is not None and fault_event_allowed(event, window):
            continue
        failures.append(detail or "unclassified transport event")
    return failures


def protected_android_ui(xml: str) -> bool:
    lowered = xml.casefold()
    return any(marker in lowered for marker in PROTECTED_ANDROID_MARKERS)


class E17Harness:
    """Small stdlib coordinator with durable phase state and append-only evidence."""

    def __init__(
        self,
        directory: Path,
        *,
        runner: Runner,
        host: str,
        adb: str,
        execute: bool,
        stereo_mix: str,
    ) -> None:
        self.directory = directory
        self.runner = runner
        self.host = host
        self.adb = adb
        self.execute = execute
        self.stereo_mix = stereo_mix
        self.state_path = directory / "state.json"
        self.commands_path = directory / "commands.jsonl"
        self.command_number = (
            len(self.commands_path.read_text(encoding="utf-8").splitlines())
            if self.commands_path.exists()
            else 0
        )
        self.state = self._load_state()

    @classmethod
    def create(
        cls,
        output: Path | None,
        *,
        label: str,
        resume: Path | None,
        runner: Runner,
        host: str,
        adb: str,
        execute: bool,
        stereo_mix: str,
    ) -> E17Harness:
        if resume is not None:
            directory = resume.resolve()
            if not (directory / "state.json").is_file():
                raise RuntimeError(f"cannot resume {directory}: state.json is absent")
        else:
            directory = (output or DEFAULT_RESULTS / f"{utc_stamp()}-{label}").resolve()
            directory.mkdir(parents=True, exist_ok=False)
        return cls(directory, runner=runner, host=host, adb=adb, execute=execute, stereo_mix=stereo_mix)

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            state = json_object(self.state_path.read_text(encoding="utf-8"), "state")
            if state.get("schema") != SCHEMA:
                raise RuntimeError("unsupported E17 state schema")
            return state
        state = {
            "schema": SCHEMA,
            "created_utc": datetime.now(UTC).isoformat(),
            "mode": "execute" if self.execute else "plan-only",
            "phases": {},
            "human_action": None,
            "fault_injection_policy": {
                "phase": 7,
                "rule": "only target teardown/re-enumeration inside a precise timestamped window",
                "outside_window_or_off_target": "FAIL",
            },
        }
        self._save_state(state)
        return state

    def _save_state(self, state: dict[str, Any] | None = None) -> None:
        value = self.state if state is None else state
        self.state_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def mark(self, phase: str, status: str, **extra: Any) -> None:
        self.state["phases"][phase] = {"status": status, "updated_utc": datetime.now(UTC).isoformat(), **extra}
        self._save_state()

    def command(
        self,
        name: str,
        command: list[str],
        *,
        timeout: float = 45.0,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> CommandResult:
        result = CommandResult(command, 0, "", "") if not self.execute else self.runner.run(command, timeout=timeout)
        self.command_number += 1
        record = {"number": self.command_number, "name": name, "executed": self.execute, **asdict(result)}
        with self.commands_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        stem = self.directory / f"{self.command_number:04d}-{name}"
        stem.with_suffix(".stdout.log").write_text(result.stdout, encoding="utf-8")
        stem.with_suffix(".stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode not in allowed_returncodes:
            raise RuntimeError(f"{name} failed with exit {result.returncode}")
        return result

    def ssh(
        self,
        name: str,
        script: str,
        *,
        timeout: float = 45.0,
        allowed_returncodes: tuple[int, ...] = (0,),
    ) -> CommandResult:
        return self.command(
            name,
            ["ssh", self.host, script],
            timeout=timeout,
            allowed_returncodes=allowed_returncodes,
        )

    def adb_command(self, name: str, *args: str, timeout: float = 45.0) -> CommandResult:
        return self.command(name, [self.adb, *args], timeout=timeout)

    def resolve_roles(self) -> dict[str, Any]:
        result = self.ssh(
            "controllers",
            "cd /home/admin/rpi-lark-bridge && python3 pi/bridged/controller_roles.py --policy final-usb status",
        )
        roles = json_object(result.stdout, "controller role resolver") if self.execute else {"ready": True, "planned": True}
        if not roles.get("ready"):
            raise RuntimeError("red transport gate: configured controller roles are not ready")
        self.mark("role_resolution", "passed", controllers=roles)
        return roles

    def capture_evidence(self) -> None:
        # One bounded snapshot is cheap.  Long btmon is deliberately excluded here.
        self.ssh(
            "pi-snapshot",
            "cd /home/admin/rpi-lark-bridge; "
            "python3 pi/bridged/controller_roles.py --policy final-usb status; "
            "cat /run/user/1000/bridge-status.json; "
            "cat /run/larkbridge/storage-health.json; "
            "busctl --system tree org.bluez; "
            "XDG_RUNTIME_DIR=/run/user/1000 pw-dump; "
            "systemctl --no-pager --full status bluetooth.service bridge-btwatchdog@call.service bridge-btwatchdog@output.service; "
            "journalctl -k -n 120 -o short-precise",
            timeout=60,
        )
        self.adb_command("adb-audio", "shell", "dumpsys", "audio")
        self.adb_command("adb-bluetooth", "shell", "dumpsys", "bluetooth_manager")
        # UI hierarchy is evidence; screenshots are deliberately forbidden by E17.
        self.adb_command("adb-ui", "exec-out", "uiautomator", "dump", "/dev/tty")
        self.mark("evidence", "captured")

    def targeted_bluetooth_capture(self, operation: str, seconds: int = 15) -> None:
        if not 1 <= seconds <= 30:
            raise ValueError("targeted Bluetooth captures must be between 1 and 30 seconds")
        self.ssh(
            f"btmon-{operation}",
            f"sudo -n timeout {seconds}s btmon -T -c never",
            timeout=seconds + 10,
        )

    def lightweight_monitor(self, scenario: str, seconds: int) -> None:
        """Collect low-load periodic state during a run; use btmon only at an operation."""
        if not 5 <= seconds <= 3600:
            raise ValueError("lightweight monitor duration must be between 5 seconds and one hour")
        script = (
            "cd /home/admin/rpi-lark-bridge; "
            f"deadline=$((SECONDS + {seconds})); "
            "while [ $SECONDS -lt $deadline ]; do "
            "date --iso-8601=ns; "
            "python3 rig/pi/measure/invariants.py --json; "
            "python3 pi/bridged/controller_roles.py --policy final-usb status; "
            "journalctl -k -n 8 -o short-precise --no-pager; "
            "sleep 5; done"
        )
        self.ssh(f"light-monitor-{scenario}", script, timeout=seconds + 30)

    def run_aec_bench(self, scenario: str) -> dict[str, Any]:
        """Run the existing objective metric tool only after speaker preflight succeeds."""
        remote = f"/tmp/e17-{scenario}-aec"
        result = self.ssh(
            f"aec-bench-{scenario}",
            "cd /home/admin/rpi-lark-bridge && "
            f"python3 rig/pi/measure/aec_bench.py --out {remote}",
            timeout=90,
        )
        report = json_object(result.stdout, "AEC bench") if self.execute else {"verdict": "PASS", "suppression_db": MIN_AEC_SUPPRESSION_DB, "planned": True}
        failures = evaluate_objective_audio({"verdict": "ready", "level_margin_db": MIN_MARGIN_DB, "tone_margin_db": MIN_MARGIN_DB, "active": {}}, report)
        if failures:
            raise RuntimeError("red AEC gate: " + "; ".join(failures))
        return report

    def preflight_speaker(self, scenario: str) -> dict[str, Any] | None:
        remote = f"/tmp/e17-{scenario}-speaker-preflight"
        result = self.ssh(
            f"speaker-preflight-{scenario}",
            "cd /home/admin/rpi-lark-bridge && "
            f"python3 rig/pi/measure/speaker_preflight.py --out {remote}",
            timeout=45,
            allowed_returncodes=(0, SPEAKER_PREFLIGHT_EXIT),
        )
        if result.returncode == SPEAKER_PREFLIGHT_EXIT:
            self.mark(scenario, "paused", reason="speaker_wake_required")
            return None
        if result.returncode:
            raise RuntimeError(f"speaker preflight failed for {scenario}")
        report = json_object(result.stdout, "speaker preflight") if self.execute else {"verdict": "ready", "planned": True}
        failures = evaluate_objective_audio(report, None)
        if failures:
            raise RuntimeError("red acoustic gate: " + "; ".join(failures))
        return report

    def pixel_pairing_attempt(self) -> str:
        """Make the autonomous attempt and record exactly one human-tap pause if protected."""
        self.adb_command("adb-bluetooth-intent", "shell", "am", "start", "-a", "android.settings.BLUETOOTH_SETTINGS")
        hierarchy = self.adb_command("adb-pairing-ui", "exec-out", "uiautomator", "dump", "/dev/tty").stdout
        if self.execute and protected_android_ui(hierarchy):
            self.state["human_action"] = {
                "state": "pixel_protected_confirmation",
                "instruction": "Tap the one Android Bluetooth confirmation, then resume this result directory.",
                "created_utc": datetime.now(UTC).isoformat(),
            }
            self._save_state()
            self.mark("pixel_pairing", "paused", reason="protected_android_ui")
            return "human_tap_required"
        self.mark("pixel_pairing", "attempted")
        return "automation_attempted"

    def stereo_mix_plan(self, wav: Path) -> dict[str, list[str]]:
        if not self.stereo_mix:
            raise RuntimeError("Stereo Mix capture device is required for PC audio scenarios")
        return {
            "drive": ["ffplay", "-nodisp", "-autoexit", str(wav)],
            "capture": ["ffmpeg", "-f", "dshow", "-i", f"audio={self.stereo_mix}", "-t", "30", str(self.directory / "pc-stereo-mix.wav")],
        }

    def keepalive_plan(self, stimulus: Path, *, interval_seconds: int = 20) -> list[str]:
        """Periodic known playback prevents speaker sleep outside measured windows."""
        if interval_seconds < 5:
            raise ValueError("keepalive interval must remain lightweight (at least five seconds)")
        quoted = str(stimulus).replace("'", "'\\''")
        return [
            "ssh",
            self.host,
            (
                "cd /home/admin/rpi-lark-bridge && while :; do "
                f"pw-play --target \"$(jq -r '.output.chosen.node' /run/user/1000/bridge-status.json)\" '{quoted}'; "
                f"sleep {interval_seconds}; done"
            ),
        ]

    def assert_no_candidate_mutation(self, events: Sequence[dict[str, Any]], *, selected_at_utc: str) -> None:
        failures = candidate_mutation_failures(events, selected_at_utc=selected_at_utc)
        if failures:
            raise RuntimeError("red selection gate: " + "; ".join(failures))

    def require_transport_clean(self, events: Sequence[dict[str, Any]], window: FaultWindow | None = None) -> None:
        failures = transport_failures(events, window)
        if failures:
            self.mark("transport", "failed", failures=failures)
            raise RuntimeError("red transport gate: " + "; ".join(failures))
        self.mark("transport", "passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="larkbridge")
    parser.add_argument("--adb", default=shutil.which("adb") or "adb")
    parser.add_argument("--label", default="qualification")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--execute", action="store_true", help="allow live orchestration after Phase 4/5 approval")
    parser.add_argument("--stereo-mix", default=os.environ.get("E17_STEREO_MIX", ""))
    parser.add_argument("--phase", choices=("baseline", "pairing", "speaker", "soak"), default="baseline")
    args = parser.parse_args(argv)
    harness = E17Harness.create(
        args.output,
        label=args.label,
        resume=args.resume,
        runner=SubprocessRunner(),
        host=args.host,
        adb=args.adb,
        execute=args.execute,
        stereo_mix=args.stereo_mix,
    )
    try:
        harness.resolve_roles()
        harness.capture_evidence()
        if args.phase == "pairing":
            harness.pixel_pairing_attempt()
        elif args.phase in {"speaker", "soak"} and harness.preflight_speaker(args.phase) is None:
            print(json.dumps({"directory": str(harness.directory), "state": "speaker_wake_required"}))
            return SPEAKER_PREFLIGHT_EXIT
        harness.mark(args.phase, "ready" if not args.execute else "completed")
    except RuntimeError as exc:
        harness.mark(args.phase, "failed", error=str(exc))
        print(json.dumps({"directory": str(harness.directory), "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"directory": str(harness.directory), "mode": harness.state["mode"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
