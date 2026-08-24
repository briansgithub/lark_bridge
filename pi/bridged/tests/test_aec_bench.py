from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
