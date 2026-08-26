from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SUPERVISOR_DIR = REPO / "pi" / "bridged"


@pytest.mark.parametrize(
    ("name", "relative_path", "supervisor_module_name"),
    [
        (
            "aec_capabilities_loader_test",
            "rig/pi/measure/aec_capabilities.py",
            "aec_capability_supervisor",
        ),
        (
            "crackle_probe_loader_test",
            "rig/pi/measure/crackle_probe.py",
            "crackle_probe_supervisor",
        ),
    ],
)
def test_standalone_measurement_loader_exposes_supervisor_siblings(
    name: str, relative_path: str, supervisor_module_name: str
) -> None:
    path = REPO / relative_path
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    measure = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(measure)

    original_path = sys.path.copy()
    sys.path = [
        entry for entry in sys.path if Path(entry or ".").resolve() != SUPERVISOR_DIR
    ]
    try:
        supervisor = measure.load_supervisor()
        settings = supervisor.load_settings(REPO / "config" / "bridge.toml.example")
    finally:
        sys.path = original_path
        sys.modules.pop(supervisor_module_name, None)

    assert [candidate.id for candidate in settings.microphone_candidates] == [
        "lark-a1",
        "fifine-k054",
    ]
