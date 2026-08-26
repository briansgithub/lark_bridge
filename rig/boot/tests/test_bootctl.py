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
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "bootctl.py"
SPEC = importlib.util.spec_from_file_location("bootctl_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
bootctl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootctl
SPEC.loader.exec_module(bootctl)


def microphone_probe(
    candidate_id: str,
    *,
    boot_id: str = "new-boot",
    ready: bool = True,
) -> dict:
    node = (
        "alsa_input.usb-LARK"
        if candidate_id == "lark-a1"
        else "alsa_input.usb-FIFINE"
    )
    return {
        "boot_id": boot_id,
        "ready": ready,
        "failures": [],
        "bridge": {
            "state": "CALL_DOWN",
            "endpoints": {
                "microphone": node,
                "lark": node if candidate_id == "lark-a1" else None,
                "wired_output": "alsa_output.platform-aux",
            },
            "microphone": {
                "selected": {
                    "id": candidate_id,
                    "node": node,
                    "identity": {"usb_vendor_id": "0c76"},
                    "format": {
                        "rate": 48000,
                        "format": "S16LE",
                        "channels": 1,
                    },
                }
            },
            "graph": {"missing_links": [], "unexpected_links": []},
            "aec": {"enabled": True, "verified": False, "owner_pid": None},
        },
    }


def mocked_boot_run(
    root: Path, *, observed_microphone: str, expected_microphone: str
) -> tuple[dict, dict]:
    inventory = root / "inventory.toml"
    inventory.write_text('pi_host = "pi"\n', encoding="utf-8")
    config = bootctl.Config.load(inventory, root / "artifacts")
    probes = [
        microphone_probe(observed_microphone, boot_id="old-boot"),
        microphone_probe(observed_microphone, boot_id="new-boot"),
    ]

    class FakeSsh:
        def __init__(self, _config):
            self.probes = list(probes)

        def manifest(self):
            return {"fixture": True}

        def probe(self, expected=None):
            assert expected == expected_microphone
            return self.probes.pop(0)

        def run(self, *_args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    def collect(_ssh, directory):
        (directory / "journal.txt").write_text("", encoding="utf-8")

    with (
        mock.patch.object(bootctl, "Ssh", FakeSsh),
        mock.patch.object(
            bootctl, "git_metadata", return_value={"tracked_status": []}
        ),
        mock.patch.object(bootctl, "wait_for_port", return_value=True),
        mock.patch.object(bootctl, "start_serial", return_value=None),
        mock.patch.object(bootctl, "confirm_trial", return_value={"ok": True}),
        mock.patch.object(bootctl, "collect_evidence", side_effect=collect),
    ):
        path = bootctl.run_boot(
            config,
            mode="warm",
            candidate="fixture",
            require_functional=False,
            expected_microphone=expected_microphone,
        )
    return (
        json.loads((path / "result.json").read_text(encoding="utf-8")),
        json.loads((path / "manifest.json").read_text(encoding="utf-8")),
    )


class BootCtlTests(unittest.TestCase):
    def test_config_defaults_expected_microphone_and_accepts_override(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory) / "inventory.toml"
            inventory.write_text('pi_host = "pi"\n', encoding="utf-8")
            self.assertEqual(
                bootctl.Config.load(inventory).expected_microphone,
                "lark-a1",
            )
            inventory.write_text(
                'pi_host = "pi"\nboot_expected_microphone = "fifine-k054"\n',
                encoding="utf-8",
            )
            self.assertEqual(
                bootctl.Config.load(inventory).expected_microphone,
                "fifine-k054",
            )

    def test_selected_microphone_accepts_fifine_status(self):
        selected = {"id": "fifine-k054", "node": "alsa_input.usb-FIFINE"}
        value, error = bootctl.selected_microphone(
            {
                "microphone": {"selected": selected},
                "endpoints": {"microphone": selected["node"], "lark": None},
            }
        )
        self.assertEqual(value, selected)
        self.assertIsNone(error)

    def test_selected_microphone_accepts_legacy_lark_status(self):
        value, error = bootctl.selected_microphone(
            {"endpoints": {"lark": "alsa_input.usb-LARK"}}
        )
        self.assertEqual(value["id"], "lark-a1")
        self.assertIsNone(error)

    def test_ambiguous_status_does_not_use_legacy_endpoint(self):
        value, error = bootctl.selected_microphone(
            {
                "microphone": {
                    "selected": None,
                    "selection_reason": "lark-a1 is ambiguous",
                },
                "endpoints": {"lark": "stale-node"},
            }
        )
        self.assertIsNone(value)
        self.assertEqual(error, "lark-a1 is ambiguous")

    def test_expected_fifine_identity_passes(self):
        evidence = bootctl.require_expected_microphone(
            microphone_probe("fifine-k054"), "fifine-k054"
        )
        self.assertTrue(evidence["matches"])
        self.assertEqual(evidence["observed_id"], "fifine-k054")

    def test_expected_microphone_mismatch_fails(self):
        with self.assertRaisesRegex(
            RuntimeError,
            "selected microphone is 'fifine-k054', expected 'lark-a1'",
        ):
            bootctl.require_expected_microphone(
                microphone_probe("fifine-k054"), "lark-a1"
            )

    def test_generic_endpoint_without_microphone_status_is_not_legacy_evidence(self):
        value, error = bootctl.selected_microphone(
            {"endpoints": {"microphone": "alsa_input.usb-FIFINE", "lark": None}}
        )
        self.assertIsNone(value)
        self.assertEqual(error, "no legacy Lark endpoint is present")

    def test_run_boot_accepts_and_records_expected_fifine(self):
        with tempfile.TemporaryDirectory() as directory:
            result, manifest = mocked_boot_run(
                Path(directory),
                observed_microphone="fifine-k054",
                expected_microphone="fifine-k054",
            )

        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["expected_microphone"], "fifine-k054")
        self.assertEqual(result["observed_microphone"]["id"], "fifine-k054")
        self.assertTrue(result["microphone_evidence"]["idle"]["matches"])
        self.assertEqual(manifest["microphone"]["expected_id"], "fifine-k054")
        self.assertEqual(
            manifest["microphone"]["idle"]["observed_id"], "fifine-k054"
        )

    def test_run_boot_rejects_ready_probe_with_wrong_microphone(self):
        with tempfile.TemporaryDirectory() as directory:
            result, manifest = mocked_boot_run(
                Path(directory),
                observed_microphone="fifine-k054",
                expected_microphone="lark-a1",
            )

        self.assertEqual(result["verdict"], "FAIL")
        self.assertIn("expected 'lark-a1'", result["failure"])
        self.assertEqual(result["observed_microphone"]["id"], "fifine-k054")
        self.assertFalse(manifest["microphone"]["idle"]["matches"])

    def test_percentile_interpolates(self):
        self.assertEqual(bootctl.percentile([1, 2, 3], 0.5), 2)
        self.assertEqual(bootctl.percentile([0, 10], 0.95), 9.5)

    def test_command_expansion_does_not_use_a_shell(self):
        self.assertEqual(
            bootctl.expanded(("relay", "--log={output}"), output="run dir/serial.log"),
            ["relay", "--log=run dir/serial.log"],
        )

    def test_boot_commands_accept_expected_microphone_option(self):
        cases = (
            ["run", "--candidate", "candidate"],
            ["baseline"],
            [
                "screen",
                "--baseline",
                "base",
                "--baseline-rev",
                "base-rev",
                "--candidate",
                "candidate",
                "--candidate-rev",
                "candidate-rev",
            ],
        )
        for arguments in cases:
            parsed = bootctl.parser().parse_args(
                [*arguments, "--expected-microphone", "fifine-k054"]
            )
            self.assertEqual(parsed.expected_microphone, "fifine-k054")

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

    def test_screen_passes_expected_microphone_to_every_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(
                artifacts=root,
                expected_microphone="lark-a1",
            )
            calls = []

            def fake_run_boot(_config, **arguments):
                calls.append(arguments)
                path = root / f"run-{len(calls):02d}"
                path.mkdir()
                (path / "result.json").write_text(
                    json.dumps({"verdict": "PASS"}), encoding="utf-8"
                )
                return path

            with (
                mock.patch.object(bootctl, "apply_variant"),
                mock.patch.object(bootctl, "run_boot", side_effect=fake_run_boot),
                mock.patch.object(bootctl, "confirm_trial"),
                mock.patch.object(bootctl.time, "sleep"),
            ):
                verdict = bootctl.screen(
                    config,
                    baseline_label="base",
                    baseline_revision="base-rev",
                    candidate_label="candidate",
                    candidate_revision="candidate-rev",
                    pairs=10,
                    mode="warm",
                    require_functional=False,
                    seed=0,
                    expected_microphone="fifine-k054",
                )

        self.assertEqual(verdict, 0)
        self.assertEqual(len(calls), 20)
        self.assertEqual(
            {call["expected_microphone"] for call in calls},
            {"fifine-k054"},
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
