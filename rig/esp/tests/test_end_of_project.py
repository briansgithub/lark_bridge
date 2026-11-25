import copy
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "soaks"))
import end_of_project as campaign


class CampaignReducerTests(unittest.TestCase):
    def setUp(self):
        self.checks = dict.fromkeys(campaign.PREFLIGHT_CHECKS, True)
        clear_audio = dict.fromkeys(campaign.ERROR_COUNTERS, 0)
        metrics = [
            dict(clear_audio, seconds=14400, fifo_min=3000, fifo_max=5000, max_abs_ppm=180),
            dict(
                capture_seconds=1800, p7_recovered=100, uac_disconnects=100, uac_reopens=100,
                uac_disconnect_lines=100, uac_failure_lines=0, uac_rx_gap_intervals=0,
                reboot_markers=0, uac_xfer_err_delta=0, uac_read_err_delta=0,
            ),
            {"cycles": [dict(cycle=i, recovery_seconds=2.4) for i in range(1, 11)]},
            dict(clear_audio, seconds=14400, nonzero_output_blocks=1),
        ]
        self.results = [
            dict(soak=name, verdict="PASS", metrics=data, reasons=[])
            for name, data in zip(campaign.STAGES, metrics)
        ]

    def verdict(self, stages=None, fault=None):
        return campaign.reduce_campaign(
            self.checks, self.results if stages is None else stages, fault
        )["verdict"]

    def test_complete_campaign_passes(self):
        self.assertEqual(self.verdict(), "PASS")

    def test_preflight_requires_actual_positive_checks(self):
        for check in campaign.PREFLIGHT_CHECKS:
            with self.subTest(check=check):
                del self.checks[check]
                self.assertEqual(self.verdict(), "FAIL")
                self.checks[check] = 1
                self.assertEqual(self.verdict(), "FAIL")
                self.checks[check] = True

    def test_missing_duplicate_out_of_order_and_malformed_stages_fail(self):
        for stages in (
            [],
            self.results[:-1],
            self.results + [self.results[1]],
            list(reversed(self.results)),
            [*self.results[:3], None],
            [*self.results[:3], dict(self.results[3], soak="unknown")],
        ):
            self.assertEqual(self.verdict(stages), "FAIL")

    def test_child_failure_or_contradictory_pass_fails(self):
        for verdict in ("PARTIAL", "INCONCLUSIVE", "FAIL", "SKIPPED", None, True):
            stages = copy.deepcopy(self.results)
            stages[0]["verdict"] = verdict
            self.assertEqual(self.verdict(stages), "FAIL")
        self.results[0]["reasons"] = ["recording was truncated"]
        self.assertEqual(self.verdict(), "FAIL")

    def test_faults_override_complete_results(self):
        for fault in ("unexpected boot or reset loop", "unknown USB device", "lease lost"):
            self.assertEqual(self.verdict(fault=fault), "FAIL")

    def test_missing_or_invalid_metrics_cannot_pass(self):
        for index, key, value in (
            (0, "seconds", 60),
            (0, "under", True),
            (0, "fifo_min", 7000),
            (0, "max_abs_ppm", 500),
            (1, "capture_seconds", float("nan")),
            (1, "p7_recovered", 99),
            (1, "uac_disconnect_lines", 99),
            (1, "uac_failure_lines", 1),
            (2, "cycles", [{"cycle": 1, "recovery_seconds": 0.1}] * 10),
            (3, "nonzero_output_blocks", 0),
            (3, "seconds", float("inf")),
            (3, "i2s_err", -1),
        ):
            with self.subTest(index=index, key=key, value=value):
                stages = copy.deepcopy(self.results)
                del stages[index]["metrics"][key]
                self.assertEqual(self.verdict(stages), "FAIL")
                stages[index]["metrics"][key] = value
                self.assertEqual(self.verdict(stages), "FAIL")


class CampaignStopTests(unittest.TestCase):
    def test_console_faults_latch_before_cleanup_can_actuate(self):
        for line in (
            "ESP-ROM:esp32s31",
            "Guru Meditation Error",
            "Ignoring UAC microphone 1234:5678",
            "Could not identify UAC device: ESP_FAIL",
        ):
            with self.subTest(line=line):
                bench = object.__new__(campaign.CampaignBench)
                bench.records = []
                bench.log = io.StringIO()
                bench.last_uptime = {"s31": 3000}
                with self.assertRaises(campaign.h2h5.BenchError):
                    bench.add(t="console", role="s31", line=line)
                self.assertIn(line, bench.fatal)
                with self.assertRaises(campaign.h2h5.BenchError):
                    bench.poll()  # exits before the port enumerator is reached


if __name__ == "__main__":
    unittest.main()
