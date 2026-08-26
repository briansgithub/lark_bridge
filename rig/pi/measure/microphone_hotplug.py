#!/usr/bin/env python3
"""Operator-driven, non-destructive active-call microphone hotplug qualification.

This program never changes USB authorization, services, configuration, or PipeWire links.
It only samples the bridge status file and ``pw-link -l`` while an operator performs
physical plug/unplug actions. Prompts go to stderr; stdout and the artifact files remain
machine-readable.

Typical campaigns::

    # One complete both/either/neither matrix.
    python3 rig/pi/measure/microphone_hotplug.py --campaign matrix

    # The E18 timing gates: 20 promotion and 20 fallback transitions.
    python3 rig/pi/measure/microphone_hotplug.py --campaign promotion-fallback

    # Twenty physical replug cycles proving a new FIFINE instance token each time.
    python3 rig/pi/measure/microphone_hotplug.py --campaign fifine-replug

The default 0.15 second interval leaves scheduling margin below the 0.20 second evidence
limit. Every sample is flushed immediately to ``timeline.jsonl``. ``summary.json`` carries
the transition timing gates and never upgrades a short smoke run into qualification.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
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
DEFAULT_SETTLE_SECONDS = 0.60
DEFAULT_REPEATED_CYCLES = QUALIFICATION_CYCLES
USB_ACTION_OBSERVATION_TIMEOUT_SECONDS = 60.0
USB_SYSFS_ROOT = Path("/sys/bus/usb/devices")
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
USB_TIMING_ORIGIN = "usb_sysfs_edge"
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
        }
    return {
        "seq": sample.get("seq"),
        "remote_seq": sample.get("remote_seq"),
        "capture_started_monotonic": sample.get("capture_started_monotonic"),
        "usb_microphones": copy.deepcopy(sample.get("usb_microphones")),
        "usb_error": sample.get("usb_error"),
    }


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


def validate_action_usb_baseline(phase: str, baseline: dict[str, Any]) -> None:
    """Reject a pre-changed, missing, or ambiguous gated baseline before NOW."""
    gate_kind = USB_GATE_BY_PHASE.get(phase)
    if gate_kind is None:
        return
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
    elif current_wall - float(timestamp) > 6.0:
        violation(
            "H0", f"bridge status stale by {current_wall - float(timestamp):.2f}s"
        )

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
    completed_fast = sum(
        1
        for item in considered
        if item.get("outcome") == "completed"
        and item.get("timing_origin") == USB_TIMING_ORIGIN
        and isinstance(item.get("transition_latency_s"), (int, float))
        and not isinstance(item.get("transition_latency_s"), bool)
        and 0.0 <= float(item["transition_latency_s"]) <= fast_limit_s
    )
    bounded_or_safe = sum(
        1
        for item in considered
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

    def mark_action(self, phase: str, cycle: int, instruction: str) -> dict[str, Any]:
        counts, error = self._query_service_counts()
        if error:
            raise CampaignAbort(f"restart-count evidence failed: {error}")
        with self._condition:
            self._service_counts = dict(counts)
            self._service_error = None
            usb_baseline = usb_baseline_from_sample(
                self._recent[-1] if self._recent else None
            )
            validate_action_usb_baseline(phase, usb_baseline)
            action_monotonic = time.monotonic()
            self._phase = phase
            self._cycle = cycle
            self._action_id = f"{phase}:{cycle}:{time.time_ns()}"
            event = {
                "type": "operator_action",
                "timestamp": time.time(),
                "elapsed_s": round(action_monotonic - self._started_monotonic, 6),
                "monotonic": action_monotonic,
                "after_seq": self._seq,
                "phase": phase,
                "cycle": cycle,
                "action_id": self._action_id,
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
    for key in ("capture_started_monotonic", "remote_elapsed_s", "elapsed_s"):
        before = earlier.get(key)
        after = later.get(key)
        if (
            isinstance(before, (int, float))
            and not isinstance(before, bool)
            and isinstance(after, (int, float))
            and not isinstance(after, bool)
        ):
            return float(after) - float(before)
    raise CampaignAbort("samples omitted a shared monotonic timing origin")


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
    }


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
    usb_event_sample: dict[str, Any] | None = None
    usb_event_error: str | None = None
    latest: dict[str, Any] | None = None
    seen_seq = after_seq
    outcome = "timeout"

    if gated_usb_timing:
        baseline = action.get("usb_baseline")
        if not isinstance(baseline, dict):
            usb_event_error = "USB action baseline is missing"
            outcome = "usb_event_error"
        else:
            candidate_id, expected_before, _expected_after = USB_GATE_TARGET[gate_kind]
            baseline_devices, baseline_error = _validated_usb_devices(
                baseline.get("usb_microphones"), candidate_id
            )
            if baseline.get("usb_error"):
                usb_event_error = f"USB action baseline failed: {baseline['usb_error']}"
            elif baseline_error:
                usb_event_error = f"USB action baseline is invalid: {baseline_error}"
            elif len(baseline_devices or []) != expected_before:
                usb_event_error = (
                    f"USB action baseline for {gate_kind} expected {expected_before} "
                    f"{candidate_id} instance(s), found {len(baseline_devices or [])}"
                )
            if usb_event_error:
                outcome = "usb_event_error"

    while time.monotonic() < deadline:
        if outcome == "usb_event_error":
            break
        samples = sampler.samples_after(after_seq)
        for sample in samples:
            if int(sample["seq"]) <= seen_seq:
                continue
            seen_seq = int(sample["seq"])
            if not _sample_captured_by_deadline(sample, action, deadline):
                continue
            latest = sample
            if gated_usb_timing and usb_event_sample is None:
                assert isinstance(action.get("usb_baseline"), dict)
                edge_state, edge_error = observe_expected_usb_edge(
                    gate_kind, action["usb_baseline"], sample
                )
                if edge_state == "error":
                    usb_event_error = edge_error or "ambiguous USB topology"
                    outcome = "usb_event_error"
                    break
                if edge_state == "waiting":
                    continue
                usb_event_sample = sample
                # The 60 second runtime window starts only after raw USB sysfs
                # exposes the expected edge. Host receipt time bounds waiting;
                # source monotonic timestamps below measure the actual Pi interval.
                deadline = _sample_observed_monotonic(sample, action) + timeout_s
                first_match = None
            failures = expectation_failures(sample, expected)
            if failures:
                first_match = None
                continue
            if first_match is None:
                first_match = sample
            if float(sample["elapsed_s"]) - float(first_match["elapsed_s"]) >= settle_s:
                outcome = "completed"
                break
        if outcome == "completed":
            break
        if outcome == "usb_event_error":
            break
        sampler.wait_for_new_sample(
            seen_seq, min(1.0, max(0.01, deadline - time.monotonic()))
        )

    if gated_usb_timing and usb_event_sample is None and outcome == "timeout":
        outcome = "usb_event_missing"
        usb_event_error = (
            f"expected {gate_kind} USB sysfs edge was not observed within "
            f"{action_observation_timeout_s:.1f}s"
        )

    transition_samples = [
        sample
        for sample in sampler.samples_after(after_seq)
        if _sample_captured_by_deadline(sample, action, deadline)
    ]
    latest = transition_samples[-1] if transition_samples else latest
    if (
        outcome == "timeout"
        and (not gated_usb_timing or usb_event_sample is not None)
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

    if gated_usb_timing and usb_event_sample is not None:
        first_latency = (
            round(_sample_interval_seconds(usb_event_sample, first_match), 6)
            if first_match is not None
            else None
        )
        completion_latency = (
            round(_sample_interval_seconds(usb_event_sample, latest), 6)
            if outcome == "completed" and latest is not None
            else None
        )
        action_to_usb_s = round(
            _sample_observed_monotonic(usb_event_sample, action)
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
        "gate_kind": gate_kind,
        "action_id": action["action_id"],
        "action_timestamp": action["timestamp"],
        "outcome": outcome,
        "timing_origin": timing_origin,
        "operator_latency_s": action_to_usb_s,
        "action_to_usb_s": action_to_usb_s,
        "usb_action_observation_timeout_s": (
            action_observation_timeout_s if gated_usb_timing else None
        ),
        "usb_baseline": copy.deepcopy(action.get("usb_baseline")),
        "usb_event": (
            _usb_event_evidence(usb_event_sample, gate_kind)
            if usb_event_sample is not None and gated_usb_timing
            else None
        ),
        "usb_event_error": usb_event_error,
        "transition_latency_s": first_latency,
        "settled_latency_s": completion_latency,
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
    sampler.record_event("transition_result", result=result)
    return result


def operator_action(
    sampler: LiveSampler,
    *,
    phase: str,
    cycle: int,
    instruction: str,
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
    action = sampler.mark_action(phase, cycle, instruction)
    print(f"NOW: {instruction}", file=sys.stderr, flush=True)
    action["required_acknowledgement"] = required
    action["operator_acknowledged"] = True
    sampler.record_event(
        "operator_acknowledgement",
        phase=phase,
        cycle=cycle,
        action_id=action["action_id"],
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
) -> dict[str, Any]:
    action = operator_action(
        sampler,
        phase=phase,
        cycle=cycle,
        instruction=instruction,
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
        promotion = run_step(
            sampler,
            transitions,
            phase="lark_promotion",
            cycle=cycle,
            instruction="Plug in the Lark while leaving the FIFINE connected.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="lark-a1",
                different_instance_token=fifine_token,
                generation_after=fifine_generation,
                candidate_states={"fifine-k054": frozenset({"usable"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="promotion",
            input_fn=input_fn,
        )
        lark_token, lark_generation = _sample_identity(promotion)
        fallback = run_step(
            sampler,
            transitions,
            phase="lark_fallback",
            cycle=cycle,
            instruction="Unplug the Lark while leaving the FIFINE connected.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="fifine-k054",
                different_instance_token=lark_token,
                generation_after=lark_generation,
                candidate_states={"lark-a1": frozenset({"absent"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="fallback",
            input_fn=input_fn,
        )
        prior_fifine_token, prior_fifine_generation = _sample_identity(fallback)

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
        restored_fifine = run_step(
            sampler,
            transitions,
            phase="restore_fifine",
            cycle=cycle,
            instruction="Reconnect only the FIFINE.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="fifine-k054",
                different_instance_token=prior_fifine_token,
                generation_after=prior_fifine_generation,
                candidate_states={"lark-a1": frozenset({"absent"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="fifine_replug",
            input_fn=input_fn,
        )
        restored_token, restored_generation = _sample_identity(restored_fifine)
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
    )
    previous_token, previous_generation = _sample_identity(opening)
    for cycle in range(1, cycles + 1):
        promoted = run_step(
            sampler,
            transitions,
            phase="lark_promotion",
            cycle=cycle,
            instruction="Plug in the Lark; leave FIFINE connected.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="lark-a1",
                different_instance_token=previous_token,
                generation_after=previous_generation,
                candidate_states={"fifine-k054": frozenset({"usable"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="promotion",
            input_fn=input_fn,
        )
        previous_token, previous_generation = _sample_identity(promoted)
        fallback = run_step(
            sampler,
            transitions,
            phase="lark_fallback",
            cycle=cycle,
            instruction="Unplug the Lark; leave FIFINE connected.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="fifine-k054",
                different_instance_token=previous_token,
                generation_after=previous_generation,
                candidate_states={"lark-a1": frozenset({"absent"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="fallback",
            input_fn=input_fn,
        )
        previous_token, previous_generation = _sample_identity(fallback)


def run_fifine_replug(
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
    )
    previous_token, previous_generation = _sample_identity(opening)
    for cycle in range(1, cycles + 1):
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
        )
        restored = run_step(
            sampler,
            transitions,
            phase="fifine_replug/restore",
            cycle=cycle,
            instruction="Reconnect the same FIFINE to any allowed USB port.",
            expected=Expectation(
                state="ACTIVE",
                selected_id="fifine-k054",
                different_instance_token=previous_token,
                generation_after=previous_generation,
                candidate_states={"lark-a1": frozenset({"absent"})},
            ),
            timeout_s=timeout_s,
            settle_s=settle_s,
            gate_kind="fifine_replug",
            input_fn=input_fn,
        )
        previous_token, previous_generation = _sample_identity(restored)


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
    ):
        qualification_gate = "PASS"
    elif (
        timing_gates
        and all(item["verdict"] != "FAIL" for item in timing_gates.values())
        and any(item["verdict"] == "INCOMPLETE" for item in timing_gates.values())
        and evidence_gates_passed
    ):
        qualification_gate = "INCOMPLETE"
    else:
        qualification_gate = "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": qualification_gate,
        "qualification_gate": qualification_gate,
        "campaign": campaign,
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
    if args.cycles is None:
        args.cycles = 1 if args.campaign == "matrix" else DEFAULT_REPEATED_CYCLES
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
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "verdict": "FAIL",
        "qualification_gate": "FAIL",
        "campaign": campaign,
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
            )
        elif args.campaign == "fifine-replug":
            run_fifine_replug(
                sampler,
                transitions,
                cycles=args.cycles,
                timeout_s=args.timeout,
                settle_s=args.settle,
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
