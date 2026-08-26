from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CAPTURE_PATH = REPO / "rig" / "pi" / "measure" / "call_capture.py"
SUPERVISOR_DIR = REPO / "pi" / "bridged"


def test_standalone_capture_loads_supervisor_siblings() -> None:
    spec = importlib.util.spec_from_file_location(
        "bt500_aux_call_capture", CAPTURE_PATH
    )
    assert spec is not None and spec.loader is not None
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)

    original_path = sys.path.copy()
    sys.path = [
        entry for entry in sys.path if Path(entry or ".").resolve() != SUPERVISOR_DIR
    ]
    try:
        supervisor = capture.load_supervisor()
        settings = supervisor.load_settings(REPO / "config" / "bridge.toml.example")
    finally:
        sys.path = original_path
        sys.modules.pop("e12_supervisor", None)

    assert settings.controller_roles is not None
    assert settings.controller_roles.call.address == "A0:AD:9F:73:6C:24"


def test_pwtop_discovery_row_does_not_inflate_error_delta(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "bt500_aux_call_capture", CAPTURE_PATH
    )
    assert spec is not None and spec.loader is not None
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)

    pwtop = tmp_path / "pwtop.txt"
    pwtop.write_text(
        "S ID QUANT RATE WAIT BUSY W/Q B/Q ERR FORMAT NAME\n"
        "C 103 0 0 --- --- --- --- 0 F32P 1 48000 + bridge.aec.source\n"
        "S ID QUANT RATE WAIT BUSY W/Q B/Q ERR FORMAT NAME\n"
        "R 103 1920 48000 1us 2us 0.00 0.00 6 F32P 1 48000 + bridge.aec.source\n"
        "S ID QUANT RATE WAIT BUSY W/Q B/Q ERR FORMAT NAME\n"
        "R 103 1920 48000 1us 2us 0.00 0.00 6 F32P 1 48000 + bridge.aec.source\n"
    )

    result = capture.parse_pwtop(pwtop, ["bridge.aec.source"])

    assert result["bridge.aec.source"]["err_first"] == 6
    assert result["bridge.aec.source"]["err_last"] == 6
    assert result["bridge.aec.source"]["err_delta"] == 0


def test_authoritative_null_microphone_does_not_fall_back_to_lark_alias() -> None:
    spec = importlib.util.spec_from_file_location(
        "bt500_aux_call_capture", CAPTURE_PATH
    )
    assert spec is not None and spec.loader is not None
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)

    selected, node = capture.microphone_from_status(
        {
            "microphone": {"selected": None, "selection_reason": "identity conflict"},
            "endpoints": {
                "microphone": None,
                "lark": "alsa_input.usb-STALE-LARK",
            },
        }
    )

    assert selected is None
    assert node is None


def test_microphone_status_endpoint_and_legacy_fallback_are_separate() -> None:
    spec = importlib.util.spec_from_file_location(
        "bt500_aux_call_capture", CAPTURE_PATH
    )
    assert spec is not None and spec.loader is not None
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)

    selected, node = capture.microphone_from_status(
        {
            "microphone": {"selected": None},
            "endpoints": {
                "microphone": "alsa_input.usb-FIFINE",
                "lark": "alsa_input.usb-STALE-LARK",
            },
        }
    )
    assert selected is None
    assert node == "alsa_input.usb-FIFINE"

    selected, node = capture.microphone_from_status(
        {"endpoints": {"lark": "alsa_input.usb-LEGACY-LARK"}}
    )
    assert selected is None
    assert node == "alsa_input.usb-LEGACY-LARK"
