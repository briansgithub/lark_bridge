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


class FakeRunner:
    def __init__(self, results: list[harness.CommandResult]) -> None:
        self.results = results
        self.commands: list[list[str]] = []

    def run(self, command: list[str], *, timeout: float = 45.0) -> harness.CommandResult:
        self.commands.append(command)
        if not self.results:
            raise AssertionError(f"unexpected command: {command}")
        result = self.results.pop(0)
        return harness.CommandResult(command, result.returncode, result.stdout, result.stderr)


class ObjectiveGatesTests(unittest.TestCase):
    def test_thresholds_reject_margin_clipping_and_aec_regressions(self) -> None:
        preflight = {
            "verdict": "ready", "level_margin_db": 9.9, "tone_margin_db": 10.0,
            "active": {"peak_dbfs": -0.2, "clipped_pct": 0.02},
        }
        failures = harness.evaluate_objective_audio(preflight, {"verdict": "PASS", "suppression_db": 9.9})
        self.assertEqual(len(failures), 3)
        self.assertEqual(harness.validate_recovery(3.01), ["output recovery 3.010s exceeds 3s"])

    def test_precise_fault_window_is_target_only(self) -> None:
        window = harness.FaultWindow("output", "2026-08-24T12:00:00Z", "2026-08-24T12:01:00Z")
        allowed = {"role": "output", "kind": "teardown", "timestamp_utc": "2026-08-24T12:00:30Z", "detail": "controller reset"}
        wrong = {"role": "call", "kind": "teardown", "timestamp_utc": "2026-08-24T12:00:30Z", "detail": "controller reset"}
        outside = {"role": "output", "kind": "teardown", "timestamp_utc": "2026-08-24T12:01:01Z", "detail": "controller reset"}
        self.assertTrue(harness.fault_event_allowed(allowed, window))
        self.assertEqual(harness.transport_failures([allowed, wrong, outside], window), ["controller reset", "controller reset"])

    def test_candidate_is_never_mutated_before_pixel_selection(self) -> None:
        events = [
            {"operation": "Pair", "timestamp_utc": "2026-08-24T12:00:01Z"},
            {"operation": "ConnectProfile", "timestamp_utc": "2026-08-24T12:00:03Z"},
        ]
        self.assertEqual(
            harness.candidate_mutation_failures(events, selected_at_utc="2026-08-24T12:00:02Z"),
            ["candidate Pair occurred before Pixel selection"],
        )


class StateAndOrchestrationTests(unittest.TestCase):
    def test_plan_only_creates_resumable_machine_readable_state_without_devices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            runner = FakeRunner([])
            instance = harness.E17Harness.create(directory, label="unused", resume=None, runner=runner, host="pi", adb="adb", execute=False, stereo_mix="Stereo Mix")
            instance.resolve_roles()
            instance.capture_evidence()
            instance.mark("baseline", "ready")
            self.assertEqual(runner.commands, [])
            state = json.loads((directory / "state.json").read_text())
            self.assertEqual(state["phases"]["baseline"]["status"], "ready")
            resumed = harness.E17Harness.create(None, label="unused", resume=directory, runner=runner, host="pi", adb="adb", execute=False, stereo_mix="Stereo Mix")
            self.assertEqual(resumed.state["schema"], 1)

    def test_preflight_pauses_only_for_explicit_speaker_wake_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            runner = FakeRunner([harness.CommandResult([], 78, "", "")])
            instance = harness.E17Harness.create(directory, label="unused", resume=None, runner=runner, host="pi", adb="adb", execute=True, stereo_mix="Stereo Mix")
            self.assertIsNone(instance.preflight_speaker("speaker"))
            self.assertEqual(instance.state["phases"]["speaker"]["reason"], "speaker_wake_required")

    def test_lightweight_monitor_never_substitutes_long_btmon_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            runner = FakeRunner([harness.CommandResult([], 0)])
            instance = harness.E17Harness.create(directory, label="unused", resume=None, runner=runner, host="pi", adb="adb", execute=True, stereo_mix="Stereo Mix")
            instance.lightweight_monitor("soak", 5)
            command = " ".join(runner.commands[0])
            self.assertIn("invariants.py --json", command)
            self.assertNotIn("btmon", command)

    def test_pixel_protection_uses_hierarchy_not_screenshot_and_records_one_tap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            runner = FakeRunner([
                harness.CommandResult([], 0),
                harness.CommandResult([], 0, '<node text="Pair with LarkBridge"/>'),
            ])
            instance = harness.E17Harness.create(directory, label="unused", resume=None, runner=runner, host="pi", adb="adb", execute=True, stereo_mix="Stereo Mix")
            self.assertEqual(instance.pixel_pairing_attempt(), "human_tap_required")
            self.assertEqual(instance.state["human_action"]["state"], "pixel_protected_confirmation")
            flattened = " ".join(" ".join(command) for command in runner.commands)
            self.assertIn("uiautomator dump", flattened)
            self.assertNotIn("screencap", flattened)


if __name__ == "__main__":
    unittest.main()
