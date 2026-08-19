from __future__ import annotations

import math

from rig.analysis.aec_metrics import correlated_level


def test_correlated_level_finds_delay_and_attenuation() -> None:
    rate = 500
    reference = [math.sin(2 * math.pi * 17 * index / rate) for index in range(rate * 2)]
    lag = 75
    capture = [0.0] * lag + [0.25 * value for value in reference] + [0.0] * 20
    level, found_lag = correlated_level(reference, capture, rate)
    expected_rms = 0.25 / math.sqrt(2)
    assert found_lag == lag
    assert abs(level - expected_rms) < 0.01
