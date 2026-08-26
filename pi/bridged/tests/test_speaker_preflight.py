from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "rig" / "pi" / "measure" / "speaker_preflight.py"
SPEC = importlib.util.spec_from_file_location("speaker_preflight_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
preflight = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


def metrics(rms: float, tone: float, snr: float, peak: float = -12.0, clipped: float = 0.0):
    return {
        "per_channel": [
            {
                "rms_dbfs": rms,
                "tone_dbfs": tone,
                "snr_db": snr,
                "peak_dbfs": peak,
                "clipped_pct": clipped,
            }
        ]
    }


class VerdictTests(unittest.TestCase):
    def test_ready_when_speaker_tone_is_clear(self) -> None:
        result = preflight.verdict(metrics(-58, -58, 0), metrics(-30, -27, 24))
        self.assertEqual(result["verdict"], "ready")
        self.assertEqual(result["exit_code"], 0)

    def test_missing_speaker_is_a_paused_fixture(self) -> None:
        result = preflight.verdict(metrics(-58, -58, 0), metrics(-54, -52, 4))
        self.assertEqual(result["verdict"], "speaker-not-detected")
        self.assertEqual(result["exit_code"], 78)

    def test_acoustic_distortion_does_not_hide_a_detected_speaker(self) -> None:
        result = preflight.verdict(metrics(-52.8, -58, 0), metrics(-35.6, -34.9, 3.7))
        self.assertEqual(result["verdict"], "ready")
        self.assertEqual(result["exit_code"], 0)

    def test_clipping_is_unsafe_not_missing(self) -> None:
        result = preflight.verdict(
            metrics(-58, -58, 0), metrics(-8, -7, 30, peak=0.0, clipped=0.1)
        )
        self.assertEqual(result["verdict"], "unsafe-level")
        self.assertEqual(result["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
