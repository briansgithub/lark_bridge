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
