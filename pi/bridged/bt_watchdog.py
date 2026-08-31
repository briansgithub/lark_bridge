#!/usr/bin/env python3
"""Strict USB call-controller liveness and recovery watchdog.

The process owns exactly the configured call role. Permanent address, bus, and USB
VID:PID are resolved afresh before every action; ``hciX`` and USB topology are runtime targets
only. Automatic recovery never uses class-wide rfkill or restarts BlueZ, PipeWire, or
WirePlumber.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

import btadapters
import controller_roles

# ``output`` remains an accepted CLI spelling only so a stale
# ``bridge-btwatchdog@output`` instance receives the typed
# ``controller_role_not_configured`` diagnostic instead of an argparse error.  This
# deployment owns one radio role and never starts an output watchdog.
CLI_ROLE_NAMES = ("call", "output")
ACTIVE_ROLE_NAMES = ("call",)
REPO = Path(os.environ.get("BRIDGE_REPO", "/home/admin/rpi-lark-bridge"))
CONFIG_PATH = Path(os.environ.get("BRIDGE_CONFIG", REPO / "config" / "bridge.toml"))
STATE_DIR = Path(os.environ.get("BRIDGE_WD_STATE_DIR", "/run/larkbridge/bt-watchdog"))
LOCK_DIR = Path(os.environ.get("BRIDGE_WD_LOCK_DIR", "/run/lock/larkbridge"))
SYS_RFKILL = Path("/sys/class/rfkill")
SYS_USB_DRIVERS = Path("/sys/bus/usb/drivers")
USB_INTERFACE_RE = re.compile(r"^[A-Za-z0-9._-]+:\d+\.\d+$")

PROBE_INTERVAL = float(os.environ.get("BRIDGE_WD_INTERVAL", "15"))
FAILURES_TO_ACT = int(os.environ.get("BRIDGE_WD_FAILURES", "2"))
PROBE_TIMEOUT = float(os.environ.get("BRIDGE_WD_PROBE_TIMEOUT", "20"))
RECONNECT_DELAY = float(os.environ.get("BRIDGE_WD_RECONNECT_DELAY", "20"))
RECONNECT_RETRY = float(os.environ.get("BRIDGE_WD_RECONNECT_RETRY", "30"))
STARTUP_RECONNECT_RETRY = float(
    os.environ.get("BRIDGE_WD_STARTUP_RECONNECT_RETRY", "2")
)
CALL_RECONNECT_ATTEMPTS = int(os.environ.get("BRIDGE_WD_CALL_ATTEMPTS", "3"))
CALL_RECONNECT_COOLDOWN = float(os.environ.get("BRIDGE_WD_CALL_COOLDOWN", "120"))
CONNECT_REQUEST_TIMEOUT = float(
    os.environ.get("BRIDGE_WD_CONNECT_REQUEST_TIMEOUT", "8")
)
CONNECT_PENDING_TIMEOUT = float(
    os.environ.get("BRIDGE_WD_CONNECT_PENDING_TIMEOUT", "12")
)
PAIRING_WINDOW_SECONDS = float(os.environ.get("BRIDGE_WD_PAIRING_WINDOW", "120"))
POST_CANCEL_RETRY_DELAY = float(os.environ.get("BRIDGE_WD_POST_CANCEL_RETRY", "1"))
PAIRING_SEAL_TIMER = "bridge-pairing-seal.timer"
PAIRING_SEAL_SERVICE = "bridge-pairing-seal.service"
PAIRING_SEAL_ATTEMPTS = 3
PAIRING_SEAL_RETRY_DELAY = 1.0
PAIRING_SEAL_COMMAND = Path(
    os.environ.get(
        "BRIDGE_WD_PAIRING_SEAL_COMMAND",
        "/usr/local/lib/rpi-lark-bridge/powerloss/lark_state.py",
    )
)
BACKOFF_START = 60.0
BACKOFF_MAX = 900.0

log = logging.getLogger("bt-watchdog")


class ProbeStatus(str, Enum):
    ANSWERED = "answered"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RecoveryCancelled(RuntimeError):
    """Shutdown was requested before the next recovery mutation."""


@dataclass(frozen=True)
class RecoveryResult:
    ok: bool
    action: str
    detail: str = ""


@dataclass
class RecoveryState:
    role: str
    failures: int = 0
    recoveries: int = 0
    reconnect_attempts: int = 0
    reconnect_next: float = 0.0
    connected_monotonic: float | None = None
    connect_pending_since: float | None = None
    connect_pending_target: str | None = None
    connect_collision_cancellations: int = 0
    stale_connect_signatures: int = 0
    bond_state: str = "unknown"
    repair_state: str = "idle"
    repair_trigger: str | None = None
    repair_deadline: float | None = None
    pairing_timer_paused: bool = False
    last_attempt: float = 0.0
    last_recovery: float = 0.0
    backoff: float = BACKOFF_START
    probe: str = ProbeStatus.UNKNOWN.value
    identity_error: str | None = None
    last_action: str | None = None
    last_error: str | None = None

    def as_dict(self, adapter: btadapters.Adapter | None = None) -> dict[str, Any]:
        return {
            "timestamp": time.time(),
            "role": self.role,
            "configured_state": "single-call",
            "probe": self.probe,
            "failures": self.failures,
            "recoveries": self.recoveries,
            "reconnect_attempts": self.reconnect_attempts,
            "reconnect_next_monotonic": self.reconnect_next,
            "connected_monotonic": self.connected_monotonic,
            "connect_pending_since_monotonic": self.connect_pending_since,
            "connect_pending_deadline_monotonic": (
                self.connect_pending_since + CONNECT_PENDING_TIMEOUT
                if self.connect_pending_since is not None
                else None
            ),
            "connect_pending_target": self.connect_pending_target,
            "connect_collision_cancellations": self.connect_collision_cancellations,
            "stale_connect_signatures": self.stale_connect_signatures,
            "bond_state": self.bond_state,
            "repair_state": self.repair_state,
            "repair_trigger": self.repair_trigger,
            "repair_deadline_monotonic": self.repair_deadline,
            "pairing_timer_paused": self.pairing_timer_paused,
            "last_attempt_monotonic": self.last_attempt,
            "last_recovery_monotonic": self.last_recovery,
            "backoff_seconds": self.backoff,
            "identity_error": self.identity_error,
            "last_action": self.last_action,
            "last_error": self.last_error,
            "controller": (
                {
                    "address": adapter.address,
                    "hci": adapter.hci,
                    "bus": adapter.bus,
                    "usb_id": (
                        f"{adapter.usb_vendor_id}:{adapter.usb_product_id}"
                        if adapter.usb_vendor_id and adapter.usb_product_id
                        else None
                    ),
                    "usb_interface": adapter.usb_interface,
                    "driver": adapter.driver,
                    "rfkill_index": adapter.rfkill_index,
                }
                if adapter is not None
                else None
            ),
        }


def role_lock_path(role: str) -> Path:
    if role not in ACTIVE_ROLE_NAMES:
        raise ValueError(f"invalid controller role: {role}")
    return LOCK_DIR / f"bridge-btwatchdog-{role}.lock"


def role_state_path(role: str) -> Path:
    if role not in ACTIVE_ROLE_NAMES:
        raise ValueError(f"invalid controller role: {role}")
    return STATE_DIR / f"{role}.json"


def write_state(state: RecoveryState, adapter: btadapters.Adapter | None) -> None:
    target = role_state_path(state.role)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o755)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=f".{state.role}-", delete=False
    ) as handle:
        json.dump(state.as_dict(adapter), handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, target)
    os.chmod(target, 0o644)


def role_spec(
    roles: controller_roles.ControllerRoles, role: str
) -> controller_roles.ControllerSpec:
    if role == "call":
        return controller_roles.role_spec(roles, role)
    if role == "output":
        # Ask the shared parser first so the normal bluetooth-wired configuration emits
        # its stable typed error.  Even if a stale Bluetooth-output configuration is
        # supplied, this single-radio watchdog must not start managing that controller.
        controller_roles.role_spec(roles, role)
        raise controller_roles.ControllerRoleNotConfiguredError(
            role, "the BT500+AUX deployment has no output-controller watchdog"
        )
    raise ValueError(f"invalid controller role: {role}")


def resolve_role(
    roles: controller_roles.ControllerRoles,
    role: str,
    *,
    objects: dict[str, dict] | None = None,
    inventory: list[btadapters.Adapter] | None = None,
) -> btadapters.Adapter:
    tree = objects if objects is not None else btadapters.managed_objects()
    observed = inventory if inventory is not None else btadapters.adapters(tree)
    resolved = controller_roles.resolve_controller(
        role_spec(roles, role),
        cast(Sequence[controller_roles.AdapterView], observed),
        policy=controller_roles.ReadinessPolicy.TRANSITIONAL,
    )
    return cast(btadapters.Adapter, resolved)


def probe_controller(adapter: btadapters.Adapter) -> ProbeStatus:
    """Issue one active HCI round trip against the already-resolved controller."""
    try:
        result = subprocess.run(
            ["hciconfig", adapter.hci, "version"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeStatus.FAILED
    except OSError as exc:
        log.error("%s probe unavailable: %s", adapter.address, exc)
        return ProbeStatus.UNKNOWN
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 or "Connection timed out" in output:
        return ProbeStatus.FAILED
    return (
        ProbeStatus.ANSWERED if "HCI Version" in result.stdout else ProbeStatus.FAILED
    )


def wait_for_role_answer(
    roles: controller_roles.ControllerRoles, role: str, seconds: float = 8.0
) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            adapter = resolve_role(roles, role)
        except controller_roles.ControllerRoleError:
            time.sleep(0.25)
            continue
        if probe_controller(adapter) is ProbeStatus.ANSWERED:
            return True
        time.sleep(0.25)
    return False


def recovery_device(roles: controller_roles.ControllerRoles, role: str) -> str | None:
    role_spec(roles, role)
    return roles.phone_address


def _command_ok(command: list[str], timeout: float = 20.0) -> bool:
    try:
        return (
            subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _before_mutation(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise RecoveryCancelled("watchdog shutdown requested")


def _settle(seconds: float, cancelled: Callable[[], bool] | None) -> None:
    if cancelled is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _before_mutation(cancelled)
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def cycle_hci(
    adapter: btadapters.Adapter,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    _before_mutation(cancelled)
    down = _command_ok(["hciconfig", adapter.hci, "down"])
    _settle(1, cancelled)
    _before_mutation(cancelled)
    up = _command_ok(["hciconfig", adapter.hci, "up"])
    _settle(2, cancelled)
    return down and up


def cycle_rfkill(
    adapter: btadapters.Adapter,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Cycle one rfkill index; a device class is never an accepted target."""
    if adapter.rfkill_index is None:
        return False
    index = str(adapter.rfkill_index)
    _before_mutation(cancelled)
    blocked = _command_ok(["rfkill", "block", index])
    _settle(1, cancelled)
    _before_mutation(cancelled)
    unblocked = _command_ok(["rfkill", "unblock", index])
    if blocked and unblocked:
        _settle(2, cancelled)
        return True
    soft = SYS_RFKILL / f"rfkill{index}" / "soft"
    try:
        _before_mutation(cancelled)
        soft.write_text("1", encoding="utf-8")
        _settle(1, cancelled)
        _before_mutation(cancelled)
        soft.write_text("0", encoding="utf-8")
        _settle(2, cancelled)
        return True
    except OSError:
        return False


def rebind_usb(
    adapter: btadapters.Adapter,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Unbind/rebind exactly the currently resolved USB interface."""
    interface = adapter.usb_interface or ""
    if (
        adapter.bus != "USB"
        or adapter.driver != "btusb"
        or USB_INTERFACE_RE.fullmatch(interface) is None
    ):
        return False
    driver = SYS_USB_DRIVERS / "btusb"
    try:
        _before_mutation(cancelled)
        (driver / "unbind").write_text(interface + "\n", encoding="utf-8")
        _settle(2, cancelled)
        _before_mutation(cancelled)
        (driver / "bind").write_text(interface + "\n", encoding="utf-8")
        _settle(5, cancelled)
        return True
    except OSError:
        return False


def targeted_recovery(
    roles: controller_roles.ControllerRoles,
    role: str,
    *,
    verify: Callable[[], bool] | None = None,
    refresh: Callable[[], btadapters.Adapter] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RecoveryResult:
    """Run only exact-target recovery rungs and stop at the first answered probe."""
    check = verify or (lambda: wait_for_role_answer(roles, role))
    current = refresh or (lambda: resolve_role(roles, role))

    try:
        _before_mutation(cancelled)
        adapter = current()
        _before_mutation(cancelled)
        powered, detail = btadapters.power_on(adapter, cancelled=cancelled)
        if powered and check():
            return RecoveryResult(True, "power-on", detail)

        device = recovery_device(roles, role)
        if device is not None:
            _before_mutation(cancelled)
            btadapters.disconnect(device, adapter, cancelled=cancelled)
            _settle(1, cancelled)
            if check():
                return RecoveryResult(True, "device-disconnect", device)

        _before_mutation(cancelled)
        adapter = current()
        if cycle_hci(adapter, cancelled=cancelled) and check():
            return RecoveryResult(True, "hci-down-up", adapter.hci)

        _before_mutation(cancelled)
        adapter = current()
        if cycle_rfkill(adapter, cancelled=cancelled) and check():
            return RecoveryResult(True, "rfkill-index", str(adapter.rfkill_index))

        _before_mutation(cancelled)
        adapter = current()
        if rebind_usb(adapter, cancelled=cancelled) and check():
            return RecoveryResult(
                True, "usb-interface-rebind", adapter.usb_interface or ""
            )
    except controller_roles.ControllerRoleError as exc:
        return RecoveryResult(False, "resolve", f"{exc.code}: {exc.detail}")
    except (RecoveryCancelled, btadapters.BluetoothOperationCancelled) as exc:
        return RecoveryResult(False, "cancelled", str(exc))
    return RecoveryResult(False, "exhausted", "all exact-target recovery rungs failed")


def attempt_recovery(
    state: RecoveryState,
    recover: Callable[[], RecoveryResult],
    *,
    now: float | None = None,
) -> bool:
    observed = time.monotonic() if now is None else now
    if state.failures < FAILURES_TO_ACT:
        return False
    if state.last_attempt and observed - state.last_attempt < state.backoff:
        state.last_action = "backoff"
        return False
    # A controller-level mutation invalidates any Device1 operation observed before it.
    state.connect_pending_since = None
    state.connect_pending_target = None
    state.connect_collision_cancellations = 0
    result = recover()
    state.last_action = result.action
    state.last_error = None if result.ok else result.detail
    if not result.ok:
        return False
    state.last_attempt = observed
    state.backoff = min(state.backoff * 2, BACKOFF_MAX)
    state.failures = 0
    state.recoveries += 1
    state.last_recovery = observed
    state.reconnect_attempts = 0
    state.reconnect_next = observed + RECONNECT_DELAY
    return True


def record_failure_and_recover(
    roles: controller_roles.ControllerRoles,
    role: str,
    state: RecoveryState,
    *,
    cancelled: Callable[[], bool] | None = None,
    recover: Callable[[], RecoveryResult] | None = None,
) -> bool:
    """Spend one failure only while the call role's destructive ladder is allowed."""
    operation = recover or (lambda: targeted_recovery(roles, role, cancelled=cancelled))
    try:
        _before_mutation(cancelled)
        role_spec(roles, role)
    except (RecoveryCancelled, controller_roles.ControllerRoleError) as exc:
        state.last_action = (
            "cancelled" if isinstance(exc, RecoveryCancelled) else "resolve"
        )
        state.last_error = (
            str(exc)
            if isinstance(exc, RecoveryCancelled)
            else f"{exc.code}: {exc.detail}"
        )
        return False
    state.failures += 1
    return attempt_recovery(state, operation)


def _adapter_runtime_target(adapter: btadapters.Adapter) -> str:
    """Describe one resolved runtime target without turning topology into identity."""
    return "|".join(
        (
            adapter.address,
            adapter.hci,
            adapter.usb_parent or "",
            adapter.usb_interface or "",
            str(adapter.rfkill_index) if adapter.rfkill_index is not None else "",
        )
    )


def _clear_pending_connect(state: RecoveryState) -> None:
    state.connect_pending_since = None
    state.connect_pending_target = None


def _clear_connect_collision(state: RecoveryState) -> None:
    _clear_pending_connect(state)
    state.connect_collision_cancellations = 0


def _mark_device_connected(
    state: RecoveryState,
    *,
    observed: float | None = None,
    record_time: bool = True,
) -> None:
    if record_time:
        state.connected_monotonic = time.monotonic() if observed is None else observed
    state.reconnect_attempts = 0
    state.reconnect_next = 0.0
    _clear_connect_collision(state)
    state.stale_connect_signatures = 0
    state.bond_state = "connected"
    state.repair_state = "idle"
    state.repair_trigger = None
    state.repair_deadline = None
    state.identity_error = None
    state.last_action = "connected"
    state.last_error = None


def _disconnect_quiesced(ok: bool, detail: str) -> bool:
    if ok:
        return True
    normalized = detail.casefold()
    return (
        "org.bluez.error.notconnected" in normalized
        or "not connected" in normalized
        or "org.freedesktop.dbus.error.unknownobject" in normalized
        or "unknown object" in normalized
    )


def _connect_operation_pending(ok: bool, detail: str) -> bool:
    return btadapters.connect_in_progress(ok, detail) or (
        not ok and detail.strip().casefold().endswith("timed out")
    )


def service_reconnect(
    roles: controller_roles.ControllerRoles,
    role: str,
    state: RecoveryState,
    *,
    now: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Reconnect only the device assigned to this process's controller role."""
    observed = time.monotonic() if now is None else now
    address = recovery_device(roles, role)
    if address is None:
        state.reconnect_attempts = 0
        _clear_connect_collision(state)
        return False
    try:
        _before_mutation(cancelled)
        tree = btadapters.managed_objects(cancelled=cancelled)
        adapter = resolve_role(roles, role, objects=tree)
    except (RecoveryCancelled, btadapters.BluetoothOperationCancelled) as exc:
        state.last_action = "cancelled"
        state.last_error = str(exc)
        return False
    except controller_roles.ControllerRoleError as exc:
        _clear_pending_connect(state)
        state.reconnect_next = observed + RECONNECT_DELAY
        state.identity_error = f"{exc.code}: {exc.detail}"
        state.last_action = "resolve"
        state.last_error = state.identity_error
        return False
    previously_connected = state.bond_state == "connected"
    properties = btadapters.device_properties(adapter, address, tree)
    if not properties or not btadapters.paired_on(adapter, address, tree):
        state.bond_state = "missing"
        if state.repair_state not in {
            "requested",
            "pairing_window",
            "pairing_required",
        }:
            state.repair_state = "pairing_required"
            state.repair_trigger = None
            state.last_action = "pairing_required"
            state.last_error = f"no paired Pixel object on {adapter.hci}"
        return False
    if not btadapters.bonded_on(adapter, address, tree):
        state.bond_state = "unbonded"
        if state.repair_state not in {
            "requested",
            "pairing_window",
            "pairing_required",
        }:
            state.repair_state = "pairing_required"
            state.repair_trigger = None
            state.last_action = "pairing_required"
            state.last_error = f"Pixel object on {adapter.hci} is not bonded"
        return False
    trusted = btadapters.trusted_on(adapter, address, tree)
    state.bond_state = "trusted" if trusted else "untrusted"
    if state.repair_state in {"requested", "pairing_window", "pairing_required"}:
        return False
    if not trusted:
        try:
            _before_mutation(cancelled)
            trusted, detail = btadapters.set_trusted(
                address, True, adapter, cancelled=cancelled
            )
        except (RecoveryCancelled, btadapters.BluetoothOperationCancelled) as exc:
            state.last_action = "cancelled"
            state.last_error = str(exc)
            return False
        if not trusted:
            state.last_action = "trust_failed"
            state.last_error = detail
            state.reconnect_next = observed + RECONNECT_DELAY
            return False
        state.bond_state = "trusted"
    if btadapters.connected_on(adapter, address, tree):
        _mark_device_connected(
            state,
            observed=observed,
            record_time=not previously_connected,
        )
        return True

    if state.connect_pending_since is not None:
        pending_deadline = state.connect_pending_since + CONNECT_PENDING_TIMEOUT
        if observed < pending_deadline:
            state.reconnect_next = pending_deadline
            state.last_action = "device-connect-pending"
            return False

        # The operation did not finish inside BlueZ's normal Connect window. The object tree and
        # adapter above were refreshed on this tick; validate them before the exact cancellation.
        current_target = _adapter_runtime_target(adapter)
        if current_target != state.connect_pending_target:
            log.info(
                "pending Connect target changed from %s to %s; settling without cancellation",
                state.connect_pending_target,
                current_target,
            )
            _clear_connect_collision(state)
            state.reconnect_next = observed + RECONNECT_DELAY
            state.last_action = "device-connect-target-refreshed"
            state.last_error = None
            return False

        device_path = btadapters.path_for(adapter, address)
        if "org.bluez.Device1" not in (tree.get(device_path) or {}):
            log.info(
                "pending Connect object %s disappeared; settling before retry",
                device_path,
            )
            _clear_connect_collision(state)
            state.reconnect_next = observed + RECONNECT_DELAY
            state.last_action = "device-connect-object-refreshed"
            state.last_error = (
                f"{device_path}: Device1 disappeared while Connect was pending"
            )
            return False

        try:
            _before_mutation(cancelled)
            cancel_ok, cancel_detail = btadapters.disconnect(
                address, adapter, cancelled=cancelled
            )
        except (RecoveryCancelled, btadapters.BluetoothOperationCancelled) as exc:
            state.last_action = "cancelled"
            state.last_error = str(exc)
            return False

        _clear_pending_connect(state)
        state.connect_collision_cancellations += 1
        if _disconnect_quiesced(cancel_ok, cancel_detail):
            log.warning("cancelled stale Connect on %s before retry", device_path)
            state.reconnect_next = observed + POST_CANCEL_RETRY_DELAY
            state.last_action = "device-connect-cancelled"
            state.last_error = None
        else:
            # Cancellation is uncertain, so do not issue another Connect in this burst.
            state.reconnect_attempts = CALL_RECONNECT_ATTEMPTS
            state.reconnect_next = observed + CALL_RECONNECT_COOLDOWN
            state.last_action = "device-connect-cancel-failed"
            state.last_error = cancel_detail
        return False

    if observed < state.reconnect_next:
        return False
    # A reset phone may decline every connection attempt while its cellular call remains
    # active.  A configured number of failures bounds one retry burst; they must not become a
    # lifetime ceiling that leaves a healthy, re-enumerated controller permanently disconnected.
    # Once the conservative cooldown has elapsed, begin a fresh burst.  Both controller
    # resolutions below still run before the next exact-device mutation.
    new_burst = state.reconnect_attempts >= CALL_RECONNECT_ATTEMPTS
    try:
        _before_mutation(cancelled)
        adapter = resolve_role(roles, role)
    except RecoveryCancelled as exc:
        state.last_action = "cancelled"
        state.last_error = str(exc)
        return False
    except controller_roles.ControllerRoleError as exc:
        _clear_pending_connect(state)
        state.reconnect_next = observed + RECONNECT_DELAY
        state.identity_error = f"{exc.code}: {exc.detail}"
        return False
    if new_burst:
        # Renew cancellation authority only after the exact controller resolves successfully.
        state.reconnect_attempts = 0
        state.connect_collision_cancellations = 0
    try:
        _before_mutation(cancelled)
        state.reconnect_attempts += 1
        retry_delay = (
            CALL_RECONNECT_COOLDOWN
            if state.reconnect_attempts >= CALL_RECONNECT_ATTEMPTS
            else RECONNECT_RETRY
        )
        state.reconnect_next = observed + retry_delay
        powered, detail = btadapters.power_on(adapter, cancelled=cancelled)
        if not powered:
            state.last_error = detail
            return False
        _before_mutation(cancelled)
        state.last_action = "connecting"
        ok, detail = btadapters.connect(
            address,
            adapter,
            timeout=CONNECT_REQUEST_TIMEOUT,
            cancelled=cancelled,
        )
    except (RecoveryCancelled, btadapters.BluetoothOperationCancelled) as exc:
        state.last_action = "cancelled"
        state.last_error = str(exc)
        return False
    if _connect_operation_pending(ok, detail):
        if state.connect_collision_cancellations == 0:
            # This call did not start another operation, so it does not spend a reconnect attempt.
            pending_started = time.monotonic() if now is None else observed
            state.reconnect_attempts = max(0, state.reconnect_attempts - 1)
            state.connect_pending_since = pending_started
            state.connect_pending_target = _adapter_runtime_target(adapter)
            state.reconnect_next = pending_started + CONNECT_PENDING_TIMEOUT
            state.last_action = "device-connect-pending"
            state.last_error = detail
            log.warning(
                "Connect already in progress on %s; observing for %.0f seconds",
                state.connect_pending_target,
                CONNECT_PENDING_TIMEOUT,
            )
            return False
        if btadapters.connect_in_progress(ok, detail):
            state.stale_connect_signatures += 1
            state.last_action = "stale_bond_suspected"
            state.last_error = detail
            state.repair_state = "requested"
            state.repair_trigger = "repeated-in-progress"
            state.reconnect_next = 0.0
            log.error("stale Pixel bond signature detected on %s", adapter.hci)
            return False
        # A request timeout after one exact cancellation is not proof of a bad key.  A
        # sleeping or out-of-range phone follows the normal bounded retry policy.
        state.last_action = "device-reconnect"
        state.last_error = detail
        state.reconnect_next = observed + RECONNECT_RETRY
        return False

    state.last_action = "device-reconnect"
    state.last_error = None if ok else detail
    if ok:
        _mark_device_connected(state, observed=observed)
        return True
    return False


def service_startup_reconnect(
    roles: controller_roles.ControllerRoles,
    role: str,
    state: RecoveryState,
    adapter: btadapters.Adapter,
    previous_target: str | None,
    *,
    now: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Issue one immediate reconnect when an exact runtime controller first resolves."""
    target = _adapter_runtime_target(adapter)
    if target != previous_target:
        state.reconnect_next = 0.0
        service_reconnect(roles, role, state, now=now, cancelled=cancelled)
        if (
            state.bond_state != "connected"
            and state.repair_state == "idle"
            and state.connect_pending_since is None
        ):
            observed = time.monotonic() if now is None else now
            state.reconnect_next = observed + STARTUP_RECONNECT_RETRY
    return target


def service_sleep_interval(state: RecoveryState, *, now: float | None = None) -> float:
    """Wake for a pending reconnect deadline without accelerating idle polling."""
    observed = time.monotonic() if now is None else now
    if state.bond_state != "connected" and state.reconnect_next > observed:
        return max(0.1, min(PROBE_INTERVAL, state.reconnect_next - observed))
    return PROBE_INTERVAL


def _set_production_pairing_closed(
    adapter: btadapters.Adapter,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    failures: list[str] = []
    for name in ("Discoverable", "Pairable"):
        ok, detail = btadapters.set_adapter_property(
            adapter, name, False, cancelled=cancelled
        )
        if not ok:
            failures.append(detail)
    return not failures, "; ".join(failures) if failures else "pairing closed"


def _pairing_seal() -> tuple[bool, str]:
    command = ["python3", str(PAIRING_SEAL_COMMAND), "pairing-seal"]
    detail = "pairing seal did not run"
    for attempt in range(1, PAIRING_SEAL_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            detail = (
                result.stdout.strip()
                or result.stderr.strip()
                or f"exit {result.returncode}"
            )
            if result.returncode == 0:
                return True, detail
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = str(exc)
        if attempt < PAIRING_SEAL_ATTEMPTS:
            time.sleep(PAIRING_SEAL_RETRY_DELAY)
    return False, detail


def _pairing_timer(action: str) -> tuple[bool, str]:
    if action not in {"start", "stop"}:
        raise ValueError(f"invalid timer action: {action}")
    try:
        units = (
            [PAIRING_SEAL_TIMER, PAIRING_SEAL_SERVICE]
            if action == "stop"
            else [PAIRING_SEAL_TIMER]
        )
        result = subprocess.run(
            ["systemctl", action, *units],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (
        result.stderr.strip()
        or result.stdout.strip()
        or f"{PAIRING_SEAL_TIMER} {action}ed"
    )
    return result.returncode == 0, detail


def repair_phone_bond(
    roles: controller_roles.ControllerRoles,
    state: RecoveryState,
    adapter: btadapters.Adapter,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """Replace only the configured Pixel bond in one recoverable transaction."""
    address = roles.phone_address
    trigger = state.repair_trigger or "manual"
    state.last_action = (
        "stale_bond_suspected" if trigger != "manual" else "pairing_required"
    )
    state.repair_state = "preparing"
    state.last_error = None
    write_state(state, adapter)

    # A timed-out window deliberately leaves the timer paused and the live tree bondless.
    # Reopening it -- including after a watchdog-only restart -- must retain the prior
    # rollback point instead of promoting the temporary bondless tree to newest-good state.
    rollback_already_sealed = state.bond_state == "missing"
    if not state.pairing_timer_paused:
        stopped, detail = _pairing_timer("stop")
        if not stopped:
            state.repair_state = "pairing_required"
            state.last_action = "pairing_required"
            state.last_error = f"could not pause {PAIRING_SEAL_TIMER}: {detail}"
            return False
        state.pairing_timer_paused = True

    if not rollback_already_sealed:
        sealed, detail = _pairing_seal()
        if not sealed:
            _pairing_timer("start")
            state.pairing_timer_paused = False
            state.repair_state = "pairing_required"
            state.last_action = "pairing_required"
            state.last_error = f"pre-repair pairing snapshot failed: {detail}"
            return False

    try:
        _before_mutation(cancelled)
        tree = btadapters.managed_objects(cancelled=cancelled)
        current = resolve_role(roles, "call", objects=tree)
        if _adapter_runtime_target(current) != _adapter_runtime_target(adapter):
            raise controller_roles.ControllerIdentityMismatchError(
                "call", "runtime target changed before stale bond removal"
            )
        baseline = btadapters.paired_addresses_on(current, tree) - {address}
        if btadapters.device_properties(current, address, tree):
            removed, detail = btadapters.remove_device(address, current)
            if not removed:
                state.repair_state = "pairing_required"
                state.last_action = "pairing_required"
                state.last_error = f"exact Pixel bond removal failed: {detail}"
                return False

        state.bond_state = "missing"
        state.repair_state = "pairing_window"
        state.last_action = "pairing_window"
        state.repair_deadline = time.monotonic() + PAIRING_WINDOW_SECONDS
        state.last_error = (
            "Open Pixel Bluetooth settings, tap LarkBridge BT500, and approve Pair"
        )

        def heartbeat(_elapsed: float, _duration: float) -> None:
            write_state(state, current)

        result = btadapters.incoming_pairing_window(
            address,
            current,
            timeout=PAIRING_WINDOW_SECONDS,
            preexisting_paired=baseline,
            cancelled=cancelled,
            heartbeat=heartbeat,
        )
        state.repair_deadline = None
        if not result.ok:
            state.repair_state = "pairing_required"
            state.last_action = "pairing_required"
            state.last_error = result.detail
            return False

        sealed, detail = _pairing_seal()
        if not sealed:
            state.repair_state = "pairing_required"
            state.last_action = "pairing_required"
            state.last_error = f"new bond is live but was not sealed: {detail}"
            return False
        started, timer_detail = _pairing_timer("start")
        state.pairing_timer_paused = not started
        if not started:
            state.repair_state = "pairing_required"
            state.last_action = "pairing_required"
            state.last_error = (
                f"new bond sealed but timer did not resume: {timer_detail}"
            )
            return False

        state.bond_state = "trusted"
        state.repair_state = "idle"
        state.repair_trigger = None
        state.stale_connect_signatures = 0
        state.connect_collision_cancellations = 0
        state.reconnect_attempts = 0
        state.reconnect_next = 0.0
        _clear_pending_connect(state)
        state.last_action = "bond_sealed"
        state.last_error = None
        return True
    except (
        RecoveryCancelled,
        btadapters.BluetoothOperationCancelled,
        controller_roles.ControllerRoleError,
    ) as exc:
        state.repair_deadline = None
        state.repair_state = "pairing_required"
        state.last_action = "pairing_required"
        state.last_error = str(exc)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=CLI_ROLE_NAMES, required=True)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("BRIDGE_LOG", "INFO").upper(),
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        roles = controller_roles.load_controller_roles(args.config)
    except (OSError, ValueError) as exc:
        log.error("controller role configuration rejected: %s", exc)
        return 2

    try:
        configured = role_spec(roles, args.role)
    except controller_roles.ControllerRoleError as exc:
        log.error("%s: %s", exc.code, exc.detail)
        return 4

    state = RecoveryState(args.role)
    stopping = False
    repair_requested = False

    def on_signal(signum: int, _frame: Any) -> None:
        nonlocal repair_requested, stopping
        if signum == signal.SIGUSR1:
            log.warning("manual Pixel repair requested")
            repair_requested = True
            return
        log.info("signal %s; stopping %s watchdog", signum, args.role)
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGUSR1, on_signal)

    with btadapters.speaker_radio_lock(
        role_lock_path(args.role), blocking=False
    ) as acquired:
        if not acquired:
            log.error("another %s watchdog already owns its role lock", args.role)
            return 3
        log.info(
            "watching %s controller %s (%s)",
            args.role,
            configured.address,
            configured.bus,
        )
        adapter: btadapters.Adapter | None = None
        pairing_closed_target: str | None = None
        startup_connect_target: str | None = None
        while not stopping:
            try:
                tree = btadapters.managed_objects()
                adapter = resolve_role(roles, args.role, objects=tree)
                state.identity_error = None
            except controller_roles.ControllerRoleError as exc:
                adapter = None
                pairing_closed_target = None
                startup_connect_target = None
                state.probe = ProbeStatus.UNKNOWN.value
                _clear_pending_connect(state)
                state.reconnect_next = time.monotonic() + RECONNECT_DELAY
                state.identity_error = f"{exc.code}: {exc.detail}"
                state.last_error = state.identity_error
                write_state(state, None)
                time.sleep(PROBE_INTERVAL)
                continue

            target = _adapter_runtime_target(adapter)
            if (
                pairing_closed_target != target
                and state.repair_state != "pairing_window"
            ):
                closed, detail = _set_production_pairing_closed(
                    adapter, cancelled=lambda: stopping
                )
                if closed:
                    pairing_closed_target = target
                else:
                    state.last_error = detail

            if repair_requested:
                repair_requested = False
                state.repair_state = "requested"
                state.repair_trigger = "manual"
                state.last_action = "pairing_required"

            startup_connect_target = service_startup_reconnect(
                roles,
                args.role,
                state,
                adapter,
                startup_connect_target,
                cancelled=lambda: stopping,
            )

            result = probe_controller(adapter)
            state.probe = result.value
            if result is ProbeStatus.ANSWERED:
                state.failures = 0
                if (
                    state.last_attempt
                    and time.monotonic() - state.last_attempt > BACKOFF_MAX
                ):
                    state.backoff = BACKOFF_START
                service_reconnect(roles, args.role, state, cancelled=lambda: stopping)
                if state.repair_state == "requested" and not stopping:
                    repaired = repair_phone_bond(
                        roles,
                        state,
                        adapter,
                        cancelled=lambda: stopping,
                    )
                    pairing_closed_target = target
                    if repaired and not stopping:
                        service_reconnect(
                            roles,
                            args.role,
                            state,
                            cancelled=lambda: stopping,
                        )
            elif result is ProbeStatus.FAILED:
                record_failure_and_recover(
                    roles,
                    args.role,
                    state,
                    cancelled=lambda: stopping,
                )
            write_state(state, adapter)
            time.sleep(service_sleep_interval(state))

    write_state(state, adapter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
