from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).resolve().parents[1] / "bootctl.py"
SPEC = importlib.util.spec_from_file_location("bootctl_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
bootctl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootctl
SPEC.loader.exec_module(bootctl)


class BootCtlTests(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(bootctl.percentile([1, 2, 3], 0.5), 2)
        self.assertEqual(bootctl.percentile([0, 10], 0.95), 9.5)

    def test_command_expansion_does_not_use_a_shell(self):
        self.assertEqual(
            bootctl.expanded(("relay", "--log={output}"), output="run dir/serial.log"),
            ["relay", "--log=run dir/serial.log"],
        )

    def test_config_rejects_string_command(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "inventory.toml"
            inventory.write_text(
                'pi_host = "pi"\nboot_power_on_command = "unsafe shell"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                bootctl.Config.load(inventory)

    def test_bootstrap_detects_large_improvement(self):
        low, high = bootctl.bootstrap_median_delta(
            [20, 20.1, 19.9, 20.2, 19.8],
            [15, 15.1, 14.9, 15.2, 14.8],
            samples=500,
        )
        self.assertGreater(low, 4)
        self.assertGreater(high, low)

    def test_functional_result_requires_both_watermarks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "functional-result.json"
            value = {
                "schema_version": 1,
                "run_id": "run-1",
                "pass": True,
                "call_active": True,
                "lark_to_far_end": {"watermark": "mark", "detected": True},
                "far_end_to_output": {"watermark": "mark", "detected": True},
                "feedback_detected": False,
                "dropouts": 0,
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertTrue(
                bootctl.validate_functional_result(path, "run-1", "mark")["pass"]
            )
            value["far_end_to_output"]["detected"] = False
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                bootctl.validate_functional_result(path, "run-1", "mark")

    def test_health_summary_counts_known_failures(self):
        result = bootctl.summarize_health(
            "PipeWire xrun\nBluetooth hci0 command timed out\nEXT4-fs error\n"
        )
        self.assertEqual(result["xrun"], 1)
        self.assertEqual(result["hci_failure"], 1)
        self.assertEqual(result["filesystem"], 1)

    def test_load_results_can_filter_boot_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, mode in enumerate(("warm", "cold")):
                run = root / f"boot-run-{index}"
                run.mkdir()
                (run / "result.json").write_text(
                    json.dumps(
                        {
                            "candidate": "base",
                            "mode": mode,
                            "verdict": "PASS",
                            "readiness_level": "idle",
                            "timings_s": {"idle_ready": 10 + index},
                        }
                    ),
                    encoding="utf-8",
                )
            runs, values, level = bootctl.load_results(root, "base", "warm")
            self.assertEqual(len(runs), 1)
            self.assertEqual(values, [10.0])
            self.assertEqual(level, "idle")

    def test_screen_requires_ten_randomized_pairs(self):
        with self.assertRaises(ValueError):
            bootctl.screen(
                None,
                baseline_label="base",
                baseline_revision="base-rev",
                candidate_label="candidate",
                candidate_revision="candidate-rev",
                pairs=9,
                mode="warm",
                require_functional=False,
                seed=0,
            )

    def test_compare_rejects_more_health_affected_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, timing in (("base", 20.0), ("candidate", 10.0)):
                for index in range(10):
                    run = root / f"boot-run-{label}-{index}"
                    run.mkdir()
                    hci_failures = (
                        4 if index == 0 or (label == "candidate" and index == 1) else 0
                    )
                    (run / "result.json").write_text(
                        json.dumps(
                            {
                                "candidate": label,
                                "mode": "warm",
                                "verdict": "PASS",
                                "readiness_level": "functional",
                                "timings_s": {"functional_ready": timing},
                                "health_events": {"hci_failure": hci_failures},
                            }
                        ),
                        encoding="utf-8",
                    )
            output = io.StringIO()
            with redirect_stdout(output):
                verdict = bootctl.compare(
                    SimpleNamespace(artifacts=root),
                    "base",
                    "candidate",
                    False,
                    "warm",
                )
            self.assertEqual(verdict, 1)
            report = json.loads(output.getvalue())
            self.assertEqual(
                report["health_regressions"]["hci_failure"]["candidate_affected_runs"],
                2,
            )

    def test_compare_labels_idle_improvement_as_provisional(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, timing in (("base", 20.0), ("candidate", 10.0)):
                for index in range(10):
                    run = root / f"boot-run-{label}-{index}"
                    run.mkdir()
                    (run / "result.json").write_text(
                        json.dumps(
                            {
                                "candidate": label,
                                "mode": "warm",
                                "verdict": "PASS",
                                "readiness_level": "idle",
                                "timings_s": {"idle_ready": timing},
                                "health_events": {},
                            }
                        ),
                        encoding="utf-8",
                    )
            output = io.StringIO()
            with redirect_stdout(output):
                verdict = bootctl.compare(
                    SimpleNamespace(artifacts=root),
                    "base",
                    "candidate",
                    True,
                    "warm",
                )
            self.assertEqual(verdict, 1)
            self.assertEqual(
                json.loads(output.getvalue())["verdict"],
                "PROVISIONAL_IDLE_IMPROVEMENT",
            )


if __name__ == "__main__":
    unittest.main()
