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
CALL_RECONNECT_ATTEMPTS = int(os.environ.get("BRIDGE_WD_CALL_ATTEMPTS", "3"))
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
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent, prefix=f".{state.role}-", delete=False
    ) as handle:
        json.dump(state.as_dict(adapter), handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, target)


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
    return ProbeStatus.ANSWERED if "HCI Version" in result.stdout else ProbeStatus.FAILED


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
            return RecoveryResult(True, "usb-interface-rebind", adapter.usb_interface or "")
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
        state.last_action = "cancelled" if isinstance(exc, RecoveryCancelled) else "resolve"
        state.last_error = (
            str(exc) if isinstance(exc, RecoveryCancelled) else f"{exc.code}: {exc.detail}"
        )
        return False
    state.failures += 1
    return attempt_recovery(state, operation)


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
        state.identity_error = f"{exc.code}: {exc.detail}"
        return False
    if btadapters.connected_on(adapter, address, tree):
        state.reconnect_attempts = 0
        state.reconnect_next = 0.0
        return True
    if observed < state.reconnect_next or state.reconnect_attempts >= CALL_RECONNECT_ATTEMPTS:
        return False
    try:
        _before_mutation(cancelled)
        adapter = resolve_role(roles, role)
    except RecoveryCancelled as exc:
        state.last_action = "cancelled"
        state.last_error = str(exc)
        return False
    except controller_roles.ControllerRoleError as exc:
        state.identity_error = f"{exc.code}: {exc.detail}"
        return False
    try:
        _before_mutation(cancelled)
        state.reconnect_attempts += 1
        state.reconnect_next = observed + RECONNECT_RETRY
        powered, detail = btadapters.power_on(adapter, cancelled=cancelled)
        if not powered:
            state.last_error = detail
            return False
        _before_mutation(cancelled)
        ok, detail = btadapters.connect(address, adapter, cancelled=cancelled)
    except (RecoveryCancelled, btadapters.BluetoothOperationCancelled) as exc:
        state.last_action = "cancelled"
        state.last_error = str(exc)
        return False
    state.last_action = "device-reconnect"
    state.last_error = None if ok else detail
    if ok:
        state.reconnect_attempts = 0
    return ok


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

    def on_signal(signum: int, _frame: Any) -> None:
        nonlocal stopping
        log.info("signal %s; stopping %s watchdog", signum, args.role)
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    with btadapters.speaker_radio_lock(role_lock_path(args.role), blocking=False) as acquired:
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
        while not stopping:
            try:
                tree = btadapters.managed_objects()
                adapter = resolve_role(roles, args.role, objects=tree)
                state.identity_error = None
            except controller_roles.ControllerRoleError as exc:
                adapter = None
                state.probe = ProbeStatus.UNKNOWN.value
                state.identity_error = f"{exc.code}: {exc.detail}"
                state.last_error = state.identity_error
                write_state(state, None)
                time.sleep(PROBE_INTERVAL)
                continue

            result = probe_controller(adapter)
            state.probe = result.value
            if result is ProbeStatus.ANSWERED:
                state.failures = 0
                if state.last_attempt and time.monotonic() - state.last_attempt > BACKOFF_MAX:
                    state.backoff = BACKOFF_START
                service_reconnect(roles, args.role, state, cancelled=lambda: stopping)
            elif result is ProbeStatus.FAILED:
                record_failure_and_recover(
                    roles,
                    args.role,
                    state,
                    cancelled=lambda: stopping,
                )
            write_state(state, adapter)
            time.sleep(PROBE_INTERVAL)

    write_state(state, adapter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
