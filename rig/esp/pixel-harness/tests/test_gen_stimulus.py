import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import gen_stimulus  # noqa: E402


class GenStimulusTest(unittest.TestCase):
    def test_tone_wav_format_and_content(self):
        with tempfile.TemporaryDirectory() as d:
            gen_stimulus.main([d])
            with wave.open(str(Path(d) / "tone_1k_16k_3s.wav"), "rb") as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 16000))
                self.assertEqual(w.getnframes(), 48000)
                data = struct.unpack("<48000h", w.readframes(48000))
        peak = max(abs(s) for s in data)
        self.assertAlmostEqual(20 * math.log10(peak / 32767), -6.0, delta=0.05)
        self.assertEqual(data[0], 0)
        mid = data[16000:16032]
        self.assertEqual(mid[:16], mid[16:])  # exactly 16 samples per cycle
        sec = data[8000:24000]
        crossings = sum(1 for a, b in zip(sec, sec[1:]) if (a < 0) != (b < 0))
        self.assertAlmostEqual(crossings, 2000, delta=4)  # 1000 cycles in 1 s

    def test_deterministic(self):
        args = (1000.0, 16000, 0.5, -6.0, 10.0)
        self.assertEqual(gen_stimulus.tone_samples(*args), gen_stimulus.tone_samples(*args))


if __name__ == "__main__":
    unittest.main()
