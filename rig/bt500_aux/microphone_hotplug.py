#!/usr/bin/env python3
"""Host-side E18 physical microphone hotplug qualification orchestrator.

The orchestrator streams the current stdlib sampler source to ``python3 -`` over SSH.
It does not install files, restart services, change USB authorization, or mutate the Pi.
The only mutable operations are local evidence files and the operator's prompted physical
plug/unplug actions.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any, TextIO

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    from .harness import (
        EvidenceError,
        HardFailure,
        HardwareNotReady,
        SshBackend,
        atomic_json,
        evidence_manifest,
        new_error_lines,
        service_restarts,
        validate_snapshot,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bt500_aux.harness import (  # type: ignore[no-redef]
        EvidenceError,
        HardFailure,
        HardwareNotReady,
        SshBackend,
        atomic_json,
        evidence_manifest,
        new_error_lines,
        service_restarts,
        validate_snapshot,
    )

from rig.pi.measure import microphone_hotplug as core

CHECKPOINT_SCHEMA = "larkbridge.e18.microphone-hotplug.v1"
DEFAULT_REMOTE_REPO = "/home/admin/rpi-lark-bridge"
DEFAULT_HOST = "larkbridge"
TIMELINE_NAME = "samples.jsonl"
PROVENANCE_NAME = "provenance.json"
SUMMARY_NAME = "summary.json"
MANIFEST_NAME = "evidence-manifest.json"
RESUME_NAME = "resume.json"
RESUME_SCHEMA = "larkbridge.e18.microphone-hotplug-resume.v1"
QUALIFICATION_FAST_LIMIT_SECONDS = 30.0
QUALIFICATION_MAX_LIMIT_SECONDS = 60.0
ARTIFACT_NAMES = {
    "preflight": "preflight.json",
    "closing": "closing.json",
    "samples": TIMELINE_NAME,
    "checkpoint": "checkpoint.json",
    "provenance": PROVENANCE_NAME,
    "summary": SUMMARY_NAME,
    "manifest": MANIFEST_NAME,
    "resume": RESUME_NAME,
}
LOCAL_DECISION_SOURCE_PATHS = {
    "host_harness": "rig/bt500_aux/microphone_hotplug.py",
    "harness_library": "rig/bt500_aux/harness.py",
    "streamed_sampler": "rig/pi/measure/microphone_hotplug.py",
    "local_invariants": "rig/pi/measure/invariants.py",
}
REMOTE_TRACKED_SOURCE_PATHS = {
    "invariants": "rig/pi/measure/invariants.py",
    "remote_snapshot": "rig/bt500_aux/remote.py",
    "remote_harness": "rig/bt500_aux/harness.py",
    "bridge_supervisor": "pi/bridged/bridge_supervisor.py",
    "microphone_resolver": "pi/bridged/microphones.py",
    "output_resolver": "pi/bridged/outputs.py",
    "controller_roles": "pi/bridged/controller_roles.py",
    "btadapters": "pi/bridged/btadapters.py",
}
PRESERVED_CONFIG_SOURCE = "bridge_config"
PRESERVED_CONFIG_REPOSITORY_PATH = "config/bridge.toml"
PRESERVED_CONFIG_BINDING_KIND = "preserved_untracked_hash_only"


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def git_commit(repo: Path = REPO) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def default_run_dir(repo: Path = REPO, *, commit: str | None = None) -> Path:
    revision = commit or git_commit(repo)
    return (
        repo
        / "docs"
        / "experiments"
        / "results"
        / "E18"
        / "field"
        / f"hotplug-{utc_stamp()}-{revision[:12]}"
    )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} is unreadable or malformed: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} is not a JSON object")
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _verified_predecessor_manifest(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = root / MANIFEST_NAME
    manifest = _json_object(manifest_path, "predecessor evidence manifest")
    if manifest.get("schema_version") != 1 or not isinstance(
        manifest.get("files"), list
    ):
        raise EvidenceError("predecessor evidence manifest has an unsupported schema")

    records: dict[str, dict[str, Any]] = {}
    for raw in manifest["files"]:
        if not isinstance(raw, dict):
            raise EvidenceError(
                "predecessor evidence manifest contains a malformed entry"
            )
        name = raw.get("path")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).is_absolute()
            or "\\" in name
            or Path(name).as_posix() != name
        ):
            raise EvidenceError(f"predecessor manifest path is unsafe: {name!r}")
        path = root / name
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise EvidenceError(
                f"predecessor manifest path escapes or is missing: {name!r}"
            ) from exc
        if name in records or not path.is_file():
            raise EvidenceError(
                f"predecessor manifest entry is duplicate or not a file: {name!r}"
            )
        expected_bytes = raw.get("bytes")
        expected_sha = raw.get("sha256")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 0
            or not isinstance(expected_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
        ):
            raise EvidenceError(f"predecessor manifest metadata is invalid: {name!r}")
        if path.stat().st_size != expected_bytes or sha256_file(path) != expected_sha:
            raise EvidenceError(
                f"predecessor artifact does not match its manifest: {name!r}"
            )
        records[name] = dict(raw)

    required = {
        "checkpoint.json",
        "closing.json",
        "preflight.json",
        PROVENANCE_NAME,
        TIMELINE_NAME,
        SUMMARY_NAME,
    }
    if not required.issubset(records):
        missing = sorted(required - records.keys())
        raise EvidenceError(f"predecessor manifest omits required artifacts: {missing}")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if actual != set(records):
        raise EvidenceError(
            "predecessor directory and evidence manifest file sets differ"
        )
    return manifest, records


def _predecessor_recording_end(checkpoint: Mapping[str, Any]) -> float:
    value = checkpoint.get("updated_timestamp")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise EvidenceError("predecessor checkpoint lacks a finite end timestamp")
    return float(value)


def load_resume_evidence(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Verify a stopped direct-port predecessor and bind its completed cycles."""
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise EvidenceError(
            f"resume predecessor directory is unavailable: {exc}"
        ) from exc
    if not root.is_dir():
        raise EvidenceError("resume predecessor is not a directory")

    manifest, records = _verified_predecessor_manifest(root)
    summary = _json_object(root / SUMMARY_NAME, "predecessor summary")
    checkpoint = _json_object(root / "checkpoint.json", "predecessor checkpoint")
    provenance = _json_object(root / PROVENANCE_NAME, "predecessor provenance")
    if (
        summary.get("schema") != CHECKPOINT_SCHEMA
        or summary.get("schema_version") != 1
        or summary.get("campaign") != "promotion-fallback"
        or summary.get("connection_plan") != core.CONNECTION_PLAN_DIRECT10_HUB10
        or summary.get("requested_cycles") != core.QUALIFICATION_CYCLES
    ):
        raise EvidenceError(
            "predecessor is not a canonical promotion-fallback direct10-hub10 run"
        )
    configured = (summary.get("qualification_configuration") or {}).get("configured")
    if not isinstance(configured, dict) or configured != {
        "cycles": core.QUALIFICATION_CYCLES,
        "fast_limit_s": core.QUALIFICATION_FAST_LIMIT_SECONDS,
        "max_limit_s": core.QUALIFICATION_MAX_LIMIT_SECONDS,
        "connection_plan": core.CONNECTION_PLAN_DIRECT10_HUB10,
    }:
        raise EvidenceError("predecessor qualification settings are not canonical")
    if summary.get("aborted") != "CampaignAbort: operator aborted the campaign":
        raise EvidenceError("predecessor did not end with an explicit operator abort")
    if (
        summary.get("fatal_errors") != []
        or summary.get("remote_stderr") != []
        or summary.get("remote_returncode") not in (None, 0)
        or (summary.get("sampling_gate") or {}).get("verdict") != "PASS"
        or (summary.get("link_safety_gate") or {}).get("verdict") != "PASS"
        or (summary.get("restart_gate") or {}).get("verdict") != "PASS"
        or (summary.get("provenance_gate") or {}).get("verdict") != "PASS"
    ):
        raise EvidenceError("predecessor observed-segment evidence is not clean")
    if provenance.get("status") != "PASS" or summary.get("provenance") != provenance:
        raise EvidenceError(
            "predecessor provenance is not PASS or is not summary-bound"
        )
    commit = summary.get("commit")
    local_repository = provenance.get("local_repository")
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
        or not isinstance(local_repository, dict)
        or local_repository.get("head") != commit
    ):
        raise EvidenceError("predecessor commit and local provenance disagree")

    transitions = summary.get("transitions")
    if (
        not isinstance(transitions, list)
        or transitions != checkpoint.get("transitions")
        or any(not isinstance(item, dict) for item in transitions)
    ):
        raise EvidenceError("predecessor summary and checkpoint transitions disagree")
    handoffs = [
        item
        for item in transitions
        if item.get("phase") == core.CONNECTION_LAYOUT_HANDOFF_PHASE
    ]
    if handoffs:
        raise EvidenceError("minimal resume accepts only a pre-handoff predecessor")
    baseline = [
        item
        for item in transitions
        if item.get("phase") == "fifine_only_baseline"
        and item.get("cycle") == 0
        and item.get("outcome") == "completed"
        and item.get("connection_layout") == core.CONNECTION_LAYOUT_DIRECT
    ]
    if len(baseline) != 1:
        raise EvidenceError("predecessor lacks exactly one valid direct baseline")

    gated = [item for item in transitions if item.get("gate_kind") is not None]
    if len(transitions) != 1 + len(gated):
        raise EvidenceError(
            "predecessor contains unsupported recovery or non-gated transitions"
        )
    accepted: list[dict[str, Any]] = []
    cycles_by_kind: dict[str, list[int]] = {"promotion": [], "fallback": []}
    phase_by_kind = {"promotion": "lark_promotion", "fallback": "lark_fallback"}
    for item in gated:
        kind = item.get("gate_kind")
        cycle = item.get("cycle")
        if (
            kind not in cycles_by_kind
            or not isinstance(cycle, int)
            or isinstance(cycle, bool)
        ):
            raise EvidenceError("predecessor contains an unsupported gated transition")
        if (
            item.get("phase") != phase_by_kind[kind]
            or item.get("outcome") != "completed"
            or item.get("connection_layout") != core.CONNECTION_LAYOUT_DIRECT
            or item.get("safety_clean") is not True
            or item.get("restart_clean") is not True
        ):
            raise EvidenceError(
                f"predecessor {kind} cycle {cycle} is not a clean direct completion"
            )
        timing_error = core._timing_evidence_error(item, kind)
        if timing_error:
            raise EvidenceError(
                f"predecessor {kind} cycle {cycle} timing evidence is invalid: {timing_error}"
            )
        for topology in core._transition_usb_topologies(item):
            for candidate_id in core.USB_MICROPHONE_FINGERPRINTS:
                devices, device_error = core._validated_usb_devices(
                    topology, candidate_id
                )
                if device_error:
                    raise EvidenceError(
                        f"predecessor {kind} cycle {cycle} USB evidence is invalid: {device_error}"
                    )
                for device in devices or []:
                    _ancestors, ancestry_error = core._validated_hub_ancestors(device)
                    if ancestry_error:
                        raise EvidenceError(
                            f"predecessor {kind} cycle {cycle} USB ancestry is invalid: {ancestry_error}"
                        )
                    if core._external_hub_ancestor_generations(device):
                        raise EvidenceError(
                            f"predecessor {kind} cycle {cycle} used an external USB hub"
                        )
        cycles_by_kind[kind].append(cycle)
        accepted.append(
            {"cycle": cycle, "gate_kind": kind, "sha256": _canonical_sha256(item)}
        )
    if cycles_by_kind["promotion"] != cycles_by_kind["fallback"]:
        raise EvidenceError("predecessor promotion and fallback cycles do not pair")
    completed_cycles = cycles_by_kind["promotion"]
    if not completed_cycles or completed_cycles != list(
        range(1, len(completed_cycles) + 1)
    ):
        raise EvidenceError(
            "predecessor completed cycles are not contiguous from cycle 1"
        )
    if len(completed_cycles) >= core.CONNECTION_LAYOUT_BOUNDARY_CYCLE:
        raise EvidenceError(
            "minimal resume requires a predecessor ending before cycle 10"
        )

    for kind in cycles_by_kind:
        recomputed = core.summarize_timing_gate(
            transitions,
            kind=kind,
            expected_cycles=core.QUALIFICATION_CYCLES,
        )
        if recomputed != (summary.get("timing_gates") or {}).get(kind):
            raise EvidenceError(
                f"predecessor {kind} timing summary is not reproducible"
            )

    binding = {
        "schema": RESUME_SCHEMA,
        "schema_version": 1,
        "status": "PASS",
        "predecessor_artifact_dir": str(root),
        "predecessor_manifest": {
            "path": MANIFEST_NAME,
            "bytes": (root / MANIFEST_NAME).stat().st_size,
            "sha256": sha256_file(root / MANIFEST_NAME),
            "schema_version": manifest.get("schema_version"),
            "files": list(manifest["files"]),
        },
        "predecessor_summary": {
            **records[SUMMARY_NAME],
            "campaign": summary["campaign"],
            "connection_plan": summary["connection_plan"],
            "commit": commit,
            "remote_boot_id": summary.get("remote_boot_id"),
            "aborted": summary["aborted"],
        },
        "predecessor_provenance": {
            **records[PROVENANCE_NAME],
            "status": provenance["status"],
            "local_commit": commit,
            "remote_deployed_commit": (
                ((provenance.get("remote") or {}).get("deployed_release") or {})
                .get("document", {})
                .get("commit")
            ),
        },
        "accepted_predecessor_evidence": {
            "completed_cycle_count": len(completed_cycles),
            "completed_cycles": completed_cycles,
            "accepted_transition_count": len(accepted),
            "transitions": sorted(
                accepted, key=lambda item: (item["cycle"], item["gate_kind"])
            ),
            "next_cycle": len(completed_cycles) + 1,
        },
        "unmonitored_inter_segment_gap": {
            "observation_kind": "unmonitored_inter_segment_gap",
            "continuously_sampled": False,
            "continuity_verdict": "INCONCLUSIVE",
            "predecessor_recording_ended_timestamp": _predecessor_recording_end(
                checkpoint
            ),
            "resumed_monitoring_started_timestamp": None,
            "approximate_duration_s": None,
            "predecessor_boot_id": summary.get("remote_boot_id"),
            "resumed_boot_id": None,
            "same_boot": None,
        },
    }
    return binding, [dict(item) for item in transitions]


def _git_blob_binding(
    repo: Path,
    *,
    commit: str,
    repository_path: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    """Hash one blob exactly as stored at ``commit`` without checking it out."""
    binding: dict[str, Any] = {
        "commit": commit,
        "repository_path": repository_path,
        "git_object_type": None,
        "git_blob_sha256": None,
        "status": "FAIL",
        "errors": [],
    }
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        binding["errors"].append(
            f"declared commit {commit!r} is not a full 40-character hexadecimal id"
        )
        return binding

    object_name = f"{commit}:{repository_path}"
    try:
        object_type = runner(
            ["git", "cat-file", "-t", object_name],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        binding["errors"].append(
            f"cannot resolve tracked path {repository_path!r} at {commit}: {exc}"
        )
        return binding
    if object_type.returncode != 0:
        detail = str(object_type.stderr or "").strip() or "object is unavailable"
        binding["errors"].append(
            f"tracked path {repository_path!r} is missing at commit {commit}: {detail}"
        )
        return binding
    binding["git_object_type"] = str(object_type.stdout or "").strip()
    if binding["git_object_type"] != "blob":
        binding["errors"].append(
            f"tracked path {repository_path!r} at {commit} resolves to "
            f"{binding['git_object_type']!r}, not a blob"
        )
        return binding

    try:
        blob = runner(
            ["git", "cat-file", "blob", object_name],
            cwd=repo,
            capture_output=True,
            text=False,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        binding["errors"].append(
            f"cannot read tracked blob {repository_path!r} at {commit}: {exc}"
        )
        return binding
    if blob.returncode != 0:
        stderr = blob.stderr
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", errors="replace").strip()
        else:
            detail = str(stderr or "").strip()
        binding["errors"].append(
            f"cannot read tracked blob {repository_path!r} at {commit}: "
            f"{detail or 'git cat-file failed'}"
        )
        return binding
    payload = blob.stdout
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(payload, bytes):
        binding["errors"].append(
            f"git returned malformed blob content for {repository_path!r} at {commit}"
        )
        return binding
    binding["git_blob_sha256"] = hashlib.sha256(payload).hexdigest()
    binding["status"] = "PASS"
    return binding


def _local_source_provenance(
    repo: Path,
    *,
    name: str,
    path: Path,
    expected_repository_path: str,
    commit: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "repository_path": None,
        "sha256": None,
        "binding": {
            "kind": "local_head_git_blob",
            "commit": commit,
            "repository_path": expected_repository_path,
            "status": "FAIL",
            "matches_working_file": False,
            "errors": [],
        },
    }
    binding = record["binding"]
    try:
        repository_path = path.resolve().relative_to(repo.resolve()).as_posix()
    except (OSError, ValueError) as exc:
        binding["errors"].append(
            f"local decision source {name!r} is outside repository {repo}: {exc}"
        )
        repository_path = None
    record["repository_path"] = repository_path
    if repository_path != expected_repository_path:
        binding["errors"].append(
            f"local decision source {name!r} must be {expected_repository_path!r}; "
            f"observed {repository_path!r}"
        )

    if not record["exists"]:
        binding["errors"].append(f"local decision source is missing: {path}")
    else:
        try:
            record.update(bytes=path.stat().st_size, sha256=sha256_file(path))
        except OSError as exc:
            binding["errors"].append(f"cannot hash local decision source {path}: {exc}")

    blob_binding = _git_blob_binding(
        repo,
        commit=commit,
        repository_path=expected_repository_path,
        runner=runner,
    )
    binding.update(
        git_object_type=blob_binding.get("git_object_type"),
        git_blob_sha256=blob_binding.get("git_blob_sha256"),
    )
    binding["errors"].extend(blob_binding.get("errors", []))
    if (
        not binding["errors"]
        and blob_binding.get("status") == "PASS"
        and record.get("sha256") == blob_binding.get("git_blob_sha256")
    ):
        binding["matches_working_file"] = True
        binding["status"] = "PASS"
    elif (
        record.get("sha256")
        and blob_binding.get("git_blob_sha256")
        and record.get("sha256") != blob_binding.get("git_blob_sha256")
    ):
        binding["errors"].append(
            f"local decision source {expected_repository_path!r} does not match "
            f"the blob at declared HEAD {commit}"
        )
    return record


def _local_repository_provenance(
    repo: Path,
    *,
    commit: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "path": str(repo),
        "head": commit,
        "observed_head": None,
        "head_matches": False,
        "dirty": None,
        "porcelain": [],
        "status": "FAIL",
        "errors": [],
    }
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        document["errors"].append(
            f"declared local HEAD {commit!r} is not a full 40-character hexadecimal id"
        )
    try:
        head_result = runner(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        commit_type = runner(
            ["git", "cat-file", "-t", commit],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status_result = runner(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        document["errors"].append(f"cannot inspect local Git repository: {exc}")
        document["error"] = document["errors"][-1]
        return document

    if head_result.returncode != 0:
        document["errors"].append(
            "cannot resolve local HEAD: "
            f"{str(head_result.stderr or '').strip() or 'git rev-parse failed'}"
        )
    else:
        observed_head = str(head_result.stdout or "").strip()
        document["observed_head"] = observed_head
        document["head_matches"] = bool(
            re.fullmatch(r"[0-9a-fA-F]{40}", observed_head)
            and observed_head.lower() == commit.lower()
        )
        if not document["head_matches"]:
            document["errors"].append(
                f"declared local HEAD {commit} does not match observed HEAD {observed_head!r}"
            )
    if commit_type.returncode != 0:
        document["errors"].append(
            f"declared local HEAD {commit} is unavailable (the checkout may be shallow): "
            f"{str(commit_type.stderr or '').strip() or 'git cat-file failed'}"
        )
    elif str(commit_type.stdout or "").strip() != "commit":
        document["errors"].append(
            f"declared local HEAD {commit} is not a commit object: "
            f"{str(commit_type.stdout or '').strip()!r}"
        )

    lines = [line for line in status_result.stdout.splitlines() if line]
    document["porcelain"] = lines
    document["dirty"] = bool(lines) if status_result.returncode == 0 else None
    if status_result.returncode != 0:
        document["errors"].append(
            f"git status exited {status_result.returncode}: "
            f"{str(status_result.stderr or '').strip()}"
        )
    if not document["errors"]:
        document["status"] = "PASS"
    elif document["errors"]:
        document["error"] = document["errors"][0]
    return document


def _remote_provenance_program(remote_repo: str) -> str:
    return f"""\
import hashlib
import json
import pathlib
import re
import shutil
import subprocess

repo = pathlib.Path({remote_repo!r})

def git(*arguments):
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"git {{' '.join(arguments)}} exited {{result.returncode}}: "
            f"{{result.stderr.strip()}}"
        )
    return result.stdout.strip()

def file_record(path):
    record = {{"path": str(path), "exists": path.is_file()}}
    if not record["exists"]:
        return record
    try:
        payload = path.read_bytes()
    except OSError as error:
        record["error"] = str(error)
        return record
    record["bytes"] = len(payload)
    record["sha256"] = hashlib.sha256(payload).hexdigest()
    return record

errors = []
deployed_path = pathlib.Path("/etc/larkbridge/DEPLOYED.json")
deployed = file_record(deployed_path)
deployed_commit = None
if deployed.get("exists") and deployed.get("sha256"):
    try:
        value = json.loads(deployed_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("top level is not an object")
        deployed["document"] = value
        deployed_commit = value.get("commit")
        archive_sha256 = value.get("archive_sha256")
        if not isinstance(deployed_commit, str) or not re.fullmatch(
            r"[0-9a-fA-F]{{40}}", deployed_commit
        ):
            raise ValueError("commit is missing or is not a 40-character hexadecimal id")
        if not isinstance(archive_sha256, str) or not re.fullmatch(
            r"[0-9a-fA-F]{{64}}", archive_sha256
        ):
            raise ValueError("archive_sha256 is missing or malformed")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        deployed["error"] = str(error)
        errors.append(f"deployed release provenance unavailable: {{error}}")
else:
    errors.append(f"deployed release provenance unavailable: {{deployed}}")

git_available = shutil.which("git") is not None
git_checkout = (repo / ".git").exists()
head = deployed_commit
porcelain = []
dirty = None
identity_source = "deployed_release"
if git_available and git_checkout:
    identity_source = "git"
    try:
        head = git("rev-parse", "HEAD")
        porcelain = git(
            "status", "--porcelain=v1", "--untracked-files=all"
        ).splitlines()
        dirty = bool(porcelain)
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        errors.append(str(error))
if deployed_commit and head and deployed_commit.lower() != head.lower():
    errors.append(
        f"deployed commit {{deployed_commit}} does not match repository head {{head}}"
    )
if not repo.is_dir():
    errors.append(f"release tree is absent: {{repo}}")

files = {{
    "invariants": file_record(repo / "rig/pi/measure/invariants.py"),
    "bridge_config": file_record(repo / "config/bridge.toml"),
    "remote_snapshot": file_record(repo / "rig/bt500_aux/remote.py"),
    "remote_harness": file_record(repo / "rig/bt500_aux/harness.py"),
    "bridge_supervisor": file_record(repo / "pi/bridged/bridge_supervisor.py"),
    "microphone_resolver": file_record(repo / "pi/bridged/microphones.py"),
    "output_resolver": file_record(repo / "pi/bridged/outputs.py"),
    "controller_roles": file_record(repo / "pi/bridged/controller_roles.py"),
    "btadapters": file_record(repo / "pi/bridged/btadapters.py"),
}}
for name, record in files.items():
    if not record.get("exists") or not record.get("sha256"):
        errors.append(f"{{name}} provenance unavailable: {{record}}")

print(json.dumps({{
    "status": "PASS" if not errors else "FAIL",
    "repository": {{
        "path": str(repo),
        "head": head,
        "dirty": dirty,
        "porcelain": porcelain,
        "git_available": git_available,
        "git_checkout": git_checkout,
        "identity_source": identity_source,
    }},
    "installed_files": files,
    "deployed_release": deployed,
    "errors": errors,
}}, sort_keys=True))
"""


def _bind_remote_sources_to_deployed_commit(
    remote: Mapping[str, Any],
    *,
    repo: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    """Bind installed tracked hashes to blobs from the declared deployed commit."""
    bound = dict(remote)
    deployed = remote.get("deployed_release")
    deployed = deployed if isinstance(deployed, Mapping) else {}
    deployed_document = deployed.get("document")
    deployed_document = (
        deployed_document if isinstance(deployed_document, Mapping) else {}
    )
    deployed_commit = deployed_document.get("commit")
    commit = deployed_commit if isinstance(deployed_commit, str) else ""

    original_installed = remote.get("installed_files")
    original_installed = (
        original_installed if isinstance(original_installed, Mapping) else {}
    )
    installed: dict[str, Any] = {}
    binding_errors: list[str] = []

    commit_binding: dict[str, Any] = {
        "commit": commit,
        "git_object_type": None,
        "status": "FAIL",
        "errors": [],
    }
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        commit_binding["errors"].append(
            f"deployed commit {commit!r} is not a full 40-character hexadecimal id"
        )
    else:
        try:
            commit_type = runner(
                ["git", "cat-file", "-t", commit],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            commit_binding["errors"].append(
                f"cannot resolve deployed commit {commit} locally: {exc}"
            )
        else:
            if commit_type.returncode != 0:
                commit_binding["errors"].append(
                    f"deployed commit {commit} is unavailable locally "
                    "(the checkout may be shallow): "
                    f"{str(commit_type.stderr or '').strip() or 'git cat-file failed'}"
                )
            else:
                commit_binding["git_object_type"] = str(
                    commit_type.stdout or ""
                ).strip()
                if commit_binding["git_object_type"] != "commit":
                    commit_binding["errors"].append(
                        f"deployed object {commit} resolves to "
                        f"{commit_binding['git_object_type']!r}, not a commit"
                    )
                else:
                    commit_binding["status"] = "PASS"
    bound["deployed_commit_binding"] = commit_binding
    binding_errors.extend(
        f"remote deployed commit: {error}" for error in commit_binding["errors"]
    )

    for name, repository_path in REMOTE_TRACKED_SOURCE_PATHS.items():
        original_record = original_installed.get(name)
        record = dict(original_record) if isinstance(original_record, Mapping) else {}
        blob_binding = _git_blob_binding(
            repo,
            commit=commit,
            repository_path=repository_path,
            runner=runner,
        )
        remote_sha256 = record.get("sha256")
        binding_errors_for_source = list(blob_binding.get("errors", []))
        matches = bool(
            blob_binding.get("status") == "PASS"
            and isinstance(remote_sha256, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", remote_sha256)
            and remote_sha256.lower()
            == str(blob_binding.get("git_blob_sha256") or "").lower()
        )
        if (
            blob_binding.get("status") == "PASS"
            and isinstance(remote_sha256, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", remote_sha256)
            and not matches
        ):
            binding_errors_for_source.append(
                f"installed {name} hash does not match {repository_path!r} "
                f"at deployed commit {commit}"
            )
        binding = {
            "kind": "deployed_commit_git_blob",
            "commit": commit,
            "repository_path": repository_path,
            "git_object_type": blob_binding.get("git_object_type"),
            "git_blob_sha256": blob_binding.get("git_blob_sha256"),
            "matches_installed_file": matches,
            "status": "PASS" if matches and not binding_errors_for_source else "FAIL",
            "errors": binding_errors_for_source,
        }
        record["binding"] = binding
        installed[name] = record
        binding_errors.extend(f"remote {name}: {error}" for error in binding["errors"])

    config_original = original_installed.get(PRESERVED_CONFIG_SOURCE)
    config_record = (
        dict(config_original) if isinstance(config_original, Mapping) else {}
    )
    config_hash = config_record.get("sha256")
    config_errors: list[str] = []
    if (
        not config_record.get("exists")
        or not isinstance(config_hash, str)
        or not re.fullmatch(r"[0-9a-fA-F]{64}", config_hash)
    ):
        config_errors.append(
            "preserved bridge.toml is absent or its SHA-256 identity is unavailable"
        )
    config_record["binding"] = {
        "kind": PRESERVED_CONFIG_BINDING_KIND,
        "repository_path": PRESERVED_CONFIG_REPOSITORY_PATH,
        "status": "PASS" if not config_errors else "FAIL",
        "errors": config_errors,
        "exception": (
            "bridge.toml is a preserved deployment-local, untracked configuration; "
            "it is identified by its collected SHA-256 and is intentionally not bound "
            "to the deployed Git commit"
        ),
    }
    installed[PRESERVED_CONFIG_SOURCE] = config_record
    binding_errors.extend(
        f"remote {PRESERVED_CONFIG_SOURCE}: {error}" for error in config_errors
    )

    for name, original_record in original_installed.items():
        if name not in installed:
            installed[name] = original_record
    bound["installed_files"] = installed
    bound["source_binding_errors"] = binding_errors
    return bound


def _remote_provenance_errors(remote: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if remote.get("status") != "PASS":
        errors.append(f"remote status is {remote.get('status')!r}")
    reported_errors = remote.get("errors")
    if not isinstance(reported_errors, list) or reported_errors:
        errors.append("remote errors are missing, malformed, or nonempty")

    repository = remote.get("repository")
    repository = repository if isinstance(repository, Mapping) else {}
    head = repository.get("head")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        errors.append("remote repository/deployment head is missing or malformed")

    deployed = remote.get("deployed_release")
    deployed = deployed if isinstance(deployed, Mapping) else {}
    document = deployed.get("document")
    document = document if isinstance(document, Mapping) else {}
    deployed_commit = document.get("commit")
    archive_sha256 = document.get("archive_sha256")
    if not deployed.get("exists") or not re.fullmatch(
        r"[0-9a-fA-F]{64}", str(deployed.get("sha256") or "")
    ):
        errors.append("DEPLOYED.json is absent or its content hash is unavailable")
    if not isinstance(deployed_commit, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", deployed_commit
    ):
        errors.append("DEPLOYED.json commit is missing or malformed")
    elif isinstance(head, str) and deployed_commit.lower() != head.lower():
        errors.append("DEPLOYED.json commit does not match the release-tree identity")
    if not isinstance(archive_sha256, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", archive_sha256
    ):
        errors.append("DEPLOYED.json archive_sha256 is missing or malformed")

    installed = remote.get("installed_files")
    installed = installed if isinstance(installed, Mapping) else {}
    for name in (*REMOTE_TRACKED_SOURCE_PATHS, PRESERVED_CONFIG_SOURCE):
        record = installed.get(name)
        record = record if isinstance(record, Mapping) else {}
        if not record.get("exists") or not re.fullmatch(
            r"[0-9a-fA-F]{64}", str(record.get("sha256") or "")
        ):
            errors.append(f"remote {name} source hash is unavailable")
        binding = record.get("binding")
        binding = binding if isinstance(binding, Mapping) else {}
        expected_kind = (
            PRESERVED_CONFIG_BINDING_KIND
            if name == PRESERVED_CONFIG_SOURCE
            else "deployed_commit_git_blob"
        )
        if binding.get("kind") != expected_kind:
            errors.append(f"remote {name} source binding kind is missing or malformed")
        if binding.get("status") != "PASS":
            detail = binding.get("errors")
            errors.append(f"remote {name} source is not commit-bound: {detail}")
        if name == PRESERVED_CONFIG_SOURCE:
            if not binding.get("exception"):
                errors.append(
                    "remote bridge_config hash-only preserved-config exception is unlabeled"
                )
            if binding.get("repository_path") != PRESERVED_CONFIG_REPOSITORY_PATH:
                errors.append("remote bridge_config repository path is malformed")
            continue
        if binding.get("commit") != deployed_commit:
            errors.append(f"remote {name} binding commit does not match DEPLOYED.json")
        if binding.get("repository_path") != REMOTE_TRACKED_SOURCE_PATHS[name]:
            errors.append(f"remote {name} binding repository path is malformed")
        if binding.get("git_object_type") != "blob":
            errors.append(f"remote {name} deployed object is not a blob")
        expected_hash = binding.get("git_blob_sha256")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(expected_hash or "")):
            errors.append(f"remote {name} deployed blob hash is unavailable")
        elif str(record.get("sha256") or "").lower() != str(expected_hash).lower():
            errors.append(f"remote {name} installed hash does not match deployed blob")
        if binding.get("matches_installed_file") is not True:
            errors.append(f"remote {name} installed-file binding did not match")
    source_binding_errors = remote.get("source_binding_errors")
    if not isinstance(source_binding_errors, list):
        errors.append("remote source binding errors are missing or malformed")
    elif source_binding_errors:
        errors.extend(str(item) for item in source_binding_errors)
    commit_binding = remote.get("deployed_commit_binding")
    commit_binding = commit_binding if isinstance(commit_binding, Mapping) else {}
    if commit_binding.get("status") != "PASS":
        errors.append(
            "deployed commit is not available as a commit object in the local "
            f"repository: {commit_binding.get('errors')}"
        )
    if commit_binding.get("commit") != deployed_commit:
        errors.append("deployed commit binding does not match DEPLOYED.json")
    if commit_binding.get("git_object_type") != "commit":
        errors.append("deployed release identity is not a Git commit object")
    return errors


def collect_provenance(
    *,
    host: str,
    remote_repo: str,
    sampler_source: Path,
    commit: str,
    repo: Path = REPO,
    host_source: Path | None = None,
    harness_library_source: Path | None = None,
    local_invariants_source: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    """Capture immutable local/remote identity without mutating the Pi."""
    harness_source = host_source or Path(__file__).resolve()
    harness_library = harness_library_source or harness_source.with_name("harness.py")
    local_invariants = local_invariants_source or sampler_source.with_name(
        "invariants.py"
    )
    local_repository = _local_repository_provenance(
        repo,
        commit=commit,
        runner=runner,
    )
    local_source_paths = {
        "host_harness": harness_source,
        "harness_library": harness_library,
        "streamed_sampler": sampler_source,
        "local_invariants": local_invariants,
    }
    local_sources = {
        name: _local_source_provenance(
            repo,
            name=name,
            path=path,
            expected_repository_path=LOCAL_DECISION_SOURCE_PATHS[name],
            commit=commit,
            runner=runner,
        )
        for name, path in local_source_paths.items()
    }

    encoded = base64.b64encode(
        _remote_provenance_program(remote_repo).encode("utf-8")
    ).decode("ascii")
    remote_command = "python3 -c " + shlex.quote(
        f"import base64;exec(base64.b64decode({encoded!r}).decode('utf-8'))"
    )
    remote: dict[str, Any]
    try:
        result = runner(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                host,
                remote_command,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        remote = {"status": "FAIL", "error": str(exc)}
    else:
        if result.returncode != 0:
            remote = {
                "status": "FAIL",
                "error": f"SSH exited {result.returncode}: {result.stderr.strip()}",
            }
        else:
            try:
                parsed = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                remote = {
                    "status": "FAIL",
                    "error": f"remote provenance emitted malformed JSON: {exc}",
                }
            else:
                remote = (
                    parsed
                    if isinstance(parsed, dict)
                    else {
                        "status": "FAIL",
                        "error": "remote provenance was not a JSON object",
                    }
                )

    remote = _bind_remote_sources_to_deployed_commit(
        remote,
        repo=repo,
        runner=runner,
    )
    validation_errors = _remote_provenance_errors(remote)
    if validation_errors:
        remote = dict(remote)
        remote["status"] = "FAIL"
        remote["host_validation_errors"] = validation_errors

    local_complete = bool(
        local_repository.get("status") == "PASS"
        and all(
            isinstance(item.get("binding"), Mapping)
            and item["binding"].get("status") == "PASS"
            for item in local_sources.values()
        )
    )
    return {
        "schema_version": 1,
        "captured_timestamp": time.time(),
        "status": "PASS"
        if local_complete and remote.get("status") == "PASS"
        else "FAIL",
        "local_repository": local_repository,
        "local_sources": local_sources,
        "remote": remote,
        "read_only": True,
    }


def artifact_map(root: Path) -> dict[str, str]:
    return {
        label: name for label, name in ARTIFACT_NAMES.items() if (root / name).is_file()
    }


class Checkpoint:
    """Small E18-specific journal; intentionally independent of CampaignStore."""

    def __init__(self, path: Path, document: dict[str, Any]):
        self.path = path
        self.document = document
        self._lock = threading.Lock()
        self.write()

    def write(self) -> None:
        with self._lock:
            self.document["updated_timestamp"] = time.time()
            atomic_json(self.path, self.document)

    def update(self, **values: Any) -> None:
        with self._lock:
            self.document.update(values)
            self.document["updated_timestamp"] = time.time()
            atomic_json(self.path, self.document)

    def append_transition(self, result: Mapping[str, Any]) -> None:
        with self._lock:
            transitions = self.document.setdefault("transitions", [])
            if isinstance(transitions, list):
                transitions.append(dict(result))
            self.document["updated_timestamp"] = time.time()
            atomic_json(self.path, self.document)


def _snapshot_status(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    status = snapshot.get("status")
    if not isinstance(status, dict):
        raise EvidenceError("snapshot.status is missing or malformed")
    return status


def validate_hotplug_snapshot(
    snapshot: Mapping[str, Any], *, expected_microphone: str
) -> None:
    """Apply active-call validation plus hotplug-specific ambiguity/link gates."""
    validate_snapshot(
        snapshot,
        require_active=True,
        expected_microphone=expected_microphone,
    )
    status = _snapshot_status(snapshot)
    snapshot_timestamp = snapshot.get("timestamp")
    if not isinstance(snapshot_timestamp, (int, float)) or isinstance(
        snapshot_timestamp, bool
    ):
        raise EvidenceError("snapshot timestamp is absent or nonnumeric")
    if not math.isfinite(float(snapshot_timestamp)):
        raise EvidenceError("snapshot timestamp is non-finite")
    timestamp = status.get("timestamp")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        raise EvidenceError("bridge status timestamp is absent or nonnumeric")
    if not math.isfinite(float(timestamp)):
        raise EvidenceError("bridge status timestamp is non-finite")
    # Both values originate on the Pi. Comparing a Pi timestamp with the host clock
    # fails whenever the directly connected Pi has no NTP source.
    age = float(snapshot_timestamp) - float(timestamp)
    if age < 0:
        raise EvidenceError(f"bridge status timestamp is {-age:.2f}s in the future")
    if age > 6.0:
        raise EvidenceError(f"bridge status is stale by {age:.2f}s")
    candidates = core.candidate_inventory(status)
    blockers = sorted(
        candidate_id
        for candidate_id, candidate in candidates.items()
        if candidate.get("state") in {"ambiguous", "conflict"}
    )
    if blockers:
        raise HardFailure(f"microphone candidate identity is unsafe: {blockers}")
    raw_links = snapshot.get("pipewire_links")
    if not isinstance(raw_links, str):
        raise EvidenceError("snapshot PipeWire links are missing")
    evaluated = core.evaluate_link_invariants(
        status,
        None,
        core.parse_pw_links(raw_links),
        now=float(snapshot_timestamp),
    )
    if evaluated["violations"]:
        raise HardFailure(
            f"snapshot link invariants failed: {evaluated['violations']!r}"
        )
    # A full snapshot must prove the exact active routes, not just absence of hazards.
    sample = {
        "state": status.get("state"),
        "status_error": None,
        "link_error": None,
        "microphone": core.selected_microphone(status),
        "candidates": candidates,
        "graph_generation": status.get("generation"),
        "aec": status.get("aec") or {},
        "invariants": evaluated,
    }
    failures = core.expectation_failures(
        sample,
        core.Expectation(state="ACTIVE", selected_id=expected_microphone),
    )
    if failures:
        raise HardFailure(f"snapshot active microphone graph is unsafe: {failures!r}")


def restart_counts(snapshot: Mapping[str, Any]) -> dict[str, int]:
    return {
        unit: service_restarts(snapshot, "user", unit) for unit in core.SERVICE_UNITS
    }


def restart_delta(
    before: Mapping[str, int], after: Mapping[str, int]
) -> dict[str, int]:
    return {unit: int(after[unit]) - int(before[unit]) for unit in core.SERVICE_UNITS}


class SshSampleStream:
    """Receive the streamed Pi sampler and expose the interface used by core transitions."""

    def __init__(
        self,
        *,
        host: str,
        remote_repo: str,
        source_path: Path,
        timeline_path: Path,
        interval: float,
        duration: float,
        status_path: str | None,
        checkpoint: Checkpoint,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        run_factory: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.host = host
        self.remote_repo = remote_repo
        self.source_path = source_path
        self.timeline_path = timeline_path
        self.interval = interval
        self.duration = duration
        self.status_path = status_path
        self.checkpoint = checkpoint
        self.process_factory = process_factory
        self.run_factory = run_factory
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._timeline: TextIO | None = None
        self._recent: deque[dict[str, Any]] = deque(maxlen=1200)
        self._phase = "preflight"
        self._cycle = 0
        self._action_id: str | None = None
        self._connection_layout: str | None = None
        self._started_monotonic = 0.0
        self._local_seq = 0
        self._last_remote_seq = 0
        self._last_host_sample_at: float | None = None
        self._service_counts = {unit: None for unit in core.SERVICE_UNITS}
        self.initial_service_counts = dict(self._service_counts)
        self.remote_boot_id: str | None = None
        self.remote_stderr: list[str] = []
        self.remote_returncode: int | None = None
        self.sequence_gaps: list[dict[str, int]] = []
        self.malformed_lines: list[str] = []
        self.candidate_node_union: set[str] = set()
        self.violation_counts: Counter[str] = Counter()
        self.first_violations: list[dict[str, Any]] = []
        self.total_samples = 0
        self.max_remote_gap = 0.0
        self.max_host_receive_gap = 0.0
        self.fatal_errors: list[str] = []
        self.stream_started = False
        self.stream_stopped = False
        self.unexpected_eof = False
        self._stopping = False

    def _record_fatal(self, detail: str) -> None:
        with self._condition:
            if detail not in self.fatal_errors:
                self.fatal_errors.append(detail)
            self._condition.notify_all()

    def _query_service_counts(
        self,
    ) -> tuple[dict[str, int | None], str | None]:
        remote_command = (
            "systemctl --user show "
            + " ".join(shlex.quote(unit) for unit in core.SERVICE_UNITS)
            + " --property=Id --property=NRestarts --no-pager"
        )
        try:
            result = self.run_factory(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    self.host,
                    "export XDG_RUNTIME_DIR=/run/user/$(id -u); " + remote_command,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {unit: None for unit in core.SERVICE_UNITS}, str(exc)
        if result.returncode != 0:
            return (
                {unit: None for unit in core.SERVICE_UNITS},
                (
                    f"SSH restart-count query exited {result.returncode}: "
                    f"{result.stderr.strip()}"
                ),
            )
        counts = core.parse_service_restart_counts(result.stdout)
        if any(value is None for value in counts.values()):
            return counts, "one or more remote NRestarts values are missing"
        return counts, None

    def _synchronous_service_counts(self) -> dict[str, int | None]:
        counts, error = self._query_service_counts()
        with self._condition:
            self._service_counts = dict(counts)
        if error:
            self._record_fatal(f"restart-count evidence failed: {error}")
        return counts

    def _emit(self, document: Mapping[str, Any]) -> None:
        payload = json.dumps(document, separators=(",", ":"), sort_keys=True)
        with self._write_lock:
            if self._timeline is not None:
                self._timeline.write(payload + "\n")
                self._timeline.flush()

    def start(self) -> None:
        self.timeline_path.parent.mkdir(parents=True, exist_ok=True)
        self._timeline = self.timeline_path.open("w", encoding="utf-8", buffering=1)
        self._started_monotonic = time.monotonic()
        remote_dir = f"{self.remote_repo.rstrip('/')}/rig/pi/measure"
        status_option = (
            ""
            if self.status_path is None
            else f" --status-path {shlex.quote(self.status_path)}"
        )
        remote_command = (
            "export XDG_RUNTIME_DIR=/run/user/$(id -u); "
            f"cd {shlex.quote(remote_dir)} && "
            "python3 -u - --remote-sampler "
            f"--interval {self.interval:.6f} --duration {self.duration:.3f} "
            f"{status_option}"
        )
        try:
            self._process = self.process_factory(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    self.host,
                    remote_command,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise EvidenceError(f"could not start SSH sampler: {exc}") from exc
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(self.source_path.read_text(encoding="utf-8"))
            self._process.stdin.close()
        except OSError as exc:
            self._process.terminate()
            raise EvidenceError(f"could not stream sampler source: {exc}") from exc
        self._reader = threading.Thread(
            target=self._read_stdout, name="hotplug-ssh-samples", daemon=True
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="hotplug-ssh-stderr", daemon=True
        )
        self._reader.start()
        self._stderr_reader.start()
        deadline = time.monotonic() + 15.0
        with self._condition:
            while not self.stream_started and not self.fatal_errors:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
        if not self.stream_started:
            detail = "; ".join(self.fatal_errors or self.remote_stderr[-3:])
            self.stop()
            raise EvidenceError(f"remote sampler did not start: {detail or 'timeout'}")

    def stop(self) -> None:
        process = self._process
        observed_returncode = process.poll() if process is not None else None
        if process is not None and observed_returncode is None:
            try:
                # Give an already-ending SSH process a small window to expose its
                # real exit status before marking the later host termination expected.
                observed_returncode = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                observed_returncode = process.poll()
        if observed_returncode not in (None, 0):
            self.remote_returncode = observed_returncode
            self._record_fatal(
                f"remote sampler exited {observed_returncode}: "
                f"{' | '.join(self.remote_stderr[-3:])}"
            )
        host_termination = False
        host_termination_returncode: int | None = None
        forced_kill = False
        if process is not None and observed_returncode is None:
            # Recheck immediately before termination. A sampler that failed while the
            # grace wait expired is an evidence failure, not an expected host stop.
            observed_returncode = process.poll()
            if observed_returncode is None:
                host_termination = True
                self._stopping = True
                process.terminate()
                try:
                    host_termination_returncode = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    forced_kill = True
                    process.kill()
                    host_termination_returncode = process.wait(timeout=3)
            elif observed_returncode != 0:
                self.remote_returncode = observed_returncode
                self._record_fatal(
                    f"remote sampler exited {observed_returncode}: "
                    f"{' | '.join(self.remote_stderr[-3:])}"
                )
        for thread in (self._reader, self._stderr_reader):
            if thread is not None:
                thread.join(timeout=3)
        if host_termination:
            if forced_kill:
                self.remote_returncode = host_termination_returncode
                self._record_fatal(
                    "remote sampler required a forced kill during cleanup"
                )
            elif host_termination_returncode not in (0, 1, -15):
                self.remote_returncode = host_termination_returncode
                self._record_fatal(
                    f"remote sampler exited {host_termination_returncode}: "
                    f"{' | '.join(self.remote_stderr[-3:])}"
                )
        else:
            self._stopping = True
            if process is not None:
                final_returncode = process.poll()
                self.remote_returncode = final_returncode
                if final_returncode not in (None, 0) and not any(
                    f"remote sampler exited {final_returncode}" in item
                    for item in self.fatal_errors
                ):
                    self._record_fatal(
                        f"remote sampler exited {final_returncode}: "
                        f"{' | '.join(self.remote_stderr[-3:])}"
                    )
                if not self.stream_stopped:
                    self.unexpected_eof = True
                    self._record_fatal("remote sampler ended before stream_stop")
        self._emit(
            {
                "type": "host_stream_stop",
                "timestamp": time.time(),
                "samples": self.total_samples,
                "last_remote_seq": self._last_remote_seq,
                "sequence_gaps": self.sequence_gaps,
            }
        )
        with self._write_lock:
            if self._timeline is not None:
                self._timeline.close()
                self._timeline = None

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            value = line.rstrip()
            if value:
                self.remote_stderr.append(value)
                self._record_fatal(f"remote sampler stderr: {value}")

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                document = json.loads(line)
            except json.JSONDecodeError as exc:
                self.malformed_lines.append(f"{exc}: {line[:200]}")
                with self._condition:
                    self._condition.notify_all()
                continue
            if not isinstance(document, dict):
                self.malformed_lines.append(f"non-object: {line[:200]}")
                continue
            self.ingest(document)
        if not self._stopping:
            if not self.stream_stopped:
                self.unexpected_eof = True
                self._record_fatal("remote sampler ended before stream_stop")
            try:
                returncode = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                returncode = process.poll()
            self.remote_returncode = returncode
            if returncode not in (None, 0):
                self._record_fatal(
                    f"remote sampler exited {returncode}: "
                    f"{' | '.join(self.remote_stderr[-3:])}"
                )

    def ingest(self, document: dict[str, Any]) -> None:
        kind = document.get("type")
        if kind == "stream_start":
            with self._condition:
                self.stream_started = True
                self.remote_boot_id = document.get("boot_id")
                counts = document.get("service_restart_counts")
                if isinstance(counts, dict):
                    self._service_counts = {
                        unit: counts.get(unit) for unit in core.SERVICE_UNITS
                    }
                    self.initial_service_counts = dict(self._service_counts)
                if document.get("service_error"):
                    self.fatal_errors.append(str(document["service_error"]))
                self._condition.notify_all()
            self._emit(document)
            return
        if kind == "stream_stop":
            with self._condition:
                self.stream_stopped = True
                self._condition.notify_all()
            self._emit(document)
            return
        if kind != "sample":
            self._emit(document)
            return

        remote_seq = document.get("seq")
        if not isinstance(remote_seq, int):
            self.malformed_lines.append("sample omitted integer seq")
            return
        with self._condition:
            received = time.monotonic()
            expected = self._last_remote_seq + 1
            if remote_seq != expected:
                self.sequence_gaps.append(
                    {"expected": expected, "observed": remote_seq}
                )
            self._last_remote_seq = remote_seq
            self._local_seq += 1
            local_seq = self._local_seq
            phase = self._phase
            cycle = self._cycle
            action_id = self._action_id
            connection_layout = self._connection_layout
            host_gap = (
                received - self._last_host_sample_at
                if self._last_host_sample_at is not None
                else None
            )
            self._last_host_sample_at = received
        sampling = document.get("sampling") or {}
        gap = sampling.get("start_gap_s") if isinstance(sampling, dict) else None
        if isinstance(gap, (int, float)):
            self.max_remote_gap = max(self.max_remote_gap, float(gap))
        if host_gap is not None:
            self.max_host_receive_gap = max(self.max_host_receive_gap, host_gap)

        candidate_nodes = (document.get("invariants") or {}).get(
            "candidate_nodes"
        ) or []
        self.candidate_node_union.update(
            node for node in candidate_nodes if isinstance(node, str) and node
        )
        selected = document.get("microphone")
        if isinstance(selected, dict) and isinstance(selected.get("node"), str):
            self.candidate_node_union.add(selected["node"])
        for candidate in (document.get("candidates") or {}).values():
            if not isinstance(candidate, dict):
                continue
            matched = candidate.get("matched_nodes") or []
            if isinstance(matched, list):
                self.candidate_node_union.update(
                    node for node in matched if isinstance(node, str) and node
                )

        counts = document.get("service_restart_counts")
        if isinstance(counts, dict):
            self._service_counts = {
                unit: counts.get(unit) for unit in core.SERVICE_UNITS
            }
        deltas = core.service_restart_delta(
            self.initial_service_counts,
            self._service_counts,
        )
        restart_failures = [
            f"{unit} restart delta is {value!r}"
            for unit, value in deltas.items()
            if value != 0
        ]
        invariants_block = document.get("invariants")
        if not isinstance(invariants_block, dict):
            invariants_block = {"passed": False, "violations": []}
            document["invariants"] = invariants_block
        violations = invariants_block.setdefault("violations", [])
        if not isinstance(violations, list):
            violations = []
            invariants_block["violations"] = violations
        for detail in restart_failures:
            violations.append({"id": "H8", "detail": detail})
        invariants_block["passed"] = not violations

        with self._condition:
            document["remote_elapsed_s"] = document.get("elapsed_s")
            document["elapsed_s"] = round(received - self._started_monotonic, 6)
            document["host_timestamp"] = time.time()
            document["host_received_monotonic"] = received
            document["phase"] = phase
            document["cycle"] = cycle
            document["action_id"] = action_id
            document["connection_layout"] = connection_layout
            document["candidate_node_union"] = sorted(self.candidate_node_union)
            document["service_restart_delta"] = deltas
            document["seq"] = local_seq
            document["remote_seq"] = remote_seq
            self._recent.append(document)
            self.total_samples += 1
            for item in violations:
                rule = str(item.get("id") or "unknown")
                self.violation_counts[rule] += 1
                if len(self.first_violations) < 20:
                    self.first_violations.append(
                        {
                            "seq": local_seq,
                            "remote_seq": remote_seq,
                            "phase": phase,
                            **item,
                        }
                    )
                detail = f"{rule}: {item.get('detail')}"
                if detail not in self.fatal_errors:
                    self.fatal_errors.append(detail)
            self._condition.notify_all()
        self._emit(document)

    def _action_usb_baseline_locked(
        self, phase: str, post_query_seq: int
    ) -> dict[str, Any]:
        deadline = time.monotonic() + core.USB_BASELINE_OBSERVATION_TIMEOUT_SECONDS
        required = core.USB_BASELINE_DISCARD_SAMPLES + core.USB_BASELINE_STABLE_SAMPLES
        while True:
            if self.fatal_errors:
                raise core.CampaignAbort(
                    f"USB action baseline is unsafe: {self.fatal_errors[-1]}"
                )
            fresh = [
                sample
                for sample in self._recent
                if int(sample.get("seq", -1)) > post_query_seq
            ]
            if len(fresh) >= required:
                observed = fresh[core.USB_BASELINE_DISCARD_SAMPLES :]
                for earlier, later in pairwise(observed):
                    core.stable_usb_baseline_from_samples([earlier, later])
                baseline = core.stable_usb_baseline_from_samples(
                    observed[-core.USB_BASELINE_STABLE_SAMPLES :]
                )
                core.validate_action_usb_baseline(phase, baseline)
                return baseline
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise core.CampaignAbort(
                    "USB action baseline timed out waiting for fresh "
                    "post-query remote samples"
                )
            self._condition.wait(min(remaining, max(self.interval * 2.0, 0.05)))

    def mark_action(
        self,
        phase: str,
        cycle: int,
        instruction: str,
        connection_layout: str | None = None,
    ) -> dict[str, Any]:
        counts, service_error = self._query_service_counts()
        if service_error:
            detail = f"restart-count evidence failed: {service_error}"
            self._record_fatal(detail)
            raise core.CampaignAbort(detail)
        with self._condition:
            self._service_counts = dict(counts)
            if not self.stream_started or self.stream_stopped or self.fatal_errors:
                detail = (
                    self.fatal_errors[-1]
                    if self.fatal_errors
                    else "remote sample stream is not running"
                )
                raise core.CampaignAbort(f"USB action baseline is unsafe: {detail}")
            post_query_seq = self._local_seq
            usb_baseline = self._action_usb_baseline_locked(phase, post_query_seq)
            action_monotonic = time.monotonic()
            action_timestamp = time.time()
            self._phase = phase
            self._cycle = cycle
            self._action_id = f"{phase}:{cycle}:{time.time_ns()}"
            self._connection_layout = connection_layout
            event = {
                "type": "operator_action",
                "timestamp": action_timestamp,
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
                    core.USB_ACTION_OBSERVATION_TIMEOUT_SECONDS
                    if phase in core.USB_GATE_BY_PHASE
                    else None
                ),
                "service_restart_counts": counts,
                "service_error": None,
            }
        self._emit({key: value for key, value in event.items() if key != "monotonic"})
        self.checkpoint.update(
            status="running",
            current_phase=phase,
            current_cycle=cycle,
            current_connection_layout=connection_layout,
            candidate_node_union=sorted(self.candidate_node_union),
        )
        return event

    def record_event(self, event_type: str, **values: Any) -> None:
        event = {
            "type": event_type,
            "timestamp": time.time(),
            "elapsed_s": round(time.monotonic() - self._started_monotonic, 6),
            **values,
        }
        self._emit(event)
        result = values.get("result")
        if event_type == "transition_result" and isinstance(result, dict):
            self.checkpoint.append_transition(result)

    def samples_after(self, seq: int) -> list[dict[str, Any]]:
        with self._condition:
            return [sample for sample in self._recent if int(sample["seq"]) > seq]

    def wait_for_new_sample(self, seq: int, timeout: float = 1.0) -> None:
        with self._condition:
            if self.fatal_errors:
                raise core.CampaignAbort(self.fatal_errors[-1])
            if not self._recent or int(self._recent[-1]["seq"]) <= seq:
                self._condition.wait(timeout)
            if self.fatal_errors:
                raise core.CampaignAbort(self.fatal_errors[-1])

    def latest(self) -> dict[str, Any] | None:
        with self._condition:
            return dict(self._recent[-1]) if self._recent else None

    def service_counts(self) -> dict[str, int | None]:
        return self._synchronous_service_counts()

    def cached_service_counts(self) -> dict[str, int | None]:
        with self._condition:
            return dict(self._service_counts)


def initial_fixture(
    campaign: str, connection_plan: str | None = None
) -> tuple[str, str]:
    if campaign == "inactive-fifine":
        return "lark-a1", "PREPARE BOTH"
    if connection_plan == core.CONNECTION_PLAN_DIRECT10_HUB10:
        return "fifine-k054", "PREPARE DIRECT FIFINE ONLY"
    return "fifine-k054", "PREPARE FIFINE ONLY"


def final_microphone(campaign: str) -> str:
    if campaign in {"matrix", "inactive-fifine"}:
        return "lark-a1"
    return "fifine-k054"


def typed_prepare(
    campaign: str, connection_plan: str | None = None, input_fn=input
) -> None:
    _expected_microphone, phrase = initial_fixture(campaign, connection_plan)
    description = (
        "Connect both microphones and confirm the Lark is selected"
        if phrase == "PREPARE BOTH"
        else (
            "Connect only the FIFINE directly to the Pi, leave the external hub out "
            "of its USB ancestry, and confirm the Lark is unplugged"
            if phrase == "PREPARE DIRECT FIFINE ONLY"
            else "Connect only the FIFINE and confirm the Lark is unplugged"
        )
    )
    print(
        f"{description}. Keep the live call active. Type {phrase!r}: ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    try:
        response = input_fn()
    except EOFError as exc:
        raise core.CampaignAbort("operator input ended") from exc
    if response.strip() != phrase:
        raise core.CampaignAbort(
            f"operator acknowledgement was {response.strip()!r}, expected {phrase!r}"
        )


def snapshot_deltas(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "user_service_restarts": restart_delta(
            restart_counts(before), restart_counts(after)
        ),
        "new_kernel_errors": new_error_lines(before, after, "kernel_errors"),
        "new_usb_errors": new_error_lines(before, after, "usb_errors"),
    }


def build_summary(
    *,
    campaign: str,
    cycles: int,
    transitions: list[dict[str, Any]],
    stream: SshSampleStream,
    preflight: Mapping[str, Any] | None,
    closing: Mapping[str, Any] | None,
    aborted: str | None,
    fast_limit_s: float,
    max_limit_s: float,
    artifact_dir: Path,
    commit: str,
    provenance: Mapping[str, Any] | None = None,
    connection_plan: str | None = None,
    resume: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    fatal_errors = list(getattr(stream, "fatal_errors", []))
    remote_stderr = list(getattr(stream, "remote_stderr", []))
    remote_returncode = getattr(stream, "remote_returncode", None)
    sequence_complete = bool(
        stream.stream_started
        and not stream.sequence_gaps
        and not stream.malformed_lines
        and not stream.unexpected_eof
        and not fatal_errors
        and not remote_stderr
        and remote_returncode in (None, 0)
    )
    measured_gap_limit = float(getattr(core, "MAX_MEASURED_GAP_SECONDS", 0.25))
    configured_interval_limit = float(
        getattr(core, "MAX_CONFIGURED_INTERVAL_SECONDS", 0.20)
    )
    interval_complete = bool(
        stream.total_samples
        and 0 < stream.interval <= configured_interval_limit
        and stream.max_remote_gap <= measured_gap_limit
    )
    resume_verified = bool(resume and resume.get("status") == "PASS")
    provenance_complete = bool(
        provenance
        and provenance.get("status") == "PASS"
        and (resume is None or resume_verified)
    )
    safety_clean = not stream.violation_counts
    deltas: dict[str, Any] = {}
    snapshot_restart_clean = False
    if preflight is not None and closing is not None:
        try:
            deltas = snapshot_deltas(preflight, closing)
            snapshot_restart_clean = all(
                value == 0 for value in deltas["user_service_restarts"].values()
            )
        except (EvidenceError, HardFailure, HardwareNotReady) as exc:
            deltas = {"error": str(exc)}
    required_cycles = (
        int(getattr(core, "QUALIFICATION_MATRIX_CYCLES", 1))
        if campaign == "matrix"
        else int(getattr(core, "QUALIFICATION_CYCLES", 20))
    )
    canonical_fast_limit = float(
        getattr(
            core,
            "QUALIFICATION_FAST_LIMIT_SECONDS",
            QUALIFICATION_FAST_LIMIT_SECONDS,
        )
    )
    canonical_max_limit = float(
        getattr(
            core,
            "QUALIFICATION_MAX_LIMIT_SECONDS",
            QUALIFICATION_MAX_LIMIT_SECONDS,
        )
    )
    configuration_eligible = bool(
        cycles == required_cycles
        and fast_limit_s <= canonical_fast_limit
        and max_limit_s <= canonical_max_limit
        and (
            connection_plan is None
            or (
                connection_plan == core.CONNECTION_PLAN_DIRECT10_HUB10
                and campaign in core.CONNECTION_PLAN_CAMPAIGNS
                and cycles == core.QUALIFICATION_CYCLES
            )
        )
    )
    timing_gates = {
        kind: core.summarize_timing_gate(
            transitions,
            kind=kind,
            expected_cycles=required_cycles,
            fast_limit_s=canonical_fast_limit,
            max_limit_s=canonical_max_limit,
        )
        for kind in core.required_gate_kinds(campaign)
    }
    connection_layout_gate = core.summarize_connection_layout_gate(
        transitions,
        campaign=campaign,
        connection_plan=connection_plan,
    )
    connection_layout_passed = connection_layout_gate["verdict"] in {
        "PASS",
        "NOT_REQUESTED",
    }
    transitions_completed = bool(
        transitions
        and all(
            item.get("outcome") == "completed"
            or (
                item.get("gate_kind") is not None
                and item.get("outcome") == "safe_state"
            )
            for item in transitions
        )
    )
    segment_evidence_verdict = (
        "INCONCLUSIVE"
        if not sequence_complete or not interval_complete or not provenance_complete
        else (
            "PASS"
            if aborted is None
            and safety_clean
            and snapshot_restart_clean
            and transitions_completed
            else "FAIL"
        )
    )
    evidence_verdict = (
        "INCONCLUSIVE"
        if resume_verified and segment_evidence_verdict == "PASS"
        else segment_evidence_verdict
    )
    cycle_gates_passed = bool(
        configuration_eligible
        and timing_gates
        and all(item["verdict"] == "PASS" for item in timing_gates.values())
        and connection_layout_passed
    )
    qualification_gate = (
        ("INCONCLUSIVE" if resume_verified else "PASS")
        if cycle_gates_passed and segment_evidence_verdict == "PASS"
        else (
            "INCOMPLETE"
            if segment_evidence_verdict == "PASS"
            and connection_layout_passed
            and (
                not configuration_eligible
                or (
                    timing_gates
                    and any(
                        item["verdict"] == "INCOMPLETE"
                        for item in timing_gates.values()
                    )
                )
            )
            else "FAIL"
        )
    )
    return {
        "schema": CHECKPOINT_SCHEMA,
        "schema_version": 1,
        "verdict": evidence_verdict,
        "qualification_gate": qualification_gate,
        "campaign": campaign,
        "connection_plan": connection_plan,
        "resume_from": dict(resume) if resume is not None else None,
        "requested_cycles": cycles,
        "commit": commit,
        "provenance": dict(provenance) if provenance is not None else None,
        "aborted": aborted,
        "pi_mutations_performed": False,
        "artifact_dir": str(artifact_dir),
        "artifacts": artifact_map(artifact_dir),
        "qualification_configuration": {
            "verdict": "PASS" if configuration_eligible else "INELIGIBLE",
            "configured": {
                "cycles": cycles,
                "fast_limit_s": fast_limit_s,
                "max_limit_s": max_limit_s,
                "connection_plan": connection_plan,
            },
            "required": {
                "cycles": required_cycles,
                "fast_limit_s": canonical_fast_limit,
                "max_limit_s": canonical_max_limit,
                "connection_plan": (
                    core.CONNECTION_PLAN_DIRECT10_HUB10
                    if connection_plan is not None
                    else None
                ),
            },
        },
        "provenance_gate": {
            "verdict": "PASS" if provenance_complete else "INCONCLUSIVE"
        },
        "campaign_continuity_gate": {
            "verdict": "INCONCLUSIVE"
            if resume_verified
            else (
                "PASS" if interval_complete and sequence_complete else "INCONCLUSIVE"
            ),
            "continuously_sampled": not resume_verified,
            "unmonitored_inter_segment_gaps": (
                [dict(resume["unmonitored_inter_segment_gap"])]
                if resume_verified
                else []
            ),
            "note": (
                "predecessor and resumed segments are individually sampled, but the "
                "interval between them was not monitored"
                if resume_verified
                else "this campaign was sampled as one continuous segment"
            ),
        },
        "sampling_gate": {
            "verdict": (
                "PASS" if interval_complete and sequence_complete else "INCONCLUSIVE"
            ),
            "configured_interval_s": stream.interval,
            "required_max_configured_interval_s": configured_interval_limit,
            "sample_count": stream.total_samples,
            "max_remote_start_gap_s": round(stream.max_remote_gap, 6),
            "max_host_receive_gap_s": round(stream.max_host_receive_gap, 6),
            "required_max_measured_gap_s": measured_gap_limit,
            "sequence_complete": sequence_complete,
            "sequence_gaps": stream.sequence_gaps,
            "malformed_lines": stream.malformed_lines,
            "unexpected_eof": stream.unexpected_eof,
            "scope": "resumed_segment" if resume_verified else "whole_campaign",
        },
        "link_safety_gate": {
            "verdict": "PASS" if safety_clean else "FAIL",
            "violation_counts": dict(stream.violation_counts),
            "first_violations": stream.first_violations,
            "candidate_node_union": sorted(stream.candidate_node_union),
            "requirements": [
                "zero raw or inactive microphone uplinks",
                "zero inactive microphone feeds into AEC or bridge.mic",
                "zero duplicate uplink owners",
                f"only {core.MICROPHONE_OUTPUT} may feed HFP",
            ],
        },
        "restart_gate": {
            "verdict": "PASS" if snapshot_restart_clean else "FAIL",
            "snapshot_deltas": deltas,
            "stream_initial": stream.initial_service_counts,
            "stream_final": (
                stream.cached_service_counts()
                if hasattr(stream, "cached_service_counts")
                else stream.service_counts()
            ),
            "supervisor_restart_delta": (
                deltas.get("user_service_restarts", {}).get("bridge-supervisor.service")
                if isinstance(deltas, dict)
                else None
            ),
        },
        "timing_gates": timing_gates,
        "connection_layout_gate": connection_layout_gate,
        "transitions": transitions,
        "remote_boot_id": stream.remote_boot_id,
        "remote_stderr": remote_stderr,
        "remote_returncode": remote_returncode,
        "fatal_errors": fatal_errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default=os.environ.get("BRIDGE_PI_HOST", DEFAULT_HOST)
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("BRIDGE_PI_REPO", DEFAULT_REMOTE_REPO),
    )
    parser.add_argument(
        "--campaign",
        choices=("matrix", "promotion-fallback", "fifine-replug", "inactive-fifine"),
        default="matrix",
    )
    parser.add_argument("--cycles", type=int)
    parser.add_argument(
        "--connection-plan",
        choices=(core.CONNECTION_PLAN_DIRECT10_HUB10,),
        help=(
            "strict 20-cycle qualification split: cycles 1-10 direct, one "
            "observed handoff, then cycles 11-20 through an attested powered hub"
        ),
    )
    parser.add_argument(
        "--resume-from",
        type=Path,
        help=(
            "verified predecessor evidence directory; supported only for a stopped "
            "promotion-fallback direct10-hub10 campaign before its midpoint handoff"
        ),
    )
    parser.add_argument("--interval", type=float, default=core.DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--timeout", type=float, default=core.DEFAULT_TRANSITION_TIMEOUT_SECONDS
    )
    parser.add_argument("--settle", type=float, default=core.DEFAULT_SETTLE_SECONDS)
    parser.add_argument(
        "--fast-limit", type=float, default=core.DEFAULT_FAST_LIMIT_SECONDS
    )
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--status-path",
        help="override the Pi user's default bridge status path",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    configured_cycles = args.cycles
    if args.cycles is None:
        args.cycles = 1 if args.campaign == "matrix" else core.DEFAULT_REPEATED_CYCLES
    if args.connection_plan is not None:
        if args.campaign not in core.CONNECTION_PLAN_CAMPAIGNS:
            parser.error(
                "--connection-plan is accepted only for promotion-fallback or "
                "fifine-replug"
            )
        if configured_cycles not in {None, core.QUALIFICATION_CYCLES}:
            parser.error(
                f"--connection-plan {core.CONNECTION_PLAN_DIRECT10_HUB10} requires "
                f"exactly {core.QUALIFICATION_CYCLES} cycles"
            )
        args.cycles = core.QUALIFICATION_CYCLES
    if args.resume_from is not None and (
        args.campaign != "promotion-fallback"
        or args.connection_plan != core.CONNECTION_PLAN_DIRECT10_HUB10
    ):
        parser.error(
            "--resume-from requires --campaign promotion-fallback and "
            f"--connection-plan {core.CONNECTION_PLAN_DIRECT10_HUB10}"
        )
    if args.cycles <= 0:
        parser.error("--cycles must be positive")
    configured_interval_limit = float(
        getattr(core, "MAX_CONFIGURED_INTERVAL_SECONDS", 0.20)
    )
    if not 0 < args.interval <= configured_interval_limit:
        parser.error(
            f"--interval must be >0 and <= {configured_interval_limit} seconds"
        )
    if args.timeout <= 0 or args.fast_limit <= 0 or args.settle < 0:
        parser.error("timing values are outside their valid range")
    if args.fast_limit > args.timeout:
        parser.error("--fast-limit cannot exceed --timeout")
    return args


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: SshBackend | None = None,
    input_fn=input,
) -> int:
    args = parse_args(argv)
    commit = git_commit()
    artifact_dir = args.run_dir or default_run_dir(commit=commit)
    try:
        artifact_dir.mkdir(parents=True, exist_ok=False)
    except (FileExistsError, OSError) as exc:
        failure_type = (
            "OutputDirectoryExists"
            if isinstance(exc, FileExistsError)
            else type(exc).__name__
        )
        message = (
            "refusing to overwrite an existing evidence output directory"
            if isinstance(exc, FileExistsError)
            else str(exc)
        )
        failure = {
            "schema": CHECKPOINT_SCHEMA,
            "schema_version": 1,
            "verdict": "FAIL",
            "qualification_gate": "FAIL",
            "campaign": args.campaign,
            "connection_plan": args.connection_plan,
            "requested_cycles": args.cycles,
            "commit": commit,
            "failure": {"type": failure_type, "message": message},
            "artifact_dir": str(artifact_dir),
            "pi_mutations_performed": False,
            "physical_evidence_claimed": False,
        }
        json.dump(failure, sys.stdout, indent=2)
        sys.stdout.write("\n")
        print(message, file=sys.stderr, flush=True)
        return 2
    checkpoint = Checkpoint(
        artifact_dir / "checkpoint.json",
        {
            "schema": CHECKPOINT_SCHEMA,
            "schema_version": 1,
            "status": "starting",
            "campaign": args.campaign,
            "connection_plan": args.connection_plan,
            "resume_from_requested": (
                str(args.resume_from) if args.resume_from is not None else None
            ),
            "target_cycles": args.cycles,
            "commit": commit,
            "host": args.host,
            "remote_repo": args.repo,
            "interval_s": args.interval,
            "fast_limit_s": args.fast_limit,
            "timeout_s": args.timeout,
            "transitions": [],
            "pi_mutations_performed": False,
        },
    )
    implementation = backend or SshBackend(args.host, args.repo)
    sampler_source = Path(core.__file__).resolve()
    stream = SshSampleStream(
        host=args.host,
        remote_repo=args.repo,
        source_path=sampler_source,
        timeline_path=artifact_dir / TIMELINE_NAME,
        interval=args.interval,
        duration=24 * 60 * 60,
        status_path=args.status_path,
        checkpoint=checkpoint,
    )
    transitions: list[dict[str, Any]] = []
    resume_binding: dict[str, Any] | None = None
    preflight: dict[str, Any] | None = None
    closing: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    aborted: str | None = None
    expected_initial, _phrase = initial_fixture(args.campaign, args.connection_plan)
    expected_final = final_microphone(args.campaign)

    def add_abort(detail: str) -> None:
        nonlocal aborted
        aborted = f"{aborted}; {detail}" if aborted else detail

    def record_abort_event() -> None:
        if aborted is None:
            return
        try:
            stream.record_event("campaign_abort", reason=aborted)
        except Exception as exc:  # noqa: BLE001 - retain the primary failure
            add_abort(f"abort event archival failed: {type(exc).__name__}: {exc}")

    try:
        if args.resume_from is not None:
            resume_binding, transitions = load_resume_evidence(args.resume_from)
            atomic_json(artifact_dir / RESUME_NAME, resume_binding)
            checkpoint.update(
                resume=RESUME_NAME,
                resume_from_status="verified",
                predecessor_completed_cycles=resume_binding[
                    "accepted_predecessor_evidence"
                ]["completed_cycles"],
                next_cycle=resume_binding["accepted_predecessor_evidence"][
                    "next_cycle"
                ],
                transitions=[dict(item) for item in transitions],
            )
        provenance = collect_provenance(
            host=args.host,
            remote_repo=args.repo,
            sampler_source=sampler_source,
            commit=commit,
        )
        atomic_json(artifact_dir / PROVENANCE_NAME, provenance)
        checkpoint.update(provenance=PROVENANCE_NAME)
        if provenance.get("status") != "PASS":
            raise EvidenceError("local or remote provenance collection failed")
        stream.start()
        if resume_binding is not None:
            resumed_at = time.time()
            gap = resume_binding["unmonitored_inter_segment_gap"]
            predecessor_ended = float(gap["predecessor_recording_ended_timestamp"])
            if resumed_at < predecessor_ended:
                raise EvidenceError(
                    "host wall clock moved backwards across the predecessor boundary"
                )
            gap.update(
                resumed_monitoring_started_timestamp=resumed_at,
                approximate_duration_s=round(resumed_at - predecessor_ended, 6),
                resumed_boot_id=stream.remote_boot_id,
                same_boot=(
                    isinstance(gap.get("predecessor_boot_id"), str)
                    and gap.get("predecessor_boot_id") == stream.remote_boot_id
                ),
            )
            atomic_json(artifact_dir / RESUME_NAME, resume_binding)
            checkpoint.update(resume_gap=gap)
            stream.record_event(
                "campaign_resume",
                predecessor_manifest_sha256=resume_binding["predecessor_manifest"][
                    "sha256"
                ],
                completed_predecessor_cycles=resume_binding[
                    "accepted_predecessor_evidence"
                ]["completed_cycles"],
                next_cycle=resume_binding["accepted_predecessor_evidence"][
                    "next_cycle"
                ],
                unmonitored_inter_segment_gap=gap,
            )
        typed_prepare(
            args.campaign,
            connection_plan=args.connection_plan,
            input_fn=input_fn,
        )
        preflight = implementation.snapshot(full=True)
        atomic_json(artifact_dir / "preflight.json", preflight)
        validate_hotplug_snapshot(
            preflight,
            expected_microphone=expected_initial,
        )
        checkpoint.update(status="preflight_passed", preflight="preflight.json")

        if args.campaign == "matrix":
            core.run_matrix(
                stream,
                transitions,
                cycles=args.cycles,
                timeout_s=args.timeout,
                settle_s=args.settle,
                input_fn=input_fn,
            )
        elif args.campaign == "promotion-fallback":
            core.run_promotion_fallback(
                stream,
                transitions,
                cycles=args.cycles,
                timeout_s=args.timeout,
                settle_s=args.settle,
                input_fn=input_fn,
                connection_plan=args.connection_plan,
                start_cycle=(
                    resume_binding["accepted_predecessor_evidence"]["next_cycle"]
                    if resume_binding is not None
                    else 1
                ),
            )
        elif args.campaign == "fifine-replug":
            core.run_fifine_replug(
                stream,
                transitions,
                cycles=args.cycles,
                timeout_s=args.timeout,
                settle_s=args.settle,
                input_fn=input_fn,
                connection_plan=args.connection_plan,
            )
        else:
            core.run_inactive_fifine(
                stream,
                transitions,
                cycles=args.cycles,
                timeout_s=args.timeout,
                settle_s=args.settle,
                input_fn=input_fn,
            )

        closing = implementation.snapshot(full=True)
        atomic_json(artifact_dir / "closing.json", closing)
        validate_hotplug_snapshot(closing, expected_microphone=expected_final)
    except (EvidenceError, HardFailure, HardwareNotReady, core.CampaignAbort) as exc:
        add_abort(f"{type(exc).__name__}: {exc}")
        record_abort_event()
    except KeyboardInterrupt:
        add_abort("KeyboardInterrupt: operator interrupted campaign")
        record_abort_event()
    except Exception as exc:  # noqa: BLE001 - preserve structured failure evidence
        add_abort(f"{type(exc).__name__}: {exc}")
        record_abort_event()
    finally:
        if preflight is not None and closing is None:
            try:
                closing = implementation.snapshot(full=True)
                atomic_json(artifact_dir / "closing.json", closing)
            except Exception as exc:  # noqa: BLE001 - retain partial evidence
                add_abort(f"closing snapshot failed: {type(exc).__name__}: {exc}")
        try:
            stream.stop()
        except Exception as exc:  # noqa: BLE001 - summary must survive stop failure
            add_abort(f"stream stop failed: {type(exc).__name__}: {exc}")

    if getattr(stream, "fatal_errors", []):
        add_abort("stream evidence failed: " + "; ".join(stream.fatal_errors))

    summary = build_summary(
        campaign=args.campaign,
        cycles=args.cycles,
        transitions=transitions,
        stream=stream,
        preflight=preflight,
        closing=closing,
        aborted=aborted,
        fast_limit_s=args.fast_limit,
        max_limit_s=args.timeout,
        artifact_dir=artifact_dir,
        commit=commit,
        provenance=provenance,
        connection_plan=args.connection_plan,
        resume=resume_binding,
    )
    qualified = bool(
        summary["verdict"] == "PASS" and summary["qualification_gate"] == "PASS"
    )
    checkpoint.update(
        status=("complete" if qualified else "failed"),
        summary=SUMMARY_NAME,
        verdict=summary["verdict"],
        qualification_gate=summary["qualification_gate"],
        candidate_node_union=sorted(stream.candidate_node_union),
        aborted=aborted,
    )
    summary_path = artifact_dir / SUMMARY_NAME
    manifest_path = artifact_dir / MANIFEST_NAME
    atomic_json(summary_path, summary)
    atomic_json(manifest_path, {"schema_version": 1, "files": []})
    summary["artifacts"] = artifact_map(artifact_dir)
    atomic_json(summary_path, summary)
    atomic_json(manifest_path, evidence_manifest(artifact_dir))
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    if qualified:
        return 0
    if isinstance(aborted, str) and aborted.startswith("HardwareNotReady"):
        return 78
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
