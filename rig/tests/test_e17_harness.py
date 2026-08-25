from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "e17_harness.py"
SPEC = importlib.util.spec_from_file_location("e17_harness_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.waited = 0

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        self.waited += 1
        return 0

    def kill(self) -> None:
        pass


class FakeRunner:
    def __init__(self, results: list[harness.CommandResult] | None = None) -> None:
        self.results = results or []
        self.commands: list[list[str]] = []
        self.processes: list[FakeProcess] = []

    def run(
        self, command: list[str], *, timeout: float = 45.0
    ) -> harness.CommandResult:
        self.commands.append(command)
        if not self.results:
            raise AssertionError(f"unexpected command: {command}")
        result = self.results.pop(0)
        return harness.CommandResult(
            command, result.returncode, result.stdout, result.stderr
        )

    def start(self, command: list[str]) -> FakeProcess:
        self.commands.append(command)
        process = FakeProcess()
        self.processes.append(process)
        return process


class ParsingTests(unittest.TestCase):
    def test_malformed_object_is_red(self) -> None:
        with self.assertRaisesRegex(harness.RedGate, "malformed JSON"):
            harness.parse_object("not json", "fixture")

    def test_timeout_exit_is_accepted_only_for_targeted_btmon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = FakeRunner([harness.CommandResult([], 124, "", "")])
            instance = harness.E17Harness.create(
                Path(temporary) / "run", runner, execute=True
            )
            self.assertEqual(instance._targeted_btmon("selection"), [])

    def test_first_transport_red_and_exact_fault_allowlist(self) -> None:
        window = harness.FaultWindow(
            "output", "2026-08-24T12:00:00Z", "2026-08-24T12:01:00Z"
        )
        allowed = {
            "role": "output",
            "kind": "teardown",
            "timestamp_utc": "2026-08-24T12:00:30Z",
            "detail": "controller reset",
        }
        off_target = {
            "role": "call",
            "kind": "teardown",
            "timestamp_utc": "2026-08-24T12:00:30Z",
            "detail": "controller reset",
        }
        self.assertEqual(
            harness.transport_failures([allowed, off_target], window),
            ["controller reset"],
        )

    def test_light_monitor_rejects_pi_usb_host_transfer_loss(self) -> None:
        failures = (
            (
                "Aug 25 05:00:23.641311 larkbridge kernel: Transfer to device 7 "
                "endpoint 0x3 frame 1204 failed - FIQ reported NYET. Data may have been lost."
            ),
            "Aug 25 05:00:23.641546 larkbridge kernel: FIQ reported NYET.",
            (
                "Aug 25 05:00:23.641700 larkbridge kernel: Transfer to device 4 "
                "endpoint 0x7 frame 1204 failed. Data may have been lost."
            ),
        )
        for index, line in enumerate(failures):
            with self.subTest(line=line), tempfile.TemporaryDirectory() as temporary:
                runner = FakeRunner(
                    [
                        harness.CommandResult(
                            [], 0, f'E17_SAMPLE {{"state":"ACTIVE"}}\n{line}\n'
                        )
                    ]
                )
                instance = harness.E17Harness.create(
                    Path(temporary) / f"run-{index}", runner, execute=True
                )
                with self.assertRaisesRegex(harness.RedGate, "red transport gate"):
                    instance.light_monitor(5)
                self.assertIn(harness.KERNEL_ERROR_ERE, runner.commands[0][2])

    def test_light_monitor_ignores_similar_benign_kernel_lines(self) -> None:
        benign = (
            "kernel: Transfer to device cache completed\n"
            "kernel: FIQ handled a NYET retry\n"
            "kernel: device 7 endpoint 0x3 frame 1204 completed"
        )
        with tempfile.TemporaryDirectory() as temporary:
            runner = FakeRunner(
                [
                    harness.CommandResult(
                        [], 0, f'E17_SAMPLE {{"state":"ACTIVE"}}\n{benign}\n'
                    )
                ]
            )
            instance = harness.E17Harness.create(
                Path(temporary) / "run", runner, execute=True
            )
            instance.light_monitor(5)

    def test_objective_reads_nested_aec_metrics_and_all_thresholds(self) -> None:
        preflight = {
            "verdict": "ready",
            "level_margin_db": 10.0,
            "tone_margin_db": 10.0,
            "active": {"peak_dbfs": -0.2, "clipped_pct": 0.01},
        }
        aec = {
            "verdict": "PASS",
            "metrics": {"verdict": "PASS", "suppression_db": 10.0},
        }
        self.assertEqual(harness.objective_failures(preflight, aec, 3.0), [])
        self.assertIn(
            "AEC suppression is below 10 dB",
            harness.objective_failures(
                preflight,
                {
                    "verdict": "PASS",
                    "metrics": {"verdict": "PASS", "suppression_db": 9.9},
                },
                3.0,
            ),
        )
        self.assertIn(
            "output recovery 3.010s exceeds 3s",
            harness.objective_failures(preflight, aec, 3.01),
        )
        self.assertIn(
            "AEC metric tool did not pass",
            harness.objective_failures(
                preflight,
                {
                    "verdict": "FAIL",
                    "metrics": {"verdict": "FAIL", "suppression_db": 12.0},
                },
                3.0,
            ),
        )

    def test_pc_capture_requires_duration_signal_and_safe_clipping(self) -> None:
        good = {
            "duration_s": 300,
            "per_channel": [{"peak_dbfs": -12.0, "clipped_pct": 0.0}],
        }
        self.assertEqual(harness.pc_capture_failures(good, 300), [])
        bad = {
            "duration_s": 2,
            "per_channel": [{"peak_dbfs": -90.0, "clipped_pct": 0.02}],
        }
        self.assertEqual(len(harness.pc_capture_failures(bad, 300)), 3)

    def test_candidate_mutation_before_selection_is_red(self) -> None:
        events = [{"operation": "Pair", "timestamp_utc": "2026-08-24T12:00:01Z"}]
        self.assertEqual(
            harness.mutation_failures(events, "2026-08-24T12:00:02Z"),
            ["candidate Pair occurred before Pixel selection"],
        )
        self.assertEqual(
            harness.mutation_failures(events, "not-a-timestamp"),
            ["Pixel selection lacks a valid timestamp"],
        )

    def test_selection_evidence_window_is_complete_and_non_vacuous(self) -> None:
        events = [
            {"kind": "monitor-start", "timestamp_utc": "2026-08-24T12:00:00Z"},
            {"kind": "pixel-selection", "timestamp_utc": "2026-08-24T12:00:02Z"},
            {"kind": "monitor-end", "timestamp_utc": "2026-08-24T12:00:03Z"},
        ]
        self.assertEqual(
            harness.selection_evidence_failures(events, "2026-08-24T12:00:02Z"),
            [],
        )
        self.assertTrue(harness.selection_evidence_failures([], "2026-08-24T12:00:02Z"))


class ScenarioGraphTests(unittest.TestCase):
    def make_plan(self, temporary: str) -> harness.E17Harness:
        return harness.E17Harness.create(
            Path(temporary) / "run", FakeRunner(), stereo_mix="Stereo Mix"
        )

    def test_success_graph_runs_ten_cycles_and_abbreviated_soak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.make_plan(temporary).run_graph(300)
            self.assertEqual(list(state["scenarios"]), harness.scenario_names(300))
            self.assertTrue(
                all(item["status"] == "planned" for item in state["scenarios"].values())
            )
            self.assertEqual(
                state["scenarios"]["soak"]["classification"],
                "abbreviated-not-endurance-qualified",
            )
            self.assertFalse(state["scenarios"]["soak"]["endurance_qualified"])

    def test_full_soak_is_supported_but_marked_phase7_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.make_plan(temporary).run_graph(3600)
            self.assertEqual(
                state["scenarios"]["soak"]["classification"], "full-phase7-required"
            )
            self.assertFalse(state["scenarios"]["soak"]["endurance_qualified"])
            self.assertTrue(state["scenarios"]["soak"]["endurance_duration_satisfied"])

    def test_resume_converts_interrupted_running_scenario_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            instance = harness.E17Harness.create(
                directory, FakeRunner(), stereo_mix="Stereo Mix"
            )
            instance._mark("cycle-04", "running")
            resumed = harness.E17Harness.resume(
                directory, FakeRunner(), stereo_mix="Stereo Mix"
            )
            self.assertEqual(
                resumed.state["scenarios"]["cycle-04"]["status"], "pending"
            )
            self.assertTrue(resumed.state["scenarios"]["cycle-04"]["interrupted"])

    def test_plan_mode_never_runs_or_starts_external_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = FakeRunner()
            harness.E17Harness.create(
                Path(temporary) / "run", runner, stereo_mix="Stereo Mix"
            ).run_graph(300)
            self.assertEqual(runner.commands, [])

    def test_execute_cannot_resume_a_plan_only_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            harness.E17Harness.create(directory, FakeRunner(), stereo_mix="Stereo Mix")
            with self.assertRaisesRegex(harness.RedGate, "resume mode"):
                harness.E17Harness.resume(directory, FakeRunner(), execute=True)

    def test_windows_stereo_mix_lifecycle_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            monitor = 'E17_SAMPLE {"state":"ACTIVE"}'
            analysis = json.dumps(
                {
                    "duration_s": 5,
                    "per_channel": [{"peak_dbfs": -12.0, "clipped_pct": 0.0}],
                }
            )
            runner = FakeRunner(
                [
                    harness.CommandResult(
                        [], 1, "", '[dshow] "Stereo Mix (Realtek)" (audio)'
                    ),
                    harness.CommandResult([], 0),
                    harness.CommandResult([], 0, monitor),
                    harness.CommandResult([], 0, analysis),
                ]
            )
            instance = harness.E17Harness.create(
                Path(temporary) / "run", runner, execute=True
            )
            self.assertEqual(instance.discover_stereo_mix(), ["Stereo Mix (Realtek)"])
            instance.audio_session("test", 5)
            self.assertTrue(runner.processes[0].terminated)
            self.assertFalse(runner.processes[1].terminated)
            self.assertTrue(runner.processes[2].terminated)
            flattened = " ".join(" ".join(command) for command in runner.commands)
            self.assertNotIn("invariants.py", flattened)
            self.assertNotIn("pw-top", flattened)
            self.assertIn("/tmp/e17-keepalive.wav", flattened)

    def test_protected_android_ui_pauses_for_exactly_one_human_tap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = FakeRunner(
                [
                    harness.CommandResult([], 0),
                    harness.CommandResult([], 0, '<node text="Pair with LarkBridge"/>'),
                ]
            )
            instance = harness.E17Harness.create(
                Path(temporary) / "run", runner, execute=True
            )
            self.assertFalse(instance._pixel_selection_attempt("selection-proof"))
            self.assertEqual(
                instance.state["human_action"]["state"], "pixel_protected_confirmation"
            )
            self.assertEqual(len(runner.commands), 2)
            self.assertIn("uiautomator", " ".join(runner.commands[1]))
            self.assertNotIn(
                "screencap", " ".join(" ".join(command) for command in runner.commands)
            )

    def test_graph_stops_on_first_red_transport_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = json.dumps({"ready": True})
            runner = FakeRunner(
                [
                    harness.CommandResult([], 0, ready),
                    harness.CommandResult([], 1, "", '[dshow] "Stereo Mix" (audio)'),
                    harness.CommandResult([], 0),
                    harness.CommandResult([], 0),
                    harness.CommandResult([], 0),
                    harness.CommandResult([], 0),
                    harness.CommandResult([], 0),
                    harness.CommandResult([], 0, "<node/>"),
                    harness.CommandResult([], 124, "controller reset"),
                ]
            )
            evidence = Path(temporary) / "candidate.jsonl"
            evidence.write_text("", encoding="utf-8")
            instance = harness.E17Harness.create(
                Path(temporary) / "run",
                runner,
                execute=True,
                candidate_events=evidence,
                selected_at_utc="2026-08-24T12:00:02Z",
            )
            with self.assertRaisesRegex(harness.RedGate, "red transport gate"):
                instance.run_graph(300)
            self.assertEqual(instance.state["first_red"]["scenario"], "selection-proof")

            with self.assertRaisesRegex(harness.RedGate, "sealed at first red"):
                instance.run_graph(300)

    def test_plan_graph_reaches_every_cycle_audio_and_call_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            instance = harness.E17Harness.create(
                directory,
                FakeRunner(),
                stereo_mix="Stereo Mix",
                switch_target="a2dp:C9:5C:FD:6E:28:46",
                call_uri="discord://test-call",
            )
            instance.run_graph(300)
            records = [
                json.loads(line)
                for line in (directory / "commands.jsonl").read_text().splitlines()
            ]
            names = [record["name"] for record in records]
            self.assertEqual(
                sum(name.startswith("stereo-mix-analysis-cycle-") for name in names), 10
            )
            self.assertEqual(
                sum(name.startswith("adb-call-start-cycle-") for name in names), 10
            )
            self.assertEqual(
                sum(name.startswith("adb-call-stop-cycle-") for name in names), 10
            )
            self.assertIn("stereo-mix-analysis-soak", names)

    def test_recovery_drives_switch_and_captures_resolved_output_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = json.dumps(
                {
                    "verdict": "PASS",
                    "selection_apply_seconds": 2.5,
                    "uplink_gap": {"seconds": 0.2},
                    "downlink_gap": {"seconds": 2.0},
                }
            )
            runner = FakeRunner(
                [
                    harness.CommandResult([], 0),
                    harness.CommandResult([], 0, report),
                    harness.CommandResult([], 0, "clean capture"),
                ]
            )
            instance = harness.E17Harness.create(
                Path(temporary) / "run",
                runner,
                execute=True,
                switch_target="a2dp:C9:5C:FD:6E:28:46",
            )
            self.assertEqual(instance._recovery_seconds(), 2.5)
            flattened = " ".join(" ".join(command) for command in runner.commands)
            self.assertNotIn("--external-switch", flattened)
            self.assertIn("controller_roles.py --policy final-usb status", flattened)
            self.assertIn(".output.hci", flattened)

    def test_during_call_preserves_the_first_red_when_cleanup_also_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            instance = self.make_plan(temporary)
            with (
                mock.patch.object(instance, "establish_call"),
                mock.patch.object(
                    instance,
                    "teardown_call",
                    side_effect=harness.RedGate("cleanup red"),
                ),
                self.assertRaisesRegex(harness.RedGate, "measurement red"),
            ):
                instance.during_call(
                    "cycle-01",
                    lambda: (_ for _ in ()).throw(harness.RedGate("measurement red")),
                )


if __name__ == "__main__":
    unittest.main()
