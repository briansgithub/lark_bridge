#!/usr/bin/env python3
"""Serve the phone's output picker over the existing paired Bluetooth link.

Android is the RFCOMM server because the sealed Pi image has no practical way to publish an
SDP service without a long-lived helper.  The Pi connects to the phone's service, receives one
JSON request per line, and answers on the same socket.  Selection is still owned by the Pi:
the phone never caches policy and every explicit choice goes through ``bridgectl --remember``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import bridge_supervisor as supervisor
import bridgectl
import btadapters
import outputs

SERVICE_UUID = "6e0e6e72-3f13-4f7e-9d3f-87b6f5a43c11"
MAX_LINE_BYTES = 64 * 1024
RETRY_SECONDS = 5.0
SCAN_VALID_SECONDS = 60.0
PAIR_TIMEOUT = 45.0
SERVICE_TIMEOUT = 15.0
CONNECT_TIMEOUT = 45.0
AUDIO_TIMEOUT = 10.0
SAVE_TIMEOUT = 3.0
MAX_ID_CHARS = 128
MAX_SCAN_ID_CHARS = 128
log = logging.getLogger("bridge-output-remote")


ERROR_TEXT = {
    "stale_result": "Scan results expired. Scan again.",
    "pairing_timeout": "Pairing did not finish. Put the speaker in pairing mode and try again.",
    "pin_not_supported": "This device requires a PIN or passkey; only automatic pairing is supported.",
    "not_audio_output": "This device does not advertise A2DP speaker audio.",
    "speaker_adapter_unavailable": "The dedicated speaker radio is unavailable.",
    "connection_failed": "The speaker could not establish an A2DP audio connection.",
    "persistence_failed": "Speaker setup was not selected because the saved choice could not be committed.",
}


class OperationCancelled(RuntimeError):
    """RFCOMM or service lifetime ended while a transaction was active."""


class TransactionFailure(RuntimeError):
    def __init__(self, code: str, phase: str, detail: str = "") -> None:
        super().__init__(detail)
        self.code = code
        self.phase = phase
        self.detail = detail


@dataclass
class ScanRecord:
    scan_id: str
    results: dict[str, dict[str, Any]]
    completed_monotonic: float
    valid_until_monotonic: float
    started_at_ms: int
    completed_at_ms: int
    valid_until_ms: int


@dataclass
class RemoteState:
    scan: ScanRecord | None = None
    mutex: threading.RLock = field(default_factory=threading.RLock)
    stopping: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class SelectionSnapshot:
    config_path: Path
    config_bytes: bytes
    desire_path: Path
    desire_bytes: bytes | None
    status_desired_id: str | None
    status_chosen_id: str | None


_STATE = RemoteState()
_active_socket: socket.socket | None = None


def parse_rfcomm_channel(text: str) -> int | None:
    marker = "Service Name: LarkBridge Output Control"
    if marker not in text:
        return None
    section = text.split(marker, 1)[1].split("Service Name:", 1)[0]
    if SERVICE_UUID not in section.lower():
        return None
    match = re.search(r"^\s*Channel:\s*(\d+)\s*$", section, re.MULTILINE)
    if not match:
        return None
    channel = int(match.group(1))
    return channel if 1 <= channel <= 30 else None


def discover_channel(phone: str) -> int | None:
    try:
        result = subprocess.run(
            ["sdptool", "browse", phone],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_rfcomm_channel(result.stdout) if result.returncode == 0 else None


def read_status(path: Path | None = None) -> dict[str, Any]:
    target = path or supervisor.default_status_path()
    return json.loads(target.read_text(encoding="utf-8"))


def public_output_state(status: dict[str, Any]) -> dict[str, Any]:
    block = status.get("output") or {}
    chosen = block.get("chosen") or {}
    candidates = []
    for candidate in block.get("candidates") or []:
        setup_state = str(candidate.get("setup_state") or "ready")
        candidates.append(
            {
                "id": candidate.get("id"),
                "label": candidate.get("label"),
                "kind": candidate.get("kind"),
                "available": bool(candidate.get("present")) and setup_state == "ready",
                "connected": bool(candidate.get("connected")) and setup_state == "ready",
                "setup_state": setup_state,
            }
        )
    return {
        "outputs": candidates,
        "desired_id": block.get("desired_id"),
        "chosen_id": chosen.get("id"),
        "reason": block.get("reason") or "",
        "call_active": bool((status.get("call") or {}).get("hfp_nodes_present")),
    }


def _new_error(
    request_id: Any,
    code: str,
    phase: str,
    detail: str | None = None,
) -> dict[str, Any]:
    error = ERROR_TEXT[code]
    if detail:
        error = f"{error} ({detail})"
    return {
        "id": request_id,
        "done": True,
        "ok": False,
        "error_code": code,
        "error": error,
        "phase": phase,
    }


def _valid_request_id(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return False
    return len(str(value)) <= MAX_ID_CHARS


def _a2dp_address(output_id: Any) -> str | None:
    if not isinstance(output_id, str) or len(output_id) != 22 or not output_id.startswith("a2dp:"):
        return None
    address = output_id[5:]
    canonical = btadapters.canonical_mac(address)
    return canonical if canonical == address else None


def _load_operation_settings(status: dict[str, Any]) -> supervisor.Settings:
    raw = status.get("config_path")
    return supervisor.load_settings(Path(str(raw))) if raw else supervisor.load_settings()


def _resolve_speaker_controller(
    status: dict[str, Any],
    *,
    cancelled: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[supervisor.Settings, btadapters.Adapter, dict[str, dict]]:
    """Resolve the permanent address afresh and reject the call-owning controller."""
    settings = _load_operation_settings(status)
    configured = settings.speaker_adapter
    canonical = btadapters.canonical_mac(configured)
    if canonical is None or canonical != configured:
        raise btadapters.BluetoothOperationError(
            "devices.output.adapter must be a canonical uppercase controller address"
        )
    tree = btadapters.managed_objects(cancelled=cancelled, heartbeat=heartbeat)
    adapter = btadapters.adapter_by_address(canonical, tree)
    if adapter is None or adapter.address != canonical:
        raise btadapters.BluetoothOperationError("configured controller is not visible to BlueZ")
    live_address = str(
        ((((tree.get(adapter.path) or {}).get("org.bluez.Adapter1") or {}).get("Address") or {}).get(
            "data"
        ))
        or ""
    ).upper()
    if live_address != canonical or not btadapters.is_powered(adapter, tree):
        raise btadapters.BluetoothOperationError("configured controller is missing or unpowered")
    if btadapters.is_blocked(adapter) is True:
        raise btadapters.BluetoothOperationError("configured controller is rfkilled")

    phone = btadapters.canonical_mac(settings.phone_mac)
    if phone is not None:
        phone_device = btadapters.device_properties(adapter, phone, tree)
        if bool((phone_device.get("Paired") or {}).get("data")) or bool(
            (phone_device.get("Connected") or {}).get("data")
        ):
            raise btadapters.BluetoothOperationError(
                "configured speaker controller owns the phone call bond"
            )
    return settings, adapter, tree


def _controller_matches(adapter: btadapters.Adapter, tree: dict[str, dict]) -> bool:
    address = str(
        ((((tree.get(adapter.path) or {}).get("org.bluez.Adapter1") or {}).get("Address") or {}).get(
            "data"
        ))
        or ""
    ).upper()
    return address == adapter.address and btadapters.is_powered(adapter, tree)


def _emit_progress(
    request_id: Any,
    phase: str,
    callback: Callable[[dict[str, Any]], None] | None,
    **fields: Any,
) -> None:
    if callback is None:
        return
    callback(
        {
            "id": request_id,
            "event": "progress",
            "done": False,
            "phase": phase,
            **fields,
        }
    )


def _cancelled(state: RemoteState, external: Callable[[], bool] | None) -> bool:
    return state.stopping.is_set() or (external is not None and external())


def _selection_snapshot(
    status: dict[str, Any], status_path: Path | None = None
) -> SelectionSnapshot:
    config_path = Path(str(status.get("config_path") or supervisor.default_config_path()))
    desire = (status_path or supervisor.default_status_path()).with_name("bridge-output.json")
    try:
        desire_bytes = desire.read_bytes()
    except FileNotFoundError:
        desire_bytes = None
    chosen = ((status.get("output") or {}).get("chosen") or {}).get("id")
    desired = (status.get("output") or {}).get("desired_id")
    return SelectionSnapshot(
        config_path=config_path,
        config_bytes=config_path.read_bytes(),
        desire_path=desire,
        desire_bytes=desire_bytes,
        status_desired_id=str(desired) if desired else None,
        status_chosen_id=str(chosen) if chosen else None,
    )


def _restore_desire(snapshot: SelectionSnapshot) -> tuple[bool, str]:
    temporary: Path | None = None
    try:
        if snapshot.desire_bytes is None:
            snapshot.desire_path.unlink(missing_ok=True)
            return True, "runtime desire removed"
        snapshot.desire_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=snapshot.desire_path.parent,
            prefix=".bridge-output-rollback-",
            delete=False,
        ) as handle:
            handle.write(snapshot.desire_bytes)
            handle.flush()
            temporary = Path(handle.name)
        temporary.replace(snapshot.desire_path)
        temporary = None
        return True, "runtime desire restored"
    except OSError as exc:
        return False, str(exc)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _restore_selection(
    snapshot: SelectionSnapshot,
    *,
    restore_config: bool,
    restore_desire: bool,
) -> tuple[bool, str]:
    failures: list[str] = []
    if restore_config:
        ok, detail = bridgectl.restore_startup_config(snapshot.config_bytes, snapshot.config_path)
        if not ok:
            failures.append(detail)
    if restore_desire:
        ok, detail = _restore_desire(snapshot)
        if not ok:
            failures.append(detail)
    return not failures, "; ".join(failures)


def _wait_for_rollback(
    snapshot: SelectionSnapshot,
    status_path: Path | None,
    *,
    timeout: float = SAVE_TIMEOUT,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = read_status(status_path)
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        block = status.get("output") or {}
        chosen = (block.get("chosen") or {}).get("id")
        if (
            block.get("desired_id") == snapshot.status_desired_id
            and chosen == snapshot.status_chosen_id
        ):
            return True
        time.sleep(0.1)
    return False


def _seal_pairing(
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[bool, str]:
    command = [
        "sudo",
        "-n",
        "python3",
        str(bridgectl.state_tool_path()),
        "pairing-seal",
        "--source",
        "/var/lib/bluetooth",
    ]
    try:
        result = runner(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, (result.stderr or result.stdout).strip() or f"exit {result.returncode}"


def _wait_status_choice(
    output_id: str,
    status_path: Path | None,
    *,
    timeout: float = SAVE_TIMEOUT,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled():
            raise OperationCancelled("request owner disconnected while saving")
        try:
            status = read_status(status_path)
        except (OSError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        block = status.get("output") or {}
        chosen = block.get("chosen") or {}
        if block.get("desired_id") == output_id and chosen.get("id") == output_id:
            return status
        time.sleep(0.1)
    return None


def _handle_scan(
    request_id: Any,
    status: dict[str, Any],
    *,
    state: RemoteState,
    status_path: Path | None,
    progress: Callable[[dict[str, Any]], None] | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    # Supersession is immediate. A failed/cancelled scan deliberately leaves no token.
    state.scan = None
    try:
        with btadapters.speaker_radio_lock(
            blocking=True,
            cancelled=lambda: _cancelled(state, cancelled),
        ) as acquired:
            if not acquired:  # blocking Linux acquisition should not return False
                raise btadapters.BluetoothOperationError("speaker radio lock unavailable")
            _settings, adapter, _tree = _resolve_speaker_controller(
                status,
                cancelled=lambda: _cancelled(state, cancelled),
                heartbeat=lambda: _emit_progress(
                    request_id,
                    "scanning",
                    progress,
                    elapsed_ms=0,
                    duration_ms=int(btadapters.DISCOVERY_SECONDS * 1000),
                ),
            )

            def scan_progress(elapsed_ms: int, duration_ms: int) -> None:
                if _cancelled(state, cancelled):
                    raise OperationCancelled("scan owner disconnected")
                _emit_progress(
                    request_id,
                    "scanning",
                    progress,
                    elapsed_ms=elapsed_ms,
                    duration_ms=duration_ms,
                )

            run = btadapters.discover_bredr(
                adapter,
                duration=btadapters.DISCOVERY_SECONDS,
                cancelled=lambda: _cancelled(state, cancelled),
                progress=scan_progress,
            )
            tree = btadapters.managed_objects()
            if str(
                ((((tree.get(adapter.path) or {}).get("org.bluez.Adapter1") or {}).get(
                    "Address"
                ) or {}).get("data"))
                or ""
            ).upper() != adapter.address:
                raise btadapters.BluetoothOperationError("speaker controller changed during scan")
            result_list = outputs.discovery_results(run.observations, tree, adapter)
    except (OperationCancelled, btadapters.BluetoothOperationCancelled):
        state.scan = None
        raise OperationCancelled("scan cancelled")
    except (OSError, ValueError, btadapters.BluetoothOperationError) as exc:
        state.scan = None
        return _new_error(request_id, "speaker_adapter_unavailable", "scanning", str(exc))

    completed_at_ms = int(time.time() * 1000)
    started_at_ms = completed_at_ms - int(btadapters.DISCOVERY_SECONDS * 1000)
    valid_until_ms = completed_at_ms + int(SCAN_VALID_SECONDS * 1000)
    scan_id = secrets.token_urlsafe(24)
    record = ScanRecord(
        scan_id=scan_id,
        results={item["output_id"]: item for item in result_list},
        completed_monotonic=run.completed_monotonic,
        valid_until_monotonic=run.completed_monotonic + SCAN_VALID_SECONDS,
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        valid_until_ms=valid_until_ms,
    )
    state.scan = record
    try:
        refreshed = read_status(status_path)
    except (OSError, json.JSONDecodeError):
        refreshed = status
    return {
        "id": request_id,
        "done": True,
        "ok": True,
        "scan_id": scan_id,
        "started_at_ms": started_at_ms,
        "completed_at_ms": completed_at_ms,
        "valid_until_ms": valid_until_ms,
        "duration_ms": int(btadapters.DISCOVERY_SECONDS * 1000),
        "results": result_list,
        **public_output_state(refreshed),
    }


def _device_has_a2dp(device: dict) -> bool:
    uuids = [
        str(value).lower() for value in ((device.get("UUIDs") or {}).get("data") or [])
    ]
    return btadapters.A2DP_SINK_UUID in uuids


def _wait_for_services(
    adapter: btadapters.Adapter,
    address: str,
    request_id: Any,
    progress: Callable[[dict[str, Any]], None] | None,
    cancelled: Callable[[], bool],
) -> dict | None:
    deadline = time.monotonic() + SERVICE_TIMEOUT
    next_progress = time.monotonic() + 1.0
    last: dict = {}
    while time.monotonic() < deadline:
        if cancelled():
            raise OperationCancelled("request owner disconnected during service resolution")
        tree = btadapters.managed_objects(
            cancelled=cancelled,
            heartbeat=lambda: _emit_progress(request_id, "resolving_services", progress),
        )
        if not _controller_matches(adapter, tree):
            raise btadapters.BluetoothOperationError(
                "speaker controller disappeared during service resolution"
            )
        last = btadapters.device_properties(adapter, address, tree)
        if _device_has_a2dp(last):
            return last
        if bool((last.get("ServicesResolved") or {}).get("data")):
            return None
        now = time.monotonic()
        if now >= next_progress:
            _emit_progress(request_id, "resolving_services", progress)
            next_progress = now + 1.0
        time.sleep(min(0.25, max(0.0, deadline - now)))
    return last if _device_has_a2dp(last) else None


def _wait_for_connected(
    adapter: btadapters.Adapter,
    address: str,
    deadline: float,
    request_id: Any,
    progress: Callable[[dict[str, Any]], None] | None,
    cancelled: Callable[[], bool],
) -> bool:
    next_progress = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if cancelled():
            raise OperationCancelled("request owner disconnected while connecting")
        tree = btadapters.managed_objects(
            cancelled=cancelled,
            heartbeat=lambda: _emit_progress(request_id, "connecting", progress),
        )
        if not _controller_matches(adapter, tree):
            raise btadapters.BluetoothOperationError(
                "speaker controller disappeared while connecting"
            )
        if btadapters.connected_on(adapter, address, tree):
            return True
        now = time.monotonic()
        if now >= next_progress:
            _emit_progress(request_id, "connecting", progress)
            next_progress = now + 1.0
        time.sleep(min(0.2, max(0.0, deadline - now)))
    return False


def _wait_for_audio(
    adapter: btadapters.Adapter,
    address: str,
    request_id: Any,
    progress: Callable[[dict[str, Any]], None] | None,
    cancelled: Callable[[], bool],
) -> str | None:
    deadline = time.monotonic() + AUDIO_TIMEOUT
    next_progress = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if cancelled():
            raise OperationCancelled("request owner disconnected while waiting for audio")
        tree = btadapters.managed_objects(
            cancelled=cancelled,
            heartbeat=lambda: _emit_progress(request_id, "waiting_for_audio", progress),
        )
        if not _controller_matches(adapter, tree):
            raise btadapters.BluetoothOperationError(
                "speaker controller disappeared while waiting for audio"
            )
        node = outputs.find_a2dp_node(supervisor.pw_nodes() or {}, address)
        if node:
            return node
        now = time.monotonic()
        if now >= next_progress:
            _emit_progress(request_id, "waiting_for_audio", progress)
            next_progress = now + 1.0
        time.sleep(min(0.2, max(0.0, deadline - now)))
    return None


def _rollback_unvalidated_bond(
    adapter: btadapters.Adapter | None,
    address: str | None,
    *,
    new_bond: bool,
    pair_attempted: bool,
    validated: bool,
) -> str:
    if adapter is None or address is None or not new_bond or not pair_attempted or validated:
        return ""
    ok, detail = btadapters.remove_device(address, adapter)
    return "" if ok else f"new-bond rollback failed: {detail}"


def _handle_pair_select(
    request: dict[str, Any],
    status: dict[str, Any],
    *,
    state: RemoteState,
    status_path: Path | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    progress: Callable[[dict[str, Any]], None] | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    request_id = request.get("id")
    output_id = request.get("output_id")
    address = _a2dp_address(output_id)
    if address is None:
        return _new_error(request_id, "stale_result", "validating")
    scan_id = request.get("scan_id")
    if scan_id is not None and (
        not isinstance(scan_id, str) or not scan_id or len(scan_id) > MAX_SCAN_ID_CHARS
    ):
        return _new_error(request_id, "stale_result", "validating")

    # The token's 60-second deadline is an admission deadline, not a transaction deadline.
    # Capture it before waiting for the cross-process radio lock; once accepted, a valid
    # request may finish after the scan token itself expires.
    accepted_record = state.scan
    accepted_scan_result = bool(
        accepted_record is not None
        and isinstance(scan_id, str)
        and scan_id == accepted_record.scan_id
        and time.monotonic() < accepted_record.valid_until_monotonic
        and output_id in accepted_record.results
    )

    adapter: btadapters.Adapter | None = None
    snapshot: SelectionSnapshot | None = None
    new_bond = False
    pair_attempted = False
    validated = False
    config_changed = False
    desire_attempted = False
    committed = False
    current_phase = "validating"
    label = f"Bluetooth device {address[-5:]}"

    def owner_gone() -> bool:
        return _cancelled(state, cancelled)

    def phase(name: str) -> None:
        nonlocal current_phase
        current_phase = name
        if owner_gone():
            raise OperationCancelled(f"request owner disconnected during {name}")
        _emit_progress(request_id, name, progress)

    try:
        phase("validating")
        with btadapters.speaker_radio_lock(
            blocking=True,
            cancelled=owner_gone,
        ) as acquired:
            if not acquired:
                raise TransactionFailure(
                    "speaker_adapter_unavailable", "validating", "speaker radio lock unavailable"
                )
            # Resolve the permanent address only after the transaction owns the radio.  A
            # controller can disappear and return with a different hciX while lock acquisition
            # waits; carrying a pre-lock object path into Pair/ConnectProfile would then target
            # stale controller state.
            try:
                status = read_status(status_path)
            except (OSError, json.JSONDecodeError) as exc:
                raise TransactionFailure(
                    "persistence_failed", "validating", f"bridge status unavailable: {exc}"
                ) from exc
            _settings, adapter, tree = _resolve_speaker_controller(
                status,
                cancelled=owner_gone,
                heartbeat=lambda: phase("validating"),
            )

            device = btadapters.device_properties(adapter, address, tree)
            already_ready = bool((device.get("Paired") or {}).get("data")) and _device_has_a2dp(
                device
            )
            if not already_ready and not accepted_scan_result:
                raise TransactionFailure("stale_result", "validating")

            if accepted_record is not None and output_id in accepted_record.results:
                label = str(accepted_record.results[output_id]["label"])
            else:
                shaped = outputs.discovery_results({address: None}, tree, adapter)
                if shaped:
                    label = str(shaped[0]["label"])

            try:
                snapshot = _selection_snapshot(status, status_path)
            except OSError as exc:
                raise TransactionFailure("persistence_failed", "validating", str(exc)) from exc
            new_bond = not bool((device.get("Paired") or {}).get("data"))

            if new_bond:
                phase("pairing")
                pair_attempted = True
                result = btadapters.pair_device(
                    address,
                    adapter,
                    timeout=PAIR_TIMEOUT,
                    cancelled=owner_gone,
                    heartbeat=lambda: phase("pairing"),
                )
                if not result.ok:
                    code = "pin_not_supported" if result.pin_requested else "pairing_timeout"
                    raise TransactionFailure(code, "pairing", result.detail)

            phase("resolving_services")
            resolved = _wait_for_services(
                adapter, address, request_id, progress, owner_gone
            )
            if resolved is None:
                raise TransactionFailure(
                    "not_audio_output", "resolving_services", "A2DP Sink UUID was absent"
                )
            validated = True

            phase("pinning_trust")
            pin = btadapters.pin_to_adapter(address, adapter)
            if not pin.ok:
                raise TransactionFailure(
                    "connection_failed", "pinning_trust", "; ".join(pin.failures)
                )

            phase("connecting")
            connect_deadline = time.monotonic() + CONNECT_TIMEOUT
            ok, detail = btadapters.connect_profile(
                address,
                adapter,
                btadapters.A2DP_SINK_UUID,
                timeout=CONNECT_TIMEOUT,
                cancelled=owner_gone,
                heartbeat=lambda: phase("connecting"),
            )
            if not ok or not _wait_for_connected(
                adapter,
                address,
                connect_deadline,
                request_id,
                progress,
                owner_gone,
            ):
                raise TransactionFailure("connection_failed", "connecting", detail)

            phase("waiting_for_audio")
            node = _wait_for_audio(adapter, address, request_id, progress, owner_gone)
            if node is None:
                raise TransactionFailure(
                    "connection_failed", "waiting_for_audio", "A2DP PipeWire node did not appear"
                )

            phase("saving")
            sealed, detail = _seal_pairing(runner)
            if not sealed:
                raise TransactionFailure("persistence_failed", "saving", detail)

            target = {
                "id": output_id,
                "kind": "a2dp",
                "label": label,
                "node": node,
                "present": True,
                "connected": True,
                "address": address,
                "adapter": adapter.hci,
                "adapter_address": adapter.address,
                "setup_state": "ready",
            }
            assert snapshot is not None
            saved, detail = bridgectl.remember_startup_output(target, snapshot.config_path)
            if not saved:
                raise TransactionFailure("persistence_failed", "saving", detail)
            config_changed = True

            desire_attempted = True
            try:
                supervisor.write_desire(
                    str(output_id), source="output_remote_pair_select", path=snapshot.desire_path
                )
            except OSError as exc:
                raise TransactionFailure("persistence_failed", "saving", str(exc)) from exc
            refreshed = _wait_status_choice(
                str(output_id), status_path, cancelled=owner_gone
            )
            if refreshed is None:
                raise TransactionFailure(
                    "persistence_failed", "saving", "supervisor did not confirm the new route"
                )
            committed = True
    except btadapters.BluetoothOperationCancelled as exc:
        rollback = _rollback_unvalidated_bond(
            adapter,
            address,
            new_bond=new_bond,
            pair_attempted=pair_attempted,
            validated=validated,
        )
        if snapshot is not None and (config_changed or desire_attempted) and not committed:
            restored, _detail = _restore_selection(
                snapshot,
                restore_config=config_changed,
                restore_desire=desire_attempted,
            )
            if restored:
                _wait_for_rollback(snapshot, status_path)
        raise OperationCancelled(str(exc) or rollback) from exc
    except OperationCancelled:
        _rollback_unvalidated_bond(
            adapter,
            address,
            new_bond=new_bond,
            pair_attempted=pair_attempted,
            validated=validated,
        )
        if snapshot is not None and (config_changed or desire_attempted) and not committed:
            restored, _detail = _restore_selection(
                snapshot,
                restore_config=config_changed,
                restore_desire=desire_attempted,
            )
            if restored:
                _wait_for_rollback(snapshot, status_path)
        raise
    except btadapters.BluetoothOperationError as exc:
        if current_phase == "pairing":
            code = "pairing_timeout"
        elif current_phase in {"pinning_trust", "connecting", "waiting_for_audio"}:
            code = "connection_failed"
        else:
            code = "speaker_adapter_unavailable"
        failure = TransactionFailure(code, current_phase, str(exc))
    except TransactionFailure as exc:
        failure = exc
    # Cleanup/rollback must still own an unexpected production failure at any phase.
    except Exception as exc:  # noqa: BLE001
        code = (
            "connection_failed"
            if current_phase in {"pinning_trust", "connecting", "waiting_for_audio"}
            else "persistence_failed"
            if current_phase == "saving"
            else "speaker_adapter_unavailable"
        )
        failure = TransactionFailure(code, current_phase, str(exc))

    if "failure" in locals():
        rollback_detail = _rollback_unvalidated_bond(
            adapter,
            address,
            new_bond=new_bond,
            pair_attempted=pair_attempted,
            validated=validated,
        )
        restore_detail = ""
        if snapshot is not None and (config_changed or desire_attempted) and not committed:
            restored, restore_detail = _restore_selection(
                snapshot,
                restore_config=config_changed,
                restore_desire=desire_attempted,
            )
            if not restored:
                failure = TransactionFailure(
                    "persistence_failed", "saving", restore_detail or failure.detail
                )
            elif not _wait_for_rollback(snapshot, status_path):
                restore_detail = "restored selection was not confirmed by the supervisor"
                failure = TransactionFailure("persistence_failed", "saving", restore_detail)
        detail = "; ".join(
            part for part in (failure.detail, rollback_detail, restore_detail) if part
        )
        return _new_error(request_id, failure.code, failure.phase, detail or None)

    assert committed
    assert refreshed is not None
    return {
        "id": request_id,
        "done": True,
        "ok": True,
        "accepted_id": output_id,
        "accepted_label": label,
        "setup_state": "ready",
        **public_output_state(refreshed),
    }


def handle_request(
    request: dict[str, Any],
    *,
    status_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    bridgectl_path: Path | None = None,
    state: RemoteState | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    active_state = state or _STATE
    request_id = request.get("id")
    if not _valid_request_id(request_id):
        return {"id": None, "ok": False, "error": "invalid request id"}
    op = request.get("op")
    base: dict[str, Any] = {"id": request_id}
    with active_state.mutex:
        try:
            status = read_status(status_path)
        except (OSError, json.JSONDecodeError) as exc:
            return {**base, "ok": False, "error": f"bridge status unavailable: {exc}"}

        if op in {"list", "status"}:
            return {**base, "ok": True, **public_output_state(status)}
        if op == "scan":
            return _handle_scan(
                request_id,
                status,
                state=active_state,
                status_path=status_path,
                progress=progress,
                cancelled=cancelled,
            )
        if op == "pair_select":
            return _handle_pair_select(
                request,
                status,
                state=active_state,
                status_path=status_path,
                runner=runner,
                progress=progress,
                cancelled=cancelled,
            )
        if op != "set":
            return {**base, "ok": False, "error": "unsupported operation"}

        output_id = str(request.get("output_id") or "")
        candidates = (status.get("output") or {}).get("candidates") or []
        target = next((item for item in candidates if item.get("id") == output_id), None)
        if target is None:
            return {**base, "ok": False, "error": "output is no longer listed"}
        if target.get("setup_state", "ready") != "ready":
            return {**base, "ok": False, "error": "speaker setup is required before selection"}

        command = [
            sys.executable,
            str(bridgectl_path or Path(__file__).with_name("bridgectl.py")),
            "output",
            "set",
            output_id,
            "--remember",
            "--no-chime",
        ]
        try:
            result = runner(
                command,
                capture_output=True,
                text=True,
                timeout=35,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {**base, "ok": False, "error": f"selection failed: {exc}"}
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            return {**base, "ok": False, "error": detail}

        # The supervisor applies the desire asynchronously. Wait briefly so the first response
        # the phone renders already distinguishes the request from a wired fallback.
        refreshed = status
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                refreshed = read_status(status_path)
            except (OSError, json.JSONDecodeError):
                break
            if refreshed.get("output", {}).get("desired_id") == output_id:
                break
            time.sleep(0.1)
        return {
            **base,
            "ok": True,
            "accepted_id": output_id,
            "accepted_label": target.get("label"),
            "message": (result.stderr or result.stdout).strip(),
            **public_output_state(refreshed),
        }


def serve_connection(
    sock: socket.socket,
    status_path: Path | None = None,
    *,
    state: RemoteState | None = None,
) -> None:
    active_state = state or _STATE
    disconnected = threading.Event()
    stream = sock.makefile("rwb", buffering=0)

    def write_object(response: dict[str, Any]) -> None:
        try:
            stream.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        except OSError as exc:
            disconnected.set()
            raise OperationCancelled("RFCOMM stream was lost") from exc

    while True:
        line = stream.readline(MAX_LINE_BYTES + 1)
        if not line:
            disconnected.set()
            return
        close_after_response = len(line) > MAX_LINE_BYTES or not line.endswith(b"\n")
        if close_after_response:
            response = {"id": None, "ok": False, "error": "request is too large"}
        else:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise TypeError("request must be an object")
                response = handle_request(
                    request,
                    status_path=status_path,
                    state=active_state,
                    progress=write_object,
                    cancelled=disconnected.is_set,
                )
            except OperationCancelled:
                disconnected.set()
                return
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                response = {"id": None, "ok": False, "error": str(exc)}
        try:
            write_object(response)
        except OperationCancelled:
            return
        if close_after_response:
            # ``readline(limit)`` leaves the rest of an oversized physical line buffered.
            # Continuing would let that suffix masquerade as a second JSON command.
            disconnected.set()
            return


def run(
    phone: str,
    status_path: Path | None = None,
    *,
    state: RemoteState | None = None,
) -> None:
    global _active_socket
    active_state = state or _STATE
    while not active_state.stopping.is_set():
        channel = discover_channel(phone)
        if channel is None:
            active_state.stopping.wait(RETRY_SECONDS)
            continue
        try:
            with socket.socket(
                socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM
            ) as client:
                _active_socket = client
                client.settimeout(15)
                client.connect((phone, channel))
                client.settimeout(None)
                log.info("phone output control connected on RFCOMM channel %s", channel)
                serve_connection(client, status_path, state=active_state)
        except OSError as exc:
            log.info("phone output control disconnected: %s", exc)
        finally:
            _active_socket = None
        active_state.stopping.wait(RETRY_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone", help="override the configured phone Bluetooth address")
    parser.add_argument("--status", type=Path, help="override bridge-status.json (tests)")
    args = parser.parse_args(argv)
    settings = supervisor.load_settings()
    phone = (args.phone or settings.phone_mac).strip().upper()
    if btadapters.canonical_mac(phone) != phone:
        raise SystemExit("no phone Bluetooth address configured")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = RemoteState()

    def stop(_signum: int, _frame: Any) -> None:
        state.stopping.set()
        if _active_socket is not None:
            try:
                _active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            _active_socket.close()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run(phone, args.status, state=state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
