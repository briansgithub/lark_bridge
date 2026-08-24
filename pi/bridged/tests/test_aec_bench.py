from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[3] / "rig" / "pi" / "measure" / "aec_bench.py"
if not hasattr(os, "sysconf"):
    os.sysconf_names = {"SC_CLK_TCK": "SC_CLK_TCK"}  # type: ignore[attr-defined]
    os.sysconf = lambda _name: 100  # type: ignore[attr-defined]
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("aec_bench_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bench = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bench
SPEC.loader.exec_module(bench)


class EffectiveNodeLatencyTests(unittest.TestCase):
    def test_inherits_production_latency_by_default(self) -> None:
        self.assertEqual(bench.effective_node_latency(None, 1920), 1920)

    def test_explicit_instrument_override_wins(self) -> None:
        self.assertEqual(bench.effective_node_latency(1440, 1920), 1440)

    def test_unconfigured_latency_remains_unset(self) -> None:
        self.assertIsNone(bench.effective_node_latency(None, None))


class EffectiveOutputTests(unittest.TestCase):
    WIRED = "alsa_output.platform-wired"
    A2DP = "bluez_output.C9_5C_FD_6E_28_46.1"

    def test_selected_runtime_output_is_the_bench_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "bridge-status.json"
            status.write_text(
                '{"output":{"chosen":{"node":"' + self.A2DP + '"}}}',
                encoding="utf-8",
            )
            module = SimpleNamespace(default_status_path=lambda: status)
            self.assertEqual(
                bench.effective_output(module, self.WIRED, None),
                self.A2DP,
            )

    def test_explicit_instrument_output_still_wins(self) -> None:
        module = SimpleNamespace(default_status_path=lambda: Path("missing"))
        self.assertEqual(
            bench.effective_output(module, self.WIRED, "instrument-output"),
            "instrument-output",
        )

    def test_missing_status_retains_wired_fallback(self) -> None:
        module = SimpleNamespace(default_status_path=lambda: Path("missing"))
        self.assertEqual(bench.effective_output(module, self.WIRED, None), self.WIRED)


if __name__ == "__main__":
    unittest.main()
