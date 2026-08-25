#!/usr/bin/env python3
"""Resolve configured Bluetooth-controller roles by permanent hardware identity.

Runtime names and topology (``hciX``, USB port, object enumeration order, and
BlueZ's default adapter) are observations only.  A wired AUX output deliberately
has no Bluetooth-controller role.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

REPO = Path(os.environ.get("BRIDGE_REPO", "/home/admin/rpi-lark-bridge"))
DEFAULT_CONFIG = Path(os.environ.get("BRIDGE_CONFIG", REPO / "config" / "bridge.toml"))

MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
USB_ID_RE = re.compile(r"^[0-9a-f]{4}$")
IDENTITY_KEYS = {
    "adapter",
    "adapter_product",
    "adapter_bus",
    "adapter_usb_vendor_id",
    "adapter_usb_product_id",
}


class ReadinessPolicy(str, Enum):
    """The transitional deploy gate may temporarily admit the onboard UART."""

    TRANSITIONAL = "transitional"
    FINAL_USB = "final-usb"


class ControllerRoleError(RuntimeError):
    """Base for typed role-resolution failures."""

    code = "controller_error"

    def __init__(self, role: str, detail: str) -> None:
        super().__init__(detail)
        self.role = role
        self.detail = detail


class ControllerMissingError(ControllerRoleError):
    code = "controller_missing"


class ControllerDuplicateError(ControllerRoleError):
    code = "controller_duplicate"


class ControllerIdentityMismatchError(ControllerRoleError):
    code = "controller_identity_mismatch"


class ControllerPolicyError(ControllerRoleError):
    code = "controller_policy_mismatch"


class ControllerRoleNotConfiguredError(ControllerRoleError):
    code = "controller_role_not_configured"


class ControllerConfigError(ValueError):
    """The durable role declaration is incomplete, ambiguous, or noncanonical."""


class AdapterView(Protocol):
    @property
    def hci(self) -> str: ...

    @property
    def address(self) -> str: ...

    @property
    def bus(self) -> str: ...

    @property
    def rfkill_index(self) -> int | None: ...

    @property
    def usb_vendor_id(self) -> str | None: ...

    @property
    def usb_product_id(self) -> str | None: ...

    @property
    def usb_parent(self) -> str | None: ...

    @property
    def usb_interface(self) -> str | None: ...

    @property
    def driver(self) -> str | None: ...

    @property
    def product(self) -> str | None: ...

    @property
    def manufacturer(self) -> str | None: ...

    @property
    def path(self) -> str: ...


@dataclass(frozen=True)
class ControllerSpec:
    role: str
    address: str
    product: str
    bus: str
    usb_vendor_id: str | None = None
    usb_product_id: str | None = None

    @property
    def expected_usb_id(self) -> str | None:
        if self.usb_vendor_id is None or self.usb_product_id is None:
            return None
        return f"{self.usb_vendor_id}:{self.usb_product_id}"


@dataclass(frozen=True)
class ControllerRoles:
    phone_address: str
    call: ControllerSpec
    output: ControllerSpec | None
    allow_transitional_uart_call: bool

    @property
    def transitional(self) -> bool:
        return self.call.bus == "UART"


def _table(parent: Mapping[str, Any], key: str, label: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise ControllerConfigError(f"{label} must be a TOML table")
    return value


def _optional_table(parent: Mapping[str, Any], key: str, label: str) -> Mapping[str, Any]:
    value = parent.get(key, {})
    if not isinstance(value, Mapping):
        raise ControllerConfigError(f"{label} must be a TOML table")
    return value


def _required_string(table: Mapping[str, Any], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ControllerConfigError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _canonical_mac(value: str, label: str) -> str:
    if not MAC_RE.fullmatch(value):
        raise ControllerConfigError(f"{label} must be an uppercase Bluetooth address")
    return value


def _usb_id(table: Mapping[str, Any], key: str, label: str) -> str:
    value = _required_string(table, key, label)
    if not USB_ID_RE.fullmatch(value):
        raise ControllerConfigError(f"{label}.{key} must be four lowercase hexadecimal digits")
    return value


def _parse_spec(table: Mapping[str, Any], role: str) -> ControllerSpec:
    label = "devices.phone" if role == "call" else "devices.output"
    address = _canonical_mac(_required_string(table, "adapter", label), f"{label}.adapter")
    product = _required_string(table, "adapter_product", label)
    bus = _required_string(table, "adapter_bus", label)
    if bus not in {"USB", "UART"}:
        raise ControllerConfigError(f"{label}.adapter_bus must be 'USB' or 'UART'")
    vendor_present = "adapter_usb_vendor_id" in table
    product_present = "adapter_usb_product_id" in table
    if bus == "USB":
        if not vendor_present or not product_present:
            raise ControllerConfigError(f"{label} USB role requires both adapter USB IDs")
        vendor = _usb_id(table, "adapter_usb_vendor_id", label)
        product_id = _usb_id(table, "adapter_usb_product_id", label)
    else:
        if vendor_present or product_present:
            raise ControllerConfigError(f"{label} UART role must omit adapter USB IDs")
        vendor = None
        product_id = None
    return ControllerSpec(role, address, product, bus, vendor, product_id)


def has_controller_role_fields(document: Mapping[str, Any]) -> bool:
    """Whether the document opts into strict call-controller configuration.

    Older output-selection payloads carried only ``devices.output.adapter``.  They
    remain readable by configuration editors, but the production supervisor's main
    entry point still rejects them because no call role is loaded.
    """
    devices = document.get("devices")
    if not isinstance(devices, Mapping):
        return False
    phone = devices.get("phone")
    return bool(isinstance(phone, Mapping) and IDENTITY_KEYS.intersection(phone))


def _bluetooth_output_required(document: Mapping[str, Any], output: Mapping[str, Any]) -> bool:
    bridge = _optional_table(document, "bridge", "bridge")
    mode = str(bridge.get("mode", "bluetooth-wired"))
    output_id = str(output.get("id", "") or "").strip().lower()
    address = str(output.get("address", "") or "").strip().upper()
    return (
        mode == "bluetooth"
        or output_id.startswith("a2dp:")
        or bool(address and address != "11:22:33:44:55:66")
    )


def parse_controller_roles(document: Mapping[str, Any]) -> ControllerRoles:
    devices = _table(document, "devices", "devices")
    phone = _table(devices, "phone", "devices.phone")
    output = _optional_table(devices, "output", "devices.output")
    phone_address = _canonical_mac(
        _required_string(phone, "address", "devices.phone"), "devices.phone.address"
    )
    call = _parse_spec(phone, "call")

    output_has_identity = bool(IDENTITY_KEYS.intersection(output))
    output_spec = _parse_spec(output, "output") if output_has_identity else None
    if _bluetooth_output_required(document, output) and output_spec is None:
        raise ControllerConfigError(
            "Bluetooth/A2DP output requires a complete devices.output controller role"
        )
    if output_spec is not None:
        if call.address == output_spec.address:
            raise ControllerConfigError("call and output controller addresses must be distinct")
        if output_spec.bus != "USB":
            raise ControllerConfigError("devices.output.adapter_bus must be 'USB'")

    bluetooth = _optional_table(document, "bluetooth", "bluetooth")
    opt_in = bluetooth.get("allow_transitional_uart_call", False)
    if not isinstance(opt_in, bool):
        raise ControllerConfigError("bluetooth.allow_transitional_uart_call must be a boolean")
    if call.bus == "UART" and not opt_in:
        raise ControllerConfigError(
            "UART call role requires bluetooth.allow_transitional_uart_call = true"
        )
    if call.bus == "USB" and opt_in:
        raise ControllerConfigError(
            "remove stale bluetooth.allow_transitional_uart_call from a USB call configuration"
        )
    return ControllerRoles(phone_address, call, output_spec, opt_in)


def load_controller_roles(path: Path) -> ControllerRoles:
    with path.open("rb") as handle:
        return parse_controller_roles(tomllib.load(handle))


def _matches(spec: ControllerSpec, inventory: Sequence[AdapterView]) -> list[AdapterView]:
    return [adapter for adapter in inventory if adapter.address == spec.address]


def _observed_usb_id(adapter: AdapterView) -> str | None:
    if adapter.usb_vendor_id is None or adapter.usb_product_id is None:
        return None
    return f"{adapter.usb_vendor_id}:{adapter.usb_product_id}"


def resolve_controller(
    spec: ControllerSpec,
    inventory: Sequence[AdapterView],
    *,
    policy: ReadinessPolicy = ReadinessPolicy.TRANSITIONAL,
) -> AdapterView:
    matches = _matches(spec, inventory)
    if not matches:
        raise ControllerMissingError(spec.role, f"{spec.address} is not present")
    if len(matches) != 1:
        names = ", ".join(sorted(adapter.hci for adapter in matches))
        raise ControllerDuplicateError(
            spec.role, f"{spec.address} is claimed by {len(matches)} controllers ({names})"
        )
    adapter = matches[0]
    if policy is ReadinessPolicy.FINAL_USB and spec.bus != "USB":
        raise ControllerPolicyError(spec.role, "final readiness requires a USB controller")
    if adapter.bus != spec.bus:
        raise ControllerIdentityMismatchError(
            spec.role, f"expected bus {spec.bus}, observed {adapter.bus or 'unknown'}"
        )
    if spec.bus == "USB" and _observed_usb_id(adapter) != spec.expected_usb_id:
        raise ControllerIdentityMismatchError(
            spec.role,
            f"expected USB {spec.expected_usb_id}, observed {_observed_usb_id(adapter) or 'unknown'}",
        )
    return adapter


def role_spec(roles: ControllerRoles, role: str) -> ControllerSpec:
    if role == "call":
        return roles.call
    if role == "output":
        if roles.output is None:
            raise ControllerRoleNotConfiguredError("output", "wired output has no controller")
        return roles.output
    raise ControllerRoleNotConfiguredError(role, f"unknown controller role {role!r}")


def resolve_controllers(
    roles: ControllerRoles,
    inventory: Sequence[AdapterView],
    *,
    policy: ReadinessPolicy = ReadinessPolicy.TRANSITIONAL,
) -> dict[str, AdapterView]:
    resolved = {"call": resolve_controller(roles.call, inventory, policy=policy)}
    if roles.output is not None:
        resolved["output"] = resolve_controller(roles.output, inventory, policy=policy)
    return resolved


def _adapter_fields(adapter: AdapterView | None) -> dict[str, Any]:
    return {
        "observed_address": getattr(adapter, "address", None),
        "observed_bus": getattr(adapter, "bus", None),
        "observed_usb_id": _observed_usb_id(adapter) if adapter is not None else None,
        "hci": getattr(adapter, "hci", None),
        "bluez_path": getattr(adapter, "path", None),
        "sysfs_path": getattr(adapter, "usb_parent", None),
        "usb_parent": getattr(adapter, "usb_parent", None),
        "usb_interface": getattr(adapter, "usb_interface", None),
        "driver": getattr(adapter, "driver", None),
        "product": getattr(adapter, "product", None),
        "manufacturer": getattr(adapter, "manufacturer", None),
        "rfkill_index": getattr(adapter, "rfkill_index", None),
    }


def controller_status(
    spec: ControllerSpec,
    inventory: Sequence[AdapterView],
    *,
    policy: ReadinessPolicy,
) -> dict[str, Any]:
    matches = _matches(spec, inventory)
    observed = matches[0] if len(matches) == 1 else None
    error: str | None = None
    try:
        observed = resolve_controller(spec, inventory, policy=policy)
    except ControllerRoleError as exc:
        error = f"{exc.code}: {exc.detail}"
    return {
        "required": True,
        "configured": True,
        "configured_address": spec.address,
        "expected_bus": spec.bus,
        "expected_usb_id": spec.expected_usb_id,
        "expected_product": spec.product,
        **_adapter_fields(observed),
        "ready": error is None,
        "reason": None,
        "error": error,
    }


def _wired_output_status() -> dict[str, Any]:
    return {
        "required": False,
        "configured": False,
        "configured_address": None,
        "expected_bus": None,
        "expected_usb_id": None,
        "expected_product": None,
        **_adapter_fields(None),
        "ready": True,
        "reason": "wired-output",
        "error": None,
    }


def controllers_status(
    roles: ControllerRoles,
    inventory: Sequence[AdapterView],
    *,
    policy: ReadinessPolicy = ReadinessPolicy.TRANSITIONAL,
) -> dict[str, Any]:
    call = controller_status(roles.call, inventory, policy=policy)
    output = (
        controller_status(roles.output, inventory, policy=policy)
        if roles.output is not None
        else _wired_output_status()
    )
    return {
        "policy": policy.value,
        "transitional_uart_call": roles.transitional,
        "ready": bool(call["ready"] and output["ready"]),
        "call": call,
        "output": output,
    }


def live_inventory() -> Sequence[AdapterView]:
    import btadapters

    return btadapters.adapters()


def live_controllers_status(
    path: Path = DEFAULT_CONFIG,
    *,
    policy: ReadinessPolicy,
) -> dict[str, Any]:
    return controllers_status(load_controller_roles(path), live_inventory(), policy=policy)


def _resolved_field(adapter: AdapterView, field: str) -> object:
    if field == "bluez-path":
        return adapter.path
    if field == "usb-id":
        return _observed_usb_id(adapter) or ""
    return getattr(adapter, field.replace("-", "_"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--policy",
        choices=[policy.value for policy in ReadinessPolicy],
        default=ReadinessPolicy.TRANSITIONAL.value,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    resolver = commands.add_parser("resolve")
    resolver.add_argument("role", choices=("call", "output"))
    resolver.add_argument(
        "--field",
        choices=(
            "address",
            "bluez-path",
            "bus",
            "driver",
            "hci",
            "rfkill-index",
            "usb-id",
            "usb-interface",
            "usb-parent",
        ),
        default="hci",
    )
    args = parser.parse_args(argv)
    policy = ReadinessPolicy(args.policy)
    try:
        roles = load_controller_roles(args.config)
        inventory = live_inventory()
        if args.command == "status":
            status = controllers_status(roles, inventory, policy=policy)
            print(json.dumps(status, sort_keys=True))
            return 0 if status["ready"] else 1
        adapter = resolve_controller(role_spec(roles, args.role), inventory, policy=policy)
        value = _resolved_field(adapter, args.field)
        print("" if value is None else value)
        return 0
    except (OSError, ControllerConfigError) as exc:
        print(f"controller_config_error: {exc}", file=sys.stderr)
        return 2
    except ControllerRoleError as exc:
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
