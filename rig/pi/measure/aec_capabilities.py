#!/usr/bin/env python3
"""Report the installed PipeWire WebRTC AEC capabilities and exact baseline arguments."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SUPERVISOR_PATH = REPO / "pi" / "bridged" / "bridge_supervisor.py"
SOURCE_REFERENCE = (
    "https://gitlab.freedesktop.org/pipewire/pipewire/-/raw/1.4.2/"
    "spa/plugins/aec/aec-webrtc.cpp"
)
MODULE_SOURCE_REFERENCE = (
    "https://gitlab.freedesktop.org/pipewire/pipewire/-/raw/1.4.2/"
    "src/modules/module-echo-cancel.c"
)
KNOWN_PROPERTIES = {
    "webrtc.beamforming",
    "webrtc.delay_agnostic",
    "webrtc.experimental_agc",
    "webrtc.experimental_ns",
    "webrtc.extended_filter",
    "webrtc.gain_control",
    "webrtc.high_pass_filter",
    "webrtc.mic-geometry",
    "webrtc.noise_suppression",
    "webrtc.target-direction",
    "webrtc.transient_suppression",
    "webrtc.voice_detection",
}
KNOWN_MODULE_CONTROLS = {
    "buffer.max_size",
    "buffer.play_delay",
    "debug.aec.wav-path",
}


def command(*args: str) -> str:
    result = subprocess.run(
        args, capture_output=True, text=True, check=False, timeout=10
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def load_supervisor():
    spec = importlib.util.spec_from_file_location(
        "aec_capability_supervisor", SUPERVISOR_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load bridge supervisor")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    candidates = list(
        Path("/usr/lib").glob("*/spa-0.2/aec/libspa-aec-webrtc.so")
    ) + list(Path("/usr/lib").glob("spa-0.2/aec/libspa-aec-webrtc.so"))
    if len(candidates) != 1:
        raise SystemExit(
            f"expected one installed WebRTC AEC library, found {candidates}"
        )
    library = candidates[0]
    binary = library.read_bytes()
    strings = command("strings", str(library)).splitlines()
    compiled_strings = {
        match.group(0)
        for line in strings
        for match in re.finditer(r"webrtc\.[a-z0-9_-]+", line)
    }
    properties = sorted(compiled_strings & KNOWN_PROPERTIES)
    module_candidates = list(
        Path("/usr/lib").glob(
            "*/pipewire-0.3/libpipewire-module-echo-cancel.so"
        )
    ) + list(
        Path("/usr/lib").glob("pipewire-0.3/libpipewire-module-echo-cancel.so")
    )
    if len(module_candidates) != 1:
        raise SystemExit(
            f"expected one installed echo-cancel module, found {module_candidates}"
        )
    echo_module = module_candidates[0]
    echo_module_binary = echo_module.read_bytes()
    echo_module_strings = set(command("strings", str(echo_module)).splitlines())
    module_controls = sorted(echo_module_strings & KNOWN_MODULE_CONTROLS)
    if "webrtc.extended_filter" in properties:
        variant = "legacy-webrtc"
    elif "webrtc.transient_suppression" in properties:
        variant = "webrtc1-or-newer"
    else:
        variant = "unknown"

    supervisor = load_supervisor()
    settings = supervisor.AecSettings(enabled=True)
    module_command = supervisor.NativeAecHost(
        settings, "<stable-lark>", "<wired-output>"
    ).module_command()
    packages = command(
        "dpkg-query",
        "-W",
        "pipewire",
        "libpipewire-0.3-modules",
        "libspa-0.2-modules",
    ).splitlines()
    result = {
        "verdict": "PASS",
        "library": str(library),
        "sha256": hashlib.sha256(binary).hexdigest(),
        "build_id": command("readelf", "-n", str(library)),
        "packages": packages,
        "compiled_variant": variant,
        "supported_properties": properties,
        "unsupported_planned_properties": sorted(
            {"webrtc.extended_filter"} - set(properties)
        ),
        "echo_cancel_module": {
            "path": str(echo_module),
            "sha256": hashlib.sha256(echo_module_binary).hexdigest(),
            "supported_controls": module_controls,
            "source_reference": MODULE_SOURCE_REFERENCE,
        },
        "baseline": {
            "high_pass_filter": settings.high_pass_filter,
            "noise_suppression": settings.noise_suppression,
            "gain_control": settings.gain_control,
            "voice_detection": settings.voice_detection,
            "transient_suppression": settings.transient_suppression,
            "module_command": module_command.strip(),
        },
        "source_reference": SOURCE_REFERENCE,
        "notes": [
            "Property support is inferred from lookup strings compiled into this exact binary.",
            "extended_filter is not tested when its lookup string is absent from the installed binary.",
        ],
    }
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
