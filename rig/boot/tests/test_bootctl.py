from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
