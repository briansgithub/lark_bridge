from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "e17_harness.py"
SPEC = importlib.util.spec_from_file_location("e17_harness_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        pass


class FakeRunner:
    def __init__(self, results: list[harness.CommandResult] | None = None) -> None:
        self.results = results or []
        self.commands: list[list[str]] = []
        self.processes: list[FakeProcess] = []

    def run(self, command: list[str], *, timeout: float = 45.0) -> harness.CommandResult:
        self.commands.append(command)
        if not self.results:
            raise AssertionError(f"unexpected command: {command}")
        result = self.results.pop(0)
        return harness.CommandResult(command, result.returncode, result.stdout, result.stderr)

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
            instance = harness.E17Harness.create(Path(temporary) / "run", runner, execute=True)
            self.assertEqual(instance._targeted_btmon("selection"), [])

    def test_first_transport_red_and_exact_fault_allowlist(self) -> None:
        window = harness.FaultWindow("output", "2026-08-24T12:00:00Z", "2026-08-24T12:01:00Z")
        allowed = {"role": "output", "kind": "teardown", "timestamp_utc": "2026-08-24T12:00:30Z", "detail": "controller reset"}
        off_target = {"role": "call", "kind": "teardown", "timestamp_utc": "2026-08-24T12:00:30Z", "detail": "controller reset"}
        self.assertEqual(harness.transport_failures([allowed, off_target], window), ["controller reset"])

    def test_objective_reads_nested_aec_metrics_and_all_thresholds(self) -> None:
        preflight = {"verdict": "ready", "level_margin_db": 10.0, "tone_margin_db": 10.0, "active": {"peak_dbfs": -0.2, "clipped_pct": 0.01}}
        aec = {"metrics": {"suppression_db": 10.0}}
        self.assertEqual(harness.objective_failures(preflight, aec, 3.0), [])
        self.assertIn("AEC suppression is below 10 dB", harness.objective_failures(preflight, {"metrics": {"suppression_db": 9.9}}, 3.0))
        self.assertIn("output recovery 3.010s exceeds 3s", harness.objective_failures(preflight, aec, 3.01))

    def test_candidate_mutation_before_selection_is_red(self) -> None:
        events = [{"operation": "Pair", "timestamp_utc": "2026-08-24T12:00:01Z"}]
        self.assertEqual(harness.mutation_failures(events, "2026-08-24T12:00:02Z"), ["candidate Pair occurred before Pixel selection"])


class ScenarioGraphTests(unittest.TestCase):
    def make_plan(self, temporary: str) -> harness.E17Harness:
        return harness.E17Harness.create(Path(temporary) / "run", FakeRunner(), stereo_mix="Stereo Mix")

    def test_success_graph_runs_ten_cycles_and_abbreviated_soak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.make_plan(temporary).run_graph(300)
            self.assertEqual(list(state["scenarios"]), harness.scenario_names(300))
            self.assertTrue(all(item["status"] == "completed" for item in state["scenarios"].values()))
            self.assertEqual(state["scenarios"]["soak"]["classification"], "abbreviated-not-endurance-qualified")
            self.assertFalse(state["scenarios"]["soak"]["endurance_qualified"])

    def test_full_soak_is_supported_but_marked_phase7_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.make_plan(temporary).run_graph(3600)
            self.assertEqual(state["scenarios"]["soak"]["classification"], "full-phase7-required")
            self.assertFalse(state["scenarios"]["soak"]["endurance_qualified"])

    def test_resume_converts_interrupted_running_scenario_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            instance = harness.E17Harness.create(directory, FakeRunner(), stereo_mix="Stereo Mix")
            instance._mark("cycle-04", "running")
            resumed = harness.E17Harness.resume(directory, FakeRunner(), stereo_mix="Stereo Mix")
            self.assertEqual(resumed.state["scenarios"]["cycle-04"]["status"], "pending")
            self.assertTrue(resumed.state["scenarios"]["cycle-04"]["interrupted"])

    def test_plan_mode_never_runs_or_starts_external_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = FakeRunner()
            harness.E17Harness.create(Path(temporary) / "run", runner, stereo_mix="Stereo Mix").run_graph(300)
            self.assertEqual(runner.commands, [])

    def test_windows_stereo_mix_lifecycle_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = FakeRunner([harness.CommandResult([], 0, '"Stereo Mix (Realtek)"')])
            instance = harness.E17Harness.create(Path(temporary) / "run", runner, execute=True)
            self.assertEqual(instance.discover_stereo_mix(), ["Stereo Mix (Realtek)"])
            instance.start_keepalive(Path("tone.wav"))
            instance.start_light_monitor(5)
            instance.start_stereo_mix_capture(5)
            instance.cleanup()
            self.assertTrue(all(process.terminated for process in runner.processes))

    def test_protected_android_ui_pauses_for_exactly_one_human_tap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = FakeRunner([
                harness.CommandResult([], 0),
                harness.CommandResult([], 0, '<node text="Pair with LarkBridge"/>'),
            ])
            instance = harness.E17Harness.create(Path(temporary) / "run", runner, execute=True)
            self.assertFalse(instance._pixel_selection_attempt("selection-proof"))
            self.assertEqual(instance.state["human_action"]["state"], "pixel_protected_confirmation")
            self.assertEqual(len(runner.commands), 2)
            self.assertIn("uiautomator", " ".join(runner.commands[1]))
            self.assertNotIn("screencap", " ".join(" ".join(command) for command in runner.commands))

    def test_graph_stops_on_first_red_transport_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = json.dumps({"ready": True})
            runner = FakeRunner([
                harness.CommandResult([], 0, ready),
                harness.CommandResult([], 0, '"Stereo Mix"'),
                harness.CommandResult([], 0),
                harness.CommandResult([], 0),
                harness.CommandResult([], 0),
                harness.CommandResult([], 0),
                harness.CommandResult([], 0),
                harness.CommandResult([], 0, "<node/>"),
                harness.CommandResult([], 124, "controller reset"),
            ])
            instance = harness.E17Harness.create(Path(temporary) / "run", runner, execute=True)
            with self.assertRaisesRegex(harness.RedGate, "red transport gate"):
                instance.run_graph(300)
            self.assertEqual(instance.state["first_red"]["scenario"], "selection-proof")


if __name__ == "__main__":
    unittest.main()
