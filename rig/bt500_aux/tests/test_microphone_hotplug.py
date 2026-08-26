from __future__ import annotations

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

    def test_provenance_records_full_dirty_state_and_source_hashes(self) -> None:
        remote = {
            "status": "PASS",
            "repository": {
                "path": "/srv/bridge",
                "head": "b" * 40,
                "dirty": False,
                "porcelain": [],
            },
            "installed_files": {
                "invariants": {
                    "path": "/srv/bridge/rig/pi/measure/invariants.py",
                    "exists": True,
                    "sha256": "c" * 64,
                },
                "bridge_config": {
                    "path": "/srv/bridge/config/bridge.toml",
                    "exists": True,
                    "sha256": "d" * 64,
                },
                "remote_snapshot": {"exists": True, "sha256": "1" * 64},
                "remote_harness": {"exists": True, "sha256": "5" * 64},
                "bridge_supervisor": {"exists": True, "sha256": "2" * 64},
                "microphone_resolver": {"exists": True, "sha256": "3" * 64},
                "output_resolver": {"exists": True, "sha256": "4" * 64},
                "controller_roles": {"exists": True, "sha256": "6" * 64},
                "btadapters": {"exists": True, "sha256": "7" * 64},
            },
            "deployed_release": {
                "path": "/etc/larkbridge/DEPLOYED.json",
                "exists": True,
                "sha256": "e" * 64,
                "document": {
                    "commit": "b" * 40,
                    "archive_sha256": "f" * 64,
                },
            },
            "errors": [],
        }

        def runner(command, **_kwargs):
            if command[0] == "git":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=" M rig/bt500_aux/microphone_hotplug.py\n",
                    stderr="",
                )
            self.assertEqual(command[0], "ssh")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(remote),
                stderr="",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            harness_source = root / "host.py"
            harness_library = root / "harness.py"
            sampler_source = root / "sampler.py"
            local_invariants = root / "invariants.py"
            harness_source.write_bytes(b"host source\n")
            harness_library.write_bytes(b"harness library\n")
            sampler_source.write_bytes(b"sampler source\n")
            local_invariants.write_bytes(b"local invariants\n")
            document = hotplug.collect_provenance(
                host="bridge",
                remote_repo="/srv/bridge",
                sampler_source=sampler_source,
                host_source=harness_source,
                harness_library_source=harness_library,
                local_invariants_source=local_invariants,
                repo=root,
                commit="a" * 40,
                runner=runner,
            )

        self.assertEqual(document["status"], "PASS")
        self.assertEqual(document["local_repository"]["head"], "a" * 40)
        self.assertTrue(document["local_repository"]["dirty"])
        self.assertEqual(
            document["local_sources"]["host_harness"]["sha256"],
            hashlib.sha256(b"host source\n").hexdigest(),
        )
        self.assertEqual(
            document["local_sources"]["streamed_sampler"]["sha256"],
            hashlib.sha256(b"sampler source\n").hexdigest(),
        )
        self.assertEqual(document["remote"], remote)

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

            with mock.patch.object(
                stream,
                "_query_service_counts",
                side_effect=query_counts,
            ) as query:
                action = stream.mark_action("lark_promotion", 1, "plug")
                result_counts = stream.service_counts()

        self.assertEqual(query.call_count, 2)
        self.assertEqual(observed_boundaries[0], ("preflight", None))
        self.assertEqual(observed_boundaries[1][0], "lark_promotion")
        self.assertEqual(action["service_restart_counts"], service_counts(2))
        self.assertEqual(result_counts, service_counts(3))

    def test_ingest_cannot_relabel_a_pre_action_sample(self) -> None:
        entered = threading.Event()
        release = threading.Event()

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
            sample = {
                "type": "sample",
                "seq": 1,
                "elapsed_s": 0.15,
                "microphone": None,
                "candidates": BlockingCandidates({"candidate": {}}),
                "invariants": {"passed": True, "violations": []},
                "sampling": {"start_gap_s": 0.15},
                "service_restart_counts": service_counts(),
            }
            thread = threading.Thread(target=stream.ingest, args=(sample,))
            thread.start()
            self.assertTrue(entered.wait(2))
            with mock.patch.object(
                stream,
                "_query_service_counts",
                return_value=(service_counts(), None),
            ):
                action = stream.mark_action("lark_promotion", 1, "plug")
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            ingested = stream.latest()

        self.assertIsNotNone(ingested)
        assert ingested is not None
        self.assertEqual(action["after_seq"], 1)
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
                transitions.append(
                    {
                        "gate_kind": gate_kind,
                        "outcome": "completed",
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
            transitions.extend(
                {
                    "gate_kind": kind,
                    "outcome": "completed",
                    "transition_latency_s": 30.0,
                    "safety_clean": True,
                    "restart_clean": True,
                }
                for _ in range(19)
            )
            transitions.append(
                {
                    "gate_kind": kind,
                    "outcome": "safe_state",
                    "transition_latency_s": None,
                    "safety_clean": True,
                    "restart_clean": True,
                }
            )
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
