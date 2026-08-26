from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().with_name("invariants.py")
SPEC = importlib.util.spec_from_file_location("invariants_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
invariants = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = invariants
SPEC.loader.exec_module(invariants)


def active_status() -> dict:
    selected = "alsa_input.usb-LARK"
    fifine = "alsa_input.usb-FIFINE"
    return {
        "timestamp": time.time(),
        "state": "ACTIVE",
        "endpoints": {
            "microphone": selected,
            "lark": selected,
            "hfp_source": "bluez_input.call",
            "hfp_sink": "bluez_output.call",
        },
        "microphone": {
            "selected": {"id": "lark-a1", "node": selected},
            "candidates": [
                {
                    "id": "lark-a1",
                    "state": "selected",
                    "node": selected,
                    "matched_nodes": [selected],
                },
                {
                    "id": "fifine-k054",
                    "state": "usable",
                    "node": fifine,
                    "matched_nodes": [fifine],
                },
            ],
        },
        "aec": {"enabled": True, "verified": True, "owner_pid": 123},
        "graph": {"missing_links": [], "unexpected_links": []},
    }


def run_check(monkeypatch, status: dict, links: list[tuple[str, str]]):
    monkeypatch.setattr(invariants, "pw_links", lambda: links)
    monkeypatch.setattr(invariants, "graph_quantum", lambda: 1024)
    monkeypatch.setattr(invariants, "configured_quantum", lambda: 2048)
    monkeypatch.setattr(invariants, "bluetooth_state", dict)
    monkeypatch.setattr(invariants, "resource_counts", lambda _status: {})
    return invariants.check(status, None)


def test_active_graph_has_one_selected_aec_route(monkeypatch) -> None:
    status = active_status()
    selected = status["endpoints"]["microphone"]
    hfp_sink = status["endpoints"]["hfp_sink"]
    violations, observations = run_check(
        monkeypatch,
        status,
        [
            (selected, invariants.AEC_CAPTURE),
            (invariants.AEC_SOURCE, invariants.MICROPHONE_INPUT),
            (invariants.MICROPHONE_OUTPUT, hfp_sink),
        ],
    )

    assert violations == []
    assert observations["microphone_id"] == "lark-a1"


def test_unselected_microphone_cannot_feed_aec(monkeypatch) -> None:
    status = active_status()
    selected = status["endpoints"]["microphone"]
    hfp_sink = status["endpoints"]["hfp_sink"]
    violations, _ = run_check(
        monkeypatch,
        status,
        [
            (selected, invariants.AEC_CAPTURE),
            ("alsa_input.usb-FIFINE", invariants.AEC_CAPTURE),
            (invariants.AEC_SOURCE, invariants.MICROPHONE_INPUT),
            (invariants.MICROPHONE_OUTPUT, hfp_sink),
        ],
    )

    assert any(item["id"] == "I4" for item in violations)


def test_unselected_microphone_is_rejected_during_graph_rebuild(monkeypatch) -> None:
    status = active_status()
    status["state"] = "BUILDING"
    violations, _ = run_check(
        monkeypatch,
        status,
        [("alsa_input.usb-FIFINE", invariants.AEC_CAPTURE)],
    )

    assert any(
        "inactive microphone" in item["detail"] for item in violations
    )


def test_no_physical_microphone_can_bypass_bridge_mic(monkeypatch) -> None:
    status = active_status()
    selected = status["endpoints"]["microphone"]
    hfp_sink = status["endpoints"]["hfp_sink"]
    violations, _ = run_check(
        monkeypatch,
        status,
        [
            (selected, invariants.AEC_CAPTURE),
            (invariants.AEC_SOURCE, invariants.MICROPHONE_INPUT),
            (invariants.MICROPHONE_OUTPUT, hfp_sink),
            ("alsa_input.usb-FIFINE", hfp_sink),
        ],
    )

    assert any(item["id"] == "I1" for item in violations)
    assert any(item["id"] == "I4" for item in violations)
