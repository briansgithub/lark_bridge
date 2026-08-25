#!/usr/bin/env python3
"""Control-plane qualification campaign for USB-BT500 call audio and wired AUX."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

EXIT_HARDWARE_READY = 78
SCHEMA_VERSION = 1
DEFAULT_CYCLES = 5
DEFAULT_SOAK_SECONDS = 3600
DEFAULT_SAMPLE_SECONDS = 5.0
DEFAULT_CAPTURE_SECONDS = 60.0
BT500_ADDRESS = "A0:AD:9F:73:6C:24"
BT500_USB_ID = "0b05:1bf6"
REQUIRED_SYSTEM_SERVICES = (
    "bluetooth.service",
    "bridge-btwatchdog@call.service",
    "bridge-storage-guard.service",
    "bridge-tuning.service",
)
REQUIRED_USER_SERVICES = (
    "bridge-supervisor.service",
    "pipewire.service",
    "wireplumber.service",
)


class QualificationError(RuntimeError):
    """Base for a trustworthy negative harness outcome."""


class HardwareNotReady(QualificationError):
    """The operator or hardware must make the fixture ready before retrying."""


class EvidenceError(QualificationError):
    """Required evidence is absent, malformed, timed out, or internally inconsistent."""


class HardFailure(QualificationError):
    """Collected evidence proves an acceptance gate failed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Backend(Protocol):
    def snapshot(self, *, full: bool = True) -> dict[str, Any]: ...

    def capture(
        self, *, label: str, seconds: float, remote_out: str
    ) -> dict[str, Any]: ...

    def metrics(self, capture: Mapping[str, Any]) -> dict[str, Any]: ...

    def recycle_call(self, *, timeout: float = 75.0) -> dict[str, Any]: ...

    def fetch_cycle(self, remote_out: str, local_out: Path) -> None: ...

    def start_soak(
        self,
        *,
        soak_id: str,
        remote_out: str,
        duration: int,
        interval: float,
        resume: bool,
    ) -> dict[str, Any]: ...

    def soak_state(self, remote_out: str) -> dict[str, Any]: ...

    def fetch_soak(self, remote_out: str, local_out: Path) -> None: ...


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}-",
        delete=False,
    ) as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def strict_json(text: str, label: str) -> dict[str, Any]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{label} emitted malformed JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise EvidenceError(f"{label} must emit one JSON object")
    return document


def require_dict(parent: Mapping[str, Any], key: str, label: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise EvidenceError(f"{label}.{key} is missing or is not an object")
    return value


def require_bool(
    parent: Mapping[str, Any], key: str, expected: bool, label: str
) -> None:
    value = parent.get(key)
    if value is not expected:
        raise HardFailure(f"{label}.{key} is {value!r}, expected {expected!r}")


def service_state(
    snapshot: Mapping[str, Any], scope: str, unit: str
) -> Mapping[str, Any]:
    services = require_dict(snapshot, "services", "snapshot")
    scoped = require_dict(services, scope, "snapshot.services")
    value = scoped.get(unit)
    if not isinstance(value, dict):
        raise EvidenceError(f"service evidence missing for {scope}:{unit}")
    return value


def service_restarts(snapshot: Mapping[str, Any], scope: str, unit: str) -> int:
    raw = service_state(snapshot, scope, unit).get("NRestarts")
    if not isinstance(raw, (str, int)):
        raise EvidenceError(f"invalid NRestarts for {scope}:{unit}: {raw!r}")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise EvidenceError(f"invalid NRestarts for {scope}:{unit}: {raw!r}") from exc


def validate_snapshot(snapshot: Mapping[str, Any], *, require_active: bool) -> None:
    errors = snapshot.get("collection_errors")
    if not isinstance(errors, list):
        raise EvidenceError("snapshot.collection_errors is missing or malformed")
    if errors:
        raise EvidenceError(
            f"snapshot collection failed: {'; '.join(map(str, errors))}"
        )

    status = require_dict(snapshot, "status", "snapshot")
    controllers = require_dict(snapshot, "controllers", "snapshot")
    call_controller = require_dict(controllers, "call", "snapshot.controllers")
    output_controller = require_dict(controllers, "output", "snapshot.controllers")
    endpoints = require_dict(status, "endpoints", "snapshot.status")
    volume = require_dict(status, "wired_output_volume", "snapshot.status")

    require_bool(controllers, "ready", True, "snapshot.controllers")
    require_bool(call_controller, "ready", True, "snapshot.controllers.call")
    if call_controller.get("configured_address") != BT500_ADDRESS:
        raise HardFailure("call controller is not configured as the USB-BT500")
    if call_controller.get("observed_address") != BT500_ADDRESS:
        raise HardwareNotReady("the configured USB-BT500 is not present")
    if call_controller.get("observed_bus") != "USB":
        raise HardFailure("the call controller did not resolve on USB")
    if call_controller.get("observed_usb_id") != BT500_USB_ID:
        raise HardFailure(
            f"wrong call-controller USB identity: {call_controller.get('observed_usb_id')!r}"
        )
    if output_controller.get("required") is not False:
        raise HardFailure(
            "wired-output mode unexpectedly requires an output controller"
        )
    if output_controller.get("configured") is not False:
        raise HardFailure("an output Bluetooth controller is configured in AUX mode")
    require_bool(output_controller, "ready", True, "snapshot.controllers.output")

    if not endpoints.get("lark"):
        raise HardwareNotReady("Lark A1 is absent")
    if not endpoints.get("wired_output"):
        raise HardwareNotReady("the Pi wired output is absent")
    if status.get("mode") != "bluetooth-wired":
        raise HardFailure(
            f"bridge mode is {status.get('mode')!r}, expected bluetooth-wired"
        )
    require_bool(volume, "required", True, "snapshot.status.wired_output_volume")
    require_bool(volume, "verified", True, "snapshot.status.wired_output_volume")
    desired = volume.get("desired")
    observed = volume.get("observed")
    if not isinstance(desired, (int, float)) or not isinstance(observed, (int, float)):
        raise EvidenceError(
            "wired output volume does not contain numeric desired/observed values"
        )
    if abs(float(desired) - 0.85) > 0.001 or abs(float(observed) - 0.85) > 0.01:
        raise HardFailure(
            f"wired output volume is not 0.85 (desired={desired}, observed={observed})"
        )

    for unit in REQUIRED_SYSTEM_SERVICES:
        if service_state(snapshot, "system", unit).get("ActiveState") != "active":
            raise HardwareNotReady(f"required system service is not active: {unit}")
    for unit in REQUIRED_USER_SERVICES:
        if service_state(snapshot, "user", unit).get("ActiveState") != "active":
            raise HardwareNotReady(f"required user service is not active: {unit}")

    health = require_dict(snapshot, "health", "snapshot")
    temperature = health.get("temperature_c")
    memory = health.get("mem_available_kib")
    if not isinstance(temperature, (int, float)):
        raise EvidenceError("temperature evidence is missing or nonnumeric")
    if float(temperature) >= 80.0:
        raise HardFailure(f"temperature reached {temperature} C")
    if not isinstance(memory, int) or memory <= 0:
        raise EvidenceError("available-memory evidence is missing or invalid")
    system = require_dict(status, "system", "snapshot.status")
    throttled = system.get("throttled")
    if throttled not in ("throttled=0x0", "0x0"):
        if throttled is None:
            raise EvidenceError("firmware throttling evidence is missing")
        raise HardFailure(f"firmware throttling reported: {throttled}")

    if not require_active:
        return
    if status.get("state") != "ACTIVE":
        raise HardwareNotReady(
            f"live call is not ACTIVE (state={status.get('state')!r})"
        )
    call = require_dict(status, "call", "snapshot.status")
    require_bool(call, "controller_binding_accepted", True, "snapshot.status.call")
    aec = require_dict(status, "aec", "snapshot.status")
    require_bool(aec, "enabled", True, "snapshot.status.aec")
    require_bool(aec, "verified", True, "snapshot.status.aec")
    if aec.get("node_latency_frames") != 1920:
        raise HardFailure(
            f"AEC node latency is {aec.get('node_latency_frames')!r}, expected 1920"
        )
    graph = require_dict(status, "graph", "snapshot.status")
    if graph.get("missing_links") != []:
        raise HardFailure(
            f"ACTIVE graph has missing links: {graph.get('missing_links')!r}"
        )
    if graph.get("unexpected_links") != []:
        raise HardFailure(
            f"ACTIVE graph has unexpected links: {graph.get('unexpected_links')!r}"
        )
    quantum = snapshot.get("graph_quantum")
    if not isinstance(quantum, int):
        raise EvidenceError("snapshot.graph_quantum is missing or nonnumeric")
    if quantum < 1024:
        raise HardFailure(f"PipeWire graph quantum collapsed to {quantum}")
    transport = require_dict(snapshot, "transport", "snapshot")
    require_bool(transport, "controller_answers", True, "snapshot.transport")
    require_bool(transport, "sco", True, "snapshot.transport")


def new_error_lines(
    before: Mapping[str, Any], after: Mapping[str, Any], key: str
) -> list[Any]:
    first = before.get(key)
    second = after.get(key)
    if not isinstance(first, list) or not isinstance(second, list):
        raise EvidenceError(f"snapshot {key} evidence is missing or malformed")
    remaining = list(second)
    for item in first:
        try:
            remaining.remove(item)
        except ValueError:
            pass
    return remaining


def validate_capture(capture: Mapping[str, Any], requested_seconds: float) -> None:
    if capture.get("error"):
        raise HardFailure(f"call capture failed: {capture['error']}")
    if capture.get("links_verified") is not True:
        raise EvidenceError("call capture did not verify recorder links")
    if (
        capture.get("state_before") != "ACTIVE"
        or capture.get("state_after") != "ACTIVE"
    ):
        raise HardFailure("call left ACTIVE during the capture")
    seconds = capture.get("seconds")
    if not isinstance(seconds, (int, float)) or float(seconds) < requested_seconds:
        raise EvidenceError("call capture duration is shorter than requested")
    wavs = capture.get("wavs")
    if not isinstance(wavs, dict):
        raise EvidenceError("call capture omitted WAV paths")
    for name in ("bridge.e12.reference", "bridge.e12.raw", "bridge.e12.clean"):
        if not isinstance(wavs.get(name), str) or not wavs[name]:
            raise EvidenceError(f"call capture omitted {name}")

    pwtop = capture.get("pwtop")
    if not isinstance(pwtop, dict) or not pwtop:
        raise EvidenceError("call capture omitted pw-top evidence")
    running_quantum = 0
    for name, row in pwtop.items():
        if not isinstance(row, dict):
            raise EvidenceError(f"malformed pw-top row for {name}")
        delta = row.get("err_delta")
        quantum = row.get("quantum")
        if not isinstance(delta, int) or not isinstance(quantum, int):
            raise EvidenceError(f"malformed pw-top counters for {name}")
        if delta != 0:
            raise HardFailure(f"PipeWire node {name} accumulated {delta} errors")
        running_quantum = max(running_quantum, quantum)
    if running_quantum < 1024:
        raise HardFailure(f"capture quantum collapsed to {running_quantum}")


def validate_metrics(metrics: Mapping[str, Any]) -> None:
    if metrics.get("verdict") != "PASS":
        failures = metrics.get("failures")
        raise HardFailure(f"AEC metric failed: {failures!r}")
    suppression = metrics.get("suppression_db")
    if not isinstance(suppression, (int, float)):
        raise EvidenceError("AEC metric omitted numeric suppression_db")
    if float(suppression) < 10.0:
        raise HardFailure(f"AEC suppression is only {suppression} dB")


def validate_cycle(
    before: Mapping[str, Any],
    capture: Mapping[str, Any],
    metrics: Mapping[str, Any],
    after: Mapping[str, Any],
    requested_seconds: float,
) -> dict[str, Any]:
    validate_snapshot(before, require_active=True)
    validate_capture(capture, requested_seconds)
    validate_metrics(metrics)
    validate_snapshot(after, require_active=True)

    for scope, units in (
        ("system", REQUIRED_SYSTEM_SERVICES),
        ("user", REQUIRED_USER_SERVICES),
    ):
        for unit in units:
            if service_restarts(after, scope, unit) != service_restarts(
                before, scope, unit
            ):
                raise HardFailure(f"service restarted during cycle: {scope}:{unit}")

    before_watchdog = require_dict(before, "watchdog", "snapshot")
    after_watchdog = require_dict(after, "watchdog", "snapshot")
    if after_watchdog.get("recoveries") != before_watchdog.get("recoveries"):
        raise HardFailure("call-controller watchdog recovery occurred during the cycle")
    kernel = new_error_lines(before, after, "kernel_errors")
    usb = new_error_lines(before, after, "usb_errors")
    if kernel:
        raise HardFailure(f"new kernel Bluetooth errors: {kernel!r}")
    if usb:
        raise HardFailure(f"new USB errors: {usb!r}")
    return {
        "verdict": "PASS",
        "suppression_db": metrics["suppression_db"],
        "capture_seconds": capture["seconds"],
        "graph_quantum": after["graph_quantum"],
    }


def validate_recycle(
    recycle: Mapping[str, Any],
    before: Mapping[str, Any],
    rejoined: Mapping[str, Any],
) -> None:
    if recycle.get("verdict") != "PASS":
        raise HardwareNotReady(f"fresh call session was not restored: {recycle!r}")
    if recycle.get("adapter_address") != BT500_ADDRESS:
        raise HardFailure("call-session recycle targeted a controller other than BT500")
    if recycle.get("call_down_observed") is not True:
        raise EvidenceError("call-session recycle never observed teardown")
    if recycle.get("active_observed") is not True:
        raise HardwareNotReady("Pixel did not restore HFP audio after reconnect")
    validate_snapshot(rejoined, require_active=True)
    for scope, units in (
        ("system", REQUIRED_SYSTEM_SERVICES),
        ("user", REQUIRED_USER_SERVICES),
    ):
        for unit in units:
            if service_restarts(rejoined, scope, unit) != service_restarts(
                before, scope, unit
            ):
                raise HardFailure(
                    f"service restarted during call recycle: {scope}:{unit}"
                )
    if new_error_lines(before, rejoined, "kernel_errors"):
        raise HardFailure("new Bluetooth kernel error during call recycle")
    if new_error_lines(before, rejoined, "usb_errors"):
        raise HardFailure("new USB error during call recycle")
    opening_watchdog = require_dict(before, "watchdog", "snapshot")
    closing_watchdog = require_dict(rejoined, "watchdog", "snapshot")
    if closing_watchdog.get("recoveries") != opening_watchdog.get("recoveries"):
        raise HardFailure("watchdog recovery occurred during the normal call recycle")


class CampaignStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.checkpoint_path = self.path / "checkpoint.json"

    def create(
        self,
        *,
        target_cycles: int = DEFAULT_CYCLES,
        soak_seconds: int = DEFAULT_SOAK_SECONDS,
        sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
    ) -> dict[str, Any]:
        if self.path.exists() and any(self.path.iterdir()):
            return self.load()
        self.path.mkdir(parents=True, exist_ok=True)
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": self.path.name,
            "created_utc": utc_stamp(),
            "target_cycles": target_cycles,
            "soak_seconds": soak_seconds,
            "sample_seconds": sample_seconds,
            "baseline": {"status": "pending"},
            "cycles": [],
            "soak": {"status": "pending"},
            "verdict": "IN_PROGRESS",
        }
        self.save(document)
        return document

    def load(self) -> dict[str, Any]:
        try:
            document = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceError(f"checkpoint is absent or malformed: {exc}") from exc
        if not isinstance(document, dict):
            raise EvidenceError("checkpoint root is not an object")
        if document.get("schema_version") != SCHEMA_VERSION:
            raise EvidenceError("unsupported checkpoint schema")
        if not isinstance(document.get("cycles"), list):
            raise EvidenceError("checkpoint cycles is not a list")
        for key in ("target_cycles", "soak_seconds"):
            if not isinstance(document.get(key), int) or int(document[key]) <= 0:
                raise EvidenceError(f"checkpoint {key} is invalid")
        if not isinstance(document.get("sample_seconds"), (int, float)):
            raise EvidenceError("checkpoint sample_seconds is invalid")
        require_dict(document, "baseline", "checkpoint")
        require_dict(document, "soak", "checkpoint")
        return document

    def save(self, document: Mapping[str, Any]) -> None:
        atomic_json(self.checkpoint_path, document)

    def cycle(self, document: dict[str, Any], index: int) -> dict[str, Any]:
        for item in document["cycles"]:
            if isinstance(item, dict) and item.get("index") == index:
                return item
        item = {"index": index, "status": "pending", "attempts": []}
        document["cycles"].append(item)
        document["cycles"].sort(key=lambda row: int(row.get("index", 0)))
        return item


class QualificationHarness:
    def __init__(self, backend: Backend, store: CampaignStore):
        self.backend = backend
        self.store = store

    def baseline(self) -> dict[str, Any]:
        checkpoint = self.store.load()
        if checkpoint["baseline"].get("status") == "passed":
            return checkpoint
        directory = self.store.path / "baseline"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            snapshot = self.backend.snapshot(full=True)
            atomic_json(directory / "snapshot.json", snapshot)
            validate_snapshot(snapshot, require_active=False)
        except HardwareNotReady as exc:
            checkpoint["baseline"] = {"status": "waiting", "reason": str(exc)}
            self.store.save(checkpoint)
            raise
        except QualificationError as exc:
            checkpoint["baseline"] = {"status": "failed", "reason": str(exc)}
            checkpoint["verdict"] = "FAIL"
            self.store.save(checkpoint)
            raise
        checkpoint["baseline"] = {
            "status": "passed",
            "artifact": str(directory.relative_to(self.store.path)),
            "completed_utc": utc_stamp(),
        }
        checkpoint["verdict"] = "IN_PROGRESS"
        self.store.save(checkpoint)
        return checkpoint

    def cycle(
        self, index: int, *, seconds: float = DEFAULT_CAPTURE_SECONDS
    ) -> dict[str, Any]:
        checkpoint = self.store.load()
        if checkpoint["baseline"].get("status") != "passed":
            raise HardwareNotReady("baseline has not passed")
        if not 1 <= index <= int(checkpoint["target_cycles"]):
            raise EvidenceError(f"cycle index {index} is outside the campaign")
        cycle = self.store.cycle(checkpoint, index)
        if cycle.get("status") == "passed":
            return checkpoint
        attempt_number = len(cycle["attempts"]) + 1
        directory = (
            self.store.path
            / "cycles"
            / f"{index:03d}"
            / f"attempt-{attempt_number:02d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        attempt: dict[str, Any] = {
            "attempt": attempt_number,
            "status": "running",
            "started_utc": utc_stamp(),
            "artifact": str(directory.relative_to(self.store.path)),
        }
        cycle["attempts"].append(attempt)
        cycle["status"] = "running"
        self.store.save(checkpoint)
        remote_out = f"/var/tmp/bt500-aux/{checkpoint['campaign_id']}/cycle-{index:03d}"
        try:
            before = self.backend.snapshot(full=True)
            atomic_json(directory / "before.json", before)
            validate_snapshot(before, require_active=True)
            capture = self.backend.capture(
                label=f"bt500-aux-{index:03d}-a{attempt_number:02d}",
                seconds=seconds,
                remote_out=remote_out,
            )
            atomic_json(directory / "capture.json", capture)
            self.backend.fetch_cycle(remote_out, directory / "remote-capture")
            metrics = self.backend.metrics(capture)
            atomic_json(directory / "aec-metrics.json", metrics)
            after = self.backend.snapshot(full=True)
            atomic_json(directory / "after.json", after)
            summary = validate_cycle(before, capture, metrics, after, seconds)
            recycle = self.backend.recycle_call()
            atomic_json(directory / "session-recycle.json", recycle)
            rejoined = self.backend.snapshot(full=True)
            atomic_json(directory / "rejoined.json", rejoined)
            validate_recycle(recycle, after, rejoined)
            summary["fresh_session_rejoined"] = True
        except KeyboardInterrupt:
            attempt["status"] = "interrupted"
            attempt["completed_utc"] = utc_stamp()
            cycle["status"] = "interrupted"
            self.store.save(checkpoint)
            raise
        except HardwareNotReady as exc:
            attempt.update(status="waiting", reason=str(exc), completed_utc=utc_stamp())
            cycle.update(status="waiting", reason=str(exc))
            self.store.save(checkpoint)
            raise
        except QualificationError as exc:
            attempt.update(status="failed", reason=str(exc), completed_utc=utc_stamp())
            cycle.update(status="failed", reason=str(exc))
            checkpoint["verdict"] = "FAIL"
            self.store.save(checkpoint)
            raise
        attempt.update(status="passed", summary=summary, completed_utc=utc_stamp())
        cycle.update(status="passed", summary=summary, completed_utc=utc_stamp())
        cycle.pop("reason", None)
        checkpoint["verdict"] = "IN_PROGRESS"
        self.store.save(checkpoint)
        return checkpoint

    def campaign(self, *, seconds: float = DEFAULT_CAPTURE_SECONDS) -> dict[str, Any]:
        checkpoint = self.baseline()
        for index in range(1, int(checkpoint["target_cycles"]) + 1):
            checkpoint = self.cycle(index, seconds=seconds)
        return checkpoint

    def start_soak(self, *, resume: bool = False) -> dict[str, Any]:
        checkpoint = self.store.load()
        passed = {
            int(item["index"])
            for item in checkpoint["cycles"]
            if isinstance(item, dict) and item.get("status") == "passed"
        }
        required = set(range(1, int(checkpoint["target_cycles"]) + 1))
        if passed != required:
            raise HardwareNotReady("all acceptance cycles must pass before the soak")
        soak = checkpoint["soak"]
        remote_out = str(
            soak.get("remote_out")
            or f"/var/tmp/bt500-aux/{checkpoint['campaign_id']}/soak"
        )
        if soak.get("status") == "running":
            state = self.backend.soak_state(remote_out)
            remote_status = state.get("status")
            if remote_status in {"running", "passed"}:
                return checkpoint
            resume = True
        run_number = int(soak.get("runs", 0)) + 1
        result = self.backend.start_soak(
            soak_id=f"{checkpoint['campaign_id']}-r{run_number}",
            remote_out=remote_out,
            duration=int(checkpoint["soak_seconds"]),
            interval=float(checkpoint["sample_seconds"]),
            resume=resume,
        )
        if result.get("status") not in {"running", "passed"}:
            raise EvidenceError(f"soak did not start: {result!r}")
        checkpoint["soak"] = {
            "status": str(result["status"]),
            "remote_out": remote_out,
            "unit": result.get("unit"),
            "runs": run_number,
            "started_utc": utc_stamp(),
        }
        self.store.save(checkpoint)
        return checkpoint

    def collect(self) -> dict[str, Any]:
        checkpoint = self.store.load()
        soak = checkpoint["soak"]
        remote_out = soak.get("remote_out")
        if not isinstance(remote_out, str) or not remote_out:
            raise HardwareNotReady("soak has not been started")
        remote_state = self.backend.soak_state(remote_out)
        status = remote_state.get("status")
        if status == "running":
            raise HardwareNotReady("soak is still running")
        if status != "passed":
            soak.update(status="failed", reason=remote_state.get("reason", status))
            checkpoint["verdict"] = "FAIL"
            self.store.save(checkpoint)
            raise HardFailure(f"soak did not pass: {remote_state!r}")

        local = self.store.path / "soak" / "evidence"
        self.backend.fetch_soak(remote_out, local)
        summary = validate_soak_evidence(
            local,
            duration=int(checkpoint["soak_seconds"]),
            interval=float(checkpoint["sample_seconds"]),
        )
        soak_closing = self.backend.snapshot(full=True)
        atomic_json(self.store.path / "soak-closing-snapshot.json", soak_closing)
        validate_snapshot(soak_closing, require_active=True)
        final_capture_dir = self.store.path / "soak" / "final-aec"
        final_capture_dir.mkdir(parents=True, exist_ok=True)
        final_remote = f"{remote_out}/final-aec"
        capture = self.backend.capture(
            label="bt500-aux-soak-final",
            seconds=DEFAULT_CAPTURE_SECONDS,
            remote_out=final_remote,
        )
        atomic_json(final_capture_dir / "capture.json", capture)
        metrics = self.backend.metrics(capture)
        atomic_json(final_capture_dir / "aec-metrics.json", metrics)
        self.backend.fetch_cycle(final_remote, final_capture_dir / "remote-capture")
        after_capture = self.backend.snapshot(full=True)
        atomic_json(final_capture_dir / "after.json", after_capture)
        final_aec = validate_cycle(
            soak_closing,
            capture,
            metrics,
            after_capture,
            DEFAULT_CAPTURE_SECONDS,
        )
        summary["final_aec"] = final_aec
        recycle = self.backend.recycle_call()
        atomic_json(self.store.path / "final-session-recycle.json", recycle)
        final_snapshot = self.backend.snapshot(full=True)
        atomic_json(self.store.path / "final-snapshot.json", final_snapshot)
        validate_recycle(recycle, after_capture, final_snapshot)
        atomic_json(self.store.path / "soak-summary.json", summary)
        soak.update(status="passed", completed_utc=utc_stamp(), summary=summary)
        checkpoint["verdict"] = "PASS"
        self.store.save(checkpoint)
        manifest = evidence_manifest(self.store.path)
        atomic_json(self.store.path / "evidence-manifest.json", manifest)
        return checkpoint


def validate_soak_evidence(
    directory: Path, *, duration: int, interval: float
) -> dict[str, Any]:
    state_path = directory / "state.json"
    samples_path = directory / "samples.jsonl"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"soak state is absent or malformed: {exc}") from exc
    if not isinstance(state, dict) or state.get("status") != "passed":
        raise HardFailure(f"soak state is not passed: {state!r}")
    opening = state.get("opening")
    if not isinstance(opening, dict):
        raise EvidenceError("soak state omitted its opening evidence")
    validate_snapshot(opening, require_active=True)
    elapsed = state.get("elapsed_s")
    if not isinstance(elapsed, (int, float)) or float(elapsed) < duration:
        raise EvidenceError(f"soak elapsed time is only {elapsed!r}s")
    try:
        lines = samples_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise EvidenceError(f"soak samples are absent: {exc}") from exc
    samples: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            raise EvidenceError(f"blank soak sample at line {number}")
        document = strict_json(line, f"soak sample line {number}")
        if document.get("event"):
            if document.get("event") == "hard_failure":
                raise HardFailure(f"soak recorded a hard failure: {document!r}")
            continue
        validate_snapshot(document, require_active=True)
        for scope, units in (
            ("system", REQUIRED_SYSTEM_SERVICES),
            ("user", REQUIRED_USER_SERVICES),
        ):
            for unit in units:
                if service_restarts(document, scope, unit) != service_restarts(
                    opening, scope, unit
                ):
                    raise HardFailure(f"service restarted during soak: {scope}:{unit}")
        opening_watchdog = require_dict(opening, "watchdog", "soak opening")
        sample_watchdog = require_dict(document, "watchdog", "soak sample")
        if sample_watchdog.get("recoveries") != opening_watchdog.get("recoveries"):
            raise HardFailure("controller watchdog recovery occurred during soak")
        if new_error_lines(opening, document, "kernel_errors"):
            raise HardFailure("new Bluetooth kernel error occurred during soak")
        if new_error_lines(opening, document, "usb_errors"):
            raise HardFailure("new USB error occurred during soak")
        samples.append(document)
    minimum = max(int(duration / interval) - 1, 1)
    if len(samples) < minimum:
        raise EvidenceError(
            f"soak has only {len(samples)} samples; expected at least {minimum}"
        )
    return {
        "verdict": "PASS",
        "duration_s": float(elapsed),
        "samples": len(samples),
        "interval_s": interval,
        "runs": state.get("runs"),
    }


def evidence_manifest(root: Path) -> dict[str, Any]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "evidence-manifest.json":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
        )
    return {"schema_version": 1, "created_utc": utc_stamp(), "files": files}


class SshBackend:
    def __init__(
        self,
        host: str = "larkbridge",
        repo: str = "/home/admin/rpi-lark-bridge",
        *,
        command_timeout: float = 90.0,
    ):
        self.host = host
        self.repo = repo
        self.command_timeout = command_timeout

    def _ssh(self, command: str, *, timeout: float | None = None) -> CommandResult:
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    self.host,
                    f"export XDG_RUNTIME_DIR=/run/user/$(id -u); {command}",
                ],
                capture_output=True,
                text=True,
                timeout=timeout or self.command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EvidenceError(f"remote command timed out: {command}") from exc
        except OSError as exc:
            raise EvidenceError(f"could not execute ssh: {exc}") from exc
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def _required_json(
        self, command: str, label: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        result = self._ssh(command, timeout=timeout)
        document = strict_json(result.stdout, label) if result.stdout.strip() else None
        if result.returncode == EXIT_HARDWARE_READY:
            detail = document or {}
            raise HardwareNotReady(
                str(detail.get("reason") or detail.get("error") or label)
            )
        if result.returncode != 0:
            # A structured failure is evidence. Return it to the caller's strict
            # schema/verdict validator so the campaign archives it before stopping.
            if document is not None:
                return document
            raise EvidenceError(
                f"{label} failed with {result.returncode}: {result.stderr.strip()}"
            )
        if document is None:
            raise EvidenceError(f"{label} emitted no JSON evidence")
        return document

    @property
    def remote_program(self) -> str:
        return f"{self.repo}/rig/bt500_aux/remote.py"

    def snapshot(self, *, full: bool = True) -> dict[str, Any]:
        option = " --full" if full else ""
        return self._required_json(
            f"python3 {shlex.quote(self.remote_program)} snapshot{option}",
            "remote snapshot",
        )

    def capture(self, *, label: str, seconds: float, remote_out: str) -> dict[str, Any]:
        command = (
            f"cd {shlex.quote(self.repo)} && "
            "python3 rig/pi/measure/call_capture.py "
            f"--label {shlex.quote(label)} --seconds {seconds:.3f} "
            f"--outdir {shlex.quote(remote_out)} --mode echo"
        )
        return self._required_json(
            command,
            "call capture",
            timeout=max(self.command_timeout, seconds + 30),
        )

    def metrics(self, capture: Mapping[str, Any]) -> dict[str, Any]:
        wavs = capture.get("wavs")
        if not isinstance(wavs, Mapping):
            raise EvidenceError("capture did not provide WAV paths for AEC scoring")
        required = {}
        for key, option in (
            ("bridge.e12.raw", "--raw"),
            ("bridge.e12.clean", "--clean"),
            ("bridge.e12.reference", "--reference"),
        ):
            value = wavs.get(key)
            if not isinstance(value, str) or not value:
                raise EvidenceError(f"capture WAV path missing: {key}")
            required[option] = value
        command = (
            f"cd {shlex.quote(self.repo)} && python3 rig/analysis/aec_metrics.py "
            + " ".join(
                f"{option} {shlex.quote(value)}" for option, value in required.items()
            )
            + " --signal speech --min-suppression-db 10"
        )
        return self._required_json(command, "AEC metrics", timeout=180)

    def recycle_call(self, *, timeout: float = 75.0) -> dict[str, Any]:
        return self._required_json(
            f"python3 {shlex.quote(self.remote_program)} recycle "
            f"--timeout {timeout:.3f}",
            "call session recycle",
            timeout=timeout + 20,
        )

    def start_soak(
        self,
        *,
        soak_id: str,
        remote_out: str,
        duration: int,
        interval: float,
        resume: bool,
    ) -> dict[str, Any]:
        unit = "bt500-aux-" + "".join(
            character if character.isalnum() or character in "-_" else "-"
            for character in soak_id
        )
        resume_option = " --resume" if resume else ""
        command = (
            f"mkdir -p {shlex.quote(remote_out)} && "
            f"systemd-run --user --unit={shlex.quote(unit)} --collect "
            f"python3 {shlex.quote(self.remote_program)} soak "
            f"--out {shlex.quote(remote_out)} --duration {duration} "
            f"--interval {interval:.3f}{resume_option}"
        )
        result = self._ssh(command)
        if result.returncode != 0:
            raise EvidenceError(f"could not launch soak: {result.stderr.strip()}")
        check = self._ssh(
            f"systemctl --user is-active {shlex.quote(unit)}",
            timeout=20,
        )
        if check.returncode != 0 or check.stdout.strip() not in {
            "active",
            "activating",
        }:
            state = self.soak_state(remote_out)
            if state.get("status") != "passed":
                raise EvidenceError(
                    f"soak unit {unit} did not remain active: {state!r}"
                )
        return {"status": "running", "unit": unit, "remote_out": remote_out}

    def soak_state(self, remote_out: str) -> dict[str, Any]:
        return self._required_json(
            f"python3 {shlex.quote(self.remote_program)} state "
            f"--out {shlex.quote(remote_out)}",
            "soak state",
        )

    def fetch_soak(self, remote_out: str, local_out: Path) -> None:
        self._fetch_tree(remote_out, local_out, label="soak")

    def fetch_cycle(self, remote_out: str, local_out: Path) -> None:
        self._fetch_tree(remote_out, local_out, label="cycle capture")

    def _fetch_tree(self, remote_out: str, local_out: Path, *, label: str) -> None:
        local_out.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as handle:
            archive = Path(handle.name)
        try:
            with archive.open("wb") as output:
                try:
                    result = subprocess.run(
                        [
                            "ssh",
                            "-o",
                            "BatchMode=yes",
                            self.host,
                            f"tar -cz -C {shlex.quote(remote_out)} .",
                        ],
                        stdout=output,
                        stderr=subprocess.PIPE,
                        timeout=300,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise EvidenceError(
                        f"could not fetch {label} evidence: {exc}"
                    ) from exc
            if result.returncode != 0:
                raise EvidenceError(
                    f"{label} fetch failed: "
                    f"{result.stderr.decode(errors='replace').strip()}"
                )
            with tarfile.open(archive, "r:gz") as bundle:
                bundle.extractall(local_out, filter="data")
        finally:
            archive.unlink(missing_ok=True)


def default_campaign_path(root: Path) -> Path:
    return root / f"campaign-{utc_stamp()}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default=os.environ.get("BRIDGE_PI_HOST", "larkbridge")
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("BRIDGE_PI_REPO", "/home/admin/rpi-lark-bridge"),
    )
    parser.add_argument(
        "--artifacts",
        type=Path,
        default=Path("artifacts/bt500-aux"),
        help="root used when --campaign is omitted",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    baseline = commands.add_parser("baseline")
    baseline.add_argument("--campaign", type=Path)
    baseline.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    baseline.add_argument("--soak-seconds", type=int, default=DEFAULT_SOAK_SECONDS)
    baseline.add_argument(
        "--sample-seconds", type=float, default=DEFAULT_SAMPLE_SECONDS
    )

    cycle = commands.add_parser("cycle")
    cycle.add_argument("--campaign", type=Path, required=True)
    cycle.add_argument("--index", type=int)
    cycle.add_argument("--seconds", type=float, default=DEFAULT_CAPTURE_SECONDS)

    campaign = commands.add_parser("campaign")
    campaign.add_argument("--campaign", type=Path)
    campaign.add_argument("--cycles", type=int, default=DEFAULT_CYCLES)
    campaign.add_argument("--seconds", type=float, default=DEFAULT_CAPTURE_SECONDS)
    campaign.add_argument("--soak-seconds", type=int, default=DEFAULT_SOAK_SECONDS)
    campaign.add_argument(
        "--sample-seconds", type=float, default=DEFAULT_SAMPLE_SECONDS
    )

    soak = commands.add_parser("soak")
    soak.add_argument("--campaign", type=Path, required=True)
    soak.add_argument("--resume", action="store_true")

    collect = commands.add_parser("collect")
    collect.add_argument("--campaign", type=Path, required=True)
    return parser


def _store_for(args: argparse.Namespace) -> tuple[CampaignStore, dict[str, Any]]:
    chosen = args.campaign or default_campaign_path(args.artifacts)
    store = CampaignStore(chosen)
    if args.command in {"baseline", "campaign"}:
        document = store.create(
            target_cycles=int(args.cycles),
            soak_seconds=int(args.soak_seconds),
            sample_seconds=float(args.sample_seconds),
        )
    else:
        document = store.load()
    return store, document


def main(argv: Sequence[str] | None = None, *, backend: Backend | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "cycles", DEFAULT_CYCLES) <= 0:
        parser.error("--cycles must be positive")
    if getattr(args, "seconds", DEFAULT_CAPTURE_SECONDS) <= 0:
        parser.error("--seconds must be positive")
    if getattr(args, "soak_seconds", DEFAULT_SOAK_SECONDS) <= 0:
        parser.error("--soak-seconds must be positive")
    if getattr(args, "sample_seconds", DEFAULT_SAMPLE_SECONDS) <= 0:
        parser.error("--sample-seconds must be positive")

    try:
        store, checkpoint = _store_for(args)
        implementation = backend or SshBackend(args.host, args.repo)
        harness = QualificationHarness(implementation, store)
        if args.command == "baseline":
            checkpoint = harness.baseline()
        elif args.command == "cycle":
            passed = {
                int(item["index"])
                for item in checkpoint["cycles"]
                if isinstance(item, dict) and item.get("status") == "passed"
            }
            index = args.index or next(
                (
                    candidate
                    for candidate in range(1, int(checkpoint["target_cycles"]) + 1)
                    if candidate not in passed
                ),
                int(checkpoint["target_cycles"]),
            )
            checkpoint = harness.cycle(index, seconds=float(args.seconds))
        elif args.command == "campaign":
            checkpoint = harness.campaign(seconds=float(args.seconds))
        elif args.command == "soak":
            checkpoint = harness.start_soak(resume=bool(args.resume))
        else:
            checkpoint = harness.collect()
        print(
            json.dumps(
                {
                    "campaign": str(store.path),
                    "verdict": checkpoint.get("verdict"),
                    "baseline": checkpoint.get("baseline", {}).get("status"),
                    "cycles_passed": sum(
                        1
                        for item in checkpoint.get("cycles", [])
                        if isinstance(item, dict) and item.get("status") == "passed"
                    ),
                    "cycles_target": checkpoint.get("target_cycles"),
                    "soak": checkpoint.get("soak", {}).get("status"),
                },
                indent=2,
            )
        )
        return 0
    except HardwareNotReady as exc:
        print(json.dumps({"verdict": "WAITING", "reason": str(exc)}, indent=2))
        return EXIT_HARDWARE_READY
    except KeyboardInterrupt:
        print(json.dumps({"verdict": "INTERRUPTED"}, indent=2))
        return 130
    except QualificationError as exc:
        print(json.dumps({"verdict": "FAIL", "reason": str(exc)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
