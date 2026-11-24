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


def stats_line(seconds, *, up_ms, streaming, rx_bytes, a2dp_state=2, heap_min=70000):
    record = {"t": "stats", "role": "s31", "up_ms": up_ms, "heap": {"free": heap_min, "min": heap_min},
              "session": {"a2dp": streaming}, "bt": {"a2dp_state": a2dp_state, "a2dp_bytes": rx_bytes},
              "i2s": {"under": 0, "over": 0}}
    return f"[{audio_stamp(seconds)}] {json.dumps(record)}\n"


def audio_stamp(seconds):
    return datetime.fromtimestamp(1795500000 + seconds, timezone.utc).isoformat()


def harness_line(event, seconds, **fields):
    return json.dumps({"event": event, "tag": "soak", "ts_ms": (1795500000 + seconds) * 1000, **fields}) + "\n"


class AudioSoakTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.part = Path(temp.name) / "audio"
        self.part.mkdir()

    def test_s31_stream_drops_rx_gaps_wrap_and_resets(self):
        rate = 176400
        lines, total = [], 2**32 - 3 * rate  # wraps during the run
        for s in range(0, 10):
            total += 0 if s in (5, 6) else rate  # two seconds with no A2DP data
            lines.append(stats_line(s, up_ms=1000 * s + 5000, streaming=True, rx_bytes=total % 2**32))
        lines.append(stats_line(10, up_ms=15000, streaming=False, rx_bytes=total % 2**32))
        lines.append(stats_line(11, up_ms=500, streaming=True, rx_bytes=rate, heap_min=60000))
        (self.part / "s31-serial.log").write_text("".join(lines), encoding="utf-8")
        report = soak.audio_report(self.part)["s31"]
        self.assertEqual(report["stats_records"], 12)
        self.assertEqual([d["utc"].isoformat() for d in report["a2dp_stream_drops"]], [audio_stamp(10)])
        self.assertEqual(report["uptime_resets"], 1)
        self.assertEqual(report["heap_min"], 60000)
        self.assertEqual(len(report["rx_gaps"]), 1)
        self.assertEqual(report["rx_gaps"][0]["start_utc"].isoformat(), audio_stamp(4))
        self.assertEqual(report["rx_gaps"][0]["seconds"], 2.0)
        self.assertAlmostEqual(report["median_rx_bytes_per_s"], rate)

    def test_harness_underruns_stalls_and_routes(self):
        lines = [harness_line("stimulus_start", 0, rate=16000, routed_device={"type": "BLUETOOTH_A2DP"})]
        played = 0
        for s in range(60, 360, 60):
            played += 0 if s == 180 else 16000 * 60
            device = "BUILTIN_SPEAKER" if s == 300 else "BLUETOOTH_A2DP"
            lines.append(harness_line("stimulus_progress", s, frames_played=played,
                                      underruns=2 if s >= 240 else 0, routed_device={"type": device}))
        lines.append("not json\n")
        (self.part / "harness.log").write_text("".join(lines), encoding="utf-8")
        report = soak.audio_report(self.part)["pixel"]
        self.assertEqual(report["progress_records"], 5)
        self.assertEqual(report["underruns"], 2)
        self.assertEqual([s["end_utc"].isoformat() for s in report["playback_stalls"]], [audio_stamp(180)])
        self.assertEqual(report["non_a2dp_progress"], 1)
        self.assertEqual(report["played_seconds"], 240.0)

    def test_no_audio_evidence_gives_none(self):
        self.assertIsNone(soak.audio_report(self.part))


if __name__ == "__main__":
    unittest.main()
