from __future__ import annotations

import contextlib
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from rig.bt500_aux import microphone_hotplug as hotplug


def service_counts(value: int = 0) -> dict[str, int]:
    return {unit: value for unit in hotplug.core.SERVICE_UNITS}


def usb_topology(
    *, lark: tuple[str, ...] = (), fifine: tuple[str, ...] = ("1-1.2@4",)
) -> dict[str, list[dict]]:
    def devices(candidate_id: str, values: tuple[str, ...]) -> list[dict]:
        vendor_id, product_id = hotplug.core.USB_MICROPHONE_FINGERPRINTS[candidate_id]
        found = []
        for value in values:
            port, raw_devnum = value.rsplit("@", 1)
            found.append(
                {
                    "id": candidate_id,
                    "usb_vendor_id": vendor_id,
                    "usb_product_id": product_id,
                    "usb_product": (
                        "Wireless Microphone"
                        if candidate_id == "lark-a1"
                        else "USB PnP Audio Device"
                    ),
                    "usb_serial": None,
                    "usb_port_path": port,
                    "usb_devnum": int(raw_devnum),
                    "usb_instance_generation": value,
                }
            )
        return found

    return {
        "lark-a1": devices("lark-a1", lark),
        "fifine-k054": devices("fifine-k054", fifine),
    }


def selected_for_topology(topology: dict[str, list[dict]]) -> dict | None:
    candidate_id = (
        "lark-a1"
        if len(topology["lark-a1"]) == 1
        else ("fifine-k054" if len(topology["fifine-k054"]) == 1 else None)
    )
    if candidate_id is None:
        return None
    raw = topology[candidate_id][0]
    return {
        "id": candidate_id,
        "node": f"alsa_input.usb-{candidate_id}",
        "instance_token": f"{candidate_id}:{raw['usb_instance_generation']}",
        "identity": {key: raw.get(key) for key in hotplug.core.USB_IDENTITY_FIELDS},
    }


def completed_usb_sample(
    seq: int = 0,
    *,
    topology: dict[str, list[dict]] | None = None,
    source_monotonic: float | None = None,
    gap: float = 0.15,
) -> dict:
    observed = usb_topology() if topology is None else topology
    return {
        "seq": seq,
        "remote_seq": seq,
        "capture_started_monotonic": (
            10.0 + seq * 0.15 if source_monotonic is None else source_monotonic
        ),
        "usb_microphones": observed,
        "usb_error": None,
        "microphone": selected_for_topology(observed),
        "sampling": {"start_gap_s": gap},
    }


def valid_timing_transition(kind: str, *, outcome: str = "completed") -> dict:
    before, after, _selected_id = {
        "promotion": (
            usb_topology(),
            usb_topology(lark=("1-1.3@9",)),
            "lark-a1",
        ),
        "fallback": (
            usb_topology(lark=("1-1.3@9",)),
            usb_topology(),
            "fifine-k054",
        ),
        "fifine_replug": (
            usb_topology(fifine=()),
            usb_topology(fifine=("1-1.2@12",)),
            "fifine-k054",
        ),
    }[kind]
    baseline_samples = [
        {
            **completed_usb_sample(
                seq,
                topology=before,
                source_monotonic=99.0 + seq * 0.15,
            ),
            "remote_seq": seq,
        }
        for seq in (0, 1)
    ]
    baseline = hotplug.core.stable_usb_baseline_from_samples(baseline_samples)
    target_id = hotplug.core.USB_GATE_TARGET[kind][0]
    first = {
        **completed_usb_sample(2, topology=after, source_monotonic=100.0),
        "remote_seq": 2,
        "microphone": None,
    }
    confirmation = {
        **completed_usb_sample(3, topology=after, source_monotonic=100.15),
        "remote_seq": 3,
        "microphone": selected_for_topology(after),
    }
    settle_samples = [
        {
            **completed_usb_sample(
                seq,
                topology=after,
                source_monotonic=100.15 + (seq - 3) * 0.15,
            ),
            "remote_seq": seq,
            "microphone": selected_for_topology(after),
        }
        for seq in range(4, 8)
    ]
    final = settle_samples[-1]
    raw_devices = after[target_id]
    if kind == "fallback":
        binding = hotplug.core._identity_binding_evidence(
            baseline["microphone"], before[target_id][0], target_id, "preaction"
        )
    elif outcome == "completed":
        binding = hotplug.core._identity_binding_evidence(
            confirmation["microphone"], raw_devices[0], target_id, "final_selected"
        )
    else:
        binding = None
    final_candidate_id = hotplug.core.USB_FINAL_SELECTED_CANDIDATE[kind]
    final_binding = (
        hotplug.core._identity_binding_evidence(
            final["microphone"],
            after[final_candidate_id][0],
            final_candidate_id,
            "final_selected",
        )
        if outcome == "completed"
        else None
    )
    persistence_samples = [
        hotplug.core._event_sample_structure(first, kind),
        hotplug.core._event_sample_structure(confirmation, kind),
    ]
    if outcome == "completed":
        persistence_samples.extend(
            hotplug.core._event_sample_structure(sample, kind)
            for sample in settle_samples
        )
    return {
        "gate_kind": kind,
        "outcome": outcome,
        "timing_evidence_version": hotplug.core.TIMING_EVIDENCE_VERSION,
        "timing_origin": hotplug.core.USB_TIMING_ORIGIN,
        "transition_latency_s": 0.15 if outcome == "completed" else None,
        "settled_latency_s": 0.75 if outcome == "completed" else None,
        "settle_requirement_s": hotplug.core.QUALIFICATION_MIN_SETTLE_SECONDS,
        "state_settle_s": 0.6 if outcome == "completed" else None,
        "safe_state_latency_s": 0.15 if outcome == "safe_state" else None,
        "usb_baseline": baseline,
        "usb_event": {
            **hotplug.core._usb_event_evidence(first, kind),
            "confirmed": True,
            "confirmation": hotplug.core._usb_event_evidence(confirmation, kind),
            "persistent": True,
            "stable_through_seq": 7 if outcome == "completed" else 3,
            "persistence_samples": persistence_samples,
        },
        "usb_identity_binding": binding,
        "usb_final_identity_binding": final_binding,
        "first_matching_sample": confirmation if outcome == "completed" else None,
        "final_sample": final if outcome == "completed" else confirmation,
        "safety_clean": True,
        "restart_clean": True,
    }


def remote_usb_document(
    seq: int,
    topology: dict[str, list[dict]],
    *,
    source_monotonic: float,
    gap: float = 0.15,
) -> dict:
    return {
        "type": "sample",
        "seq": seq,
        "elapsed_s": seq * 0.15,
        "capture_started_monotonic": source_monotonic,
        "usb_microphones": topology,
        "usb_error": None,
        "microphone": selected_for_topology(topology),
        "candidates": {},
        "invariants": {"passed": True, "violations": []},
        "sampling": {"start_gap_s": gap},
        "service_restart_counts": service_counts(),
    }


@contextlib.contextmanager
def feed_host_action_baseline(
    stream,
    topologies: list[dict[str, list[dict]]],
    *,
    gaps: list[float] | None = None,
):
    stream.stream_started = True
    if not stream.initial_service_counts or any(
        value is None for value in stream.initial_service_counts.values()
    ):
        stream.initial_service_counts = service_counts()
    original = stream._action_usb_baseline_locked
    producers: list[threading.Thread] = []

    def wrapped(phase: str, after_seq: int) -> dict:
        first_remote_seq = stream._last_remote_seq + 1
        latest = stream.latest()
        source_base = (
            float(latest["capture_started_monotonic"])
            if latest is not None
            and isinstance(latest.get("capture_started_monotonic"), (int, float))
            else 500.0
        )

        def produce() -> None:
            for offset, topology in enumerate(topologies):
                stream.ingest(
                    remote_usb_document(
                        first_remote_seq + offset,
                        topology,
                        source_monotonic=source_base + (offset + 1) * 0.15,
                        gap=(gaps or [0.15] * len(topologies))[offset],
                    )
                )

        producer = threading.Thread(target=produce)
        producers.append(producer)
        producer.start()
        return original(phase, after_seq)

    with mock.patch.object(stream, "_action_usb_baseline_locked", side_effect=wrapped):
        yield
    for producer in producers:
        producer.join(2)


def snapshot(value: int = 0) -> dict:
    return {
        "services": {
            "user": {
                unit: {"NRestarts": str(value)} for unit in hotplug.core.SERVICE_UNITS
            }
        },
        "kernel_errors": [],
        "usb_errors": [],
    }


def passing_provenance() -> dict:
    return {
        "schema_version": 1,
        "status": "PASS",
        "local_repository": {
            "head": "a" * 40,
            "dirty": True,
            "porcelain": [" M local-change"],
        },
        "local_sources": {},
        "remote": {"status": "PASS"},
        "read_only": True,
    }


def initialize_provenance_repository(
    root: Path,
    *,
    omit_remote_path: str | None = None,
    non_blob_remote_path: str | None = None,
) -> tuple[str, dict[str, bytes]]:
    paths = set(hotplug.LOCAL_DECISION_SOURCE_PATHS.values())
    paths.update(hotplug.REMOTE_TRACKED_SOURCE_PATHS.values())
    payloads: dict[str, bytes] = {}
    for repository_path in sorted(paths):
        if repository_path == omit_remote_path:
            continue
        if repository_path == non_blob_remote_path:
            repository_path = f"{repository_path}/child.py"
        payload = f"tracked source: {repository_path}\n".encode()
        path = root / repository_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        payloads[repository_path] = payload

    commands = (
        ["git", "init", "--quiet"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "config", "user.name", "Hotplug Test"],
        ["git", "config", "user.email", "hotplug@example.invalid"],
        ["git", "add", "--", *sorted(payloads)],
        ["git", "commit", "--quiet", "-m", "fixture"],
    )
    for command in commands:
        subprocess.run(command, cwd=root, capture_output=True, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return head, payloads


def remote_provenance_document(
    commit: str,
    payloads: dict[str, bytes],
    *,
    hash_overrides: dict[str, str] | None = None,
) -> dict:
    overrides = hash_overrides or {}
    installed = {}
    for name, repository_path in hotplug.REMOTE_TRACKED_SOURCE_PATHS.items():
        payload = payloads.get(repository_path, b"remote file without a tracked blob\n")
        installed[name] = {
            "path": f"/srv/bridge/{repository_path}",
            "exists": True,
            "sha256": overrides.get(name, hashlib.sha256(payload).hexdigest()),
        }
    config_payload = b"preserved deployment configuration\n"
    installed[hotplug.PRESERVED_CONFIG_SOURCE] = {
        "path": "/srv/bridge/config/bridge.toml",
        "exists": True,
        "sha256": overrides.get(
            hotplug.PRESERVED_CONFIG_SOURCE,
            hashlib.sha256(config_payload).hexdigest(),
        ),
    }
    return {
        "status": "PASS",
        "repository": {
            "path": "/srv/bridge",
            "head": commit,
            "dirty": None,
            "porcelain": [],
            "identity_source": "deployed_release",
        },
        "installed_files": installed,
        "deployed_release": {
            "path": "/etc/larkbridge/DEPLOYED.json",
            "exists": True,
            "sha256": "e" * 64,
            "document": {
                "commit": commit,
                "archive_sha256": "f" * 64,
            },
        },
        "errors": [],
    }


def provenance_runner(remote: dict):
    def runner(command, **kwargs):
        if command[0] == "ssh":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(remote),
                stderr="",
            )
        return subprocess.run(command, check=kwargs.pop("check", False), **kwargs)

    return runner


def collect_fixture_provenance(
    root: Path,
    *,
    commit: str,
    remote: dict,
) -> dict:
    return hotplug.collect_provenance(
        host="bridge",
        remote_repo="/srv/bridge",
        sampler_source=root / hotplug.LOCAL_DECISION_SOURCE_PATHS["streamed_sampler"],
        host_source=root / hotplug.LOCAL_DECISION_SOURCE_PATHS["host_harness"],
        harness_library_source=(
            root / hotplug.LOCAL_DECISION_SOURCE_PATHS["harness_library"]
        ),
        local_invariants_source=(
            root / hotplug.LOCAL_DECISION_SOURCE_PATHS["local_invariants"]
        ),
        repo=root,
        commit=commit,
        runner=provenance_runner(remote),
    )


class FakeBackend:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self, *, full: bool = True) -> dict:
        self.calls += 1
        return snapshot()


class FakeStream:
    def __init__(self, **kwargs) -> None:
        self.interval = kwargs["interval"]
        self.timeline_path = kwargs.get("timeline_path")
        self.stream_started = True
        self.stream_stopped = False
        self.unexpected_eof = False
        self.sequence_gaps = []
        self.malformed_lines = []
        self.total_samples = 2
        self.max_remote_gap = 0.15
        self.max_host_receive_gap = 0.15
        self.candidate_node_union = {"alsa_input.usb-FIFINE"}
        self.violation_counts = Counter()
        self.first_violations = []
        self.initial_service_counts = service_counts()
        self.remote_boot_id = "boot-1"
        self.remote_stderr = []
        self.remote_returncode = None
        self.fatal_errors = []
        self.events = []

    def start(self) -> None:
        if self.timeline_path is not None:
            self.timeline_path.write_text('{"type":"stream_start"}\n', encoding="utf-8")

    def stop(self) -> None:
        return None

    def record_event(self, event_type: str, **values) -> None:
        self.events.append((event_type, values))

    def service_counts(self) -> dict[str, int]:
        return service_counts()

    def cached_service_counts(self) -> dict[str, int]:
        return service_counts()


class MicrophoneHotplugHostTests(unittest.TestCase):
    def test_host_mark_action_rejects_missing_remote_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stream = hotplug.SshSampleStream(
                host="unused",
                remote_repo="/unused",
                source_path=root / "unused.py",
                timeline_path=root / "samples.jsonl",
                interval=0.15,
                duration=60.0,
                status_path=None,
                checkpoint=hotplug.Checkpoint(root / "checkpoint.json", {}),
            )
            with (
                mock.patch.object(
                    stream,
                    "_query_service_counts",
                    return_value=(service_counts(), None),
                ),
                self.assertRaisesRegex(hotplug.core.CampaignAbort, "not running"),
            ):
                stream.mark_action("lark_promotion", 1, "plug")

    def test_host_mark_action_rejects_stale_and_unstable_remote_baselines(
        self,
    ) -> None:
        cases = (
            ("stale", [usb_topology()] * 3, [0.15, 0.15, 0.251]),
            (
                "unstable",
                [
                    usb_topology(),
                    usb_topology(),
                    usb_topology(lark=("1-1.3@9",)),
                ],
                None,
            ),
        )
        for label, topologies, gaps in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stream = hotplug.SshSampleStream(
                    host="unused",
                    remote_repo="/unused",
                    source_path=root / "unused.py",
                    timeline_path=root / "samples.jsonl",
                    interval=0.15,
                    duration=60.0,
                    status_path=None,
                    checkpoint=hotplug.Checkpoint(root / "checkpoint.json", {}),
                )
                stream._started_monotonic = time.monotonic()
                with (
                    feed_host_action_baseline(stream, topologies, gaps=gaps),
                    mock.patch.object(
                        stream,
                        "_query_service_counts",
                        return_value=(service_counts(), None),
                    ),
                    self.assertRaises(hotplug.core.CampaignAbort),
                ):
                    stream.mark_action("lark_promotion", 1, "plug")

    def test_direct_script_entrypoint_loads_from_repository_root(self) -> None:
        script = Path(hotplug.__file__).resolve()
        repo = script.parents[2]
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--campaign", result.stdout)

    def test_parser_defaults_to_one_matrix_or_twenty_repeated_cycles(self) -> None:
        self.assertEqual(hotplug.parse_args([]).cycles, 1)
        self.assertEqual(
            hotplug.parse_args(["--campaign", "promotion-fallback"]).cycles,
            20,
        )
        self.assertIsNone(hotplug.parse_args([]).status_path)
        with self.assertRaises(SystemExit):
            hotplug.parse_args(["--interval", "0.201"])

    def test_snapshot_freshness_uses_pi_clock_not_host_clock(self) -> None:
        document = {
            "timestamp": 1_000.0,
            "status": {
                "timestamp": 999.0,
                "microphone": {"candidates": []},
            },
            "pipewire_links": "",
        }
        with (
            mock.patch.object(hotplug, "validate_snapshot"),
            mock.patch.object(
                hotplug.core,
                "evaluate_link_invariants",
                return_value={"passed": True, "violations": []},
            ) as evaluate,
            mock.patch.object(hotplug.core, "expectation_failures", return_value=[]),
            mock.patch.object(hotplug.time, "time", return_value=999_999.0),
        ):
            hotplug.validate_hotplug_snapshot(
                document, expected_microphone="fifine-k054"
            )

        self.assertEqual(evaluate.call_args.kwargs["now"], 1_000.0)

        document["status"]["timestamp"] = 990.0
        with (
            mock.patch.object(hotplug, "validate_snapshot"),
            self.assertRaisesRegex(hotplug.EvidenceError, "stale by 10.00s"),
        ):
            hotplug.validate_hotplug_snapshot(
                document, expected_microphone="fifine-k054"
            )

    def test_snapshot_freshness_rejects_nonfinite_and_future_timestamps(self) -> None:
        document = {
            "timestamp": 1_000.0,
            "status": {
                "timestamp": 999.0,
                "microphone": {"candidates": []},
            },
            "pipewire_links": "",
        }
        with mock.patch.object(hotplug, "validate_snapshot"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with (
                    self.subTest(location="snapshot", value=value),
                    self.assertRaisesRegex(hotplug.EvidenceError, "non-finite"),
                ):
                    document["timestamp"] = value
                    hotplug.validate_hotplug_snapshot(
                        document, expected_microphone="fifine-k054"
                    )

            document["timestamp"] = 1_000.0
            for value in (float("nan"), float("inf"), float("-inf")):
                with (
                    self.subTest(location="status", value=value),
                    self.assertRaisesRegex(hotplug.EvidenceError, "non-finite"),
                ):
                    document["status"]["timestamp"] = value
                    hotplug.validate_hotplug_snapshot(
                        document, expected_microphone="fifine-k054"
                    )

            document["status"]["timestamp"] = 1_001.0
            with self.assertRaisesRegex(hotplug.EvidenceError, "in the future"):
                hotplug.validate_hotplug_snapshot(
                    document, expected_microphone="fifine-k054"
                )

    def test_provenance_binds_clean_local_and_remote_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commit, payloads = initialize_provenance_repository(root)
            document = collect_fixture_provenance(
                root,
                commit=commit,
                remote=remote_provenance_document(commit, payloads),
            )

        self.assertEqual(document["status"], "PASS")
        self.assertEqual(document["local_repository"]["status"], "PASS")
        self.assertFalse(document["local_repository"]["dirty"])
        for record in document["local_sources"].values():
            self.assertEqual(record["binding"]["status"], "PASS")
            self.assertTrue(record["binding"]["matches_working_file"])
            self.assertEqual(record["sha256"], record["binding"]["git_blob_sha256"])
        for name in hotplug.REMOTE_TRACKED_SOURCE_PATHS:
            binding = document["remote"]["installed_files"][name]["binding"]
            self.assertEqual(binding["status"], "PASS")
            self.assertTrue(binding["matches_installed_file"])

    def test_provenance_rejects_modified_local_decision_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commit, payloads = initialize_provenance_repository(root)
            source = root / hotplug.LOCAL_DECISION_SOURCE_PATHS["host_harness"]
            source.write_text("modified decision source\n", encoding="utf-8")
            document = collect_fixture_provenance(
                root,
                commit=commit,
                remote=remote_provenance_document(commit, payloads),
            )

        binding = document["local_sources"]["host_harness"]["binding"]
        self.assertEqual(document["status"], "FAIL")
        self.assertEqual(binding["status"], "FAIL")
        self.assertFalse(binding["matches_working_file"])
        self.assertTrue(any("does not match" in item for item in binding["errors"]))

    def test_provenance_allows_unrelated_dirty_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commit, payloads = initialize_provenance_repository(root)
            evidence = root / "docs/experiments/results/E18/field/untracked.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("{}\n", encoding="utf-8")
            document = collect_fixture_provenance(
                root,
                commit=commit,
                remote=remote_provenance_document(commit, payloads),
            )

        self.assertEqual(document["status"], "PASS")
        self.assertTrue(document["local_repository"]["dirty"])
        self.assertTrue(
            any(
                "untracked.json" in item
                for item in document["local_repository"]["porcelain"]
            )
        )

    def test_provenance_rejects_remote_tracked_source_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commit, payloads = initialize_provenance_repository(root)
            remote = remote_provenance_document(
                commit,
                payloads,
                hash_overrides={"bridge_supervisor": "0" * 64},
            )
            document = collect_fixture_provenance(
                root,
                commit=commit,
                remote=remote,
            )

        binding = document["remote"]["installed_files"]["bridge_supervisor"]["binding"]
        self.assertEqual(document["status"], "FAIL")
        self.assertEqual(binding["status"], "FAIL")
        self.assertTrue(any("does not match" in item for item in binding["errors"]))

    def test_provenance_rejects_missing_deployed_commit_path_or_blob(self) -> None:
        cases = (
            ("missing commit", None, None, "0" * 40, "unavailable"),
            (
                "missing path",
                hotplug.REMOTE_TRACKED_SOURCE_PATHS["btadapters"],
                None,
                None,
                "missing at commit",
            ),
            (
                "non-blob path",
                None,
                hotplug.REMOTE_TRACKED_SOURCE_PATHS["btadapters"],
                None,
                "not a blob",
            ),
        )
        for label, omitted_path, non_blob_path, deployed_override, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                commit, payloads = initialize_provenance_repository(
                    root,
                    omit_remote_path=omitted_path,
                    non_blob_remote_path=non_blob_path,
                )
                deployed_commit = deployed_override or commit
                remote = remote_provenance_document(deployed_commit, payloads)
                document = collect_fixture_provenance(
                    root,
                    commit=commit,
                    remote=remote,
                )
                self.assertEqual(document["status"], "FAIL")
                all_errors = json.dumps(document["remote"], sort_keys=True)
                self.assertIn(expected, all_errors)

    def test_preserved_bridge_config_is_explicit_hash_only_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commit, payloads = initialize_provenance_repository(root)
            document = collect_fixture_provenance(
                root,
                commit=commit,
                remote=remote_provenance_document(commit, payloads),
            )

        config = document["remote"]["installed_files"][hotplug.PRESERVED_CONFIG_SOURCE]
        self.assertEqual(document["status"], "PASS")
        self.assertEqual(
            config["binding"]["kind"], hotplug.PRESERVED_CONFIG_BINDING_KIND
        )
        self.assertEqual(config["binding"]["status"], "PASS")
        self.assertIn("intentionally not bound", config["binding"]["exception"])
        self.assertNotIn("git_blob_sha256", config["binding"])

    def test_remote_provenance_rejects_missing_deployed_release(self) -> None:
        installed = {
            name: {"exists": True, "sha256": "a" * 64}
            for name in (
                "invariants",
                "bridge_config",
                "remote_snapshot",
                "remote_harness",
                "bridge_supervisor",
                "microphone_resolver",
                "output_resolver",
                "controller_roles",
                "btadapters",
            )
        }
        errors = hotplug._remote_provenance_errors(
            {
                "status": "PASS",
                "errors": [],
                "repository": {"head": "b" * 40},
                "installed_files": installed,
                "deployed_release": {"exists": False},
            }
        )
        self.assertTrue(any("DEPLOYED.json" in item for item in errors))

    def test_action_boundaries_query_restart_counts_synchronously(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = hotplug.Checkpoint(root / "checkpoint.json", {})
            stream = hotplug.SshSampleStream(
                host="unused",
                remote_repo="/unused",
                source_path=root / "unused.py",
                timeline_path=root / "samples.jsonl",
                interval=0.15,
                duration=60.0,
                status_path=None,
                checkpoint=checkpoint,
            )
            stream._started_monotonic = time.monotonic()
            observed_boundaries = []
            responses = iter([(service_counts(2), None), (service_counts(3), None)])

            def query_counts():
                observed_boundaries.append((stream._phase, stream._action_id))
                return next(responses)

            with (
                feed_host_action_baseline(stream, [usb_topology()] * 3),
                mock.patch.object(
                    stream,
                    "_query_service_counts",
                    side_effect=query_counts,
                ) as query,
            ):
                action = stream.mark_action("lark_promotion", 1, "plug")
                result_counts = stream.service_counts()

        self.assertEqual(query.call_count, 2)
        self.assertEqual(observed_boundaries[0], ("preflight", None))
        self.assertEqual(observed_boundaries[1][0], "lark_promotion")
        self.assertEqual(action["service_restart_counts"], service_counts(2))
        self.assertEqual(result_counts, service_counts(3))

    def test_host_action_captures_last_completed_remote_usb_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stream = hotplug.SshSampleStream(
                host="unused",
                remote_repo="/unused",
                source_path=root / "unused.py",
                timeline_path=root / "samples.jsonl",
                interval=0.15,
                duration=60.0,
                status_path=None,
                checkpoint=hotplug.Checkpoint(root / "checkpoint.json", {}),
            )
            stream._started_monotonic = time.monotonic()
            stream.ingest(
                {
                    "type": "stream_start",
                    "boot_id": "boot-1",
                    "service_restart_counts": service_counts(),
                    "service_error": None,
                }
            )
            topology = usb_topology()
            stream.ingest(
                {
                    "type": "sample",
                    "seq": 1,
                    "elapsed_s": 0.15,
                    "capture_started_monotonic": 500.15,
                    "usb_microphones": topology,
                    "usb_error": None,
                    "microphone": None,
                    "candidates": {},
                    "invariants": {"passed": True, "violations": []},
                    "sampling": {"start_gap_s": 0.15},
                    "service_restart_counts": service_counts(),
                }
            )
            with (
                feed_host_action_baseline(stream, [topology] * 3),
                mock.patch.object(
                    stream,
                    "_query_service_counts",
                    return_value=(service_counts(), None),
                ),
            ):
                action = stream.mark_action("lark_promotion", 1, "plug")

        self.assertEqual(action["usb_baseline"]["seq"], 4)
        self.assertEqual(action["usb_baseline"]["remote_seq"], 4)
        self.assertEqual(action["usb_baseline"]["usb_microphones"], topology)
        sample = stream.latest()
        assert sample is not None
        self.assertAlmostEqual(sample["capture_started_monotonic"], 500.6)
        self.assertIsInstance(sample["host_received_monotonic"], float)

    def test_host_rejects_ambiguous_usb_baseline_before_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stream = hotplug.SshSampleStream(
                host="unused",
                remote_repo="/unused",
                source_path=root / "unused.py",
                timeline_path=root / "samples.jsonl",
                interval=0.15,
                duration=60.0,
                status_path=None,
                checkpoint=hotplug.Checkpoint(root / "checkpoint.json", {}),
            )
            stream._started_monotonic = time.monotonic()
            topology = usb_topology(lark=("1-1.3@9", "1-1.4@10"))
            with (
                feed_host_action_baseline(stream, [topology] * 3),
                mock.patch.object(
                    stream,
                    "_query_service_counts",
                    return_value=(service_counts(), None),
                ),
                self.assertRaisesRegex(hotplug.core.CampaignAbort, "ambiguous"),
            ):
                stream.mark_action("lark_fallback", 1, "unplug")

    def test_ingest_cannot_relabel_a_pre_action_sample(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        boundary_ready = threading.Event()

        class BlockingCandidates(dict):
            def values(self):
                entered.set()
                self_outer.assertTrue(release.wait(2))
                return super().values()

        self_outer = self
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = hotplug.Checkpoint(root / "checkpoint.json", {})
            stream = hotplug.SshSampleStream(
                host="unused",
                remote_repo="/unused",
                source_path=root / "unused.py",
                timeline_path=root / "samples.jsonl",
                interval=0.15,
                duration=60.0,
                status_path=None,
                checkpoint=checkpoint,
            )
            stream._started_monotonic = time.monotonic()
            stream.stream_started = True
            stream.initial_service_counts = service_counts()
            sample = {
                "type": "sample",
                "seq": 1,
                "elapsed_s": 0.15,
                "capture_started_monotonic": 500.15,
                "usb_microphones": usb_topology(),
                "usb_error": None,
                "microphone": None,
                "candidates": BlockingCandidates({"candidate": {}}),
                "invariants": {"passed": True, "violations": []},
                "sampling": {"start_gap_s": 0.15},
                "service_restart_counts": service_counts(),
            }
            thread = threading.Thread(target=stream.ingest, args=(sample,))
            thread.start()
            self.assertTrue(entered.wait(2))
            action: dict = {}
            original_baseline = stream._action_usb_baseline_locked

            def baseline_after_inflight(phase, after_seq):
                self.assertEqual(after_seq, 1)
                boundary_ready.set()
                return original_baseline(phase, after_seq)

            def mark() -> None:
                action.update(stream.mark_action("lark_promotion", 1, "plug"))

            with (
                mock.patch.object(
                    stream,
                    "_query_service_counts",
                    return_value=(service_counts(), None),
                ),
                mock.patch.object(
                    stream,
                    "_action_usb_baseline_locked",
                    side_effect=baseline_after_inflight,
                ),
            ):
                action_thread = threading.Thread(target=mark)
                action_thread.start()
                self.assertTrue(boundary_ready.wait(2))
                release.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                ingested = stream.latest()
                for seq in (2, 3, 4):
                    stream.ingest(
                        remote_usb_document(
                            seq,
                            usb_topology(),
                            source_monotonic=500.0 + seq * 0.15,
                        )
                    )
                action_thread.join(2)
                self.assertFalse(action_thread.is_alive())

        self.assertIsNotNone(ingested)
        assert ingested is not None
        self.assertEqual(action["after_seq"], 4)
        self.assertEqual(ingested["phase"], "preflight")
        self.assertIsNone(ingested["action_id"])
        self.assertLessEqual(ingested["elapsed_s"], action["elapsed_s"])
        self.assertEqual(stream.samples_after(action["after_seq"]), [])

    def test_stream_tracks_sequence_gaps_candidate_union_and_restart_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = hotplug.Checkpoint(
                root / "checkpoint.json",
                {"schema": hotplug.CHECKPOINT_SCHEMA},
            )
            stream = hotplug.SshSampleStream(
                host="unused",
                remote_repo="/unused",
                source_path=root / "unused.py",
                timeline_path=root / "samples.jsonl",
                interval=0.15,
                duration=60.0,
                status_path=None,
                checkpoint=checkpoint,
            )
            stream._started_monotonic = time.monotonic()
            stream.ingest(
                {
                    "type": "stream_start",
                    "boot_id": "boot-1",
                    "service_restart_counts": service_counts(),
                    "service_error": None,
                }
            )
            base_sample = {
                "type": "sample",
                "elapsed_s": 0.15,
                "microphone": {
                    "id": "fifine-k054",
                    "node": "selected-node",
                    "instance_token": "token-1",
                },
                "candidates": {
                    "fifine-k054": {"matched_nodes": ["profile-a", "profile-b"]}
                },
                "invariants": {
                    "passed": True,
                    "violations": [],
                    "candidate_nodes": ["profile-a"],
                },
                "sampling": {"start_gap_s": 0.15},
                "service_restart_counts": service_counts(),
            }
            stream.ingest({**base_sample, "seq": 1})
            stream.ingest(
                {
                    **base_sample,
                    "seq": 3,
                    "service_restart_counts": service_counts(1),
                }
            )

            self.assertEqual(stream.sequence_gaps, [{"expected": 2, "observed": 3}])
            self.assertEqual(
                stream.candidate_node_union,
                {"selected-node", "profile-a", "profile-b"},
            )
            self.assertGreater(stream.violation_counts["H8"], 0)
            self.assertTrue(stream.fatal_errors)

            summary = hotplug.build_summary(
                campaign="inactive-fifine",
                cycles=1,
                transitions=[],
                stream=stream,
                preflight=None,
                closing=None,
                aborted=None,
                fast_limit_s=30.0,
                max_limit_s=60.0,
                artifact_dir=root,
                commit="abc123",
                provenance=passing_provenance(),
            )
            self.assertEqual(summary["verdict"], "INCONCLUSIVE")
            self.assertFalse(summary["sampling_gate"]["sequence_complete"])

    def test_mocked_host_orchestration_writes_complete_artifacts(self) -> None:
        def fake_matrix(stream, transitions, **kwargs) -> None:
            self.assertIn("input_fn", kwargs)
            for gate_kind in ("promotion", "fallback", "fifine_replug"):
                transitions.append(valid_timing_transition(gate_kind))

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            with (
                mock.patch.object(hotplug, "SshSampleStream", FakeStream),
                mock.patch.object(hotplug, "validate_hotplug_snapshot"),
                mock.patch.object(hotplug, "git_commit", return_value="a" * 40),
                mock.patch.object(
                    hotplug, "collect_provenance", return_value=passing_provenance()
                ),
                mock.patch.object(hotplug.core, "run_matrix", side_effect=fake_matrix),
            ):
                result = hotplug.main(
                    ["--run-dir", str(run_dir), "--cycles", "1"],
                    backend=FakeBackend(),
                    input_fn=lambda: "PREPARE FIFINE ONLY",
                )

            self.assertEqual(result, 0)
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            checkpoint = json.loads(
                (run_dir / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["verdict"], "PASS")
            self.assertEqual(summary["qualification_gate"], "PASS")
            self.assertFalse(summary["pi_mutations_performed"])
            self.assertEqual(checkpoint["status"], "complete")
            for name in (
                "preflight.json",
                "closing.json",
                "samples.jsonl",
                "provenance.json",
                "summary.json",
                "evidence-manifest.json",
            ):
                self.assertTrue((run_dir / name).is_file())
            manifest = json.loads(
                (run_dir / "evidence-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {item["path"] for item in manifest["files"]},
                {
                    "checkpoint.json",
                    "closing.json",
                    "preflight.json",
                    "provenance.json",
                    "samples.jsonl",
                    "summary.json",
                },
            )
            self.assertTrue(
                all(
                    (run_dir / name).is_file() for name in summary["artifacts"].values()
                )
            )
            for item in manifest["files"]:
                payload = (run_dir / item["path"]).read_bytes()
                self.assertEqual(item["bytes"], len(payload))
                self.assertEqual(item["sha256"], hashlib.sha256(payload).hexdigest())

    def test_weakened_cli_settings_cannot_qualify(self) -> None:
        def fake_repeated(_stream, transitions, **_kwargs) -> None:
            for gate_kind in ("promotion", "fallback"):
                transitions.append(
                    {
                        "gate_kind": gate_kind,
                        "outcome": "completed",
                        "timing_origin": hotplug.core.USB_TIMING_ORIGIN,
                        "transition_latency_s": 1.0,
                        "safety_clean": True,
                        "restart_clean": True,
                    }
                )

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            with (
                mock.patch.object(hotplug, "SshSampleStream", FakeStream),
                mock.patch.object(hotplug, "validate_hotplug_snapshot"),
                mock.patch.object(hotplug, "git_commit", return_value="a" * 40),
                mock.patch.object(
                    hotplug, "collect_provenance", return_value=passing_provenance()
                ),
                mock.patch.object(
                    hotplug.core,
                    "run_promotion_fallback",
                    side_effect=fake_repeated,
                ),
            ):
                result = hotplug.main(
                    [
                        "--run-dir",
                        str(run_dir),
                        "--campaign",
                        "promotion-fallback",
                        "--cycles",
                        "1",
                        "--fast-limit",
                        "45",
                        "--timeout",
                        "90",
                    ],
                    backend=FakeBackend(),
                    input_fn=lambda: "PREPARE FIFINE ONLY",
                )

            summary = json.loads((run_dir / "summary.json").read_text())
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())

        self.assertEqual(result, 1)
        self.assertEqual(summary["verdict"], "PASS")
        self.assertNotEqual(summary["qualification_gate"], "PASS")
        self.assertEqual(
            summary["qualification_configuration"]["verdict"], "INELIGIBLE"
        )
        self.assertEqual(summary["timing_gates"]["promotion"]["fast_limit_s"], 30.0)
        self.assertEqual(summary["timing_gates"]["promotion"]["max_limit_s"], 60.0)
        self.assertEqual(checkpoint["status"], "failed")

    def test_timing_gate_failure_forces_nonzero_exit(self) -> None:
        def slow_matrix(_stream, transitions, **_kwargs) -> None:
            for gate_kind in ("promotion", "fallback", "fifine_replug"):
                transitions.append(
                    {
                        "gate_kind": gate_kind,
                        "outcome": "completed",
                        "timing_origin": hotplug.core.USB_TIMING_ORIGIN,
                        "transition_latency_s": 31.0,
                        "safety_clean": True,
                        "restart_clean": True,
                    }
                )

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            with (
                mock.patch.object(hotplug, "SshSampleStream", FakeStream),
                mock.patch.object(hotplug, "validate_hotplug_snapshot"),
                mock.patch.object(hotplug, "git_commit", return_value="a" * 40),
                mock.patch.object(
                    hotplug, "collect_provenance", return_value=passing_provenance()
                ),
                mock.patch.object(hotplug.core, "run_matrix", side_effect=slow_matrix),
            ):
                result = hotplug.main(
                    ["--run-dir", str(run_dir)],
                    backend=FakeBackend(),
                    input_fn=lambda: "PREPARE FIFINE ONLY",
                )
            summary = json.loads((run_dir / "summary.json").read_text())
            checkpoint = json.loads((run_dir / "checkpoint.json").read_text())

        self.assertEqual(summary["verdict"], "PASS")
        self.assertEqual(summary["qualification_gate"], "FAIL")
        self.assertEqual(checkpoint["status"], "failed")
        self.assertEqual(result, 1)

    def test_tail_errors_nonzero_exit_and_fatal_errors_break_sequence(self) -> None:
        transitions = [
            {
                "gate_kind": gate_kind,
                "outcome": "completed",
                "timing_origin": hotplug.core.USB_TIMING_ORIGIN,
                "transition_latency_s": 1.0,
                "safety_clean": True,
                "restart_clean": True,
            }
            for gate_kind in ("promotion", "fallback", "fifine_replug")
        ]
        cases = (
            ("remote_stderr", ["ssh: connection reset"]),
            ("remote_returncode", 255),
            ("fatal_errors", ["remote sampler ended before stream_stop"]),
        )
        for attribute, value in cases:
            with self.subTest(attribute=attribute):
                stream = FakeStream(interval=0.15)
                setattr(stream, attribute, value)
                summary = hotplug.build_summary(
                    campaign="matrix",
                    cycles=1,
                    transitions=transitions,
                    stream=stream,
                    preflight=snapshot(),
                    closing=snapshot(),
                    aborted=None,
                    fast_limit_s=30.0,
                    max_limit_s=60.0,
                    artifact_dir=Path("unused"),
                    commit="a" * 40,
                    provenance=passing_provenance(),
                )
                self.assertFalse(summary["sampling_gate"]["sequence_complete"])
                self.assertEqual(summary["verdict"], "INCONCLUSIVE")
                self.assertNotEqual(summary["qualification_gate"], "PASS")

    def test_stop_observes_a_tail_nonzero_exit_before_cleanup(self) -> None:
        class EndingProcess:
            def __init__(self) -> None:
                self.waited = False

            def poll(self):
                return 255 if self.waited else None

            def wait(self, *, timeout):
                self.waited = True
                return 255

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stream = hotplug.SshSampleStream(
                host="unused",
                remote_repo="/unused",
                source_path=root / "unused.py",
                timeline_path=root / "samples.jsonl",
                interval=0.15,
                duration=60.0,
                status_path=None,
                checkpoint=hotplug.Checkpoint(root / "checkpoint.json", {}),
            )
            stream._process = EndingProcess()  # type: ignore[assignment]
            stream.stream_stopped = True
            stream.stop()

        self.assertEqual(stream.remote_returncode, 255)
        self.assertTrue(
            any("remote sampler exited 255" in item for item in stream.fatal_errors)
        )

    def test_stop_observes_nonzero_exit_after_grace_wait_timeout(self) -> None:
        class TailFailureProcess:
            def __init__(self) -> None:
                self.polls = iter((None, None, 255, 255))

            def poll(self):
                return next(self.polls, 255)

            def wait(self, *, timeout):
                raise subprocess.TimeoutExpired("ssh", timeout)

            def terminate(self):
                raise AssertionError("a failed process must not be terminated as live")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stream = hotplug.SshSampleStream(
                host="unused",
                remote_repo="/unused",
                source_path=root / "unused.py",
                timeline_path=root / "samples.jsonl",
                interval=0.15,
                duration=60.0,
                status_path=None,
                checkpoint=hotplug.Checkpoint(root / "checkpoint.json", {}),
            )
            stream._process = TailFailureProcess()  # type: ignore[assignment]
            stream.stream_stopped = True
            stream.stop()

        self.assertEqual(stream.remote_returncode, 255)
        self.assertTrue(
            any("remote sampler exited 255" in item for item in stream.fatal_errors)
        )

    def test_stop_observes_nonzero_exit_during_termination_race(self) -> None:
        class TerminationRaceProcess:
            def poll(self):
                return None

            def wait(self, *, timeout):
                if timeout == 0.25:
                    raise subprocess.TimeoutExpired("ssh", timeout)
                return 255

            def terminate(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stream = hotplug.SshSampleStream(
                host="unused",
                remote_repo="/unused",
                source_path=root / "unused.py",
                timeline_path=root / "samples.jsonl",
                interval=0.15,
                duration=60.0,
                status_path=None,
                checkpoint=hotplug.Checkpoint(root / "checkpoint.json", {}),
            )
            stream._process = TerminationRaceProcess()  # type: ignore[assignment]
            stream.stream_stopped = True
            stream.stop()

        self.assertEqual(stream.remote_returncode, 255)
        self.assertTrue(
            any("remote sampler exited 255" in item for item in stream.fatal_errors)
        )

    def test_measured_gap_uses_quarter_second_evidence_limit(self) -> None:
        transitions = [
            {
                "gate_kind": gate_kind,
                "outcome": "completed",
                "timing_origin": hotplug.core.USB_TIMING_ORIGIN,
                "transition_latency_s": 1.0,
                "safety_clean": True,
                "restart_clean": True,
            }
            for gate_kind in ("promotion", "fallback", "fifine_replug")
        ]
        stream = FakeStream(interval=0.15)
        stream.max_remote_gap = 0.24
        passing = hotplug.build_summary(
            campaign="matrix",
            cycles=1,
            transitions=transitions,
            stream=stream,
            preflight=snapshot(),
            closing=snapshot(),
            aborted=None,
            fast_limit_s=30.0,
            max_limit_s=60.0,
            artifact_dir=Path("unused"),
            commit="a" * 40,
            provenance=passing_provenance(),
        )
        stream.max_remote_gap = 0.251
        failing = hotplug.build_summary(
            campaign="matrix",
            cycles=1,
            transitions=transitions,
            stream=stream,
            preflight=snapshot(),
            closing=snapshot(),
            aborted=None,
            fast_limit_s=30.0,
            max_limit_s=60.0,
            artifact_dir=Path("unused"),
            commit="a" * 40,
            provenance=passing_provenance(),
        )

        self.assertEqual(passing["sampling_gate"]["verdict"], "PASS")
        self.assertEqual(passing["sampling_gate"]["required_max_measured_gap_s"], 0.25)
        self.assertEqual(failing["sampling_gate"]["verdict"], "INCONCLUSIVE")

    def test_start_failure_still_writes_honest_structured_artifacts(self) -> None:
        class StartFailureStream(FakeStream):
            def __init__(self, **kwargs) -> None:
                super().__init__(**kwargs)
                self.stream_started = False
                self.total_samples = 0

            def start(self) -> None:
                raise RuntimeError("start exploded")

        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            with (
                mock.patch.object(hotplug, "SshSampleStream", StartFailureStream),
                mock.patch.object(hotplug, "git_commit", return_value="a" * 40),
                mock.patch.object(
                    hotplug, "collect_provenance", return_value=passing_provenance()
                ),
            ):
                result = hotplug.main(
                    ["--run-dir", str(run_dir)],
                    backend=FakeBackend(),
                )
            summary = json.loads((run_dir / "summary.json").read_text())
            manifest = json.loads((run_dir / "evidence-manifest.json").read_text())

            self.assertEqual(result, 1)
            self.assertIn("RuntimeError: start exploded", summary["aborted"])
            self.assertEqual(summary["verdict"], "INCONCLUSIVE")
            self.assertNotIn("preflight", summary["artifacts"])
            self.assertNotIn("closing", summary["artifacts"])
            self.assertNotIn("samples", summary["artifacts"])
            self.assertTrue(
                all(
                    (run_dir / name).is_file() for name in summary["artifacts"].values()
                )
            )
            self.assertEqual(
                {item["path"] for item in manifest["files"]},
                {
                    name
                    for name in summary["artifacts"].values()
                    if name != hotplug.MANIFEST_NAME
                },
            )

    def test_artifact_map_lists_only_files_that_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "checkpoint.json").write_text("{}\n", encoding="utf-8")
            (root / "preflight.json").write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                hotplug.artifact_map(root),
                {
                    "preflight": "preflight.json",
                    "checkpoint": "checkpoint.json",
                },
            )

    def test_existing_run_directory_returns_structured_non_destructive_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "existing"
            run_dir.mkdir()
            sentinel = run_dir / "sentinel.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            stdout = mock.MagicMock()
            stderr = mock.MagicMock()
            with (
                mock.patch.object(hotplug, "git_commit", return_value="a" * 40),
                mock.patch.object(hotplug.sys, "stdout", stdout),
                mock.patch.object(hotplug.sys, "stderr", stderr),
            ):
                result = hotplug.main(["--run-dir", str(run_dir)])

            payload = "".join(call.args[0] for call in stdout.write.call_args_list)
            document = json.loads(payload)
            self.assertEqual(result, 2)
            self.assertEqual(document["failure"]["type"], "OutputDirectoryExists")
            self.assertFalse(document["physical_evidence_claimed"])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_one_actionable_safe_cycle_does_not_fail_global_evidence_gate(self) -> None:
        stream = FakeStream(interval=0.15)
        transitions = []
        for kind in ("promotion", "fallback"):
            transitions.extend(valid_timing_transition(kind) for _ in range(19))
            transitions.append(valid_timing_transition(kind, outcome="safe_state"))
        summary = hotplug.build_summary(
            campaign="promotion-fallback",
            cycles=20,
            transitions=transitions,
            stream=stream,
            preflight=snapshot(),
            closing=snapshot(),
            aborted=None,
            fast_limit_s=30.0,
            max_limit_s=60.0,
            artifact_dir=Path("unused"),
            commit="abc123",
            provenance=passing_provenance(),
        )
        self.assertEqual(summary["verdict"], "PASS")
        self.assertEqual(summary["qualification_gate"], "PASS")


if __name__ == "__main__":
    unittest.main()
