from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from rig.bt500_aux import harness


def good_snapshot(*, active: bool = True) -> dict:
    return {
        "timestamp": 1.0,
        "collection_errors": [],
        "controllers": {
            "ready": True,
            "call": {
                "ready": True,
                "configured_address": harness.BT500_ADDRESS,
                "observed_address": harness.BT500_ADDRESS,
                "observed_bus": "USB",
                "observed_usb_id": harness.BT500_USB_ID,
                "hci": "hci7",
            },
            "output": {
                "required": False,
                "configured": False,
                "ready": True,
                "reason": "wired-output",
            },
        },
        "status": {
            "state": "ACTIVE" if active else "CALL_DOWN",
            "mode": "bluetooth-wired",
            "endpoints": {
                "lark": "alsa_input.usb-LARK",
                "wired_output": "alsa_output.platform-aux",
            },
            "wired_output_volume": {
                "required": True,
                "verified": True,
                "desired": 0.85,
                "observed": 0.85,
            },
            "call": {"controller_binding_accepted": active},
            "aec": {
                "enabled": True,
                "verified": active,
                "node_latency_frames": 1920,
            },
            "graph": {"missing_links": [], "unexpected_links": []},
            "system": {"throttled": "throttled=0x0"},
        },
        "services": {
            "system": {
                unit: {"ActiveState": "active", "NRestarts": "0"}
                for unit in (
                    *harness.REQUIRED_SYSTEM_SERVICES,
                    "bridge-tuning.service",
                )
            },
            "user": {
                unit: {"ActiveState": "active", "NRestarts": "0"}
                for unit in harness.REQUIRED_USER_SERVICES
            },
        },
        "watchdog": {"recoveries": 0},
        "graph_quantum": 1024 if active else 2048,
        "transport": {
            "controller_answers": True,
            "acl": active,
            "sco": active,
        },
        "kernel_errors": [],
        "usb_errors": [],
        "health": {"temperature_c": 50.0, "mem_available_kib": 500000},
    }


def good_capture(seconds: float = 60.0) -> dict:
    return {
        "seconds": seconds,
        "links_verified": True,
        "state_before": "ACTIVE",
        "state_after": "ACTIVE",
        "wavs": {
            "bridge.e12.reference": "/tmp/reference.wav",
            "bridge.e12.raw": "/tmp/raw.wav",
            "bridge.e12.clean": "/tmp/clean.wav",
        },
        "pwtop": {
            "echo-cancel-playback": {
                "quantum": 1024,
                "err_delta": 0,
            }
        },
    }


def good_metrics() -> dict:
    return {"verdict": "PASS", "suppression_db": 12.5, "failures": []}


class FakeBackend:
    def __init__(self) -> None:
        self.snapshots: list[dict] = []
        self.capture_calls = 0
        self.capture_failure: BaseException | None = None
        self.metrics_document = good_metrics()
        self.soak_document = {
            "status": "passed",
            "elapsed_s": 10.0,
            "runs": 1,
            "opening": good_snapshot(),
        }
        self.started: list[dict] = []

    def snapshot(self, *, full: bool = True) -> dict:
        if self.snapshots:
            return copy.deepcopy(self.snapshots.pop(0))
        return good_snapshot()

    def capture(self, *, label: str, seconds: float, remote_out: str) -> dict:
        self.capture_calls += 1
        if self.capture_failure is not None:
            failure = self.capture_failure
            self.capture_failure = None
            raise failure
        return good_capture(seconds)

    def metrics(self, capture) -> dict:
        return copy.deepcopy(self.metrics_document)

    def recycle_call(self, *, timeout: float = 75.0) -> dict:
        return {
            "verdict": "PASS",
            "adapter_address": harness.BT500_ADDRESS,
            "call_down_observed": True,
            "active_observed": True,
        }

    def start_soak(
        self,
        *,
        soak_id: str,
        remote_out: str,
        duration: int,
        interval: float,
        resume: bool,
    ) -> dict:
        self.started.append(
            {
                "soak_id": soak_id,
                "remote_out": remote_out,
                "duration": duration,
                "interval": interval,
                "resume": resume,
            }
        )
        return {"status": "running", "unit": soak_id}

    def soak_state(self, remote_out: str) -> dict:
        return copy.deepcopy(self.soak_document)

    def fetch_cycle(self, remote_out: str, local_out: Path) -> None:
        local_out.mkdir(parents=True, exist_ok=True)
        (local_out / "capture.marker").write_text(remote_out, encoding="utf-8")

    def fetch_soak(self, remote_out: str, local_out: Path) -> None:
        local_out.mkdir(parents=True, exist_ok=True)
        (local_out / "state.json").write_text(
            json.dumps(self.soak_document), encoding="utf-8"
        )
        with (local_out / "samples.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(good_snapshot()) + "\n")
            handle.write(json.dumps(good_snapshot()) + "\n")
            handle.write(json.dumps({"event": "finished"}) + "\n")


class CampaignTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "campaign"
        self.store = harness.CampaignStore(self.path)
        self.store.create(target_cycles=5, soak_seconds=10, sample_seconds=5.0)
        self.backend = FakeBackend()
        self.subject = harness.QualificationHarness(self.backend, self.store)

    def test_complete_campaign_is_resumable_and_runs_five_cycles(self) -> None:
        result = self.subject.campaign(seconds=60)
        self.assertEqual(result["baseline"]["status"], "passed")
        self.assertEqual(
            [cycle["status"] for cycle in result["cycles"]],
            ["passed"] * 5,
        )
        self.assertEqual(self.backend.capture_calls, 5)

        resumed = self.subject.campaign(seconds=60)
        self.assertEqual(resumed["cycles"], result["cycles"])
        self.assertEqual(self.backend.capture_calls, 5)

    def test_hardware_readiness_is_checkpointed_without_a_false_failure(self) -> None:
        missing = good_snapshot(active=False)
        missing["status"]["endpoints"]["lark"] = None
        self.backend.snapshots = [missing]
        with self.assertRaises(harness.HardwareNotReady):
            self.subject.baseline()
        checkpoint = self.store.load()
        self.assertEqual(checkpoint["baseline"]["status"], "waiting")
        self.assertEqual(checkpoint["verdict"], "IN_PROGRESS")

    def test_metric_failure_fails_closed_and_keeps_evidence(self) -> None:
        self.subject.baseline()
        self.backend.metrics_document = {
            "verdict": "FAIL",
            "suppression_db": 5.0,
            "failures": ["below gate"],
        }
        with self.assertRaises(harness.HardFailure):
            self.subject.cycle(1)
        checkpoint = self.store.load()
        self.assertEqual(checkpoint["cycles"][0]["status"], "failed")
        attempt = checkpoint["cycles"][0]["attempts"][0]
        artifact = self.path / attempt["artifact"]
        self.assertTrue((artifact / "before.json").is_file())
        self.assertTrue((artifact / "aec-metrics.json").is_file())

    def test_timeout_is_an_evidence_failure_not_hardware_absence(self) -> None:
        self.subject.baseline()
        self.backend.capture_failure = harness.EvidenceError("remote command timed out")
        with self.assertRaises(harness.EvidenceError):
            self.subject.cycle(1)
        checkpoint = self.store.load()
        self.assertEqual(checkpoint["cycles"][0]["status"], "failed")
        self.assertIn("timed out", checkpoint["cycles"][0]["reason"])

    def test_interrupted_cycle_retries_same_index_on_resume(self) -> None:
        self.subject.baseline()
        self.backend.capture_failure = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.subject.cycle(1)
        interrupted = self.store.load()["cycles"][0]
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(len(interrupted["attempts"]), 1)

        resumed = self.subject.cycle(1)
        self.assertEqual(resumed["cycles"][0]["status"], "passed")
        self.assertEqual(len(resumed["cycles"][0]["attempts"]), 2)

    def test_malformed_snapshot_never_becomes_a_pass(self) -> None:
        self.backend.snapshots = [{"status": {}}]
        with self.assertRaises(harness.EvidenceError):
            self.subject.baseline()
        self.assertEqual(self.store.load()["baseline"]["status"], "failed")

    def test_checkpoint_corruption_stops_resume(self) -> None:
        self.store.checkpoint_path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(harness.EvidenceError):
            self.store.load()

    def test_soak_requires_all_cycles_then_collects_checksummed_evidence(self) -> None:
        self.subject.campaign(seconds=60)
        started = self.subject.start_soak()
        self.assertEqual(started["soak"]["status"], "running")
        self.assertEqual(self.backend.started[0]["duration"], 10)
        completed = self.subject.collect()
        self.assertEqual(completed["verdict"], "PASS")
        manifest = json.loads(
            (self.path / "evidence-manifest.json").read_text(encoding="utf-8")
        )
        names = {entry["path"] for entry in manifest["files"]}
        self.assertIn("checkpoint.json", names)
        self.assertIn("soak/evidence/samples.jsonl", names)

    def test_collect_rejects_malformed_soak_line(self) -> None:
        self.subject.campaign(seconds=60)
        self.subject.start_soak()

        def malformed(_remote: str, local: Path) -> None:
            local.mkdir(parents=True)
            (local / "state.json").write_text(
                json.dumps({"status": "passed", "elapsed_s": 10}),
                encoding="utf-8",
            )
            (local / "samples.jsonl").write_text("{bad\n", encoding="utf-8")

        self.backend.fetch_soak = malformed  # type: ignore[method-assign]
        with self.assertRaises(harness.EvidenceError):
            self.subject.collect()


class ValidationTests(unittest.TestCase):
    def test_new_kernel_or_usb_error_fails_cycle(self) -> None:
        before = good_snapshot()
        after = good_snapshot()
        after["kernel_errors"] = ["Bluetooth: hci7 command tx timeout"]
        with self.assertRaises(harness.HardFailure):
            harness.validate_cycle(
                before,
                good_capture(),
                good_metrics(),
                after,
                60.0,
            )

    def test_service_restart_fails_cycle(self) -> None:
        before = good_snapshot()
        after = good_snapshot()
        after["services"]["user"]["pipewire.service"]["NRestarts"] = "1"
        with self.assertRaises(harness.HardFailure):
            harness.validate_cycle(
                before,
                good_capture(),
                good_metrics(),
                after,
                60.0,
            )

    def test_cli_uses_exit_78_for_readiness(self) -> None:
        missing = good_snapshot(active=False)
        missing["status"]["endpoints"]["lark"] = None
        backend = FakeBackend()
        backend.snapshots = [missing]
        with tempfile.TemporaryDirectory() as directory:
            code = harness.main(
                [
                    "baseline",
                    "--campaign",
                    str(Path(directory) / "campaign"),
                ],
                backend=backend,
            )
        self.assertEqual(code, harness.EXIT_HARDWARE_READY)


if __name__ == "__main__":
    unittest.main()
