from __future__ import annotations

import math

from tools.audio.tone_gen import _samples


def test_multitone_is_deterministic_and_bounded() -> None:
    first = list(_samples("multitone", 1000, 48_000, 0.01, 1.0))
    second = list(_samples("multitone", 1000, 48_000, 0.01, 1.0))
    assert first == second
    assert max(abs(value) for value in first) <= 1.0


def test_speech_contains_signal_and_silence() -> None:
    samples = list(_samples("speech", 1000, 8_000, 2.0, 1.0))
    assert any(abs(value) > 0.1 for value in samples)
    assert sum(math.isclose(value, 0.0) for value in samples) > 1_000
