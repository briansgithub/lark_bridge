from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
SPEC = importlib.util.spec_from_file_location(
    "powerloss_verify_tested", MODULE_DIR / "powerloss_verify.py"
)
assert SPEC is not None and SPEC.loader is not None
powerloss_verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(powerloss_verify)


def test_required_units_match_bt500_wired_profile() -> None:
    assert "bridge-btwatchdog@call.service" in powerloss_verify.SYSTEM_UNITS
    assert "bridge-btfw.service" not in powerloss_verify.SYSTEM_UNITS
    assert "bridge-btwatchdog.service" not in powerloss_verify.SYSTEM_UNITS
    assert "bridge-output-remote.service" not in powerloss_verify.USER_UNITS


def test_call_bluetooth_ready_requires_closed_adapter_and_trusted_bond() -> None:
    adapter = {
        "rc": 0,
        "stderr": "",
        "stdout": "Powered: yes\nPairable: no\nDiscoverable: no\n",
    }
    watchdog = {
        "bond_state": "trusted",
        "repair_state": "idle",
        "repair_trigger": None,
        "repair_deadline_monotonic": None,
        "reconnect_attempts": 1,
        "reconnect_next_monotonic": 42.0,
        "last_action": "device-reconnect",
    }

    assert powerloss_verify.call_bluetooth_failures(adapter, watchdog) == []


def test_call_bluetooth_ready_rejects_open_or_incomplete_repair_state() -> None:
    adapter = {
        "rc": 0,
        "stderr": "",
        "stdout": "Powered: yes\nPairable: yes\nDiscoverable: yes\n",
    }
    watchdog = {
        "bond_state": "missing",
        "repair_state": "pairing_window",
        "repair_trigger": "repeated-in-progress",
        "repair_deadline_monotonic": 123.0,
        "reconnect_attempts": 0,
        "reconnect_next_monotonic": 0.0,
        "last_action": "pairing_window",
    }

    failures = powerloss_verify.call_bluetooth_failures(adapter, watchdog)
    assert "configured call adapter is pairable outside a repair window" in failures
    assert "configured call adapter is discoverable outside a repair window" in failures
    assert "call watchdog has an active pairing repair transaction" in failures
    assert "configured Pixel bond is not trusted" in failures


def test_selected_microphone_prefers_generic_status() -> None:
    selected = {
        "id": "fifine-k054",
        "node": "alsa_input.usb-0c76_USB_PnP_Audio_Device-00.mono-fallback",
    }
    value, error = powerloss_verify.selected_microphone(
        {
            "microphone": {"selected": selected},
            "endpoints": {
                "microphone": selected["node"],
                "lark": "alsa_input.usb-LARK",
            },
        }
    )

    assert value == selected
    assert error is None


def test_selected_microphone_accepts_legacy_lark_status() -> None:
    value, error = powerloss_verify.selected_microphone(
        {"endpoints": {"lark": "alsa_input.usb-LARK"}}
    )

    assert value == {
        "id": "lark-a1",
        "node": "alsa_input.usb-LARK",
        "legacy": True,
    }
    assert error is None


def test_authoritative_ambiguity_does_not_fall_back_to_stale_lark_endpoint() -> None:
    value, error = powerloss_verify.selected_microphone(
        {
            "microphone": {
                "selected": None,
                "selection_reason": "lark-a1 is ambiguous",
                "candidates": [
                    {"id": "lark-a1", "state": "ambiguous"},
                    {"id": "fifine-k054", "state": "usable"},
                ],
            },
            "endpoints": {"lark": "stale-node", "microphone": None},
        }
    )

    assert value is None
    assert error == "lark-a1 is ambiguous"
