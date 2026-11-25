import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "soaks"))
from s31_32ue_streaming import board_window, check_tap, h2h5


class StreamingEvidenceTests(unittest.TestCase):
    def test_sparse_audio_and_wrap(self):
        first = dict(host=10, nz_frames=2**32 - 10)
        for seconds, passes in [(60, True), (10, False)]:
            last = dict(ok=True, host=70, nz_frames=44100 * seconds - 10)
            if passes:
                check_tap(first, last)
            else:
                with self.assertRaises(h2h5.BenchError):
                    check_tap(first, last)

    def test_board_reset(self):
        first = dict(host=1, up_ms=5000, uart=dict(sessions=1))
        last = dict(host=61, up_ms=1000, uart=dict(sessions=1))
        with self.assertRaises(h2h5.BenchError):
            board_window([], "s31", first, last)
