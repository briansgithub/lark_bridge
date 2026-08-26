#!/usr/bin/env python3
"""Operator-driven, non-destructive active-call microphone hotplug qualification.

This program never changes USB authorization, services, configuration, or PipeWire links.
It only samples the bridge status file and ``pw-link -l`` while an operator performs
physical plug/unplug actions. Prompts go to stderr; stdout and the artifact files remain
machine-readable.

Typical campaigns::

    # One complete both/either/neither matrix.
    python3 rig/pi/measure/microphone_hotplug.py --campaign matrix

    # The E18 timing gates, split into 10 direct and 10 powered-hub cycles.
    python3 rig/pi/measure/microphone_hotplug.py --campaign promotion-fallback \
        --connection-plan direct10-hub10

    # Twenty physical replug cycles proving a new FIFINE instance token each time.
    python3 rig/pi/measure/microphone_hotplug.py --campaign fifine-replug \
        --connection-plan direct10-hub10

The default 0.15 second interval leaves scheduling margin below the 0.20 second evidence
limit. Every sample is flushed immediately to ``timeline.jsonl``. ``summary.json`` carries
the transition timing gates and never upgrades a short smoke run into qualification.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any, TextIO

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import invariants

SCHEMA_VERSION = 1
DEFAULT_INTERVAL_SECONDS = 0.15
MAX_CONFIGURED_INTERVAL_SECONDS = 0.20
MAX_MEASURED_GAP_SECONDS = 0.25
QUALIFICATION_MATRIX_CYCLES = 1
QUALIFICATION_CYCLES = 20
QUALIFICATION_REQUIRED_FAST = 19
QUALIFICATION_FAST_LIMIT_SECONDS = 30.0
QUALIFICATION_MAX_LIMIT_SECONDS = 60.0
DEFAULT_TRANSITION_TIMEOUT_SECONDS = QUALIFICATION_MAX_LIMIT_SECONDS
DEFAULT_FAST_LIMIT_SECONDS = QUALIFICATION_FAST_LIMIT_SECONDS
QUALIFICATION_MIN_SETTLE_SECONDS = 0.60
DEFAULT_SETTLE_SECONDS = QUALIFICATION_MIN_SETTLE_SECONDS
DEFAULT_REPEATED_CYCLES = QUALIFICATION_CYCLES
USB_ACTION_OBSERVATION_TIMEOUT_SECONDS = 60.0
USB_BASELINE_OBSERVATION_TIMEOUT_SECONDS = 5.0
USB_BASELINE_DISCARD_SAMPLES = 1
USB_BASELINE_STABLE_SAMPLES = 2
USB_EVENT_CONFIRMATION_SAMPLES = 2
USB_SYSFS_ROOT = Path("/sys/bus/usb/devices")
CONNECTION_PLAN_DIRECT10_HUB10 = "direct10-hub10"
CONNECTION_PLAN_CAMPAIGNS = {"promotion-fallback", "fifine-replug"}
CONNECTION_LAYOUT_DIRECT = "direct"
CONNECTION_LAYOUT_POWERED_HUB = "powered_hub"
CONNECTION_LAYOUT_HANDOFF_PHASE = "connection_layout_handoff"
CONNECTION_LAYOUT_BOUNDARY_CYCLE = 10
USB_MICROPHONE_FINGERPRINTS = {
    "lark-a1": ("3547", "0407"),
    "fifine-k054": ("0c76", "161e"),
}
USB_GATE_BY_PHASE = {
    "lark_promotion": "promotion",
    "lark_fallback": "fallback",
    "restore_fifine": "fifine_replug",
    "fifine_replug/restore": "fifine_replug",
}
USB_GATE_TARGET = {
    "promotion": ("lark-a1", 0, 1),
    "fallback": ("lark-a1", 1, 0),
    "fifine_replug": ("fifine-k054", 0, 1),
}
USB_FINAL_SELECTED_CANDIDATE = {
    "promotion": "lark-a1",
    "fallback": "fifine-k054",
    "fifine_replug": "fifine-k054",
}
USB_TIMING_ORIGIN = "usb_sysfs_edge"
TIMING_EVIDENCE_VERSION = 2
MICROPHONE_OUTPUT = invariants.MICROPHONE_OUTPUT
MICROPHONE_INPUT = invariants.MICROPHONE_INPUT
AEC_CAPTURE = invariants.AEC_CAPTURE
AEC_SOURCE = invariants.AEC_SOURCE
SERVICE_UNITS = (
    "bridge-supervisor.service",
    "pipewire.service",
    "wireplumber.service",
)
SAFE_STATES = {"SAFE", "WAITING_MIC"}


class CampaignAbort(RuntimeError):
    """The operator stopped or a prerequisite transition did not complete."""


def command_output(cmd: list[str], timeout: float) -> tuple[str, str | None]:
    """Run one read-only command and return stdout plus an evidence error."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return "", f"{' '.join(cmd)} exited {result.returncode}: {detail}"
    return result.stdout, None


def parse_pw_links(text: str) -> list[tuple[str, str]]:
    """Parse node-level source/target pairs from ``pw-link -l`` output."""
    links: list[tuple[str, str]] = []
    current: str | None = None
    for raw in text.splitlines():
        if not raw.startswith((" ", "\t")):
            current = raw.strip().split(":")[0]
            continue
        value = raw.strip()
        if current is None or not value:
            continue
        if value.startswith("|->"):
            links.append((current, value[3:].strip().split(":")[0]))
        elif value.startswith("|<-"):
            links.append((value[3:].strip().split(":")[0], current))
    return links


def read_status(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {}, f"unreadable: {exc}"
    except json.JSONDecodeError as exc:
        return {}, f"invalid JSON: {exc}"
    if not isinstance(decoded, dict):
        return {}, "status root is not an object"
    return decoded, None


def _optional_sysfs_text(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return value or None


def _usb_port_route_parts(port_path: str) -> tuple[str, list[str]] | None:
    """Parse a Linux USB sysfs device route such as ``1-1.2.3``."""
    bus_port, separator, route = port_path.partition("-")
    if not separator or not bus_port.isdigit() or not route:
        return None
    route_parts = route.split(".")
    if any(not part.isdigit() for part in route_parts):
        return None
    return bus_port, route_parts


def _usb_port_ancestor_names(port_path: str) -> list[str]:
    """Return nearest-first USB device ancestors encoded in a sysfs port name."""
    parsed = _usb_port_route_parts(port_path)
    if parsed is None:
        return []
    bus_port, route_parts = parsed
    return [
        f"{bus_port}-{'.'.join(route_parts[:depth])}"
        for depth in range(len(route_parts) - 1, 0, -1)
    ]


def _read_usb_hub_ancestors(
    sysfs_root: Path,
    port_path: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read physical hub ancestors without inferring whether they are externally powered."""
    hubs: list[dict[str, Any]] = []
    errors: list[str] = []
    for ancestor_name in _usb_port_ancestor_names(port_path):
        ancestor = sysfs_root / ancestor_name
        try:
            device_class = (
                (ancestor / "bDeviceClass").read_text(encoding="ascii").strip().lower()
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(
                f"{ancestor_name}: device class unreadable: {type(exc).__name__}: {exc}"
            )
            continue
        if device_class.removeprefix("0x").zfill(2) != "09":
            continue
        try:
            vendor_id = (
                (ancestor / "idVendor").read_text(encoding="ascii").strip().lower()
            )
            product_id = (
                (ancestor / "idProduct").read_text(encoding="ascii").strip().lower()
            )
            raw_devnum = (ancestor / "devnum").read_text(encoding="ascii").strip()
        except (FileNotFoundError, OSError) as exc:
            errors.append(
                f"{ancestor_name}: hub identity unreadable: {type(exc).__name__}: {exc}"
            )
            continue
        if not raw_devnum.isdigit():
            errors.append(f"{ancestor_name}: hub devnum is not numeric: {raw_devnum!r}")
            continue
        devnum = int(raw_devnum)
        hubs.append(
            {
                "usb_device_class": "09",
                "usb_vendor_id": vendor_id,
                "usb_product_id": product_id,
                "usb_product": _optional_sysfs_text(ancestor / "product"),
                "usb_port_path": ancestor_name,
                "usb_devnum": devnum,
                "usb_instance_generation": f"{ancestor_name}@{devnum}",
            }
        )
    return hubs, errors


def read_usb_microphones(
    sysfs_root: Path = USB_SYSFS_ROOT,
) -> tuple[dict[str, list[dict[str, Any]]], str | None]:
    """Read the E18 microphone fingerprints directly from USB sysfs.

    ALSA card numbers and PipeWire enumeration order are deliberately absent. The
    port plus USB ``devnum`` forms an instance generation that changes on replug.
    """
    observed: dict[str, list[dict[str, Any]]] = {
        candidate_id: [] for candidate_id in USB_MICROPHONE_FINGERPRINTS
    }
    reverse = {
        fingerprint: candidate_id
        for candidate_id, fingerprint in USB_MICROPHONE_FINGERPRINTS.items()
    }
    try:
        entries = sorted(sysfs_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        return observed, f"USB sysfs inventory failed: {type(exc).__name__}: {exc}"

    errors: list[str] = []
    for entry in entries:
        try:
            vendor_id = (entry / "idVendor").read_text(encoding="ascii").strip().lower()
            product_id = (
                (entry / "idProduct").read_text(encoding="ascii").strip().lower()
            )
        except FileNotFoundError:
            # USB interface entries and devices disappearing mid-scan are expected.
            continue
        except OSError as exc:
            errors.append(
                f"{entry.name}: identity unreadable: {type(exc).__name__}: {exc}"
            )
            continue
        candidate_id = reverse.get((vendor_id, product_id))
        if candidate_id is None:
            continue
        try:
            raw_devnum = (entry / "devnum").read_text(encoding="ascii").strip()
        except FileNotFoundError:
            # A hot-unplug can remove devnum after idVendor/idProduct were read. A
            # subsequent complete sample will carry the stable topology edge.
            continue
        except OSError as exc:
            errors.append(
                f"{entry.name}: devnum unreadable: {type(exc).__name__}: {exc}"
            )
            continue
        if not raw_devnum.isdigit():
            errors.append(f"{entry.name}: devnum is not numeric: {raw_devnum!r}")
            continue
        devnum = int(raw_devnum)
        hub_ancestors, hub_errors = _read_usb_hub_ancestors(sysfs_root, entry.name)
        errors.extend(hub_errors)
        observed[candidate_id].append(
            {
                "id": candidate_id,
                "usb_vendor_id": vendor_id,
                "usb_product_id": product_id,
                "usb_product": _optional_sysfs_text(entry / "product"),
                "usb_serial": _optional_sysfs_text(entry / "serial"),
                "usb_port_path": entry.name,
                "usb_devnum": devnum,
                "usb_instance_generation": f"{entry.name}@{devnum}",
                "usb_hub_ancestors": hub_ancestors,
            }
        )
    for devices in observed.values():
        devices.sort(
            key=lambda item: (
                str(item["usb_port_path"]),
                int(item["usb_devnum"]),
            )
        )
    return observed, "; ".join(errors) if errors else None


def usb_baseline_from_sample(sample: dict[str, Any] | None) -> dict[str, Any]:
    """Copy the last completed sample's raw USB evidence into an action record."""
    if not isinstance(sample, dict):
        return {
            "seq": None,
            "remote_seq": None,
            "capture_started_monotonic": None,
            "usb_microphones": None,
            "usb_error": "no completed sample was available before NOW",
            "microphone": None,
            "graph_generation": None,
            "sampling": None,
        }
    return {
        "seq": sample.get("seq"),
        "remote_seq": sample.get("remote_seq"),
        "capture_started_monotonic": sample.get("capture_started_monotonic"),
        "usb_microphones": copy.deepcopy(sample.get("usb_microphones")),
        "usb_error": sample.get("usb_error"),
        "microphone": copy.deepcopy(sample.get("microphone")),
        "graph_generation": sample.get("graph_generation"),
        "sampling": copy.deepcopy(sample.get("sampling")),
    }


def _finite_number(value: Any) -> bool:
    return bool(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _source_monotonic(sample: dict[str, Any]) -> float:
    value = sample.get("capture_started_monotonic")
    if not _finite_number(value):
        raise CampaignAbort("sample omitted finite Pi/source monotonic evidence")
    return float(value)


def _sample_sequence(sample: dict[str, Any], key: str) -> int | None:
    value = sample.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def stable_usb_baseline_from_samples(
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build v2 baseline evidence from two fresh, consecutive USB samples."""
    if len(samples) != USB_BASELINE_STABLE_SAMPLES:
        raise CampaignAbort(
            f"USB action baseline needs {USB_BASELINE_STABLE_SAMPLES} stable samples"
        )
    snapshots = [usb_baseline_from_sample(sample) for sample in samples]
    for index, (sample, snapshot) in enumerate(zip(samples, snapshots, strict=True)):
        if snapshot.get("usb_error"):
            raise CampaignAbort(
                f"USB action baseline sample {index + 1} failed: "
                f"{snapshot['usb_error']}"
            )
        sampling = sample.get("sampling")
        gap = sampling.get("start_gap_s") if isinstance(sampling, dict) else None
        if not _finite_number(gap) or not 0.0 < float(gap) <= MAX_MEASURED_GAP_SECONDS:
            raise CampaignAbort(
                f"USB action baseline sample {index + 1} is stale or gapped: "
                f"start_gap_s={gap!r}"
            )
        _source_monotonic(sample)

    first, second = samples
    first_seq = _sample_sequence(first, "seq")
    second_seq = _sample_sequence(second, "seq")
    if first_seq is None or second_seq != first_seq + 1:
        raise CampaignAbort("USB action baseline samples are not consecutive")
    first_remote = _sample_sequence(first, "remote_seq")
    second_remote = _sample_sequence(second, "remote_seq")
    if (first_remote is None) != (second_remote is None) or (
        first_remote is not None and second_remote != first_remote + 1
    ):
        raise CampaignAbort("USB action baseline remote samples are not consecutive")
    source_interval = _source_monotonic(second) - _source_monotonic(first)
    if source_interval <= 0.0:
        raise CampaignAbort("USB action baseline source monotonic time is reversed")
    if source_interval > MAX_MEASURED_GAP_SECONDS:
        raise CampaignAbort("USB action baseline source monotonic samples are gapped")
    if snapshots[0]["usb_microphones"] != snapshots[1]["usb_microphones"]:
        raise CampaignAbort("USB action baseline topology is unstable")
    for candidate_id in USB_MICROPHONE_FINGERPRINTS:
        _devices, error = _validated_usb_devices(
            snapshots[-1]["usb_microphones"], candidate_id
        )
        if error:
            raise CampaignAbort(f"USB action baseline is unsafe: {error}")

    latest = copy.deepcopy(snapshots[-1])
    latest.update(
        {
            "timing_evidence_version": TIMING_EVIDENCE_VERSION,
            "stable": True,
            "sample_count": USB_BASELINE_STABLE_SAMPLES,
            "samples": snapshots,
        }
    )
    return latest


def _validated_usb_devices(
    topology: Any, candidate_id: str
) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not isinstance(topology, dict):
        return None, "USB microphone topology is missing or malformed"
    devices = topology.get(candidate_id)
    if not isinstance(devices, list):
        return None, f"USB topology omitted {candidate_id}"
    if any(not isinstance(item, dict) for item in devices):
        return None, f"USB topology for {candidate_id} is malformed"
    expected_vendor, expected_product = USB_MICROPHONE_FINGERPRINTS[candidate_id]
    generations: list[str] = []
    for item in devices:
        port = item.get("usb_port_path")
        devnum = item.get("usb_devnum")
        generation = item.get("usb_instance_generation")
        if (
            item.get("id") != candidate_id
            or item.get("usb_vendor_id") != expected_vendor
            or item.get("usb_product_id") != expected_product
            or not isinstance(port, str)
            or not port
            or not isinstance(devnum, int)
            or isinstance(devnum, bool)
            or devnum < 1
            or generation != f"{port}@{devnum}"
        ):
            return None, f"USB topology for {candidate_id} has invalid raw identity"
        generations.append(generation)
    if len(devices) > 1:
        return None, f"ambiguous {candidate_id} USB instances: {generations}"
    return devices, None


def _validated_hub_ancestors(
    device: dict[str, Any],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    device_port = device.get("usb_port_path")
    if not isinstance(device_port, str) or _usb_port_route_parts(device_port) is None:
        return None, "USB device port path is malformed"
    expected_paths = _usb_port_ancestor_names(device_port)
    ancestors = device.get("usb_hub_ancestors")
    if not isinstance(ancestors, list):
        return None, "USB device omitted hub ancestry evidence"
    generations: set[str] = set()
    observed_paths: list[str] = []
    last_position = -1
    for ancestor in ancestors:
        if not isinstance(ancestor, dict):
            return None, "USB hub ancestry is malformed"
        port = ancestor.get("usb_port_path")
        devnum = ancestor.get("usb_devnum")
        generation = ancestor.get("usb_instance_generation")
        if (
            ancestor.get("usb_device_class") != "09"
            or not isinstance(ancestor.get("usb_vendor_id"), str)
            or not ancestor["usb_vendor_id"]
            or not isinstance(ancestor.get("usb_product_id"), str)
            or not ancestor["usb_product_id"]
            or not isinstance(port, str)
            or not port
            or not isinstance(devnum, int)
            or isinstance(devnum, bool)
            or devnum < 1
            or generation != f"{port}@{devnum}"
        ):
            return None, "USB hub ancestry contains an invalid raw identity"
        if port not in expected_paths:
            return None, "USB hub ancestry contains a non-parent port path"
        position = expected_paths.index(port)
        if position <= last_position:
            return None, "USB hub ancestry is not ordered nearest-first"
        last_position = position
        observed_paths.append(port)
        if generation in generations:
            return None, "USB hub ancestry contains duplicate generations"
        generations.add(generation)
    if observed_paths != expected_paths:
        return None, "USB hub ancestry is incomplete for the device port route"
    return ancestors, None


def _external_hub_ancestor_generations(device: dict[str, Any]) -> set[str]:
    """Return non-root hub generations; on the Pi 3 these are external hubs."""
    ancestors, error = _validated_hub_ancestors(device)
    if error or ancestors is None:
        return set()
    generations: set[str] = set()
    for ancestor in ancestors:
        parsed = _usb_port_route_parts(str(ancestor["usb_port_path"]))
        if parsed is not None and len(parsed[1]) > 1:
            generations.add(str(ancestor["usb_instance_generation"]))
    return generations


def connection_layout_for_cycle(cycle: int, connection_plan: str | None) -> str | None:
    if connection_plan != CONNECTION_PLAN_DIRECT10_HUB10:
        return None
    return (
        CONNECTION_LAYOUT_DIRECT
        if cycle <= CONNECTION_LAYOUT_BOUNDARY_CYCLE
        else CONNECTION_LAYOUT_POWERED_HUB
    )


USB_IDENTITY_FIELDS = (
    "usb_vendor_id",
    "usb_product_id",
    "usb_product",
    "usb_serial",
    "usb_port_path",
    "usb_instance_generation",
)


def _normalized_usb_identity(identity: Any) -> dict[str, str | None] | None:
    if not isinstance(identity, dict):
        return None
    normalized: dict[str, str | None] = {}
    for key in USB_IDENTITY_FIELDS:
        value = identity.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            normalized[key] = None
            continue
        if not isinstance(value, str):
            return None
        value = value.strip()
        if key in {"usb_vendor_id", "usb_product_id"}:
            value = value.lower().removeprefix("0x").zfill(4)
        normalized[key] = value
    return normalized


def _raw_usb_identity(device: dict[str, Any]) -> dict[str, str | None] | None:
    return _normalized_usb_identity(device)


def _selected_usb_identity(
    selected: Any, candidate_id: str
) -> dict[str, str | None] | None:
    if not isinstance(selected, dict) or selected.get("id") != candidate_id:
        return None
    return _normalized_usb_identity(selected.get("identity"))


def usb_identity_binding_error(
    selected: Any,
    raw_device: dict[str, Any],
    candidate_id: str,
) -> str | None:
    raw_identity = _raw_usb_identity(raw_device)
    selected_identity = _selected_usb_identity(selected, candidate_id)
    if raw_identity is None:
        return f"raw {candidate_id} USB identity is malformed"
    if selected_identity is None:
        return f"runtime did not select {candidate_id} with a complete USB identity"
    mismatches = [
        key
        for key in USB_IDENTITY_FIELDS
        if raw_identity.get(key) != selected_identity.get(key)
    ]
    if mismatches:
        return (
            f"runtime-selected {candidate_id} identity does not match raw USB "
            f"instance: {', '.join(mismatches)}"
        )
    return None


def _identity_binding_evidence(
    selected: Any,
    raw_device: dict[str, Any],
    candidate_id: str,
    stage: str,
) -> dict[str, Any]:
    error = usb_identity_binding_error(selected, raw_device, candidate_id)
    return {
        "candidate_id": candidate_id,
        "stage": stage,
        "validated": error is None,
        "error": error,
        "raw_identity": _raw_usb_identity(raw_device),
        "selected_identity": _selected_usb_identity(selected, candidate_id),
    }


def _validate_stable_baseline_structure(baseline: Any) -> str | None:
    if not isinstance(baseline, dict):
        return "USB action baseline is missing"
    if baseline.get("timing_evidence_version") != TIMING_EVIDENCE_VERSION:
        return "USB action baseline omitted timing evidence version 2"
    if baseline.get("stable") is not True:
        return "USB action baseline is not marked stable"
    samples = baseline.get("samples")
    if not isinstance(samples, list) or len(samples) != USB_BASELINE_STABLE_SAMPLES:
        return "USB action baseline omitted its two stable samples"
    try:
        rebuilt = stable_usb_baseline_from_samples(samples)
    except CampaignAbort as exc:
        return str(exc)
    for key in ("seq", "remote_seq", "capture_started_monotonic", "usb_microphones"):
        if rebuilt.get(key) != baseline.get(key):
            return f"USB action baseline {key} disagrees with its stable samples"
    return None


def validate_action_usb_baseline(phase: str, baseline: dict[str, Any]) -> None:
    """Reject a pre-changed, missing, or ambiguous gated baseline before NOW."""
    gate_kind = USB_GATE_BY_PHASE.get(phase)
    if gate_kind is None:
        return
    structural_error = _validate_stable_baseline_structure(baseline)
    if structural_error:
        raise CampaignAbort(f"USB action baseline is unsafe: {structural_error}")
    baseline_error = baseline.get("usb_error")
    if baseline_error:
        raise CampaignAbort(f"USB action baseline is unsafe: {baseline_error}")
    candidate_id, expected_before, _expected_after = USB_GATE_TARGET[gate_kind]
    devices, error = _validated_usb_devices(
        baseline.get("usb_microphones"), candidate_id
    )
    if error:
        raise CampaignAbort(f"USB action baseline is unsafe: {error}")
    assert devices is not None
    if len(devices) != expected_before:
        raise CampaignAbort(
            f"USB action baseline for {gate_kind} expected {expected_before} "
            f"{candidate_id} instance(s), found {len(devices)}"
        )
    if gate_kind == "fallback":
        raw_device = devices[0]
        for sample in baseline.get("samples") or []:
            binding_error = usb_identity_binding_error(
                sample.get("microphone"), raw_device, candidate_id
            )
            if binding_error:
                raise CampaignAbort(f"USB action baseline is unsafe: {binding_error}")


def observe_expected_usb_edge(
    gate_kind: str,
    baseline: dict[str, Any],
    sample: dict[str, Any],
) -> tuple[str, str | None]:
    """Return ``waiting``, ``observed``, or ``error`` for a gated USB edge."""
    candidate_id, expected_before, expected_after = USB_GATE_TARGET[gate_kind]
    baseline_devices, baseline_error = _validated_usb_devices(
        baseline.get("usb_microphones"), candidate_id
    )
    if baseline_error:
        return "error", f"invalid USB action baseline: {baseline_error}"
    assert baseline_devices is not None
    if len(baseline_devices) != expected_before:
        return (
            "error",
            (
                f"invalid USB action baseline for {gate_kind}: expected "
                f"{expected_before}, found {len(baseline_devices)}"
            ),
        )
    if sample.get("usb_error"):
        return "error", f"USB sysfs sample failed: {sample['usb_error']}"
    devices, error = _validated_usb_devices(sample.get("usb_microphones"), candidate_id)
    if error:
        return "error", error
    assert devices is not None
    if len(devices) == expected_after:
        return "observed", None
    if len(devices) != expected_before:
        return (
            "error",
            f"unexpected {candidate_id} USB topology count {len(devices)}",
        )
    if expected_before == 1 and devices != baseline_devices:
        return (
            "error",
            f"{candidate_id} changed instance without the expected removal edge",
        )
    return "waiting", None


def selected_microphone(status: dict[str, Any]) -> dict[str, Any] | None:
    selected, _nodes = invariants.microphone_inventory(status)
    return dict(selected) if selected is not None else None


def candidate_inventory(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    microphone = status.get("microphone")
    if not isinstance(microphone, dict):
        return {}
    raw_candidates = microphone.get("candidates")
    if not isinstance(raw_candidates, list):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("id")
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        found[candidate_id] = {
            key: candidate.get(key)
            for key in (
                "id",
                "label",
                "priority",
                "state",
                "node",
                "matched_nodes",
                "reason",
            )
        }
    return found


def _unique_sources(links: list[tuple[str, str]], targets: set[str]) -> list[str]:
    return sorted({source for source, target in links if target in targets})


def evaluate_link_invariants(
    status: dict[str, Any],
    status_error: str | None,
    links: list[tuple[str, str]],
    *,
    link_error: str | None = None,
    known_hfp_sinks: set[str] | None = None,
    known_microphone_nodes: set[str] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Evaluate the fail-closed link properties on one status/link sample.

    Silence is allowed during break-before-make. Forbidden ownership is not. The phase
    evaluator separately requires the complete ACTIVE graph before calling a transition
    finished, avoiding false failures from a sequential status/link read during teardown.
    """
    violations: list[dict[str, Any]] = []

    def violation(rule: str, detail: str) -> None:
        violations.append({"id": rule, "detail": detail})

    if status_error:
        violation("H0", f"bridge status {status_error}")
    if link_error:
        violation("H0", f"PipeWire link inventory unavailable: {link_error}")

    timestamp = status.get("timestamp")
    current_wall = time.time() if now is None else now
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        violation("H0", "bridge status timestamp is absent or nonnumeric")
    elif not math.isfinite(float(timestamp)):
        violation("H0", "bridge status timestamp is non-finite")
    elif not isinstance(current_wall, (int, float)) or isinstance(current_wall, bool):
        violation("H0", "sample timestamp is absent or nonnumeric")
    elif not math.isfinite(float(current_wall)):
        violation("H0", "sample timestamp is non-finite")
    else:
        age = float(current_wall) - float(timestamp)
        if age < 0:
            violation("H0", f"bridge status timestamp is {-age:.2f}s in the future")
        elif age > 6.0:
            violation("H0", f"bridge status stale by {age:.2f}s")

    endpoints = status.get("endpoints") or {}
    if not isinstance(endpoints, dict):
        endpoints = {}
        violation("H0", "bridge endpoints are malformed")
    selected, observed_microphone_nodes = invariants.microphone_inventory(status)
    microphone_nodes = set(known_microphone_nodes or ()) | observed_microphone_nodes
    selected_node = selected.get("node") if selected else None
    candidates = candidate_inventory(status)
    blocking_candidates = sorted(
        candidate_id
        for candidate_id, candidate in candidates.items()
        if candidate.get("state") in {"ambiguous", "conflict"}
    )
    if blocking_candidates:
        violation(
            "H7",
            f"microphone selection is blocked by {blocking_candidates}",
        )

    hfp_sinks = set(known_hfp_sinks or ())
    reported_sink = endpoints.get("hfp_sink")
    if isinstance(reported_sink, str) and reported_sink:
        hfp_sinks.add(reported_sink)
    # A new HFP node can appear in PipeWire before the supervisor publishes its next
    # status snapshot. Treat any observed bluez output target as an HFP sink immediately
    # so that a transient raw/autolink route cannot hide in that status-update window.
    hfp_sinks.update(target for _source, target in links if "bluez_output" in target)
    hfp_sources = _unique_sources(links, hfp_sinks)
    hfp_targets = sorted(
        {
            target
            for source, target in links
            if target in hfp_sinks and source == MICROPHONE_OUTPUT
        }
    )
    unexpected_hfp = sorted(set(hfp_sources) - {MICROPHONE_OUTPUT})
    if unexpected_hfp:
        violation(
            "H1",
            f"only {MICROPHONE_OUTPUT} may feed HFP; found {unexpected_hfp}",
        )
    if len(hfp_sources) > 1 or len(hfp_targets) > 1:
        violation(
            "H2",
            f"duplicate HFP uplink ownership: sources={hfp_sources}, targets={hfp_targets}",
        )

    managed_targets = {AEC_CAPTURE, MICROPHONE_INPUT, *hfp_sinks}
    linked_physical_nodes = {
        source
        for source, target in links
        if source.startswith("alsa_input.") and target in managed_targets
    }
    microphone_nodes.update(linked_physical_nodes)
    inactive_nodes = microphone_nodes - (
        {str(selected_node)}
        if isinstance(selected_node, str) and selected_node
        else set()
    )

    aec_inputs = _unique_sources(links, {AEC_CAPTURE})
    microphone_inputs = _unique_sources(links, {MICROPHONE_INPUT})
    if len(aec_inputs) > 1:
        violation("H3", f"multiple physical feeds reach AEC capture: {aec_inputs}")
    if len(microphone_inputs) > 1:
        violation("H3", f"multiple feeds reach bridge.mic: {microphone_inputs}")

    inactive_routes = sorted(
        {
            (source, target)
            for source, target in links
            if source in inactive_nodes and target in managed_targets
        }
    )
    if inactive_routes:
        violation("H4", f"inactive microphone enters managed graph: {inactive_routes}")

    aec = status.get("aec") or {}
    bypass = sorted(set(microphone_inputs) - {AEC_SOURCE})
    if bypass:
        violation("H5", f"bridge.mic bypasses AEC source: {bypass}")

    state = status.get("state")
    call = status.get("call")
    call_up = isinstance(call, dict) and call.get("hfp_nodes_present") is True
    hfp_source = endpoints.get("hfp_source")
    hfp_sink = endpoints.get("hfp_sink")
    if not (
        call_up
        and isinstance(hfp_source, str)
        and hfp_source
        and isinstance(hfp_sink, str)
        and hfp_sink
    ):
        violation(
            "H9",
            "active-call HFP endpoints were not continuously present",
        )

    if state == "ACTIVE":
        owner_pid = aec.get("owner_pid") if isinstance(aec, dict) else None
        aec_ready = bool(
            isinstance(aec, dict)
            and aec.get("enabled") is True
            and aec.get("verified") is True
            and isinstance(owner_pid, int)
            and not isinstance(owner_pid, bool)
            and owner_pid > 0
        )
        if not aec_ready:
            violation("H5", "ACTIVE requires enabled, verified, owned AEC")
        expected_aec_inputs = {str(selected_node)} if selected_node else set()
        # Break-before-make deliberately has intervals with no microphone routes.
        # Missing links are safe silence; the phase expectation below still refuses
        # to call ACTIVE complete until the exact AEC path has returned.
        if aec_inputs and set(aec_inputs) != expected_aec_inputs:
            violation(
                "H5",
                f"ACTIVE AEC capture ownership is {aec_inputs}",
            )
        if microphone_inputs and set(microphone_inputs) != {AEC_SOURCE}:
            violation(
                "H5",
                f"ACTIVE bridge.mic input ownership is {microphone_inputs}",
            )

    if state == "WAITING_MIC":
        lingering = {
            "hfp_inputs": hfp_sources,
            "aec_inputs": aec_inputs,
            "microphone_inputs": microphone_inputs,
        }
        if any(lingering.values()):
            violation("H6", f"WAITING_MIC retained microphone routes: {lingering}")
        if isinstance(aec, dict) and aec.get("owner_pid"):
            violation("H6", f"WAITING_MIC retained AEC owner {aec.get('owner_pid')}")

    return {
        "passed": not violations,
        "violations": violations,
        "selected_node": selected_node,
        "candidate_nodes": sorted(microphone_nodes),
        "inactive_candidate_nodes": sorted(inactive_nodes),
        "hfp_sinks": sorted(hfp_sinks),
        "hfp_inputs": hfp_sources,
        "hfp_uplink_targets": hfp_targets,
        "aec_capture_inputs": aec_inputs,
        "microphone_inputs": microphone_inputs,
    }


def parse_service_restart_counts(
    text: str, units: tuple[str, ...] = SERVICE_UNITS
) -> dict[str, int | None]:
    counts: dict[str, int | None] = {unit: None for unit in units}
    fields: dict[str, str] = {}

    def consume_block() -> None:
        unit = fields.get("Id")
        value = fields.get("NRestarts")
        if unit in counts and isinstance(value, str):
            counts[unit] = int(value) if value.isdigit() else None
        fields.clear()

    for raw in [*text.splitlines(), ""]:
        line = raw.strip()
        if not line:
            consume_block()
            continue
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    return counts


def service_restart_delta(
    before: dict[str, int | None], after: dict[str, int | None]
) -> dict[str, int | None]:
    return {
        unit: (
            int(after[unit]) - int(before[unit])
            if before.get(unit) is not None and after.get(unit) is not None
            else None
        )
        for unit in SERVICE_UNITS
    }


def query_service_restart_counts() -> tuple[dict[str, int | None], str | None]:
    output, error = command_output(
        [
            "systemctl",
            "--user",
            "show",
            *SERVICE_UNITS,
            "--property=Id",
            "--property=NRestarts",
            "--no-pager",
        ],
        timeout=3.0,
    )
    if error:
        return {unit: None for unit in SERVICE_UNITS}, error
    counts = parse_service_restart_counts(output)
    if any(value is None for value in counts.values()):
        return counts, "one or more NRestarts values are missing"
    return counts, None


@dataclass(frozen=True)
class Expectation:
    state: str
    selected_id: str | None = None
    require_no_selection: bool = False
    candidate_states: dict[str, frozenset[str]] = field(default_factory=dict)
    same_instance_token: str | None = None
    different_instance_token: str | None = None
    same_generation: int | None = None
    generation_after: int | None = None


def expectation_failures(sample: dict[str, Any], expected: Expectation) -> list[str]:
    failures: list[str] = []
    if sample.get("status_error"):
        failures.append(str(sample["status_error"]))
    if sample.get("link_error"):
        failures.append(str(sample["link_error"]))
    if sample.get("state") != expected.state:
        failures.append(
            f"state is {sample.get('state')!r}, expected {expected.state!r}"
        )

    selected = sample.get("microphone")
    selected = selected if isinstance(selected, dict) else None
    if expected.require_no_selection:
        if selected is not None:
            failures.append(f"microphone {selected.get('id')!r} is still selected")
    elif expected.selected_id is not None:
        observed_id = selected.get("id") if selected else None
        if observed_id != expected.selected_id:
            failures.append(
                f"selected microphone is {observed_id!r}, expected {expected.selected_id!r}"
            )

    candidates = sample.get("candidates") or {}
    for candidate_id, accepted_states in expected.candidate_states.items():
        candidate = candidates.get(candidate_id) or {}
        state = candidate.get("state")
        if state not in accepted_states:
            failures.append(
                f"candidate {candidate_id!r} state is {state!r}, "
                f"expected one of {sorted(accepted_states)!r}"
            )

    token = selected.get("instance_token") if selected else None
    identity_token_required = bool(
        expected.selected_id is not None
        or expected.same_instance_token is not None
        or expected.different_instance_token is not None
    )
    if identity_token_required and not (isinstance(token, str) and token):
        failures.append("selected microphone instance token is missing")
    elif (
        expected.same_instance_token is not None
        and token != expected.same_instance_token
    ):
        failures.append("selected microphone instance token changed")
    elif (
        expected.different_instance_token is not None
        and token == expected.different_instance_token
    ):
        failures.append("selected microphone instance token did not change")

    generation = sample.get("graph_generation")
    generation_required = bool(
        expected.selected_id is not None
        or expected.same_generation is not None
        or expected.generation_after is not None
    )
    if generation_required and (
        not isinstance(generation, int) or isinstance(generation, bool)
    ):
        failures.append("graph generation is missing")
    elif (
        expected.same_generation is not None and generation != expected.same_generation
    ):
        failures.append(
            f"graph generation changed from {expected.same_generation!r} to {generation!r}"
        )
    elif (
        expected.generation_after is not None
        and generation <= expected.generation_after
    ):
        failures.append(
            f"graph generation {generation!r} did not advance past "
            f"{expected.generation_after!r}"
        )

    link_invariants = sample.get("invariants") or {}
    if link_invariants.get("violations"):
        failures.append("one or more link invariants are violated")

    hfp_inputs = set(link_invariants.get("hfp_inputs") or [])
    aec_inputs = set(link_invariants.get("aec_capture_inputs") or [])
    microphone_inputs = set(link_invariants.get("microphone_inputs") or [])
    if expected.state == "ACTIVE":
        if hfp_inputs != {MICROPHONE_OUTPUT}:
            failures.append(f"ACTIVE HFP input ownership is {sorted(hfp_inputs)}")
        selected_node = selected.get("node") if selected else None
        aec = sample.get("aec") or {}
        owner_pid = aec.get("owner_pid") if isinstance(aec, dict) else None
        if not (
            isinstance(aec, dict)
            and aec.get("enabled") is True
            and aec.get("verified") is True
            and isinstance(owner_pid, int)
            and not isinstance(owner_pid, bool)
            and owner_pid > 0
        ):
            failures.append("ACTIVE AEC is not enabled, verified, and owned")
        if aec_inputs != ({selected_node} if selected_node else set()):
            failures.append(f"ACTIVE AEC capture ownership is {sorted(aec_inputs)}")
        if microphone_inputs != {AEC_SOURCE}:
            failures.append(
                f"ACTIVE bridge.mic input ownership is {sorted(microphone_inputs)}"
            )
    elif expected.state == "WAITING_MIC":
        if hfp_inputs or aec_inputs or microphone_inputs:
            failures.append("WAITING_MIC still has an uplink or microphone route")
        if (sample.get("aec") or {}).get("owner_pid"):
            failures.append("WAITING_MIC still has an AEC owner")
    return failures


def _timing_evidence_error(item: dict[str, Any], kind: str) -> str | None:
    if item.get("timing_evidence_version") != TIMING_EVIDENCE_VERSION:
        return "transition omitted timing evidence version 2"
    if item.get("timing_origin") != USB_TIMING_ORIGIN:
        return "transition is not anchored to a USB sysfs edge"
    baseline = item.get("usb_baseline")
    baseline_error = _validate_stable_baseline_structure(baseline)
    if baseline_error:
        return baseline_error
    phase_by_kind = {
        "promotion": "lark_promotion",
        "fallback": "lark_fallback",
        "fifine_replug": "restore_fifine",
    }
    try:
        validate_action_usb_baseline(phase_by_kind[kind], baseline)
    except (CampaignAbort, KeyError) as exc:
        return str(exc)
    settle_requirement = item.get("settle_requirement_s")
    if (
        not _finite_number(settle_requirement)
        or float(settle_requirement) < QUALIFICATION_MIN_SETTLE_SECONDS
    ):
        return (
            "transition omitted the immutable qualification settle requirement "
            f"of at least {QUALIFICATION_MIN_SETTLE_SECONDS:.2f}s"
        )

    event = item.get("usb_event")
    if not isinstance(event, dict):
        return "transition omitted USB event evidence"
    if event.get("confirmed") is not True or event.get("persistent") is not True:
        return "USB event is not confirmed and persistent"
    persistence = event.get("persistence_samples")
    if (
        not isinstance(persistence, list)
        or len(persistence) < USB_EVENT_CONFIRMATION_SAMPLES
    ):
        return "USB event omitted two-sample confirmation evidence"
    first = persistence[0]
    if not isinstance(first, dict):
        return "USB event first sample is malformed"
    candidate_id, _before, expected_after = USB_GATE_TARGET[kind]
    first_devices, first_error = _validated_usb_devices(
        first.get("usb_microphones"), candidate_id
    )
    if first_error or len(first_devices or []) != expected_after:
        return first_error or "USB event first sample has the wrong topology"
    for key in (
        "seq",
        "remote_seq",
        "capture_started_monotonic",
        "usb_microphones",
        "devices",
    ):
        expected_value = first_devices if key == "devices" else first.get(key)
        if event.get(key) != expected_value:
            return f"USB event {key} disagrees with its first persistence sample"
    for previous, sample in pairwise(persistence):
        if not isinstance(sample, dict):
            return "USB event persistence sample is malformed"
        error = _latched_usb_topology_error(first, previous, sample, kind)
        if error:
            return error
    confirmation = event.get("confirmation")
    if not isinstance(confirmation, dict):
        return "USB event confirmation sample is missing"
    for key in ("seq", "remote_seq", "capture_started_monotonic", "usb_microphones"):
        if confirmation.get(key) != persistence[1].get(key):
            return f"USB event confirmation {key} disagrees with persistence evidence"
    if event.get("stable_through_seq") != persistence[-1].get("seq"):
        return "USB event stable-through sequence is inconsistent"

    outcome = item.get("outcome")
    binding = item.get("usb_identity_binding")
    if kind == "fallback":
        devices, _error = _validated_usb_devices(
            baseline.get("usb_microphones"), candidate_id
        )
        assert devices
        expected_binding = _identity_binding_evidence(
            baseline.get("microphone"), devices[0], candidate_id, "preaction"
        )
        if binding != expected_binding or not expected_binding["validated"]:
            return "fallback baseline identity binding is invalid"
    elif outcome == "completed":
        final_sample = item.get("final_sample")
        if not isinstance(final_sample, dict) or not first_devices:
            return "completed transition omitted its final selected sample"
        expected_binding = _identity_binding_evidence(
            final_sample.get("microphone"),
            first_devices[0],
            candidate_id,
            "final_selected",
        )
        if binding != expected_binding or not expected_binding["validated"]:
            return "final selected identity binding is invalid"

    if outcome == "completed":
        final_sample = item.get("final_sample")
        if not isinstance(final_sample, dict):
            return "completed transition omitted its final selected sample"
        final_candidate_id = USB_FINAL_SELECTED_CANDIDATE[kind]
        final_devices, final_error = _validated_usb_devices(
            first.get("usb_microphones"), final_candidate_id
        )
        if final_error or len(final_devices or []) != 1:
            return final_error or (
                f"completed transition did not retain exactly one raw "
                f"{final_candidate_id} instance"
            )
        expected_final_binding = _identity_binding_evidence(
            final_sample.get("microphone"),
            final_devices[0],
            final_candidate_id,
            "final_selected",
        )
        if (
            item.get("usb_final_identity_binding") != expected_final_binding
            or not expected_final_binding["validated"]
        ):
            return "completed transition final selected identity binding is invalid"

    if outcome in {"completed", "safe_state"}:
        final_sample = item.get("final_sample")
        if not isinstance(final_sample, dict):
            return "bounded transition omitted its final sample"
        if final_sample.get("seq") != persistence[-1].get("seq"):
            return "final sample is not covered by persistent USB evidence"
        if final_sample.get("usb_microphones") != persistence[-1].get(
            "usb_microphones"
        ):
            return "final sample USB topology disagrees with persistence evidence"
    if outcome == "completed":
        first_match = item.get("first_matching_sample")
        final_sample = item.get("final_sample")
        if not isinstance(first_match, dict) or not isinstance(final_sample, dict):
            return "completed transition omitted matching/final samples"
        try:
            transition_latency = _sample_interval_seconds(first, first_match)
            settled_latency = _sample_interval_seconds(first, final_sample)
            state_settle = _sample_interval_seconds(first_match, final_sample)
        except CampaignAbort as exc:
            return str(exc)
        reported = item.get("transition_latency_s")
        settled = item.get("settled_latency_s")
        if (
            not _finite_number(reported)
            or abs(float(reported) - transition_latency) > 1e-5
        ):
            return (
                "reported transition latency disagrees with source monotonic evidence"
            )
        if not _finite_number(settled) or abs(float(settled) - settled_latency) > 1e-5:
            return "reported settle latency disagrees with source monotonic evidence"
        reported_state_settle = item.get("state_settle_s")
        if (
            not _finite_number(reported_state_settle)
            or abs(float(reported_state_settle) - state_settle) > 1e-5
        ):
            return "reported state settle disagrees with source monotonic evidence"
        if state_settle + 1e-6 < float(settle_requirement):
            return "completed transition did not remain stable for the required settle"
        if settled_latency > QUALIFICATION_MAX_LIMIT_SECONDS + 1e-6:
            return "completed transition settled after the 60-second deadline"
    elif outcome == "safe_state":
        final_sample = item.get("final_sample")
        assert isinstance(final_sample, dict)
        try:
            safe_state_latency = _sample_interval_seconds(first, final_sample)
        except CampaignAbort as exc:
            return str(exc)
        reported_safe_latency = item.get("safe_state_latency_s")
        if (
            not _finite_number(reported_safe_latency)
            or abs(float(reported_safe_latency) - safe_state_latency) > 1e-5
        ):
            return (
                "reported safe-state latency disagrees with source monotonic evidence"
            )
        if safe_state_latency > QUALIFICATION_MAX_LIMIT_SECONDS + 1e-6:
            return "actionable safe state was reached after the 60-second deadline"
    return None


def summarize_timing_gate(
    transitions: list[dict[str, Any]],
    *,
    kind: str,
    expected_cycles: int,
    fast_limit_s: float = DEFAULT_FAST_LIMIT_SECONDS,
    max_limit_s: float = DEFAULT_TRANSITION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Assess one of the two immutable E18 matrix/repeated field gates."""
    matrix_gate = expected_cycles == QUALIFICATION_MATRIX_CYCLES
    expected_cycles = (
        QUALIFICATION_MATRIX_CYCLES if matrix_gate else QUALIFICATION_CYCLES
    )
    del fast_limit_s, max_limit_s
    fast_limit_s = QUALIFICATION_FAST_LIMIT_SECONDS
    max_limit_s = QUALIFICATION_MAX_LIMIT_SECONDS
    considered = [item for item in transitions if item.get("gate_kind") == kind]
    evidence_valid = [
        item for item in considered if _timing_evidence_error(item, kind) is None
    ]
    completed_fast = sum(
        1
        for item in evidence_valid
        if item.get("outcome") == "completed"
        and item.get("timing_origin") == USB_TIMING_ORIGIN
        and isinstance(item.get("transition_latency_s"), (int, float))
        and not isinstance(item.get("transition_latency_s"), bool)
        and 0.0 <= float(item["transition_latency_s"]) <= fast_limit_s
    )
    bounded_or_safe = sum(
        1
        for item in evidence_valid
        if (
            item.get("outcome") == "completed"
            and item.get("timing_origin") == USB_TIMING_ORIGIN
            and isinstance(item.get("transition_latency_s"), (int, float))
            and not isinstance(item.get("transition_latency_s"), bool)
            and 0.0 <= float(item["transition_latency_s"]) <= max_limit_s
        )
        or (
            item.get("outcome") == "safe_state"
            and item.get("timing_origin") == USB_TIMING_ORIGIN
        )
    )
    usb_timed = sum(
        1 for item in considered if item.get("timing_origin") == USB_TIMING_ORIGIN
    )
    structurally_valid = len(evidence_valid)
    safety_clean = sum(1 for item in considered if item.get("safety_clean"))
    restart_clean = sum(1 for item in considered if item.get("restart_clean"))
    required_fast = 1 if matrix_gate else QUALIFICATION_REQUIRED_FAST
    enough = len(considered) == expected_cycles
    passed = bool(
        enough
        and completed_fast >= required_fast
        and bounded_or_safe == expected_cycles
        and safety_clean == expected_cycles
        and restart_clean == expected_cycles
        and usb_timed == expected_cycles
        and structurally_valid == expected_cycles
    )
    return {
        "verdict": (
            "PASS"
            if passed
            else ("INCOMPLETE" if len(considered) < expected_cycles else "FAIL")
        ),
        "kind": kind,
        "expected_cycles": expected_cycles,
        "observed_cycles": len(considered),
        "fast_limit_s": fast_limit_s,
        "max_limit_s": max_limit_s,
        "required_completed_under_fast_limit": required_fast,
        "completed_under_fast_limit": completed_fast,
        "within_max_or_actionable_safe": bounded_or_safe,
        "required_timing_origin": USB_TIMING_ORIGIN,
        "usb_timed_cycles": usb_timed,
        "required_timing_evidence_version": TIMING_EVIDENCE_VERSION,
        "structurally_valid_usb_evidence_cycles": structurally_valid,
        "safety_clean_cycles": safety_clean,
        "restart_clean_cycles": restart_clean,
    }


class LiveSampler:
    """Continuously sample status and PipeWire while the main thread prompts the operator."""

    def __init__(self, status_path: Path, timeline_path: Path, interval: float):
        self.status_path = status_path
        self.timeline_path = timeline_path
        self.interval = interval
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._timeline: TextIO | None = None
        self._sample_thread: threading.Thread | None = None
        self._service_thread: threading.Thread | None = None
        self._recent: deque[dict[str, Any]] = deque(maxlen=1200)
        self._phase = "startup"
        self._cycle = 0
        self._action_id: str | None = None
        self._connection_layout: str | None = None
        self._seq = 0
        self._started_monotonic = 0.0
        self._last_sample_monotonic: float | None = None
        self._known_hfp_sinks: set[str] = set()
        self._known_microphone_nodes: set[str] = set()
        self._service_counts: dict[str, int | None] = {
            unit: None for unit in SERVICE_UNITS
        }
        self._service_error: str | None = "not sampled"
        self.initial_service_counts: dict[str, int | None] = {}
        self.final_service_counts: dict[str, int | None] = {}
        self.total_samples = 0
        self.sample_gap_sum = 0.0
        self.sample_gap_count = 0
        self.max_sample_gap = 0.0
        self.max_capture_duration = 0.0
        self.deadline_misses = 0
        self.violation_counts: Counter[str] = Counter()
        self.first_violations: list[dict[str, Any]] = []

    def _emit(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
        with self._write_lock:
            if self._timeline is None:
                return
            self._timeline.write(payload + "\n")
            self._timeline.flush()

    def _query_service_counts(self) -> tuple[dict[str, int | None], str | None]:
        return query_service_restart_counts()

    def start(self) -> None:
        self.timeline_path.parent.mkdir(parents=True, exist_ok=True)
        self._timeline = self.timeline_path.open("x", encoding="utf-8", buffering=1)
        self._started_monotonic = time.monotonic()
        counts, error = self._query_service_counts()
        with self._condition:
            self._service_counts = counts
            self._service_error = error
            self.initial_service_counts = dict(counts)
        self._emit(
            {
                "type": "run_start",
                "schema_version": SCHEMA_VERSION,
                "timestamp": time.time(),
                "interval_s": self.interval,
                "service_restart_counts": counts,
                "service_error": error,
            }
        )
        self._sample_thread = threading.Thread(
            target=self._sample_loop, name="hotplug-link-sampler", daemon=True
        )
        self._service_thread = threading.Thread(
            target=self._service_loop, name="hotplug-service-sampler", daemon=True
        )
        self._sample_thread.start()
        self._service_thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        for thread in (self._sample_thread, self._service_thread):
            if thread is not None:
                thread.join(timeout=5)
        counts, error = self._query_service_counts()
        with self._condition:
            self._service_counts = counts
            self._service_error = error
            self.final_service_counts = dict(counts)
        self._emit(
            {
                "type": "run_stop",
                "timestamp": time.time(),
                "elapsed_s": round(time.monotonic() - self._started_monotonic, 6),
                "service_restart_counts": counts,
                "service_error": error,
            }
        )
        with self._write_lock:
            if self._timeline is not None:
                self._timeline.close()
                self._timeline = None

    def _service_loop(self) -> None:
        while not self._stop.wait(2.0):
            counts, error = self._query_service_counts()
            with self._condition:
                self._service_counts = counts
                self._service_error = error

    def _sample_loop(self) -> None:
        next_deadline = time.monotonic()
        while not self._stop.is_set():
            self._capture_sample(next_deadline)
            next_deadline += self.interval
            remaining = next_deadline - time.monotonic()
            if remaining > 0:
                self._stop.wait(remaining)
            else:
                self.deadline_misses += 1
                next_deadline = time.monotonic()

    def _capture_sample(self, deadline: float) -> None:
        # Reserve identity and action metadata before any blocking reads. An action that
        # occurs while this capture is in flight will therefore exclude this sample by
        # sequence and cannot relabel it as post-action evidence.
        with self._condition:
            started = time.monotonic()
            gap = (
                started - self._last_sample_monotonic
                if self._last_sample_monotonic is not None
                else None
            )
            self._last_sample_monotonic = started
            self._seq += 1
            reservation = {
                "seq": self._seq,
                "timestamp": time.time(),
                "elapsed_s": round(started - self._started_monotonic, 6),
                "capture_started_monotonic": started,
                "phase": self._phase,
                "cycle": self._cycle,
                "action_id": self._action_id,
                "connection_layout": self._connection_layout,
                "service_restart_counts": dict(self._service_counts),
                "service_error": self._service_error,
                "gap": gap,
            }

        usb_microphones, usb_error = read_usb_microphones()
        status, status_error = read_status(self.status_path)
        link_text, link_error = command_output(
            ["pw-link", "-l"], timeout=max(0.05, min(self.interval * 0.80, 0.20))
        )
        links = parse_pw_links(link_text) if link_error is None else []
        endpoints = status.get("endpoints") or {}
        if isinstance(endpoints, dict):
            hfp_sink = endpoints.get("hfp_sink")
            if isinstance(hfp_sink, str) and hfp_sink:
                self._known_hfp_sinks.add(hfp_sink)
        _selected, observed_microphone_nodes = invariants.microphone_inventory(status)
        self._known_microphone_nodes.update(observed_microphone_nodes)
        evaluated = evaluate_link_invariants(
            status,
            status_error,
            links,
            link_error=link_error,
            known_hfp_sinks=self._known_hfp_sinks,
            known_microphone_nodes=self._known_microphone_nodes,
        )
        selected = selected_microphone(status)
        microphone = status.get("microphone") or {}
        aec = status.get("aec") or {}
        finished = time.monotonic()
        with self._condition:
            gap = reservation["gap"]
            sample = {
                "type": "sample",
                "schema_version": SCHEMA_VERSION,
                "seq": reservation["seq"],
                "timestamp": reservation["timestamp"],
                "elapsed_s": reservation["elapsed_s"],
                "capture_started_monotonic": reservation["capture_started_monotonic"],
                "phase": reservation["phase"],
                "cycle": reservation["cycle"],
                "action_id": reservation["action_id"],
                "connection_layout": reservation["connection_layout"],
                "state": status.get("state"),
                "status_timestamp": status.get("timestamp"),
                "status_error": status_error,
                "usb_microphones": usb_microphones,
                "usb_error": usb_error,
                "selection_reason": (
                    microphone.get("selection_reason")
                    if isinstance(microphone, dict)
                    else None
                ),
                "microphone": selected,
                "candidates": candidate_inventory(status),
                "graph_generation": status.get("generation"),
                "aec": {
                    key: aec.get(key) for key in ("enabled", "verified", "owner_pid")
                }
                if isinstance(aec, dict)
                else {},
                "links": [list(pair) for pair in links],
                "link_error": link_error,
                "invariants": evaluated,
                "service_restart_counts": reservation["service_restart_counts"],
                "service_error": reservation["service_error"],
                "sampling": {
                    "configured_interval_s": self.interval,
                    "start_gap_s": round(gap, 6) if gap is not None else None,
                    "capture_duration_s": round(finished - started, 6),
                    "deadline_late_s": round(max(0.0, started - deadline), 6),
                },
            }
            self._recent.append(sample)
            self.total_samples += 1
            duration = finished - started
            self.max_capture_duration = max(self.max_capture_duration, duration)
            if gap is not None:
                self.sample_gap_sum += gap
                self.sample_gap_count += 1
                self.max_sample_gap = max(self.max_sample_gap, gap)
            for item in evaluated["violations"]:
                rule = str(item.get("id") or "unknown")
                self.violation_counts[rule] += 1
                if len(self.first_violations) < 20:
                    self.first_violations.append(
                        {
                            "seq": reservation["seq"],
                            "timestamp": sample["timestamp"],
                            "phase": reservation["phase"],
                            **item,
                        }
                    )
            self._condition.notify_all()
        self._emit(sample)

    def _action_usb_baseline_locked(self, phase: str, after_seq: int) -> dict[str, Any]:
        deadline = time.monotonic() + USB_BASELINE_OBSERVATION_TIMEOUT_SECONDS
        required = USB_BASELINE_DISCARD_SAMPLES + USB_BASELINE_STABLE_SAMPLES
        while True:
            fresh = [
                sample
                for sample in self._recent
                if int(sample.get("seq", -1)) > after_seq
            ]
            if len(fresh) >= required:
                observed = fresh[USB_BASELINE_DISCARD_SAMPLES:]
                for earlier, later in pairwise(observed):
                    stable_usb_baseline_from_samples([earlier, later])
                baseline = stable_usb_baseline_from_samples(
                    observed[-USB_BASELINE_STABLE_SAMPLES:]
                )
                validate_action_usb_baseline(phase, baseline)
                return baseline
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CampaignAbort(
                    "USB action baseline timed out waiting for fresh post-query samples"
                )
            self._condition.wait(min(remaining, max(self.interval * 2.0, 0.05)))

    def mark_action(
        self,
        phase: str,
        cycle: int,
        instruction: str,
        connection_layout: str | None = None,
    ) -> dict[str, Any]:
        counts, error = self._query_service_counts()
        if error:
            raise CampaignAbort(f"restart-count evidence failed: {error}")
        with self._condition:
            self._service_counts = dict(counts)
            self._service_error = None
            post_query_seq = self._seq
            usb_baseline = self._action_usb_baseline_locked(phase, post_query_seq)
            action_monotonic = time.monotonic()
            self._phase = phase
            self._cycle = cycle
            self._action_id = f"{phase}:{cycle}:{time.time_ns()}"
            self._connection_layout = connection_layout
            event = {
                "type": "operator_action",
                "timestamp": time.time(),
                "elapsed_s": round(action_monotonic - self._started_monotonic, 6),
                "monotonic": action_monotonic,
                "after_seq": int(usb_baseline["seq"]),
                "phase": phase,
                "cycle": cycle,
                "action_id": self._action_id,
                "connection_layout": connection_layout,
                "instruction": instruction,
                "usb_baseline": usb_baseline,
                "usb_action_observation_timeout_s": (
                    USB_ACTION_OBSERVATION_TIMEOUT_SECONDS
                    if phase in USB_GATE_BY_PHASE
                    else None
                ),
                "service_restart_counts": counts,
            }
        self._emit({key: value for key, value in event.items() if key != "monotonic"})
        return event

    def record_event(self, event_type: str, **values: Any) -> None:
        self._emit(
            {
                "type": event_type,
                "timestamp": time.time(),
                "elapsed_s": round(time.monotonic() - self._started_monotonic, 6),
                **values,
            }
        )

    def samples_after(self, seq: int) -> list[dict[str, Any]]:
        with self._condition:
            return [sample for sample in self._recent if int(sample["seq"]) > seq]

    def wait_for_new_sample(self, seq: int, timeout: float = 1.0) -> None:
        with self._condition:
            if not self._recent or int(self._recent[-1]["seq"]) <= seq:
                self._condition.wait(timeout)

    def latest(self) -> dict[str, Any] | None:
        with self._condition:
            return dict(self._recent[-1]) if self._recent else None

    def service_counts(self) -> dict[str, int | None]:
        counts, error = self._query_service_counts()
        with self._condition:
            self._service_counts = dict(counts)
            self._service_error = error
        return counts


def _dedupe_transition_violations(
    samples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for sample in samples:
        for violation in (sample.get("invariants") or {}).get("violations") or []:
            key = (str(violation.get("id")), str(violation.get("detail")))
            found.setdefault(
                key,
                {
                    **violation,
                    "first_seq": sample.get("seq"),
                    "first_timestamp": sample.get("timestamp"),
                },
            )
    return list(found.values())


def _sample_captured_by_deadline(
    sample: dict[str, Any],
    action: dict[str, Any],
    deadline: float,
) -> bool:
    host_observed = sample.get("host_received_monotonic")
    if isinstance(host_observed, (int, float)) and not isinstance(host_observed, bool):
        return float(host_observed) <= deadline
    capture_monotonic = sample.get("capture_started_monotonic")
    if isinstance(capture_monotonic, (int, float)) and not isinstance(
        capture_monotonic, bool
    ):
        return float(capture_monotonic) <= deadline
    sample_elapsed = sample.get("elapsed_s")
    action_elapsed = action.get("elapsed_s")
    timeout_s = deadline - float(action["monotonic"])
    return bool(
        isinstance(sample_elapsed, (int, float))
        and not isinstance(sample_elapsed, bool)
        and isinstance(action_elapsed, (int, float))
        and not isinstance(action_elapsed, bool)
        and float(sample_elapsed) <= float(action_elapsed) + timeout_s
    )


def _sample_observed_monotonic(sample: dict[str, Any], action: dict[str, Any]) -> float:
    for key in ("host_received_monotonic", "capture_started_monotonic"):
        value = sample.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    sample_elapsed = sample.get("elapsed_s")
    action_elapsed = action.get("elapsed_s")
    if not (
        isinstance(sample_elapsed, (int, float))
        and not isinstance(sample_elapsed, bool)
        and isinstance(action_elapsed, (int, float))
        and not isinstance(action_elapsed, bool)
    ):
        raise CampaignAbort("sample omitted monotonic observation evidence")
    return float(action["monotonic"]) + float(sample_elapsed) - float(action_elapsed)


def _sample_interval_seconds(earlier: dict[str, Any], later: dict[str, Any]) -> float:
    """Measure an interval within the sampler's source monotonic clock domain."""
    interval = _source_monotonic(later) - _source_monotonic(earlier)
    if not math.isfinite(interval) or interval < 0.0:
        raise CampaignAbort("Pi/source monotonic sample timing is reversed")
    return interval


def _usb_event_evidence(sample: dict[str, Any], gate_kind: str) -> dict[str, Any]:
    candidate_id, _before, _after = USB_GATE_TARGET[gate_kind]
    topology = sample.get("usb_microphones") or {}
    return {
        "seq": sample.get("seq"),
        "remote_seq": sample.get("remote_seq"),
        "timestamp": sample.get("timestamp"),
        "elapsed_s": sample.get("elapsed_s"),
        "remote_elapsed_s": sample.get("remote_elapsed_s"),
        "capture_started_monotonic": sample.get("capture_started_monotonic"),
        "candidate_id": candidate_id,
        "devices": copy.deepcopy(topology.get(candidate_id)),
        "usb_microphones": copy.deepcopy(sample.get("usb_microphones")),
        "usb_error": sample.get("usb_error"),
        "sampling": copy.deepcopy(sample.get("sampling")),
    }


def _event_sample_structure(sample: dict[str, Any], gate_kind: str) -> dict[str, Any]:
    evidence = _usb_event_evidence(sample, gate_kind)
    evidence["microphone"] = copy.deepcopy(sample.get("microphone"))
    return evidence


def _consecutive_source_samples(
    earlier: dict[str, Any], later: dict[str, Any]
) -> str | None:
    earlier_seq = _sample_sequence(earlier, "seq")
    later_seq = _sample_sequence(later, "seq")
    if earlier_seq is None or later_seq != earlier_seq + 1:
        return "USB event samples are not consecutive"
    earlier_remote = _sample_sequence(earlier, "remote_seq")
    later_remote = _sample_sequence(later, "remote_seq")
    if (earlier_remote is None) != (later_remote is None) or (
        earlier_remote is not None and later_remote != earlier_remote + 1
    ):
        return "USB event remote samples are not consecutive"
    try:
        interval = _sample_interval_seconds(earlier, later)
    except CampaignAbort as exc:
        return str(exc)
    if interval <= 0.0:
        return "USB event source monotonic samples are not strictly increasing"
    if interval > MAX_MEASURED_GAP_SECONDS:
        return "USB event source monotonic samples are gapped"
    sampling = later.get("sampling")
    gap = sampling.get("start_gap_s") if isinstance(sampling, dict) else interval
    if not _finite_number(gap) or not 0.0 < float(gap) <= MAX_MEASURED_GAP_SECONDS:
        return f"USB event sample is stale or gapped: start_gap_s={gap!r}"
    return None


def _latched_usb_topology_error(
    latched_sample: dict[str, Any],
    previous_sample: dict[str, Any],
    sample: dict[str, Any],
    gate_kind: str,
) -> str | None:
    sequence_error = _consecutive_source_samples(previous_sample, sample)
    if sequence_error:
        return sequence_error
    if sample.get("usb_error"):
        return f"USB sysfs sample failed: {sample['usb_error']}"
    candidate_id, _before, expected_after = USB_GATE_TARGET[gate_kind]
    devices, error = _validated_usb_devices(sample.get("usb_microphones"), candidate_id)
    if error:
        return error
    assert devices is not None
    if len(devices) != expected_after:
        return f"latched {gate_kind} USB edge did not persist"
    if sample.get("usb_microphones") != latched_sample.get("usb_microphones"):
        return f"latched {gate_kind} USB topology changed after the first edge"
    return None


def wait_for_expectation(
    sampler: LiveSampler,
    action: dict[str, Any],
    expected: Expectation,
    *,
    timeout_s: float,
    settle_s: float,
    gate_kind: str | None,
) -> dict[str, Any]:
    gated_usb_timing = gate_kind in USB_GATE_TARGET
    action_observation_timeout_s = float(
        action.get("usb_action_observation_timeout_s")
        or USB_ACTION_OBSERVATION_TIMEOUT_SECONDS
    )
    deadline = float(action["monotonic"]) + (
        action_observation_timeout_s if gated_usb_timing else timeout_s
    )
    after_seq = int(action["after_seq"])
    first_match: dict[str, Any] | None = None
    usb_edge_sample: dict[str, Any] | None = None
    usb_confirmation_sample: dict[str, Any] | None = None
    previous_usb_sample: dict[str, Any] | None = None
    usb_event_samples: list[dict[str, Any]] = []
    usb_event_error: str | None = None
    usb_identity_binding: dict[str, Any] | None = None
    usb_final_identity_binding: dict[str, Any] | None = None
    latest: dict[str, Any] | None = None
    processed_samples: list[dict[str, Any]] = []
    seen_seq = after_seq
    outcome = "timeout"

    if gated_usb_timing:
        baseline = action.get("usb_baseline")
        try:
            if not isinstance(baseline, dict):
                raise CampaignAbort("USB action baseline is missing")
            validate_action_usb_baseline(str(action.get("phase")), baseline)
            if gate_kind == "fallback":
                candidate_id = USB_GATE_TARGET[gate_kind][0]
                devices, _error = _validated_usb_devices(
                    baseline.get("usb_microphones"), candidate_id
                )
                assert devices
                usb_identity_binding = _identity_binding_evidence(
                    baseline.get("microphone"),
                    devices[0],
                    candidate_id,
                    "preaction",
                )
        except CampaignAbort as exc:
            usb_event_error = str(exc)
            outcome = "usb_event_error"

    while True:
        if outcome == "usb_event_error":
            break
        for sample in sampler.samples_after(after_seq):
            seq = int(sample["seq"])
            if seq <= seen_seq:
                continue
            seen_seq = seq
            if not _sample_captured_by_deadline(sample, action, deadline):
                continue
            latest = sample
            processed_samples.append(sample)
            if gated_usb_timing:
                assert gate_kind is not None
                if usb_edge_sample is None:
                    edge_state, edge_error = observe_expected_usb_edge(
                        gate_kind, action["usb_baseline"], sample
                    )
                    if edge_state == "error":
                        usb_event_error = edge_error or "ambiguous USB topology"
                        outcome = "usb_event_error"
                        break
                    if edge_state == "waiting":
                        continue
                    try:
                        _source_monotonic(sample)
                    except CampaignAbort as exc:
                        usb_event_error = str(exc)
                        outcome = "usb_event_error"
                        break
                    usb_edge_sample = sample
                    previous_usb_sample = sample
                    usb_event_samples.append(_event_sample_structure(sample, gate_kind))
                    # Wall time bounds I/O waiting. All reported transition and settle
                    # intervals stay in the Pi/source monotonic clock domain.
                    deadline = _sample_observed_monotonic(sample, action) + timeout_s
                    first_match = None
                    continue

                assert previous_usb_sample is not None
                persistence_error = _latched_usb_topology_error(
                    usb_edge_sample, previous_usb_sample, sample, gate_kind
                )
                usb_event_samples.append(_event_sample_structure(sample, gate_kind))
                previous_usb_sample = sample
                if persistence_error:
                    usb_event_error = persistence_error
                    outcome = "usb_event_error"
                    break
                if usb_confirmation_sample is None:
                    usb_confirmation_sample = sample

                final_candidate_id = USB_FINAL_SELECTED_CANDIDATE[gate_kind]
                selected = sample.get("microphone")
                if (
                    isinstance(selected, dict)
                    and selected.get("id") == final_candidate_id
                ):
                    devices, device_error = _validated_usb_devices(
                        usb_edge_sample.get("usb_microphones"), final_candidate_id
                    )
                    if device_error or not devices:
                        usb_event_error = (
                            device_error or "latched USB instance is missing"
                        )
                        outcome = "usb_event_error"
                        break
                    early_binding = _identity_binding_evidence(
                        selected, devices[0], final_candidate_id, "final_selected"
                    )
                    if not early_binding["validated"]:
                        usb_final_identity_binding = early_binding
                        usb_event_error = str(early_binding["error"])
                        outcome = "usb_event_error"
                        break
                    usb_final_identity_binding = early_binding
                    if gate_kind != "fallback":
                        usb_identity_binding = early_binding

            failures = expectation_failures(sample, expected)
            if failures:
                first_match = None
                continue
            if first_match is None:
                first_match = sample
            try:
                settled = _sample_interval_seconds(first_match, sample) >= settle_s
            except CampaignAbort as exc:
                usb_event_error = str(exc)
                outcome = "usb_event_error" if gated_usb_timing else "timeout"
                break
            if not settled:
                continue
            if gated_usb_timing:
                assert usb_edge_sample is not None
                assert gate_kind is not None
                final_candidate_id = USB_FINAL_SELECTED_CANDIDATE[gate_kind]
                devices, device_error = _validated_usb_devices(
                    usb_edge_sample.get("usb_microphones"), final_candidate_id
                )
                if device_error or not devices:
                    usb_event_error = device_error or "latched USB instance is missing"
                    outcome = "usb_event_error"
                    break
                usb_final_identity_binding = _identity_binding_evidence(
                    sample.get("microphone"),
                    devices[0],
                    final_candidate_id,
                    "final_selected",
                )
                if not usb_final_identity_binding["validated"]:
                    usb_event_error = str(usb_final_identity_binding["error"])
                    outcome = "usb_event_error"
                    break
                if gate_kind != "fallback":
                    usb_identity_binding = usb_final_identity_binding
            outcome = "completed"
            break
        if outcome in {"completed", "usb_event_error"}:
            break
        if time.monotonic() >= deadline:
            break
        sampler.wait_for_new_sample(
            seen_seq, min(1.0, max(0.01, deadline - time.monotonic()))
        )

    if gated_usb_timing and usb_edge_sample is None and outcome == "timeout":
        outcome = "usb_event_missing"
        usb_event_error = (
            f"expected {gate_kind} USB sysfs edge was not observed within "
            f"{action_observation_timeout_s:.1f}s"
        )
    elif (
        gated_usb_timing
        and usb_edge_sample is not None
        and usb_confirmation_sample is None
        and outcome == "timeout"
    ):
        outcome = "usb_event_error"
        usb_event_error = "USB sysfs edge was not confirmed by a second stable sample"

    transition_samples = list(processed_samples)
    latest = transition_samples[-1] if transition_samples else latest
    if (
        outcome == "timeout"
        and (not gated_usb_timing or usb_confirmation_sample is not None)
        and latest is not None
    ):
        invariant_block = latest.get("invariants") or {}
        actionable_reason = latest.get("selection_reason") or (
            (latest.get("microphone") or {}).get("reason")
            if isinstance(latest.get("microphone"), dict)
            else None
        )
        if (
            latest.get("state") in SAFE_STATES
            and not invariant_block.get("violations")
            and not invariant_block.get("hfp_inputs")
            and actionable_reason
        ):
            outcome = "safe_state"

    if gated_usb_timing and usb_confirmation_sample is not None:
        assert usb_edge_sample is not None
        try:
            first_latency = (
                round(_sample_interval_seconds(usb_edge_sample, first_match), 6)
                if first_match is not None
                else None
            )
            completion_latency = (
                round(_sample_interval_seconds(usb_edge_sample, latest), 6)
                if outcome == "completed" and latest is not None
                else None
            )
            state_settle = (
                round(_sample_interval_seconds(first_match, latest), 6)
                if outcome == "completed"
                and first_match is not None
                and latest is not None
                else None
            )
            safe_state_latency = (
                round(_sample_interval_seconds(usb_edge_sample, latest), 6)
                if outcome == "safe_state" and latest is not None
                else None
            )
        except CampaignAbort as exc:
            first_latency = None
            completion_latency = None
            state_settle = None
            safe_state_latency = None
            outcome = "usb_event_error"
            usb_event_error = str(exc)
        action_to_usb_s = round(
            _sample_observed_monotonic(usb_edge_sample, action)
            - float(action["monotonic"]),
            6,
        )
        timing_origin = USB_TIMING_ORIGIN
    else:
        first_latency = (
            round(float(first_match["elapsed_s"]) - float(action["elapsed_s"]), 6)
            if first_match is not None
            else None
        )
        completion_latency = (
            round(float(latest["elapsed_s"]) - float(action["elapsed_s"]), 6)
            if outcome == "completed" and latest is not None
            else None
        )
        state_settle = None
        safe_state_latency = None
        action_to_usb_s = None
        timing_origin = "usb_sysfs_edge_missing" if gated_usb_timing else "operator_now"
    violations = _dedupe_transition_violations(transition_samples)
    before_counts = action.get("service_restart_counts") or {}
    after_counts = sampler.service_counts()
    restart_delta = service_restart_delta(before_counts, after_counts)
    restart_clean = bool(
        all(value == 0 for value in restart_delta.values())
        and all(value is not None for value in restart_delta.values())
    )
    result = {
        "phase": action["phase"],
        "cycle": action["cycle"],
        "connection_layout": action.get("connection_layout"),
        "gate_kind": gate_kind,
        "action_id": action["action_id"],
        "action_timestamp": action["timestamp"],
        "outcome": outcome,
        "timing_evidence_version": (
            TIMING_EVIDENCE_VERSION if gated_usb_timing else None
        ),
        "timing_origin": timing_origin,
        "operator_latency_s": action_to_usb_s,
        "action_to_usb_s": action_to_usb_s,
        "usb_action_observation_timeout_s": (
            action_observation_timeout_s if gated_usb_timing else None
        ),
        "usb_baseline": copy.deepcopy(action.get("usb_baseline")),
        "usb_event": (
            {
                **_usb_event_evidence(usb_edge_sample, gate_kind),
                "confirmed": usb_confirmation_sample is not None,
                "confirmation": (
                    _usb_event_evidence(usb_confirmation_sample, gate_kind)
                    if usb_confirmation_sample is not None
                    else None
                ),
                "persistent": bool(
                    usb_confirmation_sample is not None and usb_event_error is None
                ),
                "stable_through_seq": (
                    usb_event_samples[-1].get("seq") if usb_event_samples else None
                ),
                "persistence_samples": usb_event_samples,
            }
            if usb_edge_sample is not None and gated_usb_timing
            else None
        ),
        "usb_event_error": usb_event_error,
        "usb_identity_binding": usb_identity_binding,
        "usb_final_identity_binding": usb_final_identity_binding,
        "transition_latency_s": first_latency,
        "settled_latency_s": completion_latency,
        "settle_requirement_s": settle_s if gated_usb_timing else None,
        "state_settle_s": state_settle,
        "safe_state_latency_s": safe_state_latency,
        "timeout_s": timeout_s,
        "samples": len(transition_samples),
        "first_matching_sample": first_match,
        "final_sample": latest,
        "safety_clean": not violations,
        "invariant_violations": violations,
        "service_restart_counts_before": before_counts,
        "service_restart_counts_after": after_counts,
        "service_restart_delta": restart_delta,
        "supervisor_restart_delta": restart_delta.get("bridge-supervisor.service"),
        "restart_clean": restart_clean,
        "expectation_failures_final": (
            expectation_failures(latest, expected)
            if latest is not None
            else ["no samples"]
        ),
    }
    return result


def operator_action(
    sampler: LiveSampler,
    *,
    phase: str,
    cycle: int,
    instruction: str,
    connection_layout: str | None = None,
    input_fn=input,
) -> dict[str, Any]:
    acknowledgements = {
        "fifine_only_baseline": "FIFINE ONLY",
        "both_microphones_baseline": "BOTH READY",
        "lark_promotion": "PLUG LARK",
        "lark_fallback": "UNPLUG LARK",
        "inactive_fifine_change/setup_lark": "PLUG LARK",
        "inactive_fifine_change/remove": "UNPLUG FIFINE",
        "inactive_fifine_change/restore": "PLUG FIFINE",
        "neither_microphone_waiting": "UNPLUG BOTH",
        "restore_fifine": "PLUG FIFINE",
        "restore_lark": "PLUG LARK",
        "fifine_replug/remove": "UNPLUG FIFINE",
        "fifine_replug/restore": "PLUG FIFINE",
        "lark_promotion/recovery": "RECOVER BOTH",
        "lark_fallback/recovery": "RECOVER FIFINE",
        "restore_fifine/recovery": "RECOVER FIFINE",
        "fifine_replug/restore/recovery": "RECOVER FIFINE",
        CONNECTION_LAYOUT_HANDOFF_PHASE: "MOVE FIFINE TO HUB",
    }
    required = acknowledgements.get(phase, "READY")
    print(f"\n[{phase} cycle {cycle}] {instruction}", file=sys.stderr, flush=True)
    print(
        f"Prepare the connector first. Type {required!r} and press Enter, but do not "
        "perform the action until the NOW prompt appears (or type ABORT): ",
        file=sys.stderr,
        end="",
        flush=True,
    )
    try:
        response = input_fn()
    except EOFError as exc:
        raise CampaignAbort("operator input ended") from exc
    if response.strip().lower() in {"q", "quit", "abort"}:
        raise CampaignAbort("operator aborted the campaign")
    if response.strip() != required:
        raise CampaignAbort(
            f"operator acknowledgement was {response.strip()!r}, expected {required!r}"
        )
    action = sampler.mark_action(
        phase,
        cycle,
        instruction,
        connection_layout=connection_layout,
    )
    print(f"NOW: {instruction}", file=sys.stderr, flush=True)
    action["required_acknowledgement"] = required
    action["operator_acknowledged"] = True
    sampler.record_event(
        "operator_acknowledgement",
        phase=phase,
        cycle=cycle,
        action_id=action["action_id"],
        connection_layout=connection_layout,
        required_acknowledgement=required,
        operator_acknowledged=True,
    )
    return action


def run_step(
    sampler: LiveSampler,
    transitions: list[dict[str, Any]],
    *,
    phase: str,
    cycle: int,
    instruction: str,
    expected: Expectation,
    timeout_s: float,
    settle_s: float,
    gate_kind: str | None = None,
    input_fn=input,
    require_completion: bool = True,
    connection_layout: str | None = None,
) -> dict[str, Any]:
    action = operator_action(
        sampler,
        phase=phase,
        cycle=cycle,
        instruction=instruction,
        connection_layout=connection_layout,
        input_fn=input_fn,
    )
    result = wait_for_expectation(
        sampler,
        action,
        expected,
        timeout_s=timeout_s,
        settle_s=settle_s,
        gate_kind=gate_kind,
    )
    transitions.append(result)
    sampler.record_event("transition_result", result=result)
    latency = result.get("transition_latency_s")
    print(
        f"{phase}: {result['outcome']}"
        + (f" in {latency:.3f}s" if isinstance(latency, (int, float)) else ""),
        file=sys.stderr,
        flush=True,
    )
    acceptable = result["outcome"] == "completed" or (
        gate_kind is not None and result["outcome"] == "safe_state"
    )
    if require_completion and not acceptable:
        raise CampaignAbort(
            f"{phase} did not reach its required state ({result['outcome']})"
        )
    return result


def _sample_identity(result: dict[str, Any]) -> tuple[str, int]:
    sample = result.get("final_sample") or result.get("first_matching_sample") or {}
    selected = sample.get("microphone") or {}
    token = selected.get("instance_token") if isinstance(selected, dict) else None
    generation = sample.get("graph_generation")
    if not isinstance(token, str) or not token:
        raise CampaignAbort("identity evidence is missing an instance token")
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise CampaignAbort("identity evidence is missing a graph generation")
    return token, generation


def _identity_after_gate_or_recovery(
    sampler: LiveSampler,
    transitions: list[dict[str, Any]],
    result: dict[str, Any],
    *,
    recovery_phase: str,
    cycle: int,
    recovery_instruction: str,
    recovery_expected: Expectation,
    timeout_s: float,
    settle_s: float,
    input_fn=input,
    connection_layout: str | None = None,
) -> tuple[str, int]:
    """Return completed identity, recovering ACTIVE after an actionable safe result."""
    outcome = result.get("outcome")
    if outcome == "completed":
        return _sample_identity(result)
    if outcome != "safe_state":
        raise CampaignAbort(
            f"{result.get('phase', 'gated transition')} cannot provide identity after "
            f"outcome {outcome!r}"
        )
    recovery = run_step(
        sampler,
        transitions,
        phase=recovery_phase,
        cycle=cycle,
        instruction=recovery_instruction,
        expected=recovery_expected,
        timeout_s=timeout_s,
        settle_s=settle_s,
        input_fn=input_fn,
        connection_layout=connection_layout,
    )
    return _sample_identity(recovery)


def operator_powered_hub_attestation(
    sampler: LiveSampler,
    *,
    input_fn=input,
) -> dict[str, Any]:
    """Record the operator's power-source claim separately from USB observations."""
    required = "ATTEST POWERED HUB"
    print(
        "\nThe next ten cycles use the external hub. Confirm that its external "
        f"power supply is connected. Type {required!r} (or type ABORT): ",
        file=sys.stderr,
        end="",
        flush=True,
    )
    try:
        response = input_fn()
    except EOFError as exc:
        raise CampaignAbort("operator input ended") from exc
    if response.strip().lower() in {"q", "quit", "abort"}:
        raise CampaignAbort("operator aborted the campaign")
    if response.strip() != required:
        raise CampaignAbort(
            f"operator acknowledgement was {response.strip()!r}, expected {required!r}"
        )
    attestation = {
        "claim": "external_hub_power_supply_connected",
        "connection_layout": CONNECTION_LAYOUT_POWERED_HUB,
        "required_acknowledgement": required,
        "operator_acknowledged": True,
        "timestamp": time.time(),
        "observation_kind": "operator_attestation",
    }
    sampler.record_event("operator_attestation", attestation=attestation)
    return attestation


def _connection_handoff_evidence(
    sampler: LiveSampler,
    action: dict[str, Any],
    result: dict[str, Any],
    attestation: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    baseline = action.get("usb_baseline")
    structural_error = _validate_stable_baseline_structure(baseline)
    if structural_error:
        errors.append(f"direct baseline: {structural_error}")
    baseline_samples = (
        baseline.get("samples")
        if isinstance(baseline, dict) and isinstance(baseline.get("samples"), list)
        else []
    )
    direct_device: dict[str, Any] | None = None
    direct_ancestors: list[dict[str, Any]] | None = None
    for sample in baseline_samples:
        topology = sample.get("usb_microphones") if isinstance(sample, dict) else None
        fifine_devices, fifine_error = _validated_usb_devices(topology, "fifine-k054")
        lark_devices, lark_error = _validated_usb_devices(topology, "lark-a1")
        if fifine_error or fifine_devices is None or len(fifine_devices) != 1:
            errors.append(
                f"direct baseline FIFINE identity: {fifine_error or 'not unique'}"
            )
            continue
        if lark_error or lark_devices is None or lark_devices:
            errors.append(
                f"direct baseline Lark absence: {lark_error or 'Lark present'}"
            )
        raw_device = fifine_devices[0]
        ancestors, ancestry_error = _validated_hub_ancestors(raw_device)
        if ancestry_error:
            errors.append(f"direct baseline ancestry: {ancestry_error}")
        binding_error = usb_identity_binding_error(
            sample.get("microphone"), raw_device, "fifine-k054"
        )
        if binding_error:
            errors.append(f"direct baseline binding: {binding_error}")
        if direct_device is None:
            direct_device = copy.deepcopy(raw_device)
            direct_ancestors = copy.deepcopy(ancestors)
        elif raw_device != direct_device:
            errors.append("direct baseline USB identity changed between stable samples")

    first_match = result.get("first_matching_sample")
    final_sample = result.get("final_sample")
    first_seq = _sample_sequence(first_match, "seq") if first_match else None
    final_seq = _sample_sequence(final_sample, "seq") if final_sample else None
    settle_samples: list[dict[str, Any]] = []
    if first_seq is None or final_seq is None or final_seq < first_seq:
        errors.append("powered-hub settled sample bounds are missing")
    elif isinstance(baseline, dict):
        baseline_seq = _sample_sequence(baseline, "seq")
        if baseline_seq is None:
            errors.append("direct baseline sequence is missing")
        else:
            settle_samples = [
                sample
                for sample in sampler.samples_after(baseline_seq)
                if first_seq <= int(sample.get("seq", -1)) <= final_seq
            ]
    if len(settle_samples) < USB_EVENT_CONFIRMATION_SAMPLES:
        errors.append("powered-hub state lacks two continuously sampled settle records")

    powered_device: dict[str, Any] | None = None
    powered_ancestors: list[dict[str, Any]] | None = None
    for index, sample in enumerate(settle_samples):
        if sample.get("usb_error"):
            errors.append(
                f"powered-hub settle sample {index + 1}: {sample['usb_error']}"
            )
            continue
        topology = sample.get("usb_microphones")
        fifine_devices, fifine_error = _validated_usb_devices(topology, "fifine-k054")
        lark_devices, lark_error = _validated_usb_devices(topology, "lark-a1")
        if fifine_error or fifine_devices is None or len(fifine_devices) != 1:
            errors.append(
                f"powered-hub settle FIFINE identity: {fifine_error or 'not unique'}"
            )
            continue
        if lark_error or lark_devices is None or lark_devices:
            errors.append(
                f"powered-hub settle Lark absence: {lark_error or 'Lark present'}"
            )
        raw_device = fifine_devices[0]
        ancestors, ancestry_error = _validated_hub_ancestors(raw_device)
        if ancestry_error:
            errors.append(f"powered-hub settle ancestry: {ancestry_error}")
        binding_error = usb_identity_binding_error(
            sample.get("microphone"), raw_device, "fifine-k054"
        )
        if binding_error:
            errors.append(f"powered-hub settle binding: {binding_error}")
        if powered_device is None:
            powered_device = copy.deepcopy(raw_device)
            powered_ancestors = copy.deepcopy(ancestors)
        elif raw_device != powered_device:
            errors.append("powered-hub USB identity changed during settled sampling")
        if index:
            sequence_error = _consecutive_source_samples(
                settle_samples[index - 1], sample
            )
            if sequence_error:
                errors.append(f"powered-hub settle sampling: {sequence_error}")

    direct_external_generations = (
        _external_hub_ancestor_generations(direct_device)
        if direct_device is not None
        else set()
    )
    powered_external_generations = (
        _external_hub_ancestor_generations(powered_device)
        if powered_device is not None
        else set()
    )
    if direct_external_generations:
        errors.append("direct baseline is already descended from an external USB hub")
    added_generations = sorted(
        powered_external_generations - direct_external_generations
    )
    if not added_generations:
        errors.append("no new observed USB hub ancestor appeared at the layout handoff")
    if (
        direct_device is not None
        and powered_device is not None
        and direct_device.get("usb_instance_generation")
        == powered_device.get("usb_instance_generation")
    ):
        errors.append("FIFINE USB instance generation did not change at handoff")
    if result.get("outcome") != "completed":
        errors.append(f"layout handoff state outcome was {result.get('outcome')!r}")
    if (
        attestation.get("operator_acknowledged") is not True
        or attestation.get("claim") != "external_hub_power_supply_connected"
    ):
        errors.append("powered-hub operator attestation is missing or malformed")

    direct_selected = (
        baseline_samples[-1].get("microphone") if baseline_samples else None
    )
    powered_selected = (
        final_sample.get("microphone") if isinstance(final_sample, dict) else None
    )
    direct_token = (
        direct_selected.get("instance_token")
        if isinstance(direct_selected, dict)
        else None
    )
    powered_token = (
        powered_selected.get("instance_token")
        if isinstance(powered_selected, dict)
        else None
    )
    direct_generation = (
        baseline_samples[-1].get("graph_generation") if baseline_samples else None
    )
    powered_generation = (
        final_sample.get("graph_generation") if isinstance(final_sample, dict) else None
    )
    if not isinstance(direct_token, str) or not direct_token:
        errors.append("direct runtime instance token is missing")
    if not isinstance(powered_token, str) or not powered_token:
        errors.append("powered-hub runtime instance token is missing")
    if direct_token == powered_token:
        errors.append("runtime instance token did not change at handoff")
    if (
        not isinstance(direct_generation, int)
        or isinstance(direct_generation, bool)
        or not isinstance(powered_generation, int)
        or isinstance(powered_generation, bool)
        or powered_generation <= direct_generation
    ):
        errors.append("graph generation did not advance at handoff")

    return {
        "validated": not errors,
        "errors": errors,
        "observation_kind": "usb_sysfs_ancestry_delta",
        "candidate_id": "fifine-k054",
        "direct_usb_device": direct_device,
        "powered_hub_usb_device": powered_device,
        "direct_hub_ancestors": direct_ancestors,
        "powered_hub_ancestors": powered_ancestors,
        "added_hub_ancestor_generations": added_generations,
        "direct_instance_token": direct_token,
        "powered_hub_instance_token": powered_token,
        "direct_graph_generation": direct_generation,
        "powered_hub_graph_generation": powered_generation,
        "lark_absent": not any(
            (sample.get("usb_microphones") or {}).get("lark-a1")
            for sample in [*baseline_samples, *settle_samples]
            if isinstance(sample, dict)
        ),
        "settled_sample_count": len(settle_samples),
        "first_settled_seq": first_seq,
        "final_settled_seq": final_seq,
    }


def run_connection_layout_handoff(
    sampler: LiveSampler,
    transitions: list[dict[str, Any]],
    *,
    previous_token: str,
    previous_generation: int,
    timeout_s: float,
    settle_s: float,
    input_fn=input,
) -> tuple[str, int]:
    """Observe the one non-gated direct-to-powered-hub midpoint handoff."""
    attestation = operator_powered_hub_attestation(sampler, input_fn=input_fn)
    action = operator_action(
        sampler,
        phase=CONNECTION_LAYOUT_HANDOFF_PHASE,
        cycle=CONNECTION_LAYOUT_BOUNDARY_CYCLE,
        instruction=(
            "Move the FIFINE from its direct Pi port to the externally powered USB "
            "hub; keep Lark unplugged and the live call connected."
        ),
        connection_layout="direct_to_powered_hub",
        input_fn=input_fn,
    )
    result = wait_for_expectation(
        sampler,
        action,
        Expectation(
            state="ACTIVE",
            selected_id="fifine-k054",
            different_instance_token=previous_token,
            generation_after=previous_generation,
            candidate_states={"lark-a1": frozenset({"absent"})},
        ),
        timeout_s=timeout_s,
        settle_s=settle_s,
        gate_kind=None,
    )
    state_outcome = result.get("outcome")
    ancestry = _connection_handoff_evidence(sampler, action, result, attestation)
    result["operator_attestation"] = attestation
    result["observed_usb_ancestry"] = ancestry
    result["layout_handoff_validated"] = ancestry["validated"]
    if not ancestry["validated"]:
        result["state_outcome"] = state_outcome
        result["outcome"] = "layout_evidence_error"
    transitions.append(result)
    sampler.record_event("transition_result", result=result)
    print(
        f"{CONNECTION_LAYOUT_HANDOFF_PHASE}: {result['outcome']}",
        file=sys.stderr,
        flush=True,
    )
    if result["outcome"] != "completed":
        detail = "; ".join(ancestry["errors"]) or str(state_outcome)
        raise CampaignAbort(f"connection layout handoff failed: {detail}")
    return _sample_identity(result)


def run_matrix(
    sampler: LiveSampler,
    transitions: list[dict[str, Any]],
    *,
    cycles: int,
    timeout_s: float,
    settle_s: float,
    input_fn=input,
) -> None:
    for cycle in range(1, cycles + 1):
        baseline = run_step(
            sampler,
            transitions,
            phase="fifine_only_baseline",
            cycle=cycle,
            instruction=(
                "Keep the live call up with only the FIFINE connected. Do not change "
                "hardware after pressing Enter."
            ),
            expected=Expectation(
                state="ACTIVE",
                selected_id="fifine-k054",
                candidate_states={"lark-a1": frozenset({"absent"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )
        fifine_token, fifine_generation = _sample_identity(baseline)
        promotion_expected = Expectation(
            state="ACTIVE",
            selected_id="lark-a1",
            different_instance_token=fifine_token,
            generation_after=fifine_generation,
            candidate_states={"fifine-k054": frozenset({"usable"})},
        )
        promotion = run_step(
            sampler,
            transitions,
            phase="lark_promotion",
            cycle=cycle,
            instruction="Plug in the Lark while leaving the FIFINE connected.",
            expected=promotion_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="promotion",
            input_fn=input_fn,
        )
        lark_token, lark_generation = _identity_after_gate_or_recovery(
            sampler,
            transitions,
            promotion,
            recovery_phase="lark_promotion/recovery",
            cycle=cycle,
            recovery_instruction=(
                "Ensure both Lark and FIFINE are connected; reconnect Lark if needed, "
                "then leave both connected."
            ),
            recovery_expected=promotion_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )
        fallback_expected = Expectation(
            state="ACTIVE",
            selected_id="fifine-k054",
            different_instance_token=lark_token,
            generation_after=lark_generation,
            candidate_states={"lark-a1": frozenset({"absent"})},
        )
        fallback = run_step(
            sampler,
            transitions,
            phase="lark_fallback",
            cycle=cycle,
            instruction="Unplug the Lark while leaving the FIFINE connected.",
            expected=fallback_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="fallback",
            input_fn=input_fn,
        )
        prior_fifine_token, prior_fifine_generation = _identity_after_gate_or_recovery(
            sampler,
            transitions,
            fallback,
            recovery_phase="lark_fallback/recovery",
            cycle=cycle,
            recovery_instruction=(
                "Ensure only FIFINE is connected; unplug Lark if needed, then leave "
                "FIFINE connected."
            ),
            recovery_expected=fallback_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )

        inactive_setup = run_step(
            sampler,
            transitions,
            phase="inactive_fifine_change/setup_lark",
            cycle=cycle,
            instruction="Reconnect the Lark and leave both microphones connected.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="lark-a1",
                candidate_states={"fifine-k054": frozenset({"usable"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )
        inactive_token, inactive_generation = _sample_identity(inactive_setup)
        run_step(
            sampler,
            transitions,
            phase="inactive_fifine_change/remove",
            cycle=cycle,
            instruction="Unplug only the inactive FIFINE; keep the Lark and call up.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="lark-a1",
                same_instance_token=inactive_token,
                same_generation=inactive_generation,
                candidate_states={"fifine-k054": frozenset({"absent"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )
        run_step(
            sampler,
            transitions,
            phase="inactive_fifine_change/restore",
            cycle=cycle,
            instruction="Reconnect the FIFINE; keep the selected Lark and call up.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="lark-a1",
                same_instance_token=inactive_token,
                same_generation=inactive_generation,
                candidate_states={"fifine-k054": frozenset({"usable"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )
        run_step(
            sampler,
            transitions,
            phase="neither_microphone_waiting",
            cycle=cycle,
            instruction="Unplug both Lark and FIFINE while keeping the call connected.",
            expected=Expectation(
                state="WAITING_MIC",
                require_no_selection=True,
                candidate_states={
                    "lark-a1": frozenset({"absent"}),
                    "fifine-k054": frozenset({"absent"}),
                },
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )
        restored_fifine_expected = Expectation(
            state="ACTIVE",
            selected_id="fifine-k054",
            different_instance_token=prior_fifine_token,
            generation_after=prior_fifine_generation,
            candidate_states={"lark-a1": frozenset({"absent"})},
        )
        restored_fifine = run_step(
            sampler,
            transitions,
            phase="restore_fifine",
            cycle=cycle,
            instruction="Reconnect only the FIFINE.",
            expected=restored_fifine_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="fifine_replug",
            input_fn=input_fn,
        )
        restored_token, restored_generation = _identity_after_gate_or_recovery(
            sampler,
            transitions,
            restored_fifine,
            recovery_phase="restore_fifine/recovery",
            cycle=cycle,
            recovery_instruction=(
                "Ensure only the same FIFINE is connected; reconnect it if needed."
            ),
            recovery_expected=restored_fifine_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )
        run_step(
            sampler,
            transitions,
            phase="restore_lark",
            cycle=cycle,
            instruction="Reconnect the Lark and leave both microphones connected.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="lark-a1",
                different_instance_token=restored_token,
                generation_after=restored_generation,
                candidate_states={"fifine-k054": frozenset({"usable"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )


def run_promotion_fallback(
    sampler: LiveSampler,
    transitions: list[dict[str, Any]],
    *,
    cycles: int,
    timeout_s: float,
    settle_s: float,
    input_fn=input,
    connection_plan: str | None = None,
) -> None:
    opening = run_step(
        sampler,
        transitions,
        phase="fifine_only_baseline",
        cycle=0,
        instruction="Begin the live call with only the FIFINE connected.",
        expected=Expectation(
            state="ACTIVE",
            selected_id="fifine-k054",
            candidate_states={"lark-a1": frozenset({"absent"})},
        ),
        timeout_s=timeout_s,
        settle_s=settle_s,
        input_fn=input_fn,
        connection_layout=connection_layout_for_cycle(0, connection_plan),
    )
    previous_token, previous_generation = _sample_identity(opening)
    for cycle in range(1, cycles + 1):
        connection_layout = connection_layout_for_cycle(cycle, connection_plan)
        promotion_expected = Expectation(
            state="ACTIVE",
            selected_id="lark-a1",
            different_instance_token=previous_token,
            generation_after=previous_generation,
            candidate_states={"fifine-k054": frozenset({"usable"})},
        )
        promoted = run_step(
            sampler,
            transitions,
            phase="lark_promotion",
            cycle=cycle,
            instruction="Plug in the Lark; leave FIFINE connected.",
            expected=promotion_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="promotion",
            input_fn=input_fn,
            connection_layout=connection_layout,
        )
        previous_token, previous_generation = _identity_after_gate_or_recovery(
            sampler,
            transitions,
            promoted,
            recovery_phase="lark_promotion/recovery",
            cycle=cycle,
            recovery_instruction=(
                "Ensure both Lark and FIFINE are connected; reconnect Lark if needed, "
                "then leave both connected."
            ),
            recovery_expected=promotion_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
            connection_layout=connection_layout,
        )
        fallback_expected = Expectation(
            state="ACTIVE",
            selected_id="fifine-k054",
            different_instance_token=previous_token,
            generation_after=previous_generation,
            candidate_states={"lark-a1": frozenset({"absent"})},
        )
        fallback = run_step(
            sampler,
            transitions,
            phase="lark_fallback",
            cycle=cycle,
            instruction="Unplug the Lark; leave FIFINE connected.",
            expected=fallback_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="fallback",
            input_fn=input_fn,
            connection_layout=connection_layout,
        )
        previous_token, previous_generation = _identity_after_gate_or_recovery(
            sampler,
            transitions,
            fallback,
            recovery_phase="lark_fallback/recovery",
            cycle=cycle,
            recovery_instruction=(
                "Ensure only FIFINE is connected; unplug Lark if needed, then leave "
                "FIFINE connected."
            ),
            recovery_expected=fallback_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
            connection_layout=connection_layout,
        )
        if (
            connection_plan == CONNECTION_PLAN_DIRECT10_HUB10
            and cycle == CONNECTION_LAYOUT_BOUNDARY_CYCLE
        ):
            previous_token, previous_generation = run_connection_layout_handoff(
                sampler,
                transitions,
                previous_token=previous_token,
                previous_generation=previous_generation,
                timeout_s=timeout_s,
                settle_s=settle_s,
                input_fn=input_fn,
            )


def run_fifine_replug(
    sampler: LiveSampler,
    transitions: list[dict[str, Any]],
    *,
    cycles: int,
    timeout_s: float,
    settle_s: float,
    input_fn=input,
    connection_plan: str | None = None,
) -> None:
    opening = run_step(
        sampler,
        transitions,
        phase="fifine_only_baseline",
        cycle=0,
        instruction="Begin the live call with only the FIFINE connected.",
        expected=Expectation(
            state="ACTIVE",
            selected_id="fifine-k054",
            candidate_states={"lark-a1": frozenset({"absent"})},
        ),
        timeout_s=timeout_s,
        settle_s=settle_s,
        input_fn=input_fn,
        connection_layout=connection_layout_for_cycle(0, connection_plan),
    )
    previous_token, previous_generation = _sample_identity(opening)
    for cycle in range(1, cycles + 1):
        connection_layout = connection_layout_for_cycle(cycle, connection_plan)
        run_step(
            sampler,
            transitions,
            phase="fifine_replug/remove",
            cycle=cycle,
            instruction="Unplug the FIFINE; keep the live call connected.",
            expected=Expectation(
                state="WAITING_MIC",
                require_no_selection=True,
                candidate_states={"fifine-k054": frozenset({"absent"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
            connection_layout=connection_layout,
        )
        restored_expected = Expectation(
            state="ACTIVE",
            selected_id="fifine-k054",
            different_instance_token=previous_token,
            generation_after=previous_generation,
            candidate_states={"lark-a1": frozenset({"absent"})},
        )
        restored = run_step(
            sampler,
            transitions,
            phase="fifine_replug/restore",
            cycle=cycle,
            instruction="Reconnect the same FIFINE to any allowed USB port.",
            expected=restored_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="fifine_replug",
            input_fn=input_fn,
            connection_layout=connection_layout,
        )
        previous_token, previous_generation = _identity_after_gate_or_recovery(
            sampler,
            transitions,
            restored,
            recovery_phase="fifine_replug/restore/recovery",
            cycle=cycle,
            recovery_instruction=(
                "Ensure only the same FIFINE is connected; reconnect it if needed."
            ),
            recovery_expected=restored_expected,
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
            connection_layout=connection_layout,
        )
        if (
            connection_plan == CONNECTION_PLAN_DIRECT10_HUB10
            and cycle == CONNECTION_LAYOUT_BOUNDARY_CYCLE
        ):
            previous_token, previous_generation = run_connection_layout_handoff(
                sampler,
                transitions,
                previous_token=previous_token,
                previous_generation=previous_generation,
                timeout_s=timeout_s,
                settle_s=settle_s,
                input_fn=input_fn,
            )


def run_inactive_fifine(
    sampler: LiveSampler,
    transitions: list[dict[str, Any]],
    *,
    cycles: int,
    timeout_s: float,
    settle_s: float,
    input_fn=input,
) -> None:
    opening = run_step(
        sampler,
        transitions,
        phase="both_microphones_baseline",
        cycle=0,
        instruction="Begin the live call with both microphones connected and Lark selected.",
        expected=Expectation(
            state="ACTIVE",
            selected_id="lark-a1",
            candidate_states={"fifine-k054": frozenset({"usable"})},
        ),
        timeout_s=timeout_s,
        settle_s=settle_s,
        input_fn=input_fn,
    )
    token, generation = _sample_identity(opening)
    for cycle in range(1, cycles + 1):
        run_step(
            sampler,
            transitions,
            phase="inactive_fifine_change/remove",
            cycle=cycle,
            instruction="Unplug only the inactive FIFINE; keep Lark and the call up.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="lark-a1",
                same_instance_token=token,
                same_generation=generation,
                candidate_states={"fifine-k054": frozenset({"absent"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )
        run_step(
            sampler,
            transitions,
            phase="inactive_fifine_change/restore",
            cycle=cycle,
            instruction="Reconnect the FIFINE; keep Lark and the call up.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="lark-a1",
                same_instance_token=token,
                same_generation=generation,
                candidate_states={"fifine-k054": frozenset({"usable"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            input_fn=input_fn,
        )


def required_gate_kinds(campaign: str) -> tuple[str, ...]:
    if campaign == "matrix":
        return ("promotion", "fallback", "fifine_replug")
    if campaign == "promotion-fallback":
        return ("promotion", "fallback")
    if campaign == "fifine-replug":
        return ("fifine_replug",)
    return ()


def _transition_usb_topologies(transition: dict[str, Any]) -> list[dict[str, Any]]:
    topologies: list[dict[str, Any]] = []
    baseline = transition.get("usb_baseline")
    if isinstance(baseline, dict):
        samples = baseline.get("samples")
        if isinstance(samples, list):
            topologies.extend(
                sample["usb_microphones"]
                for sample in samples
                if isinstance(sample, dict)
                and isinstance(sample.get("usb_microphones"), dict)
            )
    event = transition.get("usb_event")
    if isinstance(event, dict):
        persistence = event.get("persistence_samples")
        if isinstance(persistence, list):
            topologies.extend(
                sample["usb_microphones"]
                for sample in persistence
                if isinstance(sample, dict)
                and isinstance(sample.get("usb_microphones"), dict)
            )
    final_sample = transition.get("final_sample")
    if isinstance(final_sample, dict) and isinstance(
        final_sample.get("usb_microphones"), dict
    ):
        topologies.append(final_sample["usb_microphones"])
    return topologies


def _recorded_layout_handoff_error(handoff: dict[str, Any]) -> str | None:
    ancestry = handoff.get("observed_usb_ancestry")
    attestation = handoff.get("operator_attestation")
    if not isinstance(ancestry, dict) or not isinstance(attestation, dict):
        return "handoff omitted separate ancestry or operator-attestation evidence"
    if ancestry.get("observation_kind") != "usb_sysfs_ancestry_delta":
        return "handoff ancestry observation kind is invalid"
    direct_device = ancestry.get("direct_usb_device")
    powered_device = ancestry.get("powered_hub_usb_device")
    if not isinstance(direct_device, dict) or not isinstance(powered_device, dict):
        return "handoff omitted direct or powered-hub raw USB identity"
    direct_ancestors, direct_error = _validated_hub_ancestors(direct_device)
    powered_ancestors, powered_error = _validated_hub_ancestors(powered_device)
    if direct_error or direct_ancestors is None:
        return f"direct handoff ancestry is invalid: {direct_error}"
    if powered_error or powered_ancestors is None:
        return f"powered-hub handoff ancestry is invalid: {powered_error}"
    direct_external_generations = _external_hub_ancestor_generations(direct_device)
    powered_external_generations = _external_hub_ancestor_generations(powered_device)
    if direct_external_generations:
        return "direct handoff baseline is already on an external USB hub"
    expected_added = sorted(powered_external_generations - direct_external_generations)
    if (
        not expected_added
        or ancestry.get("added_hub_ancestor_generations") != expected_added
    ):
        return "handoff added-hub ancestry does not match its raw USB records"
    if direct_device.get("usb_instance_generation") == powered_device.get(
        "usb_instance_generation"
    ):
        return "handoff raw USB instance generation did not change"
    direct_token = ancestry.get("direct_instance_token")
    powered_token = ancestry.get("powered_hub_instance_token")
    if (
        not isinstance(direct_token, str)
        or not direct_token
        or not isinstance(powered_token, str)
        or not powered_token
        or direct_token == powered_token
    ):
        return "handoff runtime instance token evidence is invalid"
    direct_generation = ancestry.get("direct_graph_generation")
    powered_generation = ancestry.get("powered_hub_graph_generation")
    if (
        not isinstance(direct_generation, int)
        or isinstance(direct_generation, bool)
        or not isinstance(powered_generation, int)
        or isinstance(powered_generation, bool)
        or powered_generation <= direct_generation
    ):
        return "handoff graph-generation evidence is invalid"
    if ancestry.get("lark_absent") is not True:
        return "handoff did not prove Lark absent"
    if (
        not isinstance(ancestry.get("settled_sample_count"), int)
        or ancestry["settled_sample_count"] < USB_EVENT_CONFIRMATION_SAMPLES
    ):
        return "handoff lacks continuously sampled settled evidence"
    if (
        attestation.get("observation_kind") != "operator_attestation"
        or attestation.get("claim") != "external_hub_power_supply_connected"
        or attestation.get("operator_acknowledged") is not True
    ):
        return "handoff powered-hub operator attestation is invalid"
    if (
        handoff.get("layout_handoff_validated") is not True
        or ancestry.get("validated") is not True
    ):
        return "handoff is not marked validated"
    return None


def summarize_connection_layout_gate(
    transitions: list[dict[str, Any]],
    *,
    campaign: str,
    connection_plan: str | None,
) -> dict[str, Any]:
    """Require a bound 10-direct/10-powered-hub split without weakening timing gates."""
    if connection_plan is None:
        return {
            "verdict": "NOT_REQUESTED",
            "connection_plan": None,
            "required": False,
        }
    errors: list[str] = []
    if connection_plan != CONNECTION_PLAN_DIRECT10_HUB10:
        errors.append(f"unsupported connection plan {connection_plan!r}")
    if campaign not in CONNECTION_PLAN_CAMPAIGNS:
        errors.append(f"connection plan is not valid for campaign {campaign!r}")

    handoffs = [
        item
        for item in transitions
        if item.get("phase") == CONNECTION_LAYOUT_HANDOFF_PHASE
    ]
    external_hub_generations: set[str] = set()
    handoff_valid = False
    if len(handoffs) != 1:
        errors.append(f"expected one connection-layout handoff, found {len(handoffs)}")
        handoff_index = None
    else:
        handoff = handoffs[0]
        handoff_index = transitions.index(handoff)
        ancestry = handoff.get("observed_usb_ancestry")
        added = (
            ancestry.get("added_hub_ancestor_generations")
            if isinstance(ancestry, dict)
            else None
        )
        if isinstance(added, list) and all(
            isinstance(item, str) and item for item in added
        ):
            external_hub_generations.update(added)
        handoff_valid = bool(
            handoff.get("cycle") == CONNECTION_LAYOUT_BOUNDARY_CYCLE
            and handoff.get("connection_layout") == "direct_to_powered_hub"
            and handoff.get("outcome") == "completed"
            and external_hub_generations
            and _recorded_layout_handoff_error(handoff) is None
        )
        if not handoff_valid:
            errors.append(
                "connection-layout handoff evidence is invalid: "
                + (_recorded_layout_handoff_error(handoff) or "placement is invalid")
            )

    for transition_index, transition in enumerate(transitions):
        if transition.get("phase") == CONNECTION_LAYOUT_HANDOFF_PHASE:
            continue
        cycle = transition.get("cycle")
        if (
            not isinstance(cycle, int)
            or isinstance(cycle, bool)
            or not 0 <= cycle <= 20
        ):
            errors.append(
                f"transition {transition.get('phase')!r} has invalid cycle {cycle!r}"
            )
            continue
        expected_layout = connection_layout_for_cycle(cycle, connection_plan)
        if transition.get("connection_layout") != expected_layout:
            errors.append(
                f"transition {transition.get('phase')!r} cycle {cycle} is labeled "
                f"{transition.get('connection_layout')!r}, expected {expected_layout!r}"
            )
        if handoff_index is not None and transition.get("gate_kind") is not None:
            if (
                cycle <= CONNECTION_LAYOUT_BOUNDARY_CYCLE
                and transition_index > handoff_index
            ):
                errors.append(
                    f"transition {transition.get('phase')!r} cycle {cycle} appears "
                    "after the layout handoff"
                )
            if (
                cycle > CONNECTION_LAYOUT_BOUNDARY_CYCLE
                and transition_index < handoff_index
            ):
                errors.append(
                    f"transition {transition.get('phase')!r} cycle {cycle} appears "
                    "before the layout handoff"
                )

    observed_cycles: dict[str, dict[str, list[int]]] = {}
    for gate_kind in required_gate_kinds(campaign):
        observed_cycles[gate_kind] = {
            CONNECTION_LAYOUT_DIRECT: [],
            CONNECTION_LAYOUT_POWERED_HUB: [],
        }
        for transition in transitions:
            if transition.get("gate_kind") != gate_kind:
                continue
            cycle = transition.get("cycle")
            layout = transition.get("connection_layout")
            if layout in observed_cycles[gate_kind] and isinstance(cycle, int):
                observed_cycles[gate_kind][layout].append(cycle)
            topologies = _transition_usb_topologies(transition)
            observed_devices = 0
            shared_external_ancestor = False
            for topology in topologies:
                candidate_ancestors: dict[str, set[str]] = {}
                for candidate_id in USB_MICROPHONE_FINGERPRINTS:
                    devices, device_error = _validated_usb_devices(
                        topology, candidate_id
                    )
                    if device_error or devices is None:
                        errors.append(
                            f"{gate_kind} cycle {cycle}: {device_error or 'USB topology invalid'}"
                        )
                        continue
                    for device in devices:
                        observed_devices += 1
                        ancestors, ancestry_error = _validated_hub_ancestors(device)
                        if ancestry_error or ancestors is None:
                            errors.append(
                                f"{gate_kind} cycle {cycle}: "
                                f"{ancestry_error or 'hub ancestry missing'}"
                            )
                            continue
                        generations = {
                            str(item["usb_instance_generation"]) for item in ancestors
                        }
                        observed_external_generations = (
                            _external_hub_ancestor_generations(device)
                        )
                        candidate_ancestors[candidate_id] = generations
                        on_external_hub = bool(generations & external_hub_generations)
                        if (
                            layout == CONNECTION_LAYOUT_DIRECT
                            and observed_external_generations
                        ):
                            errors.append(
                                f"{gate_kind} cycle {cycle}: direct evidence is "
                                "descended from an external USB hub"
                            )
                        if (
                            layout == CONNECTION_LAYOUT_POWERED_HUB
                            and not on_external_hub
                        ):
                            errors.append(
                                f"{gate_kind} cycle {cycle}: powered-hub evidence is "
                                "not descended from the observed handoff hub"
                            )
                if all(
                    candidate_ancestors.get(item)
                    for item in USB_MICROPHONE_FINGERPRINTS
                ):
                    shared_external_ancestor = bool(
                        candidate_ancestors["lark-a1"]
                        & candidate_ancestors["fifine-k054"]
                        & external_hub_generations
                    )
            if not observed_devices:
                errors.append(
                    f"{gate_kind} cycle {cycle}: no raw microphone was observed"
                )
            if (
                campaign == "promotion-fallback"
                and layout == CONNECTION_LAYOUT_POWERED_HUB
                and not shared_external_ancestor
            ):
                errors.append(
                    f"{gate_kind} cycle {cycle}: Lark and FIFINE lack a shared "
                    "observed external-hub ancestor"
                )
        for layout, expected in (
            (CONNECTION_LAYOUT_DIRECT, list(range(1, 11))),
            (CONNECTION_LAYOUT_POWERED_HUB, list(range(11, 21))),
        ):
            actual = sorted(observed_cycles[gate_kind][layout])
            if actual != expected:
                errors.append(
                    f"{gate_kind} {layout} cycles are {actual}, expected {expected}"
                )

    return {
        "verdict": "PASS" if not errors else "FAIL",
        "connection_plan": connection_plan,
        "required": True,
        "required_cycles": {
            CONNECTION_LAYOUT_DIRECT: 10,
            CONNECTION_LAYOUT_POWERED_HUB: 10,
        },
        "observed_cycles": observed_cycles,
        "required_handoffs": 1,
        "observed_handoffs": len(handoffs),
        "handoff_validated": handoff_valid,
        "observed_external_hub_ancestor_generations": sorted(external_hub_generations),
        "operator_attestation_is_separate_from_usb_observation": True,
        "errors": errors,
    }


def build_summary(
    *,
    sampler: LiveSampler,
    campaign: str,
    cycles: int,
    transitions: list[dict[str, Any]],
    aborted: str | None,
    fast_limit_s: float,
    max_limit_s: float,
    started_wall: float,
    connection_plan: str | None = None,
) -> dict[str, Any]:
    initial_counts = sampler.initial_service_counts
    final_counts = sampler.final_service_counts or sampler.service_counts()
    restart_delta = service_restart_delta(initial_counts, final_counts)
    service_evidence_complete = all(
        value is not None
        for value in [*initial_counts.values(), *final_counts.values()]
    )
    restart_gate_passed = bool(
        service_evidence_complete
        and all(value == 0 for value in restart_delta.values())
    )
    sampling_gate_passed = bool(
        sampler.total_samples > 0
        and 0 < sampler.interval <= MAX_CONFIGURED_INTERVAL_SECONDS
        and sampler.max_sample_gap <= MAX_MEASURED_GAP_SECONDS
    )
    safety_gate_passed = not sampler.violation_counts
    transitions_clean = all(
        item.get("safety_clean") and item.get("restart_clean") for item in transitions
    )
    gate_cycles = (
        QUALIFICATION_MATRIX_CYCLES if campaign == "matrix" else QUALIFICATION_CYCLES
    )
    timing_gates = {
        kind: summarize_timing_gate(
            transitions,
            kind=kind,
            expected_cycles=gate_cycles,
            fast_limit_s=QUALIFICATION_FAST_LIMIT_SECONDS,
            max_limit_s=QUALIFICATION_MAX_LIMIT_SECONDS,
        )
        for kind in required_gate_kinds(campaign)
    }
    connection_layout_gate = summarize_connection_layout_gate(
        transitions,
        campaign=campaign,
        connection_plan=connection_plan,
    )
    connection_layout_passed = connection_layout_gate["verdict"] in {
        "PASS",
        "NOT_REQUESTED",
    }
    transitions_acceptable = all(
        item.get("outcome") == "completed"
        or (item.get("gate_kind") is not None and item.get("outcome") == "safe_state")
        for item in transitions
    )
    evidence_gates_passed = bool(
        aborted is None
        and transitions_acceptable
        and safety_gate_passed
        and transitions_clean
        and restart_gate_passed
        and sampling_gate_passed
    )
    if (
        timing_gates
        and all(item["verdict"] == "PASS" for item in timing_gates.values())
        and evidence_gates_passed
        and connection_layout_passed
    ):
        qualification_gate = "PASS"
    elif (
        timing_gates
        and all(item["verdict"] != "FAIL" for item in timing_gates.values())
        and any(item["verdict"] == "INCOMPLETE" for item in timing_gates.values())
        and evidence_gates_passed
        and connection_layout_passed
    ):
        qualification_gate = "INCOMPLETE"
    else:
        qualification_gate = "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": qualification_gate,
        "qualification_gate": qualification_gate,
        "campaign": campaign,
        "connection_plan": connection_plan,
        "requested_cycles": cycles,
        "started_timestamp": started_wall,
        "finished_timestamp": time.time(),
        "aborted": aborted,
        "non_destructive": True,
        "physical_evidence_claimed": False,
        "thresholds": {
            "configured_interval_max_s": MAX_CONFIGURED_INTERVAL_SECONDS,
            "measured_start_gap_max_s": MAX_MEASURED_GAP_SECONDS,
            "transition_fast_s": QUALIFICATION_FAST_LIMIT_SECONDS,
            "transition_max_s": QUALIFICATION_MAX_LIMIT_SECONDS,
            "matrix_cycles": QUALIFICATION_MATRIX_CYCLES,
            "repeated_cycles": QUALIFICATION_CYCLES,
            "required_fast_for_20": QUALIFICATION_REQUIRED_FAST,
        },
        "sampling": {
            "configured_interval_s": sampler.interval,
            "samples": sampler.total_samples,
            "average_start_gap_s": (
                round(sampler.sample_gap_sum / sampler.sample_gap_count, 6)
                if sampler.sample_gap_count
                else None
            ),
            "max_start_gap_s": round(sampler.max_sample_gap, 6),
            "max_capture_duration_s": round(sampler.max_capture_duration, 6),
            "deadline_misses": sampler.deadline_misses,
            "gate": "PASS" if sampling_gate_passed else "FAIL",
        },
        "link_safety": {
            "gate": "PASS" if safety_gate_passed else "FAIL",
            "violation_counts": dict(sampler.violation_counts),
            "first_violations": sampler.first_violations,
            "requirements": [
                "zero physical/raw microphone links to HFP",
                "zero inactive microphone links into AEC, bridge.mic, or HFP",
                "zero duplicate uplink owners",
                f"only {MICROPHONE_OUTPUT} may feed the HFP sink",
            ],
        },
        "services": {
            "gate": "PASS" if restart_gate_passed else "FAIL",
            "initial_restart_counts": initial_counts,
            "final_restart_counts": final_counts,
            "restart_delta": restart_delta,
            "supervisor_restart_delta": restart_delta.get("bridge-supervisor.service"),
            "evidence_complete": service_evidence_complete,
        },
        "timing_gates": timing_gates,
        "connection_layout_gate": connection_layout_gate,
        "transitions": transitions,
    }


def default_output_dir() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return Path(f"/var/tmp/larkbridge-microphone-hotplug-{stamp}")


def remote_sampler(
    *,
    status_path: Path,
    interval: float,
    duration: float,
) -> int:
    """Stream read-only JSONL evidence for the host orchestrator.

    The host sends this source over SSH to ``python3 -``. Nothing is copied into the Pi
    checkout and no service or graph is changed.
    """
    started = time.monotonic()
    stop_services = threading.Event()
    service_lock = threading.Lock()
    service_counts, service_error = query_service_restart_counts()

    def monitor_services() -> None:
        nonlocal service_counts, service_error
        while not stop_services.wait(2.0):
            counts, error = query_service_restart_counts()
            with service_lock:
                service_counts = counts
                service_error = error

    monitor = threading.Thread(
        target=monitor_services,
        name="hotplug-remote-service-monitor",
        daemon=True,
    )
    monitor.start()
    try:
        print(
            json.dumps(
                {
                    "type": "stream_start",
                    "schema_version": SCHEMA_VERSION,
                    "timestamp": time.time(),
                    "interval_s": interval,
                    "boot_id": (
                        Path("/proc/sys/kernel/random/boot_id")
                        .read_text(encoding="utf-8")
                        .strip()
                        if Path("/proc/sys/kernel/random/boot_id").exists()
                        else None
                    ),
                    "service_restart_counts": service_counts,
                    "service_error": service_error,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        sequence = 0
        previous_start: float | None = None
        next_deadline = started
        known_hfp_sinks: set[str] = set()
        known_microphone_nodes: set[str] = set()
        while time.monotonic() - started < duration:
            sample_start = time.monotonic()
            usb_microphones, usb_error = read_usb_microphones()
            status, status_error = read_status(status_path)
            link_text, link_error = command_output(
                ["pw-link", "-l"], timeout=max(0.05, min(interval * 0.80, 0.16))
            )
            links = parse_pw_links(link_text) if link_error is None else []
            endpoints = status.get("endpoints") or {}
            if isinstance(endpoints, dict):
                sink = endpoints.get("hfp_sink")
                if isinstance(sink, str) and sink:
                    known_hfp_sinks.add(sink)
            _selected, observed_microphone_nodes = invariants.microphone_inventory(
                status
            )
            known_microphone_nodes.update(observed_microphone_nodes)
            evaluated = evaluate_link_invariants(
                status,
                status_error,
                links,
                link_error=link_error,
                known_hfp_sinks=known_hfp_sinks,
                known_microphone_nodes=known_microphone_nodes,
            )
            microphone = status.get("microphone") or {}
            aec = status.get("aec") or {}
            sequence += 1
            with service_lock:
                counts = dict(service_counts)
                count_error = service_error
            finished = time.monotonic()
            sample = {
                "type": "sample",
                "schema_version": SCHEMA_VERSION,
                "seq": sequence,
                "timestamp": time.time(),
                "elapsed_s": round(sample_start - started, 6),
                "capture_started_monotonic": sample_start,
                "state": status.get("state"),
                "status_timestamp": status.get("timestamp"),
                "status_error": status_error,
                "usb_microphones": usb_microphones,
                "usb_error": usb_error,
                "selection_reason": (
                    microphone.get("selection_reason")
                    if isinstance(microphone, dict)
                    else None
                ),
                "microphone": selected_microphone(status),
                "candidates": candidate_inventory(status),
                "graph_generation": status.get("generation"),
                "aec": {
                    key: aec.get(key) for key in ("enabled", "verified", "owner_pid")
                }
                if isinstance(aec, dict)
                else {},
                "links": [list(pair) for pair in links],
                "link_error": link_error,
                "invariants": evaluated,
                "service_restart_counts": counts,
                "service_error": count_error,
                "sampling": {
                    "configured_interval_s": interval,
                    "start_gap_s": (
                        round(sample_start - previous_start, 6)
                        if previous_start is not None
                        else None
                    ),
                    "capture_duration_s": round(finished - sample_start, 6),
                    "deadline_late_s": round(max(0.0, sample_start - next_deadline), 6),
                },
            }
            previous_start = sample_start
            print(json.dumps(sample, separators=(",", ":")), flush=True)
            next_deadline += interval
            remaining = next_deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            else:
                next_deadline = time.monotonic()
        final_counts, final_error = query_service_restart_counts()
        print(
            json.dumps(
                {
                    "type": "stream_stop",
                    "schema_version": SCHEMA_VERSION,
                    "timestamp": time.time(),
                    "elapsed_s": round(time.monotonic() - started, 6),
                    "last_seq": sequence,
                    "service_restart_counts": final_counts,
                    "service_error": final_error,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
    except BrokenPipeError:
        return 0
    finally:
        stop_services.set()
        monitor.join(timeout=3)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--campaign",
        choices=("matrix", "promotion-fallback", "fifine-replug", "inactive-fifine"),
        default="matrix",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        help="physical cycles (default: 1 for matrix, otherwise 20)",
    )
    parser.add_argument(
        "--connection-plan",
        choices=(CONNECTION_PLAN_DIRECT10_HUB10,),
        help=(
            "strict 20-cycle qualification split: cycles 1-10 direct, one "
            "observed handoff, then cycles 11-20 through an attested powered hub"
        ),
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TRANSITION_TIMEOUT_SECONDS
    )
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE_SECONDS)
    parser.add_argument("--fast-limit", type=float, default=DEFAULT_FAST_LIMIT_SECONDS)
    parser.add_argument("--out-dir", type=Path, default=default_output_dir())
    parser.add_argument("--status-path", type=Path, default=invariants.STATUS)
    parser.add_argument(
        "--remote-sampler",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=24 * 60 * 60,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    configured_cycles = args.cycles
    if args.cycles is None:
        args.cycles = 1 if args.campaign == "matrix" else DEFAULT_REPEATED_CYCLES
    if args.connection_plan is not None:
        if args.campaign not in CONNECTION_PLAN_CAMPAIGNS:
            parser.error(
                "--connection-plan is accepted only for promotion-fallback or "
                "fifine-replug"
            )
        if configured_cycles not in {None, QUALIFICATION_CYCLES}:
            parser.error(
                f"--connection-plan {CONNECTION_PLAN_DIRECT10_HUB10} requires "
                f"exactly {QUALIFICATION_CYCLES} cycles"
            )
        args.cycles = QUALIFICATION_CYCLES
    if args.cycles <= 0:
        parser.error("--cycles must be positive")
    if not 0 < args.interval <= MAX_CONFIGURED_INTERVAL_SECONDS:
        parser.error(f"--interval must be >0 and <= {MAX_CONFIGURED_INTERVAL_SECONDS}")
    if args.timeout <= 0 or args.fast_limit <= 0 or args.settle < 0:
        parser.error(
            "--timeout/--fast-limit must be positive and --settle non-negative"
        )
    if args.fast_limit > args.timeout:
        parser.error("--fast-limit cannot exceed --timeout")
    if args.fast_limit > QUALIFICATION_FAST_LIMIT_SECONDS:
        parser.error(
            f"--fast-limit cannot exceed the fixed qualification limit of "
            f"{QUALIFICATION_FAST_LIMIT_SECONDS}"
        )
    if args.timeout > QUALIFICATION_MAX_LIMIT_SECONDS:
        parser.error(
            f"--timeout cannot exceed the fixed qualification deadline of "
            f"{QUALIFICATION_MAX_LIMIT_SECONDS}"
        )
    if args.duration <= 0:
        parser.error("--duration must be positive")
    return args


def _structured_failure(
    *,
    campaign: str,
    cycles: int,
    output_directory: Path,
    failure_type: str,
    message: str,
    connection_plan: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": "FAIL",
        "qualification_gate": "FAIL",
        "campaign": campaign,
        "connection_plan": connection_plan,
        "requested_cycles": cycles,
        "finished_timestamp": time.time(),
        "failure": {"type": failure_type, "message": message},
        "output_directory": str(output_directory),
        "non_destructive": True,
        "physical_evidence_claimed": False,
    }


def _print_json(document: dict[str, Any]) -> None:
    json.dump(document, sys.stdout, indent=2)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.remote_sampler:
        return remote_sampler(
            status_path=args.status_path,
            interval=args.interval,
            duration=args.duration,
        )
    try:
        args.out_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        failure = _structured_failure(
            campaign=args.campaign,
            cycles=args.cycles,
            output_directory=args.out_dir,
            failure_type="OutputDirectoryExists",
            message="refusing to overwrite an existing evidence output directory",
            connection_plan=args.connection_plan,
        )
        _print_json(failure)
        print(failure["failure"]["message"], file=sys.stderr, flush=True)
        return 2
    except OSError as exc:
        failure = _structured_failure(
            campaign=args.campaign,
            cycles=args.cycles,
            output_directory=args.out_dir,
            failure_type=type(exc).__name__,
            message=str(exc),
            connection_plan=args.connection_plan,
        )
        _print_json(failure)
        print(
            f"Could not create evidence directory: {exc}", file=sys.stderr, flush=True
        )
        return 2
    timeline_path = args.out_dir / "timeline.jsonl"
    summary_path = args.out_dir / "summary.json"
    started_wall = time.time()
    sampler = LiveSampler(args.status_path, timeline_path, args.interval)
    transitions: list[dict[str, Any]] = []
    aborted: str | None = None
    unexpected_failures: list[dict[str, str]] = []
    sampler_start_attempted = False

    print(
        "This harness only observes. Keep the active call connected and perform each "
        "physical action when prompted.",
        file=sys.stderr,
        flush=True,
    )
    try:
        sampler_start_attempted = True
        sampler.start()
        if args.campaign == "matrix":
            run_matrix(
                sampler,
                transitions,
                cycles=args.cycles,
                timeout_s=args.timeout,
                settle_s=args.settle,
            )
        elif args.campaign == "promotion-fallback":
            run_promotion_fallback(
                sampler,
                transitions,
                cycles=args.cycles,
                timeout_s=args.timeout,
                settle_s=args.settle,
                connection_plan=args.connection_plan,
            )
        elif args.campaign == "fifine-replug":
            run_fifine_replug(
                sampler,
                transitions,
                cycles=args.cycles,
                timeout_s=args.timeout,
                settle_s=args.settle,
                connection_plan=args.connection_plan,
            )
        else:
            run_inactive_fifine(
                sampler,
                transitions,
                cycles=args.cycles,
                timeout_s=args.timeout,
                settle_s=args.settle,
            )
    except (CampaignAbort, KeyboardInterrupt) as exc:
        aborted = str(exc) or type(exc).__name__
        try:
            sampler.record_event("campaign_abort", reason=aborted)
        except Exception as event_exc:  # noqa: BLE001 - summary still takes priority
            failure = {"type": type(event_exc).__name__, "message": str(event_exc)}
            unexpected_failures.append(failure)
            print(
                f"Could not record campaign abort event: {event_exc}",
                file=sys.stderr,
                flush=True,
            )
        print(f"Campaign stopped: {aborted}", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 - direct runner must preserve failure evidence
        failure = {"type": type(exc).__name__, "message": str(exc)}
        unexpected_failures.append(failure)
        aborted = f"{failure['type']}: {failure['message']}"
        try:
            sampler.record_event("campaign_failure", failure=failure)
        except Exception as event_exc:  # noqa: BLE001 - summary still takes priority
            print(
                f"Could not record campaign failure event: {event_exc}",
                file=sys.stderr,
                flush=True,
            )
        print(f"Campaign failed unexpectedly: {aborted}", file=sys.stderr, flush=True)
    finally:
        if sampler_start_attempted:
            try:
                sampler.stop()
            except Exception as exc:  # noqa: BLE001 - retain a structured cleanup failure
                failure = {"type": type(exc).__name__, "message": str(exc)}
                unexpected_failures.append(failure)
                aborted = aborted or f"{failure['type']}: {failure['message']}"
                print(f"Sampler cleanup failed: {exc}", file=sys.stderr, flush=True)

    summary = build_summary(
        sampler=sampler,
        campaign=args.campaign,
        cycles=args.cycles,
        transitions=transitions,
        aborted=aborted,
        fast_limit_s=args.fast_limit,
        max_limit_s=args.timeout,
        started_wall=started_wall,
        connection_plan=args.connection_plan,
    )
    summary["artifacts"] = {"summary_json": str(summary_path)}
    if timeline_path.is_file():
        summary["artifacts"]["timeline_jsonl"] = str(timeline_path)
    if unexpected_failures:
        summary["failure"] = unexpected_failures[0]
        summary["failures"] = unexpected_failures
        summary["verdict"] = "FAIL"
        summary["qualification_gate"] = "FAIL"
    boot_id: str | None = None
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    try:
        if boot_id_path.exists():
            boot_id = boot_id_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        summary.setdefault("host_errors", []).append(
            {"type": type(exc).__name__, "message": str(exc)}
        )
    summary["host"] = {
        "hostname": platform.node(),
        "boot_id": boot_id,
        "pid": os.getpid(),
    }
    try:
        summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        summary["failure"] = {"type": type(exc).__name__, "message": str(exc)}
        summary["verdict"] = "FAIL"
        summary["qualification_gate"] = "FAIL"
        _print_json(summary)
        print(f"Could not write summary: {exc}", file=sys.stderr, flush=True)
        return 1
    _print_json(summary)
    return 0 if summary["qualification_gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
