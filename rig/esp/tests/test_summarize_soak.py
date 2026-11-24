from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import summarize_soak as soak


def timestamp(seconds):
    return f"2026-11-24T05:00:{seconds:02}+00:00"


class SoakSummaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.zone = ZoneInfo("America/New_York")

    def part(self, name, seconds):
        part = self.root / name
        part.mkdir()
        (part / "samples.jsonl").write_text("".join(
            json.dumps({"utc": timestamp(s), "acl_bredr": state}) + "\n"
            for s, state in seconds
        ), encoding="utf-8")
        return part

    def test_separate_parts_do_not_bridge_gap_or_count_partial_tail(self):
        part1 = self.part("part1", [(0, True), (10, True)])
        part2 = self.part("part2", [(20, True), (30, True), (50, True)])
        (part1 / "s31-serial.log").write_text(
            f"[{timestamp(1)}] E (1) s31_link: route ACK timed out\n"
            f"[{timestamp(12)}] E (2) s31_link: route ACK timed out\n"
            f"[{timestamp(13)}] E (3) s31_link: route ACK timed out",
            encoding="utf-8",
        )
        (part2 / "events.jsonl").write_text('{"kind":', encoding="utf-8")
        report = soak.summarize([part2, part1], self.zone)
        self.assertEqual(report["elapsed_seconds"], 50)
        self.assertEqual(report["covered_seconds"], 42)
        self.assertEqual(report["gap_seconds"], 8)
        self.assertEqual(report["longest_observed_acl_seconds"], 30)
        self.assertEqual(report["route_ack_timeouts"], 2)
        self.assertEqual(report["serial_fault_markers"], [])
        self.assertEqual(report["acl_events"], [])
        self.assertEqual(report["acl_reconnects"], [])

    def test_acl_history_deduplication_reconnect_and_faults(self):
        part1 = self.part("part1", [(0, True), (40, True)])
        part2 = self.part("part2", [(43, True), (59, True)])
        events = [
            {"utc": timestamp(40), "kind": "pixel_acl", "state": state,
             "pixel_time": value, "reason": reason}
            for state, value, reason in [
                ("Connected", "11-23 23:59:59.000", 0),
                ("Disconnected", "11-24 00:00:12.500", 8),
                ("Connected", "11-24 00:00:15.000", 0),
            ]
        ]
        for part, seen in [(part1, 40), (part2, 43)]:
            (part / "events.jsonl").write_text("".join(
                json.dumps(dict(event, utc=timestamp(seen))) + "\n" for event in events
            ), encoding="utf-8")
        (part1 / "s31-serial.log").write_text("".join(
            f"[{timestamp(i)}] {line}\n" for i, line in enumerate([
                "Guru Meditation Error: Core 0 panic'ed", "Brownout detector was triggered",
                "rst:0x3 (SW_RESET),boot:0x1",
            ], 1)
        ), encoding="utf-8")
        report = soak.summarize([part1, part2], self.zone)
        self.assertEqual(len(report["acl_events"]), 3)
        self.assertFalse(report["acl_events"][0]["in_capture_window"])
        self.assertTrue(report["acl_events"][1]["in_capture_window"])
        self.assertEqual(report["acl_reconnects"][0]["seconds"], 2.5)
        self.assertEqual(report["longest_observed_acl_seconds"], 25)
        self.assertEqual([event["kind"] for event in report["serial_fault_markers"]],
                         ["panic", "brownout", "reset"])

    def test_poll_drop_does_not_invent_reconnect_times(self):
        part = self.part("part", [(0, True), (10, True), (20, False), (30, True), (40, True)])
        report = soak.summarize([part], self.zone)
        self.assertEqual(report["acl_reconnects"], [])
        self.assertEqual(report["longest_observed_acl_seconds"], 10)

    def test_pixel_history_crosses_year_boundary(self):
        observed = datetime(2026, 1, 1, 5, 0, 1, tzinfo=timezone.utc)
        self.assertEqual(soak.pixel_event_time("12-31 23:59:59.125", observed, self.zone),
                         datetime(2026, 1, 1, 4, 59, 59, 125000, tzinfo=timezone.utc))

    def test_cli_prints_json_without_writing_to_input(self):
        part = self.part("part", [(0, True), (30, True)])
        before = {p.name: p.read_bytes() for p in part.iterdir()}
        result = subprocess.run(
            [sys.executable, "-B", soak.__file__, str(part), "--pixel-timezone", str(self.zone)],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(result.stdout)["covered_seconds"], 30)
        self.assertEqual({p.name: p.read_bytes() for p in part.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
