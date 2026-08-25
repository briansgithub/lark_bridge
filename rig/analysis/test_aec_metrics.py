from __future__ import annotations

import math
import random

from rig.analysis.aec_metrics import (
    convergence_metrics,
    correlated_level,
    reference_component_metrics,
)


def test_correlated_level_finds_delay_and_attenuation() -> None:
    rate = 500
    reference = [math.sin(2 * math.pi * 17 * index / rate) for index in range(rate * 2)]
    lag = 75
    capture = [0.0] * lag + [0.25 * value for value in reference] + [0.0] * 20
    level, found_lag, correlation = correlated_level(reference, capture, rate)
    expected_rms = 0.25 / math.sqrt(2)
    assert found_lag == lag
    assert abs(level - expected_rms) < 0.01
    assert correlation > 0.99


def test_convergence_requires_two_sustained_windows() -> None:
    result = convergence_metrics(
        [-30.0, -30.0, -30.0, -30.0],
        [-39.0, -41.0, -42.0, -43.0],
        required_db=10.0,
    )
    assert result["achieved"] is True
    assert result["convergence_time_s"] == 3.0


def test_reference_component_ignores_independent_near_end_speech() -> None:
    rate = 100
    samples = 2000
    lag = 47
    reference_rng = random.Random(1)
    near_end_rng = random.Random(2)
    reference = [reference_rng.uniform(-1, 1) for _ in range(samples)]
    near_end = [near_end_rng.uniform(-1, 1) for _ in range(samples)]
    raw = [
        near_end[index] + (0.6 * reference[index - lag] if index >= lag else 0.0)
        for index in range(samples)
    ]
    clean = [
        near_end[index] + (0.06 * reference[index - lag] if index >= lag else 0.0)
        for index in range(samples)
    ]

    result = reference_component_metrics(reference, raw, clean, rate)

    assert 18.0 <= result["suppression_db"] <= 22.0
    assert result["raw_correlation"] > result["clean_correlation"]
