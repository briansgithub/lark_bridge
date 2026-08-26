from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# aec_profile reads Linux-only os.sysconf values at import time. These unit tests cover
# resolver/artifact plumbing and do not instantiate the runtime profiler.
profile_stub = ModuleType("aec_profile")
profile_stub.ActiveProfiler = object


def _no_gate_failures(_runtime):
    return []


profile_stub.gate_failures = _no_gate_failures
sys.modules.setdefault("aec_profile", profile_stub)

import aec_bench
import faultctl


class _Selection:
    node = "alsa_input.usb-fifine"

    def as_dict(self):
        return {
            "id": "fifine-k054",
            "label": "FIFINE K054",
            "priority": 1,
            "node": self.node,
            "identity": {
                "usb_vendor_id": "0c76",
                "usb_product_id": "161e",
                "usb_product": "USB PnP Audio Device",
                "usb_serial": None,
                "usb_port_path": "1-1.2",
                "pipewire_object_serial": "71",
            },
            "format": {"rate": 48000, "format": "S16LE", "channels": 1},
            "instance_token": "fifine-instance",
        }


class _Resolution:
    def __init__(self, *, selected=True, blocked=False):
        self.selected = _Selection() if selected else None
        self.blocked = blocked

    def as_dict(self):
        return {
            "selected": self.selected.as_dict() if self.selected else None,
            "selection_reason": "lark-a1 absent; using fifine-k054",
            "blocked": self.blocked,
            "candidates": [],
        }


def _module(tmp_path: Path, resolution: _Resolution):
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"generation": 17}), encoding="utf-8")
    return SimpleNamespace(
        pw_snapshot=lambda: ({_Selection.node: {}}, [{"id": 71}]),
        resolve_microphone=lambda objects, settings: resolution,
        default_status_path=lambda: status,
    )


def test_qualification_resolution_records_identity_format_and_generation(tmp_path):
    nodes, node, artifact, report = aec_bench.resolve_selected_microphone(
        _module(tmp_path, _Resolution()), object(), "fifine-k054"
    )

    assert node in nodes
    assert artifact["id"] == "fifine-k054"
    assert artifact["identity"]["usb_port_path"] == "1-1.2"
    assert artifact["format"] == {"rate": 48000, "format": "S16LE", "channels": 1}
    assert artifact["graph_generation"] == 17
    assert report["selection_reason"] == "lark-a1 absent; using fifine-k054"


def test_qualification_resolution_rejects_unexpected_or_blocked_candidate(tmp_path):
    with pytest.raises(RuntimeError, match="expected 'lark-a1'"):
        aec_bench.resolve_selected_microphone(
            _module(tmp_path, _Resolution()), object(), "lark-a1"
        )
    with pytest.raises(RuntimeError, match="unsafe"):
        aec_bench.resolve_selected_microphone(
            _module(tmp_path, _Resolution(selected=False, blocked=True)),
            object(),
            "fifine-k054",
        )


def _usb(root: Path, port: str, *, vendor="0c76", product_id="161e") -> Path:
    path = root / port
    path.mkdir()
    (path / "idVendor").write_text(vendor, encoding="utf-8")
    (path / "idProduct").write_text(product_id, encoding="utf-8")
    (path / "product").write_text("USB PnP Audio Device", encoding="utf-8")
    return path


def _status(port: str | None) -> dict:
    return {
        "microphone": {
            "selected": {
                "id": "fifine-k054",
                "node": _Selection.node,
                "identity": {
                    "usb_vendor_id": "0c76",
                    "usb_product_id": "161e",
                    "usb_product": "USB PnP Audio Device",
                    "usb_serial": None,
                    "usb_port_path": port,
                },
            }
        }
    }


def test_fault_target_uses_and_verifies_selected_port(tmp_path):
    expected = _usb(tmp_path, "1-1.2")
    _usb(tmp_path, "1-1.4")

    assert faultctl.microphone_usb_path(
        "fifine-k054", status=_status("1-1.2"), sysfs_root=tmp_path
    ) == expected


def test_fault_target_rejects_portable_ambiguity_and_candidate_mismatch(tmp_path):
    _usb(tmp_path, "1-1.2")
    _usb(tmp_path, "1-1.4")

    with pytest.raises(RuntimeError, match="resolves to 2"):
        faultctl.microphone_usb_path(
            "fifine-k054", status=_status(None), sysfs_root=tmp_path
        )
    with pytest.raises(RuntimeError, match="expected 'lark-a1'"):
        faultctl.microphone_usb_path(
            "lark-a1", status=_status("1-1.2"), sysfs_root=tmp_path
        )


def test_fault_target_does_not_revive_authoritative_null_selection(tmp_path):
    _usb(tmp_path, "1-1.2", vendor="3547", product_id="0407")
    status = {
        "microphone": {
            "selected": None,
            "selection_reason": "higher-priority microphone is ambiguous",
        },
        "endpoints": {"microphone": None, "lark": "alsa_input.usb-STALE-LARK"},
    }

    with pytest.raises(RuntimeError, match="no selected microphone"):
        faultctl.microphone_usb_path(
            "lark-a1", status=status, sysfs_root=tmp_path
        )
